"""The stochastic matchmaker's scoring core, against the matchmaking prototype it was ported from.

`stochastic_parity.json` holds random snapshots and the score components the prototype computes for
them, exported by its `matchmakers/stochastic/export_parity_fixtures.py`. Agreeing with it means the
live matchmaker scores pairs as the simulations that tuned its parameters did.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from aiarena.core.models import Competition
from aiarena.core.services.service_implementations.internal.matchmaking.scoring import (
    ScoringParams,
    sample,
    score_components,
)


_FIXTURES = json.loads((Path(__file__).parent / "stochastic_parity.json").read_text())


@pytest.mark.parametrize(
    "params, snapshot",
    [(fixture["params"], snapshot) for fixture in _FIXTURES for snapshot in fixture["snapshots"]],
    ids=lambda value: f"{len(value['ranks'])}-bots" if "ranks" in value else None,
)
def test_matches_the_prototype(params, snapshot):
    ranks, games = np.array(snapshot["ranks"]), np.array(snapshot["games"])
    since = np.array(snapshot["since"])
    a, b = np.array(snapshot["pairs"]["a"]), np.array(snapshot["pairs"]["b"])

    components = score_components(
        ScoringParams(**params),
        rank_a=ranks[a],
        rank_b=ranks[b],
        games_a=games[a],
        games_b=games[b],
        mean_games=float(games.mean()),
        since_a=since[a, b],
        since_b=since[b, a],
    )

    for name, prototype_name in [("skill", "skill"), ("fairness", "fairness"), ("variety", "variety")]:
        np.testing.assert_allclose(components[name], snapshot["expected"][prototype_name], rtol=1e-12)
    best = sample(sum(components.values()), temperature=0, rng=np.random.default_rng(0))
    assert best == snapshot["best"]


def test_params_are_the_competitions_and_default_to_the_tuned_values():
    assert ScoringParams.for_competition(Competition()) == ScoringParams()

    params = ScoringParams.for_competition(Competition(skill_weight=0.3, sampling_temperature=0.0))

    assert params.skill_weight == 0.3
    assert params.sampling_temperature == 0.0


def test_sampling_favours_higher_scores():
    rng = np.random.default_rng(1)
    scores = np.array([0.0, 0.05, 0.1])

    picks = np.bincount([sample(scores, temperature=0.02, rng=rng) for _ in range(2000)], minlength=3)

    assert picks[2] > picks[1] > picks[0] > 0
