from django.utils import timezone

from aiarena.core.exceptions import CompetitionClosing, CompetitionPaused
from aiarena.core.models import Competition, CompetitionParticipation, Round

from .divisions import update_divisions


def open_round(competition: Competition) -> Round:
    """
    Open the competition's next round, updating divisions if it starts a new cycle.
    """
    if competition.is_paused:
        raise CompetitionPaused()
    if competition.is_closing:  # we should technically never hit this
        raise CompetitionClosing()

    new_round = Round.objects.create(competition=competition)
    if competition.rounds_this_cycle >= competition.rounds_per_cycle:
        competition.rounds_this_cycle = 1
    else:
        competition.rounds_this_cycle += 1

    if competition.rounds_this_cycle == 1:
        update_divisions(competition, new_round)
    competition.save()

    placed_participants = CompetitionParticipation.objects.only("id").filter(
        competition=competition, active=True, division_num__gte=CompetitionParticipation.MIN_DIVISION
    )
    # Pre-list the IDs to get around this while on mariadb: https://code.djangoproject.com/ticket/28787
    p_ids = [p.id for p in placed_participants]
    CompetitionParticipation.objects.filter(competition=competition).exclude(id__in=p_ids).update(
        participated_in_most_recent_round=False
    )
    placed_participants.update(participated_in_most_recent_round=True)

    return new_round


def update_round_if_completed(round: Round):
    """
    If all the matches have been run, mark this round as complete
    """
    if round.complete:  # round.mark_complete() will check this, but do it here to avoid unnecessary DB queries
        # Nothing should call this method on an already completed round. Highlight the bug.
        raise ValueError(f"Round {round.id} is already complete")

    if _not_all_matches_have_a_result(round):
        return

    round.mark_complete(timezone.now())
    round.save(update_fields=["complete", "finished"])

    # After completing the round, try to close the competition
    round.competition.try_to_close()


def _not_all_matches_have_a_result(round: Round) -> bool:
    """
    Check whether there are any matches in the round that still lack a result.
    """
    return round.match_set.filter(result=None).exists()
