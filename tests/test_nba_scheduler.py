"""Tests of the NBA scoreboard scheduler (simulated clock, recorded ESPN scoreboards of Oct 20-21)."""
import asyncio
import copy
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException

from src.nba_scheduler import NbaScoreboardScheduler, needs_live_refresh

FIXTURES = Path(__file__).parent / "fixtures" / "nba"
TODAY = json.loads((FIXTURES / "scoreboard_20261020_live.json").read_text(encoding="utf-8"))   # BOS @ DET live
TOMORROW = json.loads((FIXTURES / "scoreboard_20261021.json").read_text(encoding="utf-8"))
STANDINGS = json.loads((FIXTURES / "standings_2026_final.json").read_text(encoding="utf-8"))
UTC = timezone.utc


def at(*args):
    return datetime(*args, tzinfo=UTC)


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class FakeESPN:
    def __init__(self):
        self.today, self.tomorrow = copy.deepcopy(TODAY), copy.deepcopy(TOMORROW)
        self.urls, self.fail = [], False

    async def __call__(self, url):
        self.urls.append(url)
        if self.fail:
            raise HTTPException(status_code=503, detail="Upstream API returned HTTP 503")
        if url.endswith("/standings"):
            return copy.deepcopy(STANDINGS)
        dates = parse_qs(urlsplit(url).query).get("dates")
        return copy.deepcopy(self.tomorrow if dates == ["20261021"] else self.today)


def set_state(board, short_name, state):
    event = next(e for e in board["events"] if e["shortName"] == short_name)
    event["status"]["type"]["state"] = state


def make(now):
    clock, fake = FakeClock(now), FakeESPN()
    return NbaScoreboardScheduler(fake, logging.getLogger("test"), clock=clock, sleep=clock.sleep), clock, fake


async def startup(scheduler):
    await scheduler.run_job("scores")
    await scheduler.run_job("upcoming")
    await scheduler.run_job("standings")


def seconds_to(board, clock):
    return (board.next_at - clock()).total_seconds()


class NbaSchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def test_live_game_at_startup_refreshes_every_10s(self):
        scheduler, clock, fake = make(at(2026, 10, 20, 19, 24))
        await startup(scheduler)
        scores, upcoming = scheduler.boards["scores"], scheduler.boards["upcoming"]
        self.assertEqual((scores.date, upcoming.date), ("2026-10-20", "2026-10-21"))
        self.assertTrue(fake.urls[1].endswith("scoreboard?dates=20261021&limit=25"))
        self.assertTrue(scheduler.is_live(clock()))
        self.assertEqual(seconds_to(scores, clock), 10)
        self.assertEqual(seconds_to(upcoming, clock), 1800)

    async def test_idle_refresh_every_30_min(self):
        scheduler, clock, fake = make(at(2026, 10, 20, 21, 0))
        set_state(fake.today, "BOS @ DET", "post")
        await startup(scheduler)
        self.assertFalse(scheduler.is_live(clock()))
        self.assertEqual(seconds_to(scheduler.boards["scores"], clock), 1800)  # Next tip-off 23:00Z is 2 h away

    async def test_wakes_up_10_min_before_tipoff(self):
        scheduler, clock, fake = make(at(2026, 10, 20, 22, 30))
        set_state(fake.today, "BOS @ DET", "post")
        await startup(scheduler)
        scores = scheduler.boards["scores"]
        self.assertEqual(scores.next_at, at(2026, 10, 20, 22, 50))  # PHI @ NY at 23:00Z, not 23:00 (30 min later)
        clock.now = scores.next_at
        await scheduler.step()
        self.assertEqual(seconds_to(scores, clock), 10)  # Within 10 min of tip-off: live refresh

    async def test_late_tipoff_keeps_live_refresh(self):
        scheduler, clock, fake = make(at(2026, 10, 20, 23, 20))
        set_state(fake.today, "BOS @ DET", "post")  # PHI @ NY still 'pre' 20 min after its tip-off time
        await startup(scheduler)
        self.assertEqual(seconds_to(scheduler.boards["scores"], clock), 10)
        later = {"date": "2026-10-20T19:00Z", "status": {"type": {"state": "pre"}}}
        self.assertFalse(needs_live_refresh(later, at(2026, 10, 20, 22, 30), 10))  # Given up after 3 h

    async def test_tomorrows_first_tipoff_is_watched(self):
        # ESPN still shows the finished Oct 20 day; tomorrow's first game (23:00Z) must not be missed
        scheduler, clock, fake = make(at(2026, 10, 21, 22, 30))  # 30-min idle refresh would be 23:00
        for e in fake.today["events"]:
            e["status"]["type"]["state"] = "post"
        await startup(scheduler)
        self.assertEqual(scheduler.boards["scores"].next_at, at(2026, 10, 21, 22, 50))

    async def test_day_rollover_refreshes_upcoming(self):
        scheduler, clock, fake = make(at(2026, 10, 20, 19, 24))
        await startup(scheduler)
        fake.today["day"]["date"] = "2026-10-21"
        clock.now += timedelta(seconds=10)
        await scheduler.run_job("scores")
        self.assertEqual(scheduler.boards["upcoming"].next_at, clock())

    async def test_failure_keeps_board_and_backs_off(self):
        scheduler, clock, fake = make(at(2026, 10, 20, 19, 24))
        await startup(scheduler)
        scores = scheduler.boards["scores"]
        events = scores.events
        fake.fail = True
        delays = []
        for _ in range(3):
            clock.now = scores.next_at
            await scheduler.run_job("scores")
            delays.append(seconds_to(scores, clock))
        self.assertEqual(delays, [10, 20, 40])  # Live game: retry fast, then slower
        self.assertIs(scores.events, events)
        self.assertIn("503", scores.last_error)
        fake.fail = False
        clock.now = scores.next_at
        await scheduler.run_job("scores")
        self.assertEqual((scores.failures, seconds_to(scores, clock)), (0, 10))

    async def test_upcoming_needs_the_current_day(self):
        scheduler, clock, fake = make(at(2026, 10, 20, 19, 24))
        await scheduler.run_job("upcoming")
        self.assertIsNone(scheduler.boards["upcoming"].events)
        self.assertIn("current ESPN day not known yet", scheduler.boards["upcoming"].last_error)

    async def test_health(self):
        scheduler, clock, fake = make(at(2026, 10, 20, 19, 24))
        await startup(scheduler)
        health = scheduler.health()
        self.assertTrue(health["live"])
        self.assertEqual(health["scoreboards"]["scores"]["games"], 3)
        self.assertEqual(health["espnRequests"], 3)
        self.assertTrue(health["scoreboards"]["standings"]["ready"])

    async def test_standings_cadence(self):
        scheduler, clock, fake = make(at(2026, 10, 20, 19, 24))
        await startup(scheduler)
        self.assertTrue(fake.urls[2].endswith("/apis/v2/sports/basketball/nba/standings"))
        self.assertEqual(seconds_to(scheduler.boards["standings"], clock), 300)  # Game in progress
        set_state(fake.today, "BOS @ DET", "post")
        await scheduler.run_job("scores")
        await scheduler.run_job("standings")
        self.assertEqual(seconds_to(scheduler.boards["standings"], clock), 1800)


if __name__ == "__main__":
    unittest.main()
