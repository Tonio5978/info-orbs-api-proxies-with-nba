"""Tests of /nbadata/scores, /nbadata/upcoming and the live game of /nbadata/proxy, read from the scheduler boards."""
import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src import nbadata_proxy

FIXTURES = Path(__file__).parent / "fixtures" / "nba"
TODAY = json.loads((FIXTURES / "scoreboard_20261020_live.json").read_text(encoding="utf-8"))
TOMORROW = json.loads((FIXTURES / "scoreboard_20261021.json").read_text(encoding="utf-8"))


class NbaRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(nbadata_proxy.app)  # No lifespan: the scheduler doesn't run, nothing calls ESPN

    def setUp(self):
        boards = nbadata_proxy.scheduler.boards
        boards["scores"].date, boards["scores"].events = "2026-10-20", TODAY["events"]
        boards["upcoming"].date, boards["upcoming"].events = "2026-10-21", TOMORROW["events"]

    def tearDown(self):
        for board in nbadata_proxy.scheduler.boards.values():
            board.date = board.events = None

    def test_scores_format(self):
        r = self.client.get("/scores")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["date"], body["count"]), ("2026-10-20", 3))
        live = next(g for g in body["games"] if g["status"] == "in")
        self.assertEqual(list(live), ["gameId", "status", "period", "clock", "detail", "lastPlay", "startTime", "home", "away"])
        self.assertEqual(live["lastPlay"], "Jayson Tatum defensive rebound")
        for side in ("home", "away"):
            self.assertRegex(live[side]["color"], r"^#[0-9A-Fa-f]{6}$")  # Colors also during live games
            self.assertEqual(list(live[side]), ["abbreviation", "displayName", "score", "color"])
        scheduled = next(g for g in body["games"] if g["status"] == "pre")
        self.assertTrue(scheduled["home"]["color"].startswith("#"))
        self.assertIn("proxy-info", body)

    def test_upcoming_and_filters(self):
        body = self.client.get("/upcoming?team=LAL&tz=Europe/Paris").json()
        self.assertEqual((body["date"], body["count"]), ("2026-10-21", 1))
        self.assertEqual(body["games"][0]["startTime"], "4:00 AM")  # GS @ LAL, 02:00Z
        self.assertEqual(body["games"][0]["home"]["displayName"], "Los Angeles Lakers")

    def test_not_loaded_yet(self):
        nbadata_proxy.scheduler.boards["upcoming"].events = None
        r = self.client.get("/upcoming")
        self.assertEqual((r.status_code, r.headers["retry-after"]), (503, "5"))

    def test_live_game_from_board(self):
        live = nbadata_proxy.build_live_game(nbadata_proxy.get_scoreboard(), "8")  # Detroit
        self.assertEqual((live["isLive"], live["opponent"], live["location"]), (True, "BOS", "Home"))
        self.assertEqual(nbadata_proxy.build_live_game(nbadata_proxy.get_scoreboard(), "13"), {})  # Lakers not playing

    def test_health(self):
        health = self.client.get("/health").json()
        self.assertEqual(health["scoreboards"]["scores"]["games"], 3)


if __name__ == "__main__":
    unittest.main()
