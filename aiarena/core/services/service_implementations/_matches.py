import logging

from django.db import transaction
from django.utils import timezone

from constance import config

from aiarena.core.models import ArenaClient, Competition, Match, Result

from .._bots import Bots
from ._competitions import Competitions
from .internal.match_starter import MatchStarter
from .internal.matches import CancelResult, cancel
from .internal.matchmaking.round_robin import RoundRobin
from .internal.matchmaking.rounds import update_round_if_completed


logger = logging.getLogger(__name__)


class Matches:
    def __init__(self, bots_service: Bots, competitions_service: Competitions):
        self.bots_service = bots_service
        self.competitions_service = competitions_service
        self.round_robin = RoundRobin(bots_service, competitions_service)

    def cancel(self, match_id):
        try:
            with transaction.atomic():
                match = Match.objects.select_for_update().get(pk=match_id)
                result = cancel(match.id, None)
                if result == CancelResult.MATCH_DOES_NOT_EXIST:  # should basically not happen, but just in case
                    raise Exception(f'Match "{match_id}" does not exist')
                elif result == CancelResult.RESULT_ALREADY_EXISTS:
                    raise Exception(f'A result already exists for match "{match_id}"')
        except Match.DoesNotExist:
            raise Exception(f'Match "{match_id}" does not exist')

    def timeout_overtime_bot_games(self):
        matches_without_result = (
            Match.objects.only("round")
            .select_for_update(of=("self",))
            .select_related("round")
            .filter(first_started__lt=timezone.now() - config.TIMEOUT_MATCHES_AFTER, result__isnull=True)
        )
        for match in matches_without_result:
            match.result = Result.objects.create(type="MatchCancelled", game_steps=0)
            match.save()
            if match.round is not None:  # if the match is part of a round, check for round completion
                update_round_if_completed(match.round)

    def attempt_to_start_a_requested_match(self, requesting_ac: ArenaClient):
        # Try get a requested match
        # Do we want trusted clients to run games not requiring trusted clients?
        matches = (
            Match.objects.select_related("round")
            .only("started", "assigned_to", "round")
            .filter(started__isnull=True, requested_by__isnull=False)
            .select_for_update(of=("self",))
            .order_by("created")
        )

        if matches.count() > 0:
            return MatchStarter.start_first(matches, requesting_ac)
        else:
            return None

    def start_next_match_for_competition(self, requesting_ac: ArenaClient, competition: Competition):
        # LADDER MATCHES
        match = self.round_robin.start_match_in_open_rounds(requesting_ac, competition)
        if match is not None:
            return match  # a match was found - we're done

        return self.round_robin.start_match_in_new_round(requesting_ac, competition)
