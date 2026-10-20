"""Background refresh of the soccer data from ESPN.

Clients never trigger ESPN calls: they read the payloads prepared here. Each competition has three jobs:
- full: the whole date window, month by month (ESPN rejects date ranges), every `idle_seconds`;
- live: only the days of matches in progress or about to start, every `live_seconds`;
- teams: the team list, every `teams_seconds`;
- standings: the league table, every `idle_seconds` (every `standings_live_seconds` while a match is live).
Two more jobs fill the day scoreboards of /scores and /upcoming from ESPN's all-competitions scoreboard:
- scores: the current ESPN day, every `live_seconds` while a configured competition is playing, else `scores_seconds`;
- upcoming: the day after, every `upcoming_seconds`.
Jobs run one at a time, with a pause between two ESPN requests. A failed job keeps the last valid
data and is retried with an increasing delay.
"""
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import soccer_espn as espn

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/"
STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/soccer/{code}/standings"


@dataclass
class Settings:
    tz: ZoneInfo
    days_before: int = 1
    days_after: int = 14
    live_seconds: int = 20
    idle_seconds: int = 30 * 60
    prematch_minutes: int = 10
    teams_seconds: int = 24 * 3600
    request_pause_seconds: float = 1.0
    retry_base_seconds: int = 30
    scores_seconds: int = 5 * 60
    standings_live_seconds: int = 5 * 60
    upcoming_seconds: int = 30 * 60


@dataclass
class Payload:
    """Ready-to-serve JSON body with its ETag."""
    body: bytes
    etag: str

    @classmethod
    def of(cls, data) -> "Payload":
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return cls(body, '"' + hashlib.sha1(body).hexdigest() + '"')


@dataclass
class CompetitionState:
    key: str
    espn_code: str
    name: str
    country: Optional[str]
    events: Dict[str, dict] = field(default_factory=dict)
    teams_response: Optional[dict] = None
    matches: Optional[Payload] = None
    teams: Optional[Payload] = None
    matches_updated_at: Optional[datetime] = None
    teams_updated_at: Optional[datetime] = None
    next_full_at: Optional[datetime] = None
    next_live_at: Optional[datetime] = None
    next_teams_at: Optional[datetime] = None
    full_failures: int = 0
    live_failures: int = 0
    teams_failures: int = 0
    last_error: Optional[str] = None
    last_error_at: Optional[datetime] = None
    league_id: Optional[str] = None  # ESPN league id ('775'), used to recognise the competition in day scoreboards
    standings_response: Optional[dict] = None
    standings_updated_at: Optional[datetime] = None
    next_standings_at: Optional[datetime] = None
    standings_failures: int = 0


@dataclass
class DayBoard:
    """Raw ESPN events of one day of the all-competitions scoreboard."""
    name: str
    date: Optional[str] = None
    events: Optional[List[dict]] = None
    updated_at: Optional[datetime] = None
    next_at: Optional[datetime] = None
    failures: int = 0
    last_error: Optional[str] = None
    last_error_at: Optional[datetime] = None


def backoff(failures: int, base: float, cap: float) -> float:
    """base, 2*base, 4*base... capped"""
    return min(base * 2 ** max(failures - 1, 0), cap)


class SoccerScheduler:
    def __init__(self, competitions: List[dict], aliases: Dict[str, dict], settings: Settings,
                 fetch_json: Callable[[str], Awaitable[dict]], logger: logging.Logger,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 monotonic: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.aliases = aliases
        self.fetch_json = fetch_json
        self.logger = logger
        self.clock = clock
        self.sleep = sleep
        self.monotonic = monotonic
        self.competitions: Dict[str, CompetitionState] = {
            c["key"]: CompetitionState(c["key"], c["espnCode"], c.get("name", c["key"]), c.get("country"))
            for c in competitions
        }
        now = clock()
        for state in self.competitions.values():
            state.next_full_at = now
            state.next_teams_at = now
            state.next_standings_at = now
        self.boards: Dict[str, DayBoard] = {name: DayBoard(name, next_at=now) for name in ("scores", "upcoming")}
        self._request_lock = asyncio.Lock()
        self._last_request_end: Optional[float] = None
        self.requests_made = 0

    # ── ESPN access: one request at a time, with a pause between requests ──────

    async def request(self, url: str) -> dict:
        async with self._request_lock:
            if self._last_request_end is not None:
                wait = self.settings.request_pause_seconds - (self.monotonic() - self._last_request_end)
                if wait > 0:
                    await self.sleep(wait)
            try:
                self.requests_made += 1
                return await self.fetch_json(url)
            finally:
                self._last_request_end = self.monotonic()

    async def fetch_scoreboard(self, state: CompetitionState, dates: str) -> List[dict]:
        data = await self.request(f"{BASE_URL}{state.espn_code}/scoreboard?dates={dates}&limit=1000")
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            raise ValueError(f"unexpected scoreboard response for dates={dates}")
        league_id = ((data.get("leagues") or [{}])[0] or {}).get("id")
        if league_id:
            state.league_id = str(league_id)
        return data["events"]

    def league_keys(self) -> Dict[str, str]:
        """ESPN league id -> competition key, from the competition scoreboards (or, failing that, event uids)."""
        keys = {}
        for state in self.competitions.values():
            league_id = state.league_id or next(
                (espn.league_id_from_uid(e.get("uid")) for e in state.events.values() if e.get("uid")), None)
            if league_id:
                keys[league_id] = state.key
        return keys

    async def fetch_all_scoreboard(self, date: Optional[str] = None) -> dict:
        query = f"dates={date.replace('-', '')}&limit=1000" if date else "limit=1000"  # Default limit is 100 matches
        data = await self.request(f"{BASE_URL}all/scoreboard?{query}")
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            raise ValueError("unexpected all-competitions scoreboard response")
        return data

    # ── Jobs ───────────────────────────────────────────────────────────────────

    async def refresh_full(self, state: CompetitionState):
        now = self.clock()
        start, end = espn.window_bounds(now, self.settings.tz, self.settings.days_before, self.settings.days_after)
        events = {}
        for month in espn.months_to_fetch(now, self.settings.tz, self.settings.days_before, self.settings.days_after):
            for event in await self.fetch_scoreboard(state, month):
                if espn.in_window(event, start, end):
                    projected = espn.project_event(event)
                    events[projected["id"]] = projected
        state.events = events  # Replaced only once every month was fetched successfully
        self._matches_updated(state, now)

    async def refresh_live(self, state: CompetitionState):
        now = self.clock()
        start, end = espn.window_bounds(now, self.settings.tz, self.settings.days_before, self.settings.days_after)
        days = []
        for event in state.events.values():
            if espn.needs_live_refresh(event, now, self.settings.prematch_minutes):
                days += [d for d in espn.event_days(event, self.settings.tz) if d not in days]
        updates = {}
        for day in days:
            for event in await self.fetch_scoreboard(state, day):
                if espn.in_window(event, start, end):
                    projected = espn.project_event(event)
                    updates[projected["id"]] = projected
        state.events.update(updates)
        self._matches_updated(state, now)

    async def refresh_teams(self, state: CompetitionState):
        data = await self.request(f"{BASE_URL}{state.espn_code}/teams")
        if not isinstance(data, dict) or not isinstance(data.get("sports"), list):
            raise ValueError("unexpected teams response")
        state.teams_response = data
        state.teams_updated_at = self.clock()
        self.rebuild_teams_payloads()

    async def refresh_standings(self, state: CompetitionState):
        data = await self.request(STANDINGS_URL.format(code=state.espn_code))
        # A knockout cup (Coupe de France) has a valid response without any table ('children')
        if not isinstance(data, dict) or not ("children" in data or "name" in data):
            raise ValueError("unexpected standings response")
        state.standings_response = data
        state.standings_updated_at = self.clock()

    def team_colors(self, key: str) -> Dict[str, str]:
        """'#rrggbb' color of each team of a competition, from its /teams data."""
        state = self.competitions[key]
        return {str(t.get("id")): f"#{t['color'].lstrip('#')}" for t in espn.espn_teams(state.teams_response or {})
                if t.get("color")}

    async def refresh_scores(self, board: DayBoard):
        data = await self.fetch_all_scoreboard()
        day = (data.get("day") or {}).get("date")
        if not day:  # ESPN days follow US Eastern time
            day = self.clock().astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        board.date, board.events, board.updated_at = day, data["events"], self.clock()

    async def refresh_upcoming(self, board: DayBoard):
        current = self.boards["scores"].date
        if not current:
            raise ValueError("current ESPN day not known yet")
        next_day = (datetime.strptime(current, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        data = await self.fetch_all_scoreboard(next_day)
        board.date, board.events, board.updated_at = next_day, data["events"], self.clock()

    def scores_live(self, now: datetime) -> bool:
        """True if a match of a configured competition is live or about to start in the current day."""
        board, keys = self.boards["scores"], self.league_keys()
        return any(espn.needs_live_refresh(e, now, self.settings.prematch_minutes)
                   for e in board.events or [] if espn.league_id_from_uid(e.get("uid")) in keys)

    # ── Payloads and scheduling ────────────────────────────────────────────────

    def _matches_updated(self, state: CompetitionState, now: datetime):
        start, end = espn.window_bounds(now, self.settings.tz, self.settings.days_before, self.settings.days_after)
        state.matches = Payload.of(espn.build_matches_payload(state.events.values(), start, end))
        state.matches_updated_at = now
        state.next_live_at = self.next_live_time(state, now)

    def next_live_time(self, state: CompetitionState, now: datetime) -> Optional[datetime]:
        """In `live_seconds` while a match is live or about to start, else when the next match gets close."""
        if any(espn.needs_live_refresh(e, now, self.settings.prematch_minutes) for e in state.events.values()):
            return now + timedelta(seconds=self.settings.live_seconds)
        kickoffs = [espn.parse_espn_date(e.get("date", "")) for e in state.events.values()
                    if e.get("status", {}).get("type", {}).get("state") == "pre"]
        upcoming = [k for k in kickoffs if k and k > now]
        if not upcoming:
            return None
        return min(upcoming) - timedelta(minutes=self.settings.prematch_minutes)

    def is_live(self, state: CompetitionState, now: datetime) -> bool:
        return any(espn.needs_live_refresh(e, now, self.settings.prematch_minutes) for e in state.events.values())

    def rebuild_teams_payloads(self):
        # Country of each team from the national leagues, used for the European cups
        national = {}
        for state in self.competitions.values():
            if state.country and state.teams_response:
                for team in espn.espn_teams(state.teams_response):
                    national.setdefault(str(team.get("id")), state.country)
        for state in self.competitions.values():
            if state.teams_response:
                state.teams = Payload.of(espn.build_teams_payload(
                    state.teams_response, state.key, state.country, self.aliases, national))

    def next_job(self):
        """(due time, kind, state) of the next job; full refreshes first, then live, then teams."""
        jobs = []
        for order, state in enumerate(self.competitions.values()):
            jobs.append((state.next_full_at, 0, order, "full", state))
            if state.next_live_at:
                jobs.append((state.next_live_at, 1, order, "live", state))
            jobs.append((state.next_teams_at, 2, order, "teams", state))
            jobs.append((state.next_standings_at, 5, order, "standings", state))
        jobs.append((self.boards["scores"].next_at, 3, 0, "scores", self.boards["scores"]))
        jobs.append((self.boards["upcoming"].next_at, 4, 0, "upcoming", self.boards["upcoming"]))
        due, _, _, kind, state = min(jobs, key=lambda j: j[:3])
        return due, kind, state

    async def run_job(self, kind: str, state):
        if kind in ("scores", "upcoming"):
            return await self.run_board_job(kind, state)
        s = self.settings
        try:
            if kind == "full":
                await self.refresh_full(state)
                state.full_failures = 0
                state.next_full_at = self.clock() + timedelta(seconds=s.idle_seconds)
            elif kind == "live":
                await self.refresh_live(state)
                state.live_failures = 0
            elif kind == "standings":
                await self.refresh_standings(state)
                state.standings_failures = 0
                now = self.clock()
                state.next_standings_at = now + timedelta(
                    seconds=s.standings_live_seconds if self.is_live(state, now) else s.idle_seconds)
            else:
                await self.refresh_teams(state)
                state.teams_failures = 0
                state.next_teams_at = self.clock() + timedelta(seconds=s.teams_seconds)
        except Exception as e:  # Keep the last valid data, log and retry later
            now = self.clock()
            state.last_error = f"{kind}: {type(e).__name__}: {getattr(e, 'detail', e)}"
            state.last_error_at = now
            if kind == "full":
                state.full_failures += 1
                delay = backoff(state.full_failures, s.retry_base_seconds, s.idle_seconds)
                state.next_full_at = now + timedelta(seconds=delay)
            elif kind == "live":
                state.live_failures += 1
                delay = backoff(state.live_failures, s.live_seconds, s.idle_seconds)
                state.next_live_at = now + timedelta(seconds=delay)
            elif kind == "standings":
                state.standings_failures += 1
                delay = backoff(state.standings_failures, s.retry_base_seconds, s.idle_seconds)
                state.next_standings_at = now + timedelta(seconds=delay)
            else:
                state.teams_failures += 1
                delay = backoff(state.teams_failures, s.retry_base_seconds, s.teams_seconds)
                state.next_teams_at = now + timedelta(seconds=delay)
            self.logger.error(f"{state.key} {kind} refresh failed ({state.last_error}), retry in {delay:.0f}s")

    async def run_board_job(self, kind: str, board: DayBoard):
        s = self.settings
        try:
            if kind == "scores":
                await self.refresh_scores(board)
                now = self.clock()
                board.next_at = now + timedelta(seconds=s.live_seconds if self.scores_live(now) else s.scores_seconds)
            else:
                await self.refresh_upcoming(board)
                board.next_at = self.clock() + timedelta(seconds=s.upcoming_seconds)
            board.failures = 0
        except Exception as e:  # Keep the last valid day, log and retry later
            now = self.clock()
            board.failures += 1
            board.last_error = f"{kind}: {type(e).__name__}: {getattr(e, 'detail', e)}"
            board.last_error_at = now
            delay = backoff(board.failures, s.retry_base_seconds, s.upcoming_seconds)
            board.next_at = now + timedelta(seconds=delay)
            self.logger.error(f"{kind} refresh failed ({board.last_error}), retry in {delay:.0f}s")

    async def step(self, max_wait: float = 1.0):
        """Run the next job if it's due, otherwise wait (at most `max_wait` seconds)."""
        due, kind, state = self.next_job()
        wait = (due - self.clock()).total_seconds()
        if wait > 0:
            await self.sleep(min(wait, max_wait))
            return
        await self.run_job(kind, state)

    async def run(self):
        self.logger.info(f"Soccer scheduler started for {len(self.competitions)} competitions")
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # Never let the scheduler die
                self.logger.error(f"Soccer scheduler error: {type(e).__name__}: {e}")
                await self.sleep(5)

    # ── Health ─────────────────────────────────────────────────────────────────

    def health(self) -> dict:
        now = self.clock()
        iso = lambda d: d.isoformat() if d else None
        competitions = {}
        for state in self.competitions.values():
            competitions[state.key] = {
                "espnCode": state.espn_code,
                "matchesReady": state.matches is not None,
                "matchesUpdatedAt": iso(state.matches_updated_at),
                "events": len(json.loads(state.matches.body)["events"]) if state.matches else 0,
                "live": self.is_live(state, now),
                "nextMatchesRefresh": iso(min(d for d in (state.next_full_at, state.next_live_at) if d)),
                "teamsReady": state.teams is not None,
                "teamsUpdatedAt": iso(state.teams_updated_at),
                "standingsReady": state.standings_response is not None,
                "standingsUpdatedAt": iso(state.standings_updated_at),
                "lastError": state.last_error,
                "lastErrorAt": iso(state.last_error_at),
            }
        boards = {
            name: {
                "ready": board.events is not None,
                "date": board.date,
                "events": len(board.events or []),
                "updatedAt": iso(board.updated_at),
                "nextRefresh": iso(board.next_at),
                "lastError": board.last_error,
                "lastErrorAt": iso(board.last_error_at),
            }
            for name, board in self.boards.items()
        }
        ready = all(c["matchesReady"] and c["teamsReady"] and c["standingsReady"] for c in competitions.values()) and all(
            b["ready"] for b in boards.values())
        started = any(c["matchesReady"] for c in competitions.values())
        return {
            "status": "ok" if ready else ("degraded" if started else "starting"),
            "espnRequests": self.requests_made,
            "competitions": competitions,
            "dayScoreboards": boards,
        }
