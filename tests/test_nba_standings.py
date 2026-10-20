"""Tests of /nbadata/standings on recorded ESPN standings (final 2025-26 season, and 2026-27 before tip-off)."""
import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src import nbadata_proxy

FIXTURES = Path(__file__).parent / "fixtures" / "nba"
FINAL = json.loads((FIXTURES / "standings_2026_final.json").read_text(encoding="utf-8"))
PRESEASON = json.loads((FIXTURES / "standings_2027_preseason.json").read_text(encoding="utf-8"))


class NbaStandingsTest(unittest.TestCase):
    def test_sorted_by_seed(self):
        east, west = nbadata_proxy.build_standings_response(FINAL)["conferences"]
        self.assertEqual((east["abbreviation"], west["abbreviation"]), ("East", "West"))
        self.assertEqual([t["rank"] for t in east["teams"]], list(range(1, 16)))  # ESPN order is not by seed
        self.assertEqual((east["teams"][0]["abbreviation"], east["teams"][0]["wins"], east["teams"][0]["losses"]), ("DET", 60, 22))
        self.assertEqual(list(east["teams"][0]), ["rank", "teamId", "abbreviation", "name", "color", "wins", "losses",
                                                  "winPercent", "gamesBehind", "streak", "home", "road", "vsConference",
                                                  "pointsFor", "pointsAgainst"])
        for t in east["teams"] + west["teams"]:
            self.assertRegex(t["color"], r"^#[0-9A-Fa-f]{6}$")

    def test_before_the_season(self):
        east = nbadata_proxy.build_standings_response(PRESEASON)["conferences"][0]
        self.assertEqual(nbadata_proxy.build_standings_response(PRESEASON)["season"], "2027")
        names = [t["name"] for t in east["teams"]]
        self.assertEqual(names, sorted(names))  # Every seed is 1 and every record 0-0: alphabetical
        self.assertTrue(all(t["wins"] == t["losses"] == 0 for t in east["teams"]))

    def test_filters(self):
        only_west = nbadata_proxy.build_standings_response(FINAL, conference="west")["conferences"]
        self.assertEqual([c["abbreviation"] for c in only_west], ["West"])
        lakers = nbadata_proxy.build_standings_response(FINAL, team_id="13")["conferences"]
        self.assertEqual([t["abbreviation"] for c in lakers for t in c["teams"]], ["LAL"])

    def test_route(self):
        client = TestClient(nbadata_proxy.app)  # No lifespan: nothing calls ESPN
        board = nbadata_proxy.scheduler.boards["standings"]
        self.assertEqual(client.get("/standings").status_code, 503)
        board.data = FINAL
        try:
            body = client.get("/standings?conference=East&team=DET").json()
            self.assertEqual(body["conferences"][0]["teams"][0]["rank"], 1)
            self.assertIn("proxy-info", body)
            self.assertEqual(client.get("/standings?team=nobody").status_code, 400)
        finally:
            board.data = None


if __name__ == "__main__":
    unittest.main()
