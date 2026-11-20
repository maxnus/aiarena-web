from datetime import UTC, datetime, timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

import numpy as np

from aiarena.core.exceptions import CompetitionPaused, NotEnoughAvailableBots
from aiarena.core.models import (
    ArenaClient,
    Competition,
    CompetitionParticipation,
    Game,
    GameMode,
    Map,
    Round,
    User,
)
from aiarena.core.models.bot_race import BotRace
from aiarena.core.services.service_implementations._bots import BotsImpl
from aiarena.core.services.service_implementations._competitions import Competitions
from aiarena.core.services.service_implementations._matches import Matches
from aiarena.core.services.service_implementations.internal.matches import cancel, create
from aiarena.core.services.service_implementations.internal.matchmaking.round_robin import RoundRobin
from aiarena.core.services.service_implementations.internal.matchmaking.rounds import (
    open_round,
    seal_open_rounds,
    update_round_if_completed,
)
from aiarena.core.services.service_implementations.internal.matchmaking.scoring import ScoringParams
from aiarena.core.services.service_implementations.internal.matchmaking.stochastic import (
    Stochastic,
    build_snapshot,
)
from aiarena.core.tests.testing_utils import create_bot_for_competition


STOCHASTIC = Competition.Matchmaker.STOCHASTIC
ROUND_ROBIN = Competition.Matchmaker.ROUND_ROBIN


class _MatchmakingTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="testuser", email="test@test.com")
        self.arenaclient = ArenaClient.objects.create(trusted=True, owner=self.user)
        self.game_mode = GameMode.objects.create(name="testgamemode", game=Game.objects.create(name="testgame"))
        BotRace.create_all_races()
        self.race = BotRace.objects.first()
        self.competition = self._competition("stochastic")
        self.map = Map.objects.filter(competitions=self.competition).first()
        self.matches = Matches(BotsImpl(), Competitions())
        self.matches.stochastic = Stochastic(rng=np.random.default_rng(0))
        self._bot_count = 0

    def _competition(self, name, matchmaker=STOCHASTIC, **kwargs):
        competition = Competition.objects.create(name=name, game_mode=self.game_mode, matchmaker=matchmaker, **kwargs)
        competition.playable_races.add(self.race)
        competition.open()
        Map.objects.create(name=f"{name}-map", game_mode=self.game_mode).competitions.add(competition)
        return competition

    def _bots(self, n, competition=None):
        bots = []
        for _ in range(n):
            self._bot_count += 1
            bots.append(
                create_bot_for_competition(
                    competition or self.competition, self.user, f"bot{self._bot_count}", "python", self.race
                )
            )
        return bots

    def _period(self, competition=None):
        return Round.objects.create(
            competition=competition or self.competition, matchmaker=STOCHASTIC, accepts_new_matches=True
        )

    def _play(self, round, bot1, bot2, minutes_ago=1, finish=True, **kwargs):
        """A match of the round that started `minutes_ago`, finished (cancelled) unless `finish` is False."""
        match = create(round, Map.objects.filter(competitions=round.competition).first(), bot1, bot2, **kwargs)
        match.started = match.first_started = timezone.now() - timedelta(minutes=minutes_ago)
        match.save()
        if finish:
            cancel(match.id, None)
        return match

    def _next_match(self, competition=None):
        return self.matches.start_next_match_for_competition(self.arenaclient, competition or self.competition)

    @staticmethod
    def _bots_of(match):
        return {match.participant1.bot, match.participant2.bot}


class RoundSealingTests(_MatchmakingTestCase):
    def test_an_open_period_stays_incomplete_until_sealed(self):
        bot1, bot2 = self._bots(2)
        period = self._period()
        self._play(period, bot1, bot2)  # every match of the period has a result

        update_round_if_completed(period)
        period.refresh_from_db()
        self.assertFalse(period.complete)

        seal_open_rounds(self.competition)
        period.refresh_from_db()
        self.assertFalse(period.accepts_new_matches)
        self.assertTrue(period.complete)

    def test_closing_seals_the_period_and_closes_once_its_matches_finish(self):
        bot1, bot2 = self._bots(2)
        period = self._period()
        running = self._play(period, bot1, bot2, finish=False)

        self.competition.start_closing()
        period.refresh_from_db()
        self.assertFalse(period.accepts_new_matches)
        self.assertFalse(period.complete)
        self.assertEqual(self.competition.status, "closing")

        cancel(running.id, None)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, "closed")

    def test_opening_a_round_seals_the_open_period(self):
        first = open_round(self.competition, STOCHASTIC)
        second = open_round(self.competition, STOCHASTIC)

        first.refresh_from_db()
        self.assertFalse(first.accepts_new_matches)
        self.assertTrue(second.accepts_new_matches)
        self.assertEqual(second.matchmaker, STOCHASTIC)

    def test_round_robin_rounds_are_sealed_from_the_start(self):
        competition = self._competition("round-robin", matchmaker=ROUND_ROBIN)
        self._bots(2, competition)

        round = RoundRobin(BotsImpl(), Competitions()).generate_round(competition)

        self.assertFalse(round.accepts_new_matches)
        self.assertEqual(round.matchmaker, ROUND_ROBIN)


class SnapshotTests(_MatchmakingTestCase):
    def _snapshot(self, arenaclient=None, **params):
        return build_snapshot(self.competition, arenaclient or self.arenaclient, ScoringParams(**params))

    def _candidate_ids(self, snapshot):
        return set(snapshot.bot_ids[snapshot.candidates].tolist())

    def test_bots_playing_anywhere_are_busy(self):
        in_other_competition, in_requested_match, free1, free2 = self._bots(4)
        other = self._competition("other")
        CompetitionParticipation.objects.create(competition=other, bot=in_other_competition)
        opponent = self._bots(1, other)[0]
        self._play(self._period(other), in_other_competition, opponent, finish=False)
        requested = create(None, self.map, in_requested_match, opponent, requested_by=self.user)
        requested.started = timezone.now()
        requested.save()

        snapshot = self._snapshot()

        self.assertEqual(self._candidate_ids(snapshot), {free1.id, free2.id})

    def test_an_untrusted_client_gets_only_bots_it_may_download(self):
        public1, public2, private = self._bots(3)
        for bot in (public1, public2):
            bot.bot_zip_publicly_downloadable = bot.bot_data_publicly_downloadable = True
            bot.save()
        untrusted = ArenaClient.objects.create(
            trusted=False, owner=self.user, username="untrusted", email="untrusted@test.com"
        )

        self.assertEqual(self._candidate_ids(self._snapshot(untrusted)), {public1.id, public2.id})
        self.assertEqual(self._candidate_ids(self._snapshot()), {public1.id, public2.id, private.id})

    def test_the_fairness_window_counts_the_latest_matches_whatever_their_result(self):
        a, b, c, d = self._bots(4)
        period = self._period()
        self._play(period, a, b, minutes_ago=3)  # outside a window of 2 matches
        self._play(period, a, c, minutes_ago=2)  # cancelled, like every match here
        self._play(period, c, d, minutes_ago=1)

        snapshot = self._snapshot(fairness_window_games=1)  # 1 · 4 bots / 2 = the last 2 matches

        games = dict(zip(snapshot.bot_ids.tolist(), snapshot.window_games.tolist()))
        self.assertEqual(games, {a.id: 1, b.id: 0, c.id: 2, d.id: 1})

    def test_games_since_two_bots_last_met(self):
        a, b, c, d = self._bots(4)
        period = self._period()
        self._play(period, a, c, minutes_ago=2)
        self._play(period, a, b, minutes_ago=1)

        snapshot = self._snapshot()

        position = {bot_id: k for k, bot_id in enumerate(snapshot.bot_ids[snapshot.candidates].tolist())}
        since = snapshot.since
        self.assertEqual(since[position[a.id], position[b.id]], 0)  # their latest match: a rematch
        self.assertEqual(since[position[a.id], position[c.id]], 1)
        self.assertEqual(since[position[a.id], position[d.id]], 2)  # never met: all of a's games
        self.assertEqual(since[position[d.id], position[a.id]], np.inf)  # d has no games


class DispatchTests(_MatchmakingTestCase):
    def test_starts_a_match_in_todays_period(self):
        self._bots(4)

        first = self._next_match()
        second = self._next_match()

        self.assertIsNotNone(first.started)
        self.assertTrue(first.round.accepts_new_matches)
        self.assertEqual(first.round.matchmaker, STOCHASTIC)
        self.assertEqual(second.round, first.round)

    def test_the_days_first_match_opens_a_period_and_updates_divisions(self):
        bot1, bot2 = self._bots(2)
        day1 = datetime(2026, 12, 3, 22, 0, tzinfo=UTC)
        with mock.patch("django.utils.timezone.now", return_value=day1):
            first = self._next_match()
        cancel(first.id, None)
        self.assertTrue(
            CompetitionParticipation.objects.filter(competition=self.competition, division_num__gte=1).exists()
        )

        with mock.patch("django.utils.timezone.now", return_value=day1 + timedelta(hours=3)):
            second = self._next_match()

        yesterday = Round.objects.get(id=first.round.id)
        self.assertNotEqual(second.round, yesterday)
        self.assertFalse(yesterday.accepts_new_matches)
        self.assertTrue(yesterday.complete)

    def test_a_new_bot_plays_before_it_has_a_division(self):
        (veteran,) = self._bots(1)
        open_round(self.competition, STOCHASTIC)  # places the veteran in a division
        (newcomer,) = self._bots(1)

        match = self._next_match()

        self.assertEqual(self._bots_of(match), {veteran, newcomer})
        newcomer_participation = CompetitionParticipation.objects.get(bot=newcomer)
        self.assertEqual(newcomer_participation.division_num, CompetitionParticipation.DEFAULT_DIVISION)

    def test_a_rematch_is_blocked_from_three_bots_up(self):
        a, b, c = self._bots(3)
        period = self._period()
        self._play(period, a, b)
        self._play(period, c, a, minutes_ago=5)
        self._play(period, c, b, minutes_ago=4, finish=False)  # c is busy; only a and b are free

        with self.assertRaises(NotEnoughAvailableBots):
            self._next_match()

    def test_two_bots_meet_again(self):
        a, b = self._bots(2)
        self._play(self._period(), a, b)

        match = self._next_match()

        self.assertEqual(self._bots_of(match), {a, b})

    def test_every_bot_plays_one_match_at_a_time(self):
        bots = self._bots(4)
        for bot in bots:  # bots without bot data could otherwise play several matches at once
            bot.bot_data_enabled = False
            bot.save()

        first, second = self._next_match(), self._next_match()

        self.assertEqual(self._bots_of(first) | self._bots_of(second), set(bots))
        with self.assertRaises(NotEnoughAvailableBots):
            self._next_match()

    def test_a_paused_competition_starts_no_match_and_opens_no_period(self):
        self._bots(2)
        self.competition.pause()

        with self.assertRaises(CompetitionPaused):
            self._next_match()
        self.assertFalse(Round.objects.filter(competition=self.competition).exists())


class SwitchingTests(_MatchmakingTestCase):
    def test_after_a_switch_to_stochastic_round_robin_leftovers_are_played_first(self):
        a, b, c, d = self._bots(4)
        leftover_round = Round.objects.create(competition=self.competition, matchmaker=ROUND_ROBIN)
        leftover = create(leftover_round, self.map, a, b)

        match = self._next_match()

        self.assertEqual(match.id, leftover.id)
        follow_up = self._next_match()  # nothing left over: the stochastic matchmaker takes over
        self.assertEqual(self._bots_of(follow_up), {c, d})
        self.assertEqual(follow_up.round.matchmaker, STOCHASTIC)

    def test_after_a_switch_back_the_period_is_sealed_and_round_robin_takes_over(self):
        a, b = self._bots(2)
        period = self._period()
        self._play(period, a, b)
        self.competition.matchmaker = ROUND_ROBIN
        self.competition.save()

        match = self._next_match()

        period.refresh_from_db()
        self.assertFalse(period.accepts_new_matches)
        self.assertTrue(period.complete)
        self.assertEqual(match.round.matchmaker, ROUND_ROBIN)
        self.assertFalse(match.round.accepts_new_matches)
