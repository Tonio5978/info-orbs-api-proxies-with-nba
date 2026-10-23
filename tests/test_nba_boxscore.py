"""Top scorers of finished games in /nbadata/scores, from the ESPN game summary (recorded CLE @ PHI, Oct 22)."""
import asyncio
import copy
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from src import nbadata_proxy
from src.nba_scheduler import NbaScoreboardScheduler, top_scorers

FIXTURES = Path(__file__).parent / "fixtures" / "nba"
SUMMARY = json.loads((FIXTURES / "summary_401909093.json").read_text(encoding="utf-8"))
FINAL_DAY = json.loads((FIXTURES / "scoreboard_20261022_final.json").read_text(encoding="utf-8"))  # 2 final games
FIELDS = ["name", "jersey", "minutes", "points", "fieldGoalsMade", "fieldGoalsAttempted", "threePointsMade",
          "threePointsAttempted", "freeThrowsMade", "freeThrowsAttempted", "assists", "rebounds",
          "offensiveRebounds", "blocks", "plusMinus"]


class TopScorersTest(unittest.TestCase):
    def test_five_best_scorers_per_team(self):
        teams = top_scorers(SUMMARY)
        self.assertEqual(len(teams), 2)
        box_players = {str(t["team"]["id"]): t["statistics"][0]["athletes"] for t in SUMMARY["boxscore"]["players"]}
        for team_id, players in teams.items():
            self.assertEqual(len(players), 5)
            points = [p["points"] for p in players]
            self.assertEqual(points, sorted(points, reverse=True))
            # Nobody left out scored more than the 5th
            played = [a for a in box_players[team_id] if not a.get("didNotPlay") and a.get("stats")]
            others = sorted((int(a["stats"][1]) for a in played), reverse=True)
            self.assertEqual(points, others[:5])
            for p in players:
                self.assertEqual(list(p), FIELDS)
                self.assertLessEqual(p["fieldGoalsMade"], p["fieldGoalsAttempted"])
                self.assertLessEqual(p["threePointsMade"], p["threePointsAttempted"])
                self.assertLessEqual(p["freeThrowsMade"], p["freeThrowsAttempted"])
                self.assertLessEqual(p["offensiveRebounds"], p["rebounds"])
                self.assertEqual(p["points"], 2 * p["fieldGoalsMade"] + p["threePointsMade"] + p["freeThrowsMade"])
                self.assertIsInstance(p["plusMinus"], int)

    def test_values_parsed_from_espn(self):
        phi = top_scorers(SUMMARY)["20"]
        embiid = next(p for p in phi if p["name"] == "J. Embiid")
        self.assertEqual((embiid["minutes"], embiid["points"], embiid["fieldGoalsMade"], embiid["fieldGoalsAttempted"],
                          embiid["freeThrowsMade"], embiid["freeThrowsAttempted"], embiid["plusMinus"]),
                         (34, 24, 8, 16, 7, 9, 18))  # '34', '24', '8-16', '7-9', '+18'

    def test_did_not_play_excluded(self):
        summary = copy.deepcopy(SUMMARY)
        group = summary["boxscore"]["players"][0]["statistics"][0]
        star = max(group["athletes"], key=lambda a: int(a["stats"][1]) if a.get("stats") else -1)
        star["didNotPlay"] = True
        names = [p["name"] for p in top_scorers(summary)[str(summary["boxscore"]["players"][0]["team"]["id"])]]
        self.assertNotIn(star["athlete"]["shortName"], names)


class FakeESPN:
    def __init__(self):
        self.urls, self.fail = [], False

    async def __call__(self, url):
        self.urls.append(url)
        if "summary?event=" in url:
            if self.fail:
                raise HTTPException(status_code=503, detail="Upstream API returned HTTP 503")
            summary = copy.deepcopy(SUMMARY)
            summary["header"]["id"] = url.rsplit("=", 1)[1]
            return summary
        return copy.deepcopy(FINAL_DAY)


class BoxscoreSchedulingTest(unittest.IsolatedAsyncioTestCase):
    def make(self):
        self.now = datetime(2026, 10, 23, 14, 46, tzinfo=timezone.utc)
        fake = FakeESPN()
        scheduler = NbaScoreboardScheduler(fake, logging.getLogger("test"), clock=lambda: self.now)
        return scheduler, fake

    async def test_fetched_once_per_final_game_then_every_30_min(self):
        scheduler, fake = self.make()
        await scheduler.run_job("scores")
        summaries = [u for u in fake.urls if "summary" in u]
        self.assertEqual(sorted(u.rsplit("=", 1)[1] for u in summaries), ["401909093", "401909094"])
        self.assertEqual(set(scheduler.boxscores), {"401909093", "401909094"})
        fake.urls.clear()
        self.now += timedelta(minutes=10)
        await scheduler.run_job("scores")
        self.assertFalse(any("summary" in u for u in fake.urls))  # Still fresh
        self.now += timedelta(minutes=25)
        await scheduler.run_job("scores")
        self.assertEqual(len([u for u in fake.urls if "summary" in u]), 2)  # Refreshed after 30 min

    async def test_failure_keeps_scores_and_previous_box_score(self):
        scheduler, fake = self.make()
        await scheduler.run_job("scores")
        before = scheduler.boxscores["401909093"]
        fake.fail = True
        self.now += timedelta(minutes=31)
        await scheduler.run_job("scores")
        self.assertIs(scheduler.boxscores["401909093"], before)
        self.assertIsNotNone(scheduler.boards["scores"].events)
        self.assertIsNone(scheduler.boards["scores"].last_error)  # A box score failure doesn't fail the scores

    async def test_games_leaving_the_board_are_forgotten(self):
        scheduler, fake = self.make()
        await scheduler.run_job("scores")
        scheduler.boards["scores"].events = []
        await scheduler.refresh_boxscores()
        self.assertEqual(scheduler.boxscores, {})


class ScoresRouteTest(unittest.TestCase):
    def setUp(self):
        board = nbadata_proxy.scheduler.boards["scores"]
        board.date, board.events = "2026-10-22", FINAL_DAY["events"]
        nbadata_proxy.scheduler.boxscores = {"401909093": {"teams": top_scorers(SUMMARY), "fetched_at": None}}

    def tearDown(self):
        board = nbadata_proxy.scheduler.boards["scores"]
        board.date = board.events = None
        nbadata_proxy.scheduler.boxscores = {}

    def test_top_scorers_in_final_games(self):
        games = {g["gameId"]: g for g in TestClient(nbadata_proxy.app).get("/scores").json()["games"]}
        cle_phi = games["401909093"]
        self.assertEqual(cle_phi["status"], "post")
        for side in ("home", "away"):
            self.assertEqual(len(cle_phi[side]["topScorers"]), 5)
            self.assertEqual(list(cle_phi[side]), ["abbreviation", "score", "color", "topScorers"])
        self.assertEqual([p["name"] for p in cle_phi["home"]["topScorers"]][:2], ["T. Maxey", "J. Embiid"])  # PHI: 32 and 24 pts
        self.assertIsNone(games["401909094"]["home"]["topScorers"])  # Box score not loaded yet


if __name__ == "__main__":
    unittest.main()
