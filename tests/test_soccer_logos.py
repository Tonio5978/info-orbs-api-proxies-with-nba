"""Tests of scripts/download_soccer_logos.py (no network: recorded ESPN teams responses)."""
import json
import unittest
from pathlib import Path

from scripts import download_soccer_logos as logos

FIXTURES = Path(__file__).parent / "fixtures" / "soccer"
TEAMS = {code: json.loads((FIXTURES / f"teams_{code}.json").read_text(encoding="utf-8"))
         for code in ("fra.1", "uefa.champions")}


class LogosTest(unittest.TestCase):
    def test_pick_logo(self):
        team = {"logos": [{"href": "https://a.espncdn.com/i/teamlogos/soccer/500/160.png", "rel": ["full", "default"]},
                          {"href": "https://a.espncdn.com/i/teamlogos/soccer/500-dark/160.png", "rel": ["full", "dark"]}]}
        self.assertTrue(logos.pick_logo(team, "default").endswith("/500/160.png"))
        self.assertTrue(logos.pick_logo(team, "dark").endswith("/500-dark/160.png"))
        self.assertTrue(logos.pick_logo({"logos": [{"href": "x.png", "rel": ["full"]}]}, "dark") == "x.png")
        self.assertIsNone(logos.pick_logo({}, "default"))

    def test_logo_url(self):
        href = "https://a.espncdn.com/i/teamlogos/soccer/500/160.png"
        self.assertEqual(logos.logo_url(href, None), href)
        self.assertEqual(logos.logo_url(href, 64),
                         "https://a.espncdn.com/combiner/i?img=/i/teamlogos/soccer/500/160.png&w=64&h=64")

    def test_collect_teams_deduplicates(self):
        competitions = [{"key": "ligue1", "espnCode": "fra.1"}, {"key": "cl", "espnCode": "uefa.champions"}]
        fetch = lambda url: TEAMS[url.split("/soccer/")[1].split("/")[0]]
        teams = logos.collect_teams(competitions, fetch, pause=0)
        self.assertEqual(teams["160"]["competitions"], ["ligue1", "cl"])  # PSG once, in both competitions
        self.assertEqual(teams["359"]["competitions"], ["cl"])            # Arsenal
        self.assertTrue(all(logos.pick_logo(t["team"], "default") for t in teams.values()))


if __name__ == "__main__":
    unittest.main()
