from django.db.models import Count

from aiarena.core.models import Competition, CompetitionParticipation, MatchParticipation, Round


def update_divisions(competition: Competition, new_round: Round):
    """
    Re-rank the competition's active participants into divisions, as the start of a cycle.

    Updates `competition.n_divisions` without saving it; the caller saves the competition.
    """
    # Get match counts to determine which bots are still in placements
    match_counts = list(
        MatchParticipation.objects.filter(match__result__isnull=False, match__round__competition=competition)
        .exclude(match__result__type__in=["MatchCancelled", "InitializationError", "Error"])
        .values("bot")
        .annotate(match_count=Count("bot"))
    )
    match_counts = {vs["bot"]: vs["match_count"] for vs in match_counts}
    # Order bots out of placement by elo, order bots in placement by games played
    all_participants = (
        CompetitionParticipation.objects.only("id", "bot__id", "division_num", "elo", "match_count")
        .filter(competition=competition, active=True)
        .order_by("elo")
    )
    if competition.n_placements <= 0:
        existing_participants = list(all_participants)
        placement_participants = []
    else:
        existing_participants = sorted(
            all_participants.filter(bot__id__in=[k for k, v in match_counts.items() if v >= competition.n_placements]),
            key=lambda p: p.elo,
        )
        placement_participants = sorted(
            all_participants.exclude(bot__id__in=[p.bot.id for p in existing_participants]),
            key=lambda p: (match_counts[p.bot.id] if p.bot.id in match_counts else 0, -p.elo),
            reverse=True,
        )
    active_participants = placement_participants + existing_participants
    n_active_participants = len(active_participants)
    # Update number of divisions
    if competition.should_split_divisions(n_active_participants) or competition.should_merge_divisions(
        n_active_participants
    ):
        # Does not split on the first round of every competition so initial participants can be accurately ranked
        if new_round.number > 1 and n_active_participants > competition.target_division_size:
            # Limit to target so that if lots join at once it doesn't overshoot
            competition.n_divisions = min(
                competition.target_n_divisions, n_active_participants // competition.target_division_size
            )
        else:
            competition.n_divisions = 1
    # Update bot division numbers
    updated_participants = []
    div_size, rem = divmod(n_active_participants, competition.n_divisions)
    divs = [
        active_participants[i * div_size + min(i, rem) : (i + 1) * div_size + min(i + 1, rem)]
        for i in range(competition.n_divisions)
    ]
    current_div_num = competition.n_divisions - 1 + CompetitionParticipation.MIN_DIVISION
    for d in divs:
        for p in d:
            updated_participants.append(p)
            p.division_num = current_div_num
            p.match_count = match_counts[p.bot.id] if p.bot.id in match_counts else 0
            p.in_placements = p.match_count < competition.n_placements
        current_div_num -= 1
    CompetitionParticipation.objects.bulk_update(updated_participants, ["division_num", "in_placements", "match_count"])
