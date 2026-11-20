import logging
import random
from datetime import UTC, datetime, timedelta

from django.db.models import Max
from django.utils import timezone

from rest_framework.exceptions import APIException

from aiarena.core.exceptions import MaxActiveRounds, NoMaps, NotEnoughAvailableBots
from aiarena.core.models import (
    ArenaClient,
    Bot,
    Competition,
    CompetitionParticipation,
    Map,
    Match,
    MatchParticipation,
    Round,
)
from aiarena.core.services._bots import Bots
from aiarena.core.services.service_implementations._competitions import Competitions

from ..match_starter import MatchStarter
from ..matches import create
from .rounds import open_round


logger = logging.getLogger(__name__)

# Starting width of the time window used by _last_ladder_match_starts, doubled on
# each attempt. Sized so a single attempt normally suffices: bots eligible for a
# ladder match are ones actively playing the current round, so their previous match
# is at most a round-drain old.
_LAST_MATCH_WINDOW_START = timedelta(hours=6)
# Widening is far cheaper than an extra round trip, so grow aggressively.
_LAST_MATCH_WINDOW_GROWTH = 8
# The first match on record was played in December 2019, and matches are only ever
# appended, so a window reaching back this far can no longer exclude anything.
_ARENA_EPOCH = datetime(2019, 1, 1, tzinfo=UTC)


def _last_ladder_match_starts(bot_ids) -> dict:
    """Map each bot id to the start time of its most recent ladder match.

    Bots with no ladder match at all are absent from the result.

    Queried over a time window that widens until every bot is accounted for,
    rather than in one pass over all history. The unwindowed form has to scan both
    tables in full -- restricting to ladder matches that have started excludes
    almost nothing, so there is no selective index for the planner to use, and it
    joins millions of rows only to aggregate them down to one per bot.

    A bot missing from a window proves that window was too narrow, so widening
    until none are missing yields the same answer as the unwindowed query would,
    without needing to assume how quickly rounds currently drain. Once the window
    reaches back further than any match on record it stops being a restriction at
    all and is dropped, which both terminates the search and leaves the result
    exact for a bot that genuinely has not played in a very long time.
    """
    now = timezone.now()
    remaining = set(bot_ids)
    last_starts = {}
    window = _LAST_MATCH_WINDOW_START

    while remaining:
        participations = (
            MatchParticipation.objects.filter(bot_id__in=remaining)
            .exclude(match__round=None)
            .exclude(match__started=None)
        )

        # Once the window reaches past the start of the project the filter cannot
        # exclude anything, so drop it and let this be the final, exact pass.
        cutoff = now - window
        bounded = cutoff > _ARENA_EPOCH
        if bounded:
            participations = participations.filter(match__started__gte=cutoff)

        found = dict(
            participations.values("bot_id")
            .annotate(last_start=Max("match__started"))
            .values_list("bot_id", "last_start")
        )
        last_starts.update(found)
        remaining -= found.keys()

        if not bounded:
            # Searched all of history; whatever is still missing has never played.
            break

        if found:
            window *= _LAST_MATCH_WINDOW_GROWTH
        else:
            # A pass that turns up nothing suggests the rest are bots that have not
            # played in a long time, or at all. Stepping the window out towards them
            # one multiple at a time would cost a query each, so go straight to the
            # exact pass -- by now it is selective enough to use the bot_id index.
            window = now - _ARENA_EPOCH

    return last_starts


class RoundRobin:
    """
    The round-robin matchmaker: each round, every pair of bots within a division plays once.

    A round's whole schedule is created when the round opens; its matches are then started
    one at a time as arena clients ask for them.
    """

    def __init__(self, bots_service: Bots, competitions_service: Competitions):
        self.bots_service = bots_service
        self.competitions_service = competitions_service

    def start_match_in_open_rounds(self, requesting_ac: ArenaClient, competition: Competition) -> Match | None:
        """
        Start an unstarted match from the competition's open rounds, oldest round first.
        """
        # Get rounds with un-started matches
        # The "IN" is a workaround due to postgresql not allowing "FOR UPDATE" with a DISTINCT clause
        rounds = Round.objects.raw(
            """
            SELECT
                cr.id
            FROM
                core_round cr
            WHERE
                cr.id IN (
                    SELECT
                        cr.id
                    FROM
                        core_round cr
                        INNER JOIN core_match cm ON cr.id = cm.round_id
                    WHERE
                        competition_id=%s
                    AND finished IS NULL
                    AND cm.started IS NULL)
            ORDER BY cr.number
            FOR UPDATE""",
            (competition.id,),
        )

        for round in rounds:
            match = self.start_match_in_round(requesting_ac, round)
            if match is not None:
                return match  # a match was found - we're done
        return None

    def start_match_in_new_round(self, requesting_ac: ArenaClient, competition: Competition) -> Match:
        """
        Generate the competition's next round and start one of its matches.
        """
        # If none of the existing matches were able to start, and we don't have 2 active bots available,
        # then we give up.
        # todo: does this need to be a select_for_update?
        active_bots = self.competitions_service.get_active_bots(competition).select_for_update()

        if not self.bots_service.available_is_more_than(active_bots, 2):
            raise NotEnoughAvailableBots()

        # If we get to here, then we have
        # - no matches from any existing round we can start
        # - at least 2 active bots available for play
        if self.competitions_service.has_reached_maximum_active_rounds(competition):
            raise MaxActiveRounds()

        round = self.generate_round(competition)

        match = self.start_match_in_round(requesting_ac, round)

        if match is None:
            raise APIException("Failed to start match. There might not be any available participants.")
        return match

    def generate_round(self, competition: Competition) -> Round:
        """
        Open the competition's next round, with a match for every pair of bots in each division.
        """
        # Check for maps before opening the round: the caller catches NoMaps inside its transaction,
        # so a round opened first would be kept without any matches.
        active_maps = Map.objects.filter(
            competitions__in=[
                competition,
            ]
        ).select_for_update()
        if active_maps.count() == 0:
            raise NoMaps()

        new_round = open_round(competition, Competition.Matchmaker.ROUND_ROBIN)

        active_participants = CompetitionParticipation.objects.only("id", "division_num", "bot").filter(
            competition=competition, active=True, division_num__gte=CompetitionParticipation.MIN_DIVISION
        )
        already_processed_participants = []
        # loop through and generate matches for all active participants
        for participant1 in active_participants:
            already_processed_participants.append(participant1.id)
            active_participants_in_div = (
                CompetitionParticipation.objects.only("bot")
                .filter(competition=competition, active=True, division_num=participant1.division_num)
                .exclude(id__in=already_processed_participants)
            )
            for participant2 in active_participants_in_div:
                create(
                    new_round,
                    random.choice(active_maps),
                    participant1.bot,
                    participant2.bot,
                    require_trusted_arenaclient=competition.require_trusted_infrastructure,
                )

        return new_round

    def start_match_in_round(self, requesting_ac: ArenaClient, for_round: Round) -> Match | None:
        """
        Start one of the round's unstarted matches, preferring bots with bot data and bots
        that have waited longest since their last match.
        """
        assert requesting_ac is not None
        assert for_round is not None
        ladder_matches_to_play = list(
            Match.objects.raw(
                """
                    SELECT core_match.id, core_match.started, assigned_to_id, round_id from core_match
                    inner join core_round cr on core_match.round_id = cr.id
                    where core_match.started is null and requested_by_id is null and round_id = %s
                    order by round_id
                    for update
                """,
                (for_round.id,),
            )
        )

        if len(ladder_matches_to_play) > 0:
            bots_with_a_ladder_match_to_play = (
                Bot.objects.exclude(
                    matchparticipation__in=MatchParticipation.objects.filter(
                        match__started__isnull=False,
                        match__result=None,
                        use_bot_data=True,
                        update_bot_data=True,
                    ),
                )
                .filter(
                    matchparticipation__in=MatchParticipation.objects.filter(
                        match__started=None,
                        match__requested_by=None,
                    ),
                )
                .distinct()
                .only("id")
            )

            match_ids = [match.id for match in ladder_matches_to_play]
            bot_ids = [bot.id for bot in bots_with_a_ladder_match_to_play]
            available_ladder_matches_to_play = (
                list(
                    Match.objects.raw(
                        """
                            SELECT cm.* from core_match cm
                            inner join core_matchparticipation c1 on cm.id = c1.match_id and c1.participant_number = 1
                            inner join core_matchparticipation c2 on cm.id = c2.match_id and c2.participant_number = 2
                            where cm.id in %s
                            and c1.bot_id in %s and c2.bot_id in %s
                        """,
                        (tuple(match_ids), tuple(bot_ids), tuple(bot_ids)),
                    )
                )
                if bot_ids
                else []
            )

            # Prioritize matches involving bots with data enabled (they can't play concurrent matches)
            # and bots that have waited the longest since their last match.
            if available_ladder_matches_to_play:
                available_match_ids = [m.id for m in available_ladder_matches_to_play]
                # Map each match to its participant bot IDs
                match_bot_map = {}
                for mp in MatchParticipation.objects.filter(match_id__in=available_match_ids).values(
                    "match_id", "bot_id"
                ):
                    match_bot_map.setdefault(mp["match_id"], []).append(mp["bot_id"])

                # Identify which available bots have data enabled
                data_enabled_bot_ids = set(
                    Bot.objects.filter(id__in=bot_ids, bot_data_enabled=True).values_list("id", flat=True)
                )

                # Get the most recent match start time for each bot
                last_match_start_times = _last_ladder_match_starts(bot_ids)

                # Bots with no previous match get the earliest possible time (highest priority)
                epoch = timezone.datetime.min.replace(tzinfo=timezone.utc)

                def match_sort_key(match):
                    bots_in_match = match_bot_map.get(match.id, [])
                    data_enabled_count = sum(1 for b in bots_in_match if b in data_enabled_bot_ids)
                    # Find the most recent last-match start time among the bots in this match.
                    # Ascending sort prioritizes matches where even the most-recently-started
                    # bot has been waiting longer.
                    most_recent_last_match = max(
                        (last_match_start_times.get(b, epoch) for b in bots_in_match),
                        default=epoch,
                    )
                    return (-data_enabled_count, most_recent_last_match)

                available_ladder_matches_to_play.sort(key=match_sort_key)

            # if, out of the bots that have a ladder match to play, at least 2 are active, then try starting matches.
            if self.bots_service.available_is_more_than(bots_with_a_ladder_match_to_play, 2):
                return MatchStarter.start_first(available_ladder_matches_to_play, requesting_ac)
        return None
