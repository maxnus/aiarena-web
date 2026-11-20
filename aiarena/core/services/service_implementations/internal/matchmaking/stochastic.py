"""
The stochastic matchmaker: whenever an arena client asks for a match, score every pair of the
competition's free bots on skill, fairness and opponent variety, and sample a pair, favouring high
scores. There is no schedule; each match is created when it is picked and attached to the
competition's period for the day.
"""

import logging
import random
from dataclasses import dataclass
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone

import numpy as np

from aiarena.core.exceptions import CompetitionClosing, CompetitionPaused, NoMaps, NotEnoughAvailableBots
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

from ..match_starter import MatchStarter
from ..matches import create
from .rounds import open_round
from .scoring import ScoringParams, sample, score_components


logger = logging.getLogger(__name__)

# How far back the variety term looks, in multiples of its memory: beyond five, a past match-up
# has been all but forgotten.
_HISTORY_MEMORIES = 5
# Only to bound the history query. An active bot plays its history's games in far less time than this.
_HISTORY_MAX_AGE = timedelta(days=28)
# Pairs to try when a picked match fails to start, which only a race with another competition for
# the same bot should cause.
_START_ATTEMPTS = 3


@dataclass
class Snapshot:
    """What the matchmaker knows about a competition at the moment of a request."""

    bot_ids: np.ndarray
    """The competition's active bots."""
    ranks: np.ndarray
    """Each bot's rank by rating, best first."""
    window_games: np.ndarray
    """Each bot's matches among the competition's most recent ones, finished or running."""
    candidates: np.ndarray
    """Indices of the bots that can play now, for the requesting arena client."""
    since: np.ndarray
    """For candidates k and l: the games candidate k played since it last met candidate l; all the
    games in its history if they haven't met within it, or infinity if it has none."""


def build_snapshot(competition: Competition, arenaclient: ArenaClient, params: ScoringParams) -> Snapshot:
    participations = list(
        CompetitionParticipation.objects.filter(competition=competition, active=True)
        .select_related("bot")
        .only("elo", "bot__id", "bot__bot_zip_publicly_downloadable", "bot__bot_data_publicly_downloadable")
    )
    n = len(participations)
    bot_ids = np.array([p.bot.id for p in participations], dtype=int)

    # Rank by rating, best first; ties by bot id.
    order = np.lexsort((bot_ids, -np.array([p.elo for p in participations])))
    ranks = np.empty(n, dtype=int)
    ranks[order] = np.arange(n)

    # Fairness: matches among the competition's last W·n/2, by start, whatever their result.
    window_size = max(1, round(params.fairness_window_games * n / 2))
    recent = (
        Match.objects.filter(round__competition=competition, started__isnull=False)
        .order_by("-started", "-id")
        .values("id")[:window_size]
    )
    counts = dict(
        MatchParticipation.objects.filter(match_id__in=recent)
        .values("bot_id")
        .annotate(n=Count("id"))
        .values_list("bot_id", "n")
    )
    window_games = np.array([counts.get(b, 0) for b in bot_ids], dtype=float)

    # A bot is busy while it plays any match, in any competition or requested: every bot plays one
    # match at a time, so that bots without bot data can't take every spare server on a small ladder.
    busy = set(
        MatchParticipation.objects.filter(
            bot_id__in=bot_ids.tolist(), match__started__isnull=False, match__result__isnull=True
        ).values_list("bot_id", flat=True)
    )
    candidates = np.array(
        [
            i
            for i, p in enumerate(participations)
            if p.bot.id not in busy and (arenaclient.trusted or not p.bot.requires_trusted_arenaclient)
        ],
        dtype=int,
    )

    return Snapshot(
        bot_ids=bot_ids,
        ranks=ranks,
        window_games=window_games,
        candidates=candidates,
        since=_games_since(competition, bot_ids[candidates], params),
    )


def _games_since(competition: Competition, candidate_ids: np.ndarray, params: ScoringParams) -> np.ndarray:
    """The `since` matrix of a snapshot, from each candidate's recent matches in the competition."""
    c = len(candidate_ids)
    since = np.full((c, c), np.nan)
    history_len = np.zeros(c)
    if c == 0:
        return since
    depth = int(np.ceil(_HISTORY_MEMORIES * params.variety_memory_games))
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT bot_id, opponent_id, rn FROM (
                SELECT me.bot_id AS bot_id, opp.bot_id AS opponent_id,
                       ROW_NUMBER() OVER (PARTITION BY me.bot_id ORDER BY m.started DESC, m.id DESC) AS rn
                FROM core_matchparticipation me
                JOIN core_match m ON m.id = me.match_id
                JOIN core_round r ON r.id = m.round_id
                JOIN core_matchparticipation opp
                    ON opp.match_id = me.match_id AND opp.participant_number <> me.participant_number
                WHERE r.competition_id = %s AND m.started >= %s AND me.bot_id = ANY(%s)
            ) history
            WHERE rn <= %s
            """,
            (competition.id, timezone.now() - _HISTORY_MAX_AGE, candidate_ids.tolist(), depth),
        )
        rows = cursor.fetchall()
    position = {int(b): k for k, b in enumerate(candidate_ids)}
    for bot_id, opponent_id, rn in rows:
        k = position[bot_id]
        history_len[k] = max(history_len[k], rn)
        l = position.get(opponent_id)
        if l is not None and not since[k, l] <= rn - 1:  # keep the most recent meeting
            since[k, l] = rn - 1
    never_met = np.where(history_len > 0, history_len, np.inf)
    return np.where(np.isnan(since), never_met[:, None], since)


def choose_pair(
    snapshot: Snapshot, params: ScoringParams, rng: np.random.Generator, excluded: set[tuple[int, int]] = frozenset()
) -> tuple[int, int] | None:
    """Sample the bot ids of a pair to play, or None if no pair is allowed."""
    k, l = np.triu_indices(len(snapshot.candidates), 1)
    a, b = snapshot.candidates[k], snapshot.candidates[l]
    since_a, since_b = snapshot.since[k, l], snapshot.since[l, k]

    # Rematch prevention: neither bot's latest opponent may be the other. Two bots have no one else
    # to play, so only from three up; there, the latest opponents block fewer pairs than there are.
    allowed = np.ones(len(a), dtype=bool)
    if len(snapshot.bot_ids) >= 3:
        allowed &= (since_a != 0) & (since_b != 0)
    if excluded:
        ids_a, ids_b = snapshot.bot_ids[a], snapshot.bot_ids[b]
        allowed &= np.array([(x, y) not in excluded for x, y in zip(ids_a, ids_b)], dtype=bool)
    if not allowed.any():
        return None
    a, b, since_a, since_b = a[allowed], b[allowed], since_a[allowed], since_b[allowed]

    components = score_components(
        params,
        rank_a=snapshot.ranks[a],
        rank_b=snapshot.ranks[b],
        games_a=snapshot.window_games[a],
        games_b=snapshot.window_games[b],
        mean_games=float(snapshot.window_games.mean()),
        since_a=since_a,
        since_b=since_b,
    )
    choice = sample(sum(components.values()), params.sampling_temperature, rng)
    return int(snapshot.bot_ids[a[choice]]), int(snapshot.bot_ids[b[choice]])


class _NotStarted(Exception):
    """Rolls back a match that was created but couldn't start."""


class Stochastic:
    """
    The stochastic matchmaker. Called inside the coordinator's transaction, which holds the
    competition's participations locked, so requests for one competition are handled one at a time
    and each sees the previous one's match as running.
    """

    def __init__(self, rng: np.random.Generator | None = None):
        self.rng = rng if rng is not None else np.random.default_rng()

    def start_match(self, requesting_ac: ArenaClient, competition: Competition) -> Match:
        # Every check comes before anything is written: the caller catches these exceptions inside
        # its transaction, so nothing written before them would be rolled back.
        if competition.is_paused:
            raise CompetitionPaused()
        if competition.is_closing:
            raise CompetitionClosing()
        maps = list(Map.objects.filter(competitions=competition))
        if not maps:
            raise NoMaps()

        params = ScoringParams.for_competition(competition)
        snapshot = build_snapshot(competition, requesting_ac, params)
        pair = choose_pair(snapshot, params, self.rng)
        if pair is None:
            raise NotEnoughAvailableBots()

        period = self._current_period(competition)
        excluded = set()
        for _ in range(_START_ATTEMPTS):
            match = self._start(period, random.choice(maps), pair, requesting_ac, competition)
            if match is not None:
                return match
            logger.warning(f"Stochastic match between bots {pair} failed to start; trying another pair.")
            excluded.add(pair)
            pair = choose_pair(snapshot, params, self.rng, excluded)
            if pair is None:
                break
        raise NotEnoughAvailableBots()

    @staticmethod
    def _current_period(competition: Competition) -> Round:
        """Today's period of the competition, opening it, and sealing yesterday's, on the day's first match."""
        start_of_day = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        period = (
            Round.objects.filter(competition=competition, accepts_new_matches=True, started__gte=start_of_day)
            .order_by("-number")
            .first()
        )
        return period if period is not None else open_round(competition, Competition.Matchmaker.STOCHASTIC)

    @staticmethod
    def _start(period: Round, map: Map, pair: tuple[int, int], requesting_ac: ArenaClient, competition: Competition):
        """Create the match in the period and start it, or roll it back and return None.

        A period never holds an unstarted match, so the round-robin step that starts unstarted matches
        in open rounds only ever finds round-robin leftovers.
        """
        bot1, bot2 = Bot.objects.get(id=pair[0]), Bot.objects.get(id=pair[1])
        try:
            with transaction.atomic():
                match = create(
                    period,
                    map,
                    bot1,
                    bot2,
                    require_trusted_arenaclient=competition.require_trusted_infrastructure,
                )
                if not MatchStarter.start(match, requesting_ac):
                    raise _NotStarted()
            return match
        except _NotStarted:
            return None
