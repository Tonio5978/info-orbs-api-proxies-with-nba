"""Tests of the soccer HTTP routes and headers (cache filled by hand, scheduler not started)."""
import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src import soccer_proxy
from src.soccer_scheduler import Payload


class SoccerRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(soccer_proxy.app)  # No lifespan: the scheduler doesn't run, nothing calls ESPN
        cls.ligue1 = soccer_proxy.scheduler.competitions["ligue1"]

    def tearDown(self):
        self.ligue1.matches = None

    def test_two_routes_per_competition(self):
        paths = {route.path for route in soccer_proxy.app.routes}
        for c in soccer_proxy.COMPETITIONS:
            self.assertIn(f"/{c['key']}UpcomingMatches", paths)
            self.assertIn(f"/{c['key']}Teams", paths)
        self.assertIn("/health", paths)

    def test_not_loaded_yet(self):
        r = self.client.get("/ligue1UpcomingMatches")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.headers["retry-after"], "10")
        self.assertEqual(r.headers["access-control-allow-origin"], "*")

    def test_headers_and_etag(self):
        self.ligue1.matches = Payload.of({"events": [{"id": "1", "name": "Nice at Lens"}]})
        r = self.client.get("/ligue1UpcomingMatches")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"events": [{"id": "1", "name": "Nice at Lens"}]})
        self.assertEqual(r.headers["cache-control"], "public, max-age=10")
        self.assertEqual(r.headers["access-control-allow-origin"], "*")
        self.assertTrue(r.headers["content-type"].startswith("application/json"))
        self.assertEqual(r.headers["etag"], self.ligue1.matches.etag)
        cached = self.client.get("/ligue1UpcomingMatches", headers={"If-None-Match": r.headers["etag"]})
        self.assertEqual((cached.status_code, cached.content), (304, b""))
        other = self.client.get("/ligue1UpcomingMatches", headers={"If-None-Match": '"other"'})
        self.assertEqual(other.status_code, 200)

    def test_standings_routes(self):
        paths = {route.path for route in soccer_proxy.app.routes}
        for c in soccer_proxy.COMPETITIONS:
            if c.get("standings", True):
                self.assertIn(f"/{c['key']}Standings", paths)
            else:  # Knockout cups: matches and teams, no table
                self.assertNotIn(f"/{c['key']}Standings", paths)
                self.assertIn(f"/{c['key']}UpcomingMatches", paths)
        self.assertEqual(self.client.get("/faCupStandings").status_code, 404)
        self.assertEqual(self.client.get("/ligue1Standings").status_code, 503)  # Not loaded yet
        fixtures = Path(__file__).parent / "fixtures" / "soccer"
        self.ligue1.standings_response = json.loads((fixtures / "standings_fra.1.json").read_text(encoding="utf-8"))
        try:
            r = self.client.get("/ligue1Standings?team=psg")
            self.assertEqual(r.status_code, 200)
            self.assertEqual([t["abbreviation"] for t in r.json()["groups"][0]["teams"]], ["PSG"])
            self.assertIn("etag", r.headers)
        finally:
            self.ligue1.standings_response = None

    def test_unknown_route(self):
        self.assertEqual(self.client.get("/fooUpcomingMatches").status_code, 404)

    def test_health(self):
        body = self.client.get("/health").json()
        self.assertEqual(body["service"], "soccer_proxy")
        self.assertEqual(set(body["competitions"]), {c["key"] for c in soccer_proxy.COMPETITIONS})
        self.assertIn(body["status"], ("starting", "degraded", "ok"))


class SoccerDayRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(soccer_proxy.app)
        fixtures = Path(__file__).parent / "fixtures" / "soccer"
        cls.today = json.loads((fixtures / "scoreboard_all_20261020.json").read_text(encoding="utf-8"))["events"]

    def setUp(self):
        self.board = soccer_proxy.scheduler.boards["scores"]
        soccer_proxy.scheduler.competitions["cl"].league_id = "775"

    def tearDown(self):
        self.board.date = self.board.events = None
        soccer_proxy.scheduler.competitions["cl"].league_id = None

    def test_not_loaded_yet(self):
        r = self.client.get("/upcoming")
        self.assertEqual((r.status_code, r.headers["retry-after"]), (503, "10"))

    def test_scores_filters_and_headers(self):
        self.board.date, self.board.events = "2026-10-20", self.today
        r = self.client.get("/scores")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["date"], r.json()["count"]), ("2026-10-20", 9))  # Only the configured CL
        self.assertEqual(r.headers["access-control-allow-origin"], "*")
        self.assertEqual(self.client.get("/scores", headers={"If-None-Match": r.headers["etag"]}).status_code, 304)
        self.assertEqual(self.client.get("/scores?all=true").json()["count"], len(self.today))
        self.assertEqual(self.client.get("/scores?competition=ligue1").json()["count"], 0)
        self.assertEqual(self.client.get("/scores?team=psg").json()["count"], 1)
        self.assertEqual(self.client.get("/scores?tz=UTC").json()["games"][0]["startTime"], "16:45")
        self.assertEqual(self.client.get("/scores?competition=nope").status_code, 400)
        self.assertEqual(self.client.get("/scores?tz=Mars/Base").status_code, 400)


if __name__ == "__main__":
    unittest.main()
