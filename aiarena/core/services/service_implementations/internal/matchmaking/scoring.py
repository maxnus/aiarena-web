"""
The stochastic matchmaker's score of a pair of bots, and the sampling of a pair by score.

Pure functions of arrays, one element per candidate pair, ported from the matchmaking prototype
(sc2-matchmaking-prototype) so that they can be compared with it directly.
"""

from dataclasses import dataclass, fields

import numpy as np

from aiarena.core.models import Competition


@dataclass(frozen=True)
class ScoringParams:
    skill_weight: float = 0.1
    fairness_weight: float = 1.0
    variety_weight: float = 0.2
    skill_tolerance_ranks: float = 15.0
    """Rank difference at which a pair's skill score halves."""
    variety_memory_games: float = 40.0
    """How quickly a past match-up is forgotten, in games."""
    sampling_temperature: float = 0.02
    """0 always takes the best pair."""
    fairness_window_games: float = 10.0
    """Games per bot, on average, in the fairness window."""

    @classmethod
    def for_competition(cls, competition: Competition) -> "ScoringParams":
        """The competition's parameters, with the defaults for any it leaves unset."""
        defaults = cls()
        return cls(
            **{
                f.name: getattr(competition, f.name)
                if getattr(competition, f.name) is not None
                else getattr(defaults, f.name)
                for f in fields(cls)
            }
        )


def f_skill(rank_a: np.ndarray, rank_b: np.ndarray, tau: float) -> np.ndarray:
    """Close in rank: a Cauchy kernel of the rank difference, 1 for neighbours and 1/2 at `tau`."""
    return 1.0 / (1.0 + ((rank_a - rank_b) / tau) ** 2)


def f_fair(g_a: np.ndarray, g_b: np.ndarray, g_bar: float) -> np.ndarray:
    """Underplayed: the root mean square of both bots' deficits below the mean `g_bar`, relative to it."""
    if g_bar == 0:
        return np.zeros(len(g_a))
    d_a = np.maximum(g_bar - g_a, 0) / g_bar
    d_b = np.maximum(g_bar - g_b, 0) / g_bar
    return np.sqrt((d_a**2 + d_b**2) / 2)


def f_var(a_ab: np.ndarray, b_ab: np.ndarray, lam: float) -> np.ndarray:
    """Not met lately: from the games each bot played since the two last met."""
    return 1.0 - np.exp(-np.sqrt(a_ab * b_ab) / lam)


def score_components(
    params: ScoringParams,
    rank_a: np.ndarray,
    rank_b: np.ndarray,
    games_a: np.ndarray,
    games_b: np.ndarray,
    mean_games: float,
    since_a: np.ndarray,
    since_b: np.ndarray,
) -> dict[str, np.ndarray]:
    """
    The weighted skill, fairness and variety scores of the pairs (a[k], b[k]).

    `rank_*` are the bots' ranks by rating, best first; `games_*` their games in the fairness window,
    whose mean over all the competition's bots is `mean_games`; `since_a` the games bot a played since it
    last met bot b, all its games if they never met, or infinity if it has none, and `since_b` the same
    for bot b.
    """
    p = params
    return {
        "skill": p.skill_weight * f_skill(rank_a, rank_b, p.skill_tolerance_ranks),
        "fairness": p.fairness_weight * f_fair(games_a, games_b, mean_games),
        "variety": p.variety_weight * f_var(since_a, since_b, p.variety_memory_games),
    }


def sample(scores: np.ndarray, temperature: float, rng: np.random.Generator) -> int:
    """
    The index of a pair sampled with probability proportional to exp(score / temperature),
    or of the best pair at temperature 0.
    """
    if temperature <= 0:
        return int(np.argmax(scores))
    logits = (scores - scores.max()) / temperature
    probs = np.exp(logits)
    probs /= probs.sum()
    return int(rng.choice(len(scores), p=probs))
