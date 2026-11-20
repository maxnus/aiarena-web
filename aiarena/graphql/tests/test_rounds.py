from datetime import UTC, datetime

from aiarena.core.models import Competition, Round
from aiarena.core.tests.base import GraphQLTest
from aiarena.graphql import CompetitionType


class TestCompetitionRounds(GraphQLTest):
    # language=graphql
    competition_query = """
        query ($id: ID!) {
            node(id: $id) {
                ... on CompetitionType {
                    matchmaker
                    rounds {
                        edges {
                            node {
                                name
                                matchmaker
                            }
                        }
                    }
                }
            }
        }
    """

    def _round(self, competition, matchmaker, started):
        round = Round.objects.create(competition=competition, matchmaker=matchmaker)
        Round.objects.filter(id=round.id).update(started=started)

    def test_rounds_are_numbered_and_periods_named_after_their_day(self, competition):
        competition.matchmaker = Competition.Matchmaker.STOCHASTIC
        competition.save()
        self._round(competition, Competition.Matchmaker.ROUND_ROBIN, datetime(2026, 12, 1, 9, 30, tzinfo=UTC))
        self._round(competition, Competition.Matchmaker.STOCHASTIC, datetime(2026, 12, 3, 0, 5, tzinfo=UTC))

        data = self.query(self.competition_query, variables={"id": self.to_global_id(CompetitionType, competition.id)})

        assert data["node"]["matchmaker"] == "STOCHASTIC"
        rounds = [edge["node"] for edge in data["node"]["rounds"]["edges"]]  # newest first
        assert rounds == [
            {"name": "Day 2026-12-03", "matchmaker": "STOCHASTIC"},
            {"name": "Round 1", "matchmaker": "ROUND_ROBIN"},
        ]
