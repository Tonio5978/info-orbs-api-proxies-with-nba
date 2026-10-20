"""Background refresh of the NBA scoreboards (current ESPN day and the next one).

Like the soccer proxy, clients never trigger these ESPN calls: /nbadata/scores, /nbadata/upcoming and the
liveGame part of /nbadata/proxy read the boards kept here.
- scores: every `live_seconds` while a game is live or tips off within `prematch_minutes`; otherwise every
  `idle_seconds`, but always woken up `prematch_minutes` before the next tip-off (today's or tomorrow's board),
  so a game is never missed at its start;
- upcoming: every `idle_seconds`, and right away when the ESPN day rolls over;
- standings: every `idle_seconds`, every `standings_live_seconds` while a game is live.
Failures keep the last valid board and are retried with an increasing delay.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from .sports_common import get_status_type, parse_utc_date

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"
STANDINGS_URL = "https://site.web.api.espn.com/apis/v2/sports/basketball/nba/standings"
LATE_TIPOFF_GRACE = timedelta(hours=3)  # A game still 'pre' this long after its tip-off time is considered not played


@dataclass
class Board:
    """Raw ESPN events of one scoreboard day (or, for 'standings', the raw ESPN standings in `data`)."""
    name: str
    date: Optional[str] = None
    events: Optional[List[dict]] = None
    data: Optional[dict] = None
    updated_at: Optional[datetime] = None
    next_at: Optional[datetime] = None
    failures: int = 0
    last_error: Optional[str] = None
    last_error_at: Optional[datetime] = None


def tipoff(event: dict) -> Optional[datetime]:
    try:
        return parse_utc_date(event.get("date", ""))
    except ValueError:
        return None


def needs_live_refresh(event: dict, now: datetime, prematch_minutes: int) -> bool:
    """Game in progress, about to tip off, or past its tip-off time but not started yet (late start)."""
    state = get_status_type(event).get("state")
    if state == "in":
        return True
    if state != "pre":
        return False
    when = tipoff(event)
    return when is not None and now - LATE_TIPOFF_GRACE <= when <= now + timedelta(minutes=prematch_minutes)


class NbaScoreboardScheduler:
    def __init__(self, fetch_json: Callable[[str], Awaitable[dict]], logger: logging.Logger,
                 live_seconds: int = 10, idle_seconds: int = 30 * 60, prematch_minutes: int = 10,
                 retry_base_seconds: int = 30, standings_live_seconds: int = 5 * 60,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.fetch_json = fetch_json
        self.logger = logger
        self.live_seconds = live_seconds
        self.idle_seconds = idle_seconds
        self.prematch_minutes = prematch_minutes
        self.retry_base_seconds = retry_base_seconds
        self.standings_live_seconds = standings_live_seconds
        self.clock = clock
        self.sleep = sleep
        now = clock()
        self.boards: Dict[str, Board] = {name: Board(name, next_at=now) for name in ("scores", "upcoming", "standings")}
        self.requests_made = 0

    # ── ESPN ───────────────────────────────────────────────────────────────────

    async def fetch_board(self, date: Optional[str] = None) -> dict:
        query = f"dates={date.replace('-', '')}&limit=25" if date else "limit=25"  # An NBA day has at most 15 games
        self.requests_made += 1
        data = await self.fetch_json(f"{BASE_URL}scoreboard?{query}")
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            raise ValueError("unexpected scoreboard response")
        return data

    async def refresh_scores(self):
        """Current ESPN day (rolls over around midday US Eastern time)."""
        board = self.boards["scores"]
        data = await self.fetch_board()
        day = (data.get("day") or {}).get("date") or self.clock().astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        if board.date and day != board.date:
            self.boards["upcoming"].next_at = self.clock()  # New ESPN day: tomorrow changed too
        board.date, board.events, board.updated_at = day, data["events"], self.clock()

    async def refresh_upcoming(self):
        current = self.boards["scores"].date
        if not current:
            raise ValueError("current ESPN day not known yet")
        next_day = (datetime.strptime(current, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        data = await self.fetch_board(next_day)
        board = self.boards["upcoming"]
        board.date, board.events, board.updated_at = next_day, data["events"], self.clock()

    async def refresh_standings(self):
        self.requests_made += 1
        data = await self.fetch_json(STANDINGS_URL)
        if not isinstance(data, dict) or not isinstance(data.get("children"), list):
            raise ValueError("unexpected standings response")
        board = self.boards["standings"]
        board.data, board.updated_at = data, self.clock()

    # ── Scheduling ─────────────────────────────────────────────────────────────

    def is_live(self, now: datetime) -> bool:
        return any(needs_live_refresh(e, now, self.prematch_minutes) for e in self.boards["scores"].events or [])

    def next_scores_time(self, now: datetime) -> datetime:
        if self.is_live(now):
            return now + timedelta(seconds=self.live_seconds)
        # Wake up before the next tip-off, wherever it is (tonight on the current board, or tomorrow's)
        upcoming = [tipoff(e) for name in ("scores", "upcoming") for e in self.boards[name].events or []
                    if get_status_type(e).get("state") == "pre"]
        wake = now + timedelta(seconds=self.idle_seconds)
        for when in upcoming:
            if when and when > now:
                wake = min(wake, when - timedelta(minutes=self.prematch_minutes))
        return max(wake, now + timedelta(seconds=self.live_seconds))

    def next_job(self):
        return min((board.next_at, order, name) for order, (name, board) in enumerate(self.boards.items()))

    async def run_job(self, name: str):
        board = self.boards[name]
        was_live = self.is_live(self.clock())
        try:
            if name == "scores":
                await self.refresh_scores()
                board.next_at = self.next_scores_time(self.clock())
            elif name == "standings":
                await self.refresh_standings()
                now = self.clock()
                board.next_at = now + timedelta(seconds=self.standings_live_seconds if self.is_live(now) else self.idle_seconds)
            else:
                await self.refresh_upcoming()
                board.next_at = self.clock() + timedelta(seconds=self.idle_seconds)
                # Tomorrow's first tip-off may now be the next one to watch
                scores = self.boards["scores"]
                if scores.events is not None:
                    scores.next_at = min(scores.next_at, self.next_scores_time(self.clock()))
            board.failures = 0
        except Exception as e:  # Keep the last valid board, log and retry later
            now = self.clock()
            board.failures += 1
            board.last_error = f"{name}: {type(e).__name__}: {getattr(e, 'detail', e)}"
            board.last_error_at = now
            base = self.live_seconds if (name == "scores" and was_live) else self.retry_base_seconds
            delay = min(base * 2 ** (board.failures - 1), self.idle_seconds)
            board.next_at = now + timedelta(seconds=delay)
            self.logger.error(f"NBA {name} refresh failed ({board.last_error}), retry in {delay:.0f}s")

    async def step(self, max_wait: float = 1.0):
        due, _, name = self.next_job()
        wait = (due - self.clock()).total_seconds()
        if wait > 0:
            await self.sleep(min(wait, max_wait))
            return
        await self.run_job(name)

    async def run(self):
        self.logger.info("NBA scoreboard scheduler started")
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # Never let the scheduler die
                self.logger.error(f"NBA scheduler error: {type(e).__name__}: {e}")
                await self.sleep(5)

    def health(self) -> dict:
        now = self.clock()
        iso = lambda d: d.isoformat() if d else None
        boards = {
            name: {"ready": (b.data if name == "standings" else b.events) is not None, "date": b.date, "games": len(b.events or []),
                   "updatedAt": iso(b.updated_at), "nextRefresh": iso(b.next_at),
                   "lastError": b.last_error, "lastErrorAt": iso(b.last_error_at)}
            for name, b in self.boards.items()
        }
        return {"live": self.is_live(now), "espnRequests": self.requests_made, "scoreboards": boards}
