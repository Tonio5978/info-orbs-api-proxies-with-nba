"""Soccer scores proxy (ESPN), same API as epl-vps.duckdns.org, for Node-RED / AWTRIX.

For each competition of config/competitions.json:
  GET /{key}UpcomingMatches  matches from J-1 to J+14 (ESPN scoreboard fields, reduced)
  GET /{key}Teams            teams with aliases, competitionKey and country
Data comes from a background scheduler (soccer_scheduler.py): requests only read memory.
"""
import json
import os
import re
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from fastapi import Request, Response

from .common import setup_logger, create_app, fetch_data, handle_request
from .soccer_scheduler import Payload, Settings, SoccerScheduler

logger = setup_logger("SOCCER")

CONFIG_DIR = Path(os.getenv("SOCCER_PROXY_CONFIG_DIR", str(Path(__file__).parent.parent / "config")))
KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def load_settings() -> Settings:
    return Settings(
        tz=ZoneInfo(os.getenv("SOCCER_PROXY_TZ", "Europe/Paris")),
        days_before=env_int("SOCCER_PROXY_DAYS_BEFORE", 1),
        days_after=env_int("SOCCER_PROXY_DAYS_AFTER", 14),
        live_seconds=env_int("SOCCER_PROXY_LIVE_REFRESH_SECONDS", 20),
        idle_seconds=env_int("SOCCER_PROXY_IDLE_REFRESH_MINUTES", 30) * 60,
        prematch_minutes=env_int("SOCCER_PROXY_PREMATCH_MINUTES", 10),
        teams_seconds=env_int("SOCCER_PROXY_TEAMS_REFRESH_HOURS", 24) * 3600,
        request_pause_seconds=env_int("SOCCER_PROXY_REQUEST_PAUSE_MS", 1000) / 1000,
        retry_base_seconds=env_int("SOCCER_PROXY_RETRY_BASE_SECONDS", 30),
    )


def load_competitions(path: Path) -> list:
    competitions = json.loads(path.read_text(encoding="utf-8"))
    keys = set()
    for c in competitions:
        if not KEY_PATTERN.match(c.get("key", "")) or not c.get("espnCode"):
            raise RuntimeError(f"Invalid competition in {path}: {c}")
        if c["key"] in keys:
            raise RuntimeError(f"Duplicate competition key in {path}: {c['key']}")
        keys.add(c["key"])
    return competitions


def load_aliases(path: Path) -> dict:
    if not path.exists():
        logger.warning(f"{path} not found, teams will only get automatic aliases")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


SETTINGS = load_settings()
COMPETITIONS = load_competitions(CONFIG_DIR / "competitions.json")
ALIASES = load_aliases(CONFIG_DIR / "aliases.json")


async def fetch_espn(url: str) -> dict:
    return await fetch_data(url, logger, timeout=20, app_name="soccer")


scheduler = SoccerScheduler(COMPETITIONS, ALIASES, SETTINGS, fetch_espn, logger)

app = create_app(
    "soccer_proxy", default_requests_per_minute=60, banner_title="Soccer Service Configuration",
    banner_lines=lambda: [
        f"Competitions: {', '.join(c['key'] + '=' + c['espnCode'] for c in COMPETITIONS)}",
        f"Window: J-{SETTINGS.days_before} to J+{SETTINGS.days_after} ({SETTINGS.tz.key})",
        f"Refresh: {SETTINGS.live_seconds}s when live (or kickoff within {SETTINGS.prematch_minutes} min), "
        f"{SETTINGS.idle_seconds // 60} min otherwise, teams every {SETTINGS.teams_seconds // 3600} h",
        f"ESPN requests: one at a time, {SETTINGS.request_pause_seconds:g}s pause",
    ],
    background=scheduler.run,
    health_details=scheduler.health,
)

COMMON_HEADERS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=10"}


def json_response(request: Request, payload: Optional[Payload], what: str) -> Response:
    if payload is None:
        # Nothing fetched yet since startup: never serve an empty list in place of real data
        body = json.dumps({"error": "warming_up", "message": f"{what} not loaded yet, retry shortly"}).encode()
        return Response(body, status_code=503, media_type="application/json",
                        headers={**COMMON_HEADERS, "Cache-Control": "no-store", "Retry-After": "10"})
    headers = {**COMMON_HEADERS, "ETag": payload.etag}
    if payload.etag in [tag.strip() for tag in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(payload.body, media_type="application/json", headers=headers)


def matches_endpoint(key: str) -> Callable:
    async def endpoint(request: Request):
        return json_response(request, scheduler.competitions[key].matches, f"{key} matches")
    return endpoint


def teams_endpoint(key: str) -> Callable:
    async def endpoint(request: Request):
        return json_response(request, scheduler.competitions[key].teams, f"{key} teams")
    return endpoint


for competition in COMPETITIONS:
    handle_request(app, logger, matches_endpoint(competition["key"]), methods=("GET",),
                   path=f"/{competition['key']}UpcomingMatches")
    handle_request(app, logger, teams_endpoint(competition["key"]), methods=("GET",),
                   path=f"/{competition['key']}Teams")
