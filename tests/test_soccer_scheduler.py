"""Tests of the soccer scheduler with a simulated clock and a fake ESPN (recorded events)."""
import asyncio
import copy
import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from src.soccer_scheduler import Settings, SoccerScheduler

FIXTURES = Path(__file__).parent / "fixtures" / "soccer"
RAW = json.loads((FIXTURES / "scoreboard_mixed.json").read_text(encoding="utf-8"))["events"]
TEAMS = {code: json.loads((FIXTURES / f"teams_{code}.json").read_text(encoding="utf-8"))
         for code in ("fra.1", "uefa.champions")}
STANDINGS = {code: json.loads((FIXTURES / f"standings_{code}.json").read_text(encoding="utf-8"))
             for code in ("fra.1", "uefa.champions", "fra.coupe_de_france")}
# Recorded events by competition: Ligue 1 (matches of Oct 9-16) and Champions League (live on Oct 14 at 19:00Z)
EVENTS = {
    "fra.1": [e for e in RAW if e["competitions"][0]["altGameNote"] == "Ligue 1"],
    "uefa.champions": [e for e in RAW if e["competitions"][0]["altGameNote"].startswith("UEFA Champions League")],
    "fra.coupe_de_france": [],
}
COMPETITIONS = [
    {"key": "ligue1", "espnCode": "fra.1", "country": "france"},
    {"key": "cl", "espnCode": "uefa.champions", "country": None},
]
LIVE_NOW = datetime(2026, 10, 14, 19, 50, tzinfo=timezone.utc)  # CL matches at half time / second half


class FakeClock:
    def __init__(self, now):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.now.timestamp()

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class FakeESPN:
    def __init__(self):
        self.events = copy.deepcopy(EVENTS)
        self.urls = []
        self.fail = set()       # ESPN codes that fail
        self.broken = set()     # ESPN codes returning an unexpected body
        self.active = 0
        self.max_active = 0

    async def __call__(self, url):
        self.urls.append(url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            parts = urlsplit(url)
            code = parts.path.split("/")[-2]
            if code in self.fail:
                raise HTTPException(status_code=503, detail="Upstream API returned HTTP 503")
            if code in self.broken and parts.path.endswith("/scoreboard"):
                return {"code": 400, "message": "Failed to get events endpoint."}
            if parts.path.endswith("/teams"):
                return TEAMS[code]
            if parts.path.endswith("/standings"):
                return STANDINGS[code]
            query = parse_qs(parts.query)
            if code == "all":  # All-competitions scoreboard: current ESPN day (Oct 14) unless dates= is given
                dates = query.get("dates", ["20261014"])[0]
                events = [e for c in self.events.values() for e in c if e["date"].replace("-", "").startswith(dates)]
                return {**({} if "dates" in query else {"day": {"date": "2026-10-14"}}), "events": copy.deepcopy(events)}
            dates = query["dates"][0]
            events = [e for e in self.events.get(code, []) if e["date"].replace("-", "").startswith(dates)]
            return {"events": copy.deepcopy(events)}
        finally:
            self.active -= 1


def make(now=LIVE_NOW, competitions=COMPETITIONS, **settings):
    settings.setdefault("request_pause_seconds", 0)
    clock, fake = FakeClock(now), FakeESPN()
    scheduler = SoccerScheduler(competitions, {"160": {"aliases": ["psg"]}},
                                Settings(tz=ZoneInfo("Europe/Paris"), **settings), fake, logging.getLogger("test"),
                                clock=clock, sleep=clock.sleep, monotonic=clock.monotonic)
    return scheduler, clock, fake


async def run_until_idle(scheduler, clock, max_steps=50):
    """Run every job that is due now, without letting the clock move to future jobs."""
    for _ in range(max_steps):
        due, kind, state = scheduler.next_job()
        if due > clock():
            return
        await scheduler.run_job(kind, state)


def events_of(state):
    return json.loads(state.matches.body)["events"]


class SchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def test_startup_loads_matches_then_teams(self):
        scheduler, clock, fake = make()
        await run_until_idle(scheduler, clock)
        paths = [urlsplit(u).path.split("/")[-1] + "?" + urlsplit(u).query.split("&")[0] for u in fake.urls]
        self.assertEqual(paths, ["scoreboard?dates=202610", "scoreboard?dates=202610", "teams?", "teams?",
                                 "scoreboard?limit=1000", "scoreboard?dates=20261015",  # Then /scores and /upcoming
                                 "standings?", "standings?"])
        ligue1, cl = scheduler.competitions["ligue1"], scheduler.competitions["cl"]
        self.assertEqual([e["id"] for e in events_of(ligue1)], ["401876437"])  # Window Oct 13 -> Oct 28: earlier matches dropped
        self.assertEqual(len(events_of(cl)), 2)
        teams = json.loads(cl.teams.body)["sports"][0]["leagues"][0]["teams"]
        psg = next(t["team"] for t in teams if t["team"]["id"] == "160")
        self.assertEqual((psg["country"], psg["aliases"], psg["competitionKey"]), ("france", ["psg"], "cl"))
        self.assertEqual(scheduler.health()["status"], "ok")

    async def test_refresh_cadence(self):
        scheduler, clock, fake = make()
        await run_until_idle(scheduler, clock)
        ligue1, cl = scheduler.competitions["ligue1"], scheduler.competitions["cl"]
        # Champions League is live: every 20 s. Ligue 1: every 30 min, live refresh 10 min before the next kickoff
        self.assertEqual((cl.next_live_at - clock()).total_seconds(), 20)
        self.assertAlmostEqual((ligue1.next_full_at - clock()).total_seconds(), 1800, delta=10)
        self.assertEqual(ligue1.next_live_at, datetime(2026, 10, 16, 18, 35, tzinfo=timezone.utc))
        self.assertTrue(scheduler.health()["competitions"]["cl"]["live"])
        self.assertFalse(scheduler.health()["competitions"]["ligue1"]["live"])

    async def test_live_refresh_fetches_the_day_and_updates_scores(self):
        scheduler, clock, fake = make()
        await run_until_idle(scheduler, clock)
        cl = scheduler.competitions["cl"]
        before = cl.matches.etag
        live = next(e for e in fake.events["uefa.champions"] if e["status"]["type"]["name"] == "STATUS_SECOND_HALF")
        live["competitions"][0]["competitors"][0]["score"] = "9"
        fake.urls.clear()
        clock.now = cl.next_live_at
        await scheduler.step()
        self.assertEqual(len(fake.urls), 1)
        self.assertIn("dates=20261014", fake.urls[0])  # One day, not the whole month
        updated = next(e for e in events_of(cl) if e["id"] == live["id"])
        self.assertEqual(updated["competitions"][0]["competitors"][0]["score"], "9")
        self.assertNotEqual(cl.matches.etag, before)

    async def test_switches_to_live_refresh_before_kickoff(self):
        scheduler, clock, fake = make()
        await run_until_idle(scheduler, clock)
        ligue1 = scheduler.competitions["ligue1"]
        clock.now = ligue1.next_live_at  # 10 min before Toulouse - Le Mans
        fake.urls.clear()
        await scheduler.run_job("live", ligue1)
        self.assertEqual([parse_qs(urlsplit(u).query)["dates"][0] for u in fake.urls], ["20261016"])
        self.assertEqual((ligue1.next_live_at - clock()).total_seconds(), 20)

    async def test_failure_keeps_last_data_and_backs_off(self):
        scheduler, clock, fake = make()
        await run_until_idle(scheduler, clock)
        ligue1 = scheduler.competitions["ligue1"]
        body = ligue1.matches.body
        fake.fail.add("fra.1")
        delays = []
        for _ in range(4):
            clock.now = ligue1.next_full_at
            await scheduler.run_job("full", ligue1)
            delays.append((ligue1.next_full_at - clock()).total_seconds())
        self.assertEqual(delays, [30, 60, 120, 240])
        self.assertEqual(ligue1.matches.body, body)  # Last valid data still served
        self.assertIn("503", ligue1.last_error)
        self.assertEqual(scheduler.health()["competitions"]["ligue1"]["lastError"], ligue1.last_error)
        fake.fail.clear()
        clock.now = ligue1.next_full_at
        await scheduler.run_job("full", ligue1)
        self.assertEqual((ligue1.full_failures, (ligue1.next_full_at - clock()).total_seconds()), (0, 1800))

    async def test_unexpected_body_is_a_failure(self):
        scheduler, clock, fake = make()
        fake.broken.add("fra.1")
        await run_until_idle(scheduler, clock)
        ligue1 = scheduler.competitions["ligue1"]
        self.assertIsNone(ligue1.matches)  # Not replaced by an empty list
        self.assertIn("unexpected scoreboard response", ligue1.last_error)
        self.assertEqual(scheduler.health()["status"], "degraded")

    async def test_empty_competition_is_valid(self):
        scheduler, clock, fake = make(competitions=[{"key": "coupeDeFrance", "espnCode": "fra.coupe_de_france", "country": "france"}])
        await scheduler.run_job("full", scheduler.competitions["coupeDeFrance"])
        self.assertEqual(json.loads(scheduler.competitions["coupeDeFrance"].matches.body), {"events": []})

    async def test_end_of_month_fetches_both_months(self):
        scheduler, clock, fake = make(now=datetime(2026, 10, 31, 12, tzinfo=timezone.utc))
        await scheduler.run_job("full", scheduler.competitions["ligue1"])
        self.assertEqual([parse_qs(urlsplit(u).query)["dates"][0] for u in fake.urls], ["202610", "202611"])

    async def test_day_scoreboards(self):
        scheduler, clock, fake = make()
        await run_until_idle(scheduler, clock)
        scores, upcoming = scheduler.boards["scores"], scheduler.boards["upcoming"]
        self.assertTrue(any(u.endswith("/soccer/all/scoreboard?limit=1000") for u in fake.urls))
        self.assertTrue(any(u.endswith("/soccer/all/scoreboard?dates=20261015&limit=1000") for u in fake.urls))
        self.assertEqual((scores.date, upcoming.date), ("2026-10-14", "2026-10-15"))
        self.assertEqual(len(scores.events), 2)  # The two CL matches of Oct 14
        self.assertEqual(scheduler.league_keys(), {"710": "ligue1", "775": "cl"})
        self.assertEqual((scores.next_at - clock()).total_seconds(), 20)  # A configured competition is live
        self.assertEqual((upcoming.next_at - clock()).total_seconds(), 1800)
        self.assertTrue(scheduler.health()["dayScoreboards"]["scores"]["ready"])

    async def test_scores_idle_when_no_configured_match_is_live(self):
        scheduler, clock, fake = make(competitions=[COMPETITIONS[0]])  # Ligue 1 only: the live CL matches don't count
        await run_until_idle(scheduler, clock)
        self.assertEqual((scheduler.boards["scores"].next_at - clock()).total_seconds(), 300)

    async def test_day_scoreboard_failure_keeps_last_day(self):
        scheduler, clock, fake = make()
        await run_until_idle(scheduler, clock)
        scores = scheduler.boards["scores"]
        events = scores.events
        fake.fail.add("all")
        clock.now = scores.next_at
        await scheduler.run_job("scores", scores)
        self.assertIs(scores.events, events)
        self.assertEqual((scores.failures, (scores.next_at - clock()).total_seconds()), (1, 30))
        self.assertIn("503", scores.last_error)

    async def test_upcoming_waits_for_the_current_day(self):
        scheduler, clock, fake = make()
        upcoming = scheduler.boards["upcoming"]
        await scheduler.run_job("upcoming", upcoming)
        self.assertIsNone(upcoming.events)
        self.assertIn("current ESPN day not known yet", upcoming.last_error)

    async def test_standings_cadence_and_colors(self):
        scheduler, clock, fake = make()
        await run_until_idle(scheduler, clock)
        ligue1, cl = scheduler.competitions["ligue1"], scheduler.competitions["cl"]
        self.assertTrue(fake.urls[-2].endswith("/apis/v2/sports/soccer/fra.1/standings"))
        self.assertEqual((ligue1.next_standings_at - clock()).total_seconds(), 1800)
        self.assertEqual((cl.next_standings_at - clock()).total_seconds(), 300)  # CL match in progress
        self.assertEqual(scheduler.team_colors("ligue1")["160"], "#011F68")  # PSG, from /teams
        self.assertTrue(scheduler.health()["competitions"]["ligue1"]["standingsReady"])

    async def test_standings_failure_keeps_last_table(self):
        scheduler, clock, fake = make()
        await run_until_idle(scheduler, clock)
        ligue1 = scheduler.competitions["ligue1"]
        table = ligue1.standings_response
        fake.fail.add("fra.1")
        clock.now = ligue1.next_standings_at
        await scheduler.run_job("standings", ligue1)
        self.assertIs(ligue1.standings_response, table)
        self.assertEqual((ligue1.next_standings_at - clock()).total_seconds(), 30)

    async def test_cup_without_table_is_valid(self):
        scheduler, clock, fake = make(competitions=[{"key": "coupeDeFrance", "espnCode": "fra.coupe_de_france", "country": "france"}])
        await scheduler.run_job("standings", scheduler.competitions["coupeDeFrance"])
        self.assertIsNotNone(scheduler.competitions["coupeDeFrance"].standings_response)
        self.assertIsNone(scheduler.competitions["coupeDeFrance"].last_error)

    async def test_one_request_at_a_time_with_pause(self):
        scheduler, clock, fake = make(request_pause_seconds=1.5)
        url = "https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/scoreboard?dates=202610&limit=1000"
        await asyncio.gather(*[scheduler.request(url) for _ in range(4)])
        self.assertEqual(fake.max_active, 1)
        self.assertEqual(clock.sleeps, [1.5, 1.5, 1.5])


if __name__ == "__main__":
    unittest.main()
