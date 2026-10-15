"""Tests of the soccer HTTP routes and headers (cache filled by hand, scheduler not started)."""
import json
import unittest

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

    def test_unknown_route(self):
        self.assertEqual(self.client.get("/fooUpcomingMatches").status_code, 404)

    def test_health(self):
        body = self.client.get("/health").json()
        self.assertEqual(body["service"], "soccer_proxy")
        self.assertEqual(set(body["competitions"]), {c["key"] for c in soccer_proxy.COMPETITIONS})
        self.assertIn(body["status"], ("starting", "degraded", "ok"))


if __name__ == "__main__":
    unittest.main()
