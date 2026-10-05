import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple
from zoneinfo import ZoneInfo
from fastapi import HTTPException, Request
from fastapi.staticfiles import StaticFiles
from .common import (setup_logger, create_app, fetch_data, check_force_refresh, is_force_requested, handle_request,
                     TTLCache, make_cache_key, get_or_fetch, gather_or_raise, proxy_info)
from .sports_common import (DEFAULT_TZ, load_teams, build_team_lookup, resolve_team, parse_colors, format_ordinal, get_display_tz,
                            parse_utc_date, format_game_date, get_day_of_week, format_game_time, get_status_type,
                            find_last_game, find_next_game, split_competitors, get_score_value, get_primary_logo,
                            get_broadcast_name)

logger = setup_logger("NBADATA")
BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"

# Cache configuration
CACHE = TTLCache.from_env_minutes("NBADATA_PROXY_CACHE_LIFE")
# Shorter lifetime while the team is playing (or about to), so live scores stay fresh
LIVE_CACHE_SECONDS = int(os.getenv("NBADATA_PROXY_LIVE_CACHE_SECONDS", "60"))
# The scoreboard is the same for every team: fetch it once and share it
SCOREBOARD_CACHE = TTLCache(int(os.getenv("NBADATA_PROXY_SCOREBOARD_CACHE_SECONDS", "30")))
# Next day's games (/upcoming) change rarely
UPCOMING_CACHE = TTLCache.from_env_minutes("NBADATA_PROXY_UPCOMING_CACHE_LIFE", "15")

# Load team data
TEAMS_DATA = load_teams(Path(__file__).parent / "nba_teams.json", logger)
TEAMS_BY_ID = {team["id"]: team for team in TEAMS_DATA}
TEAM_LOOKUP = build_team_lookup(TEAMS_DATA)

app = create_app("nbadata_proxy", default_requests_per_minute=15, banner_title="NBA Data Service Configuration", banner_lines=lambda: [
    f"Teams loaded: {len(TEAMS_DATA)}",
    f"Cache lifetime: {CACHE.ttl_seconds // 60} minutes ({'enabled' if CACHE.enabled else 'disabled'}), "
    f"{LIVE_CACHE_SECONDS}s during live games",
    f"Scoreboard cache (/scores): {SCOREBOARD_CACHE.ttl_seconds}s, /upcoming cache: {UPCOMING_CACHE.ttl_seconds // 60} minutes",
])

LOGO_DIR = Path("/app/nba_logos")
app.mount("/nbadata/logo", StaticFiles(directory=LOGO_DIR), name="nba_logos")


def get_current_season() -> str:
    """NBA season spans two years (Oct-Jun). Return the end year (e.g. '2025' for 2024-25)."""
    today = datetime.now()
    return str(today.year + 1 if today.month >= 10 else today.year)


def format_period(period: int) -> str:
    """Q1-Q4 for regulation, then OT, 2OT, 3OT..."""
    if period <= 4:
        return f"Q{period}"
    overtime = period - 4
    return "OT" if overtime == 1 else f"{overtime}OT"


def get_geo_broadcast(comp: dict) -> str:
    broadcasts = comp.get("geoBroadcasts") or [{}]
    return broadcasts[0].get("media", {}).get("shortName", "N/A")


def transform_data(data: dict, cached: bool = False) -> dict:
    if not data:
        raise HTTPException(status_code=502, detail="Empty API response")
    return {
        "teamId": data.get("teamId", "N/A"),
        "season": data.get("season", "N/A"),
        "team": data.get("team", {}),
        "standings": data.get("standings", {}),
        "lastGame": data.get("lastGame", {}),
        "nextGame": data.get("nextGame", {}),
        "liveGame": data.get("liveGame", {}),
        "proxy-info": proxy_info(cached)
    }


async def get_team_details(team_id: str) -> dict:
    team_data = await fetch_data(f"{BASE_URL}teams/{team_id}", logger, app_name="nbadata")
    if not team_data or "team" not in team_data:
        raise HTTPException(status_code=502, detail="Failed to fetch team data")
    return team_data


async def get_schedule(team_id: str, season: str) -> list:
    schedule_data = await fetch_data(
        f"{BASE_URL}teams/{team_id}/schedule?season={season}",
        logger, app_name="nbadata"
    )
    if not schedule_data or "events" not in schedule_data:
        raise HTTPException(status_code=502, detail="Failed to fetch schedule data")
    return schedule_data["events"]


async def fetch_scoreboard(date: Optional[str] = None) -> dict:
    """ESPN scoreboard as {'date': 'YYYY-MM-DD', 'events': [...]}.

    Without a date, ESPN returns its current scoreboard day, which only rolls over
    around midday US Eastern time (so in the morning it still shows last night's games).
    """
    query = f"dates={date.replace('-', '')}&limit=25" if date else "limit=25"  # An NBA day has at most 15 games
    scoreboard = await fetch_data(f"{BASE_URL}scoreboard?{query}", logger, app_name="nbadata") or {}
    day = (scoreboard.get("day") or {}).get("date") or date
    if not day:
        day = datetime.now(ZoneInfo(DEFAULT_TZ)).strftime("%Y-%m-%d")
    return {"date": day, "events": scoreboard.get("events", [])}


async def get_current_scoreboard(force: bool = False) -> Tuple[dict, bool]:
    """Current ESPN scoreboard day, shared by /proxy (live game) and /scores."""
    return await get_or_fetch(SCOREBOARD_CACHE, "scoreboard", fetch_scoreboard, logger, force=force)


async def get_scoreboard(force: bool = False) -> list:
    """Today's NBA scoreboard events to detect live games. Never fails the request."""
    try:
        scoreboard, _ = await get_current_scoreboard(force)
        return scoreboard["events"]
    except HTTPException:
        logger.warning("Scoreboard unavailable, live game data skipped")
        return []


def build_team(team_id: str, team_data: dict) -> dict:
    team_info = team_data.get("team", {})
    team = TEAMS_BY_ID[team_id]
    return {
        "fullName": team_info.get("displayName", "Unknown Team"),
        "shortName": team_info.get("nickname", team_info.get("shortDisplayName", "")),
        "colors": parse_colors(team.get("colors", "")),
        "logoUrl": get_primary_logo(team_info).get("href", ""),
        "logoImageFileName": team.get("logoImageFileName", ""),
        "logoBackgroundColor": team.get("logoBackgroundColor", ""),
        "abbreviation": team_info.get("abbreviation", ""),
        "standingSummary": team_data.get("standingSummary", "N/A"),
        "conference": team.get("conference", "N/A"),
        "division": team.get("division", "N/A")
    }


def build_standings(team_id: str, team_data: dict) -> dict:
    record_items = team_data.get("team", {}).get("record", {}).get("items", [])
    records = {}
    for item in record_items:
        records.setdefault(item.get("type"), item)
    total_record = records.get("total")
    if not total_record:
        return {}
    team = TEAMS_BY_ID[team_id]
    stats = {stat["name"]: stat["value"] for stat in total_record.get("stats", [])}
    return {
        "conference": team.get("conference", "N/A"),
        "conferenceRank": format_ordinal(stats.get("playoffSeed", 0)),
        "division": team.get("division", "N/A"),
        "divisionRank": format_ordinal(stats.get("divisionStandings", 0)),
        "wins": int(stats.get("wins", 0)),
        "losses": int(stats.get("losses", 0)),
        "winningPercentage": round(float(stats.get("winPercent", 0)), 3),
        "gamesBehind": stats.get("gamesBehind", "N/A"),
        "homeRecord": records.get("home", {}).get("summary", "N/A"),  # → "19-15"
        "awayRecord": records.get("road", {}).get("summary", "N/A"),  # → "15-23"
        "lastTen": stats.get("Last10", "N/A"),
        "streak": stats.get("streak", "N/A"),
        "pointsFor": stats.get("pointsFor", "N/A"),
        "pointsAgainst": stats.get("pointsAgainst", "N/A"),
        "record": total_record.get("summary", "N/A")
    }


def build_last_game(event: dict, team_id: str, tz) -> dict:
    sides = split_competitors(event, team_id)
    if not sides:
        return {}
    _, is_home, me, opp = sides
    opponent = opp.get("team", {})
    return {
        "date": format_game_date(event["date"], tz),
        "day": get_day_of_week(event["date"], tz),
        "opponent": opponent.get("abbreviation", opponent.get("shortDisplayName", "N/A")),
        "opponentFullName": opponent.get("displayName", "N/A"),
        "location": "Home" if is_home else "Away",
        "score": f"{get_score_value(me)}-{get_score_value(opp)}",
        "result": "Won" if me.get("winner") else "Lost",
        "gameTime": format_game_time(event["date"], tz),
        "gameId": event.get("id", "N/A")
    }


def build_live_game(scoreboard_events: list, team_id: str) -> dict:
    live_event = next(
        (e for e in scoreboard_events
         if get_status_type(e).get("state") == "in"
         and any(c.get("team", {}).get("id") == team_id
                 for comp in e.get("competitions", [])
                 for c in comp.get("competitors", []))),
        None
    )
    sides = split_competitors(live_event, team_id)
    if not sides:
        return {}
    comp, is_home, me, opp = sides
    opponent = opp.get("team", {})
    status = live_event.get("status", {})
    my_score, opp_score = get_score_value(me), get_score_value(opp)
    return {
        "isLive": True,
        "quarter": format_period(status.get("period", 0)),
        "clock": status.get("displayClock", ""),
        "location": "Home" if is_home else "Away",
        "opponent": opponent.get("abbreviation", opponent.get("shortDisplayName", "N/A")),
        "opponentFullName": opponent.get("displayName", "N/A"),
        "myScore": my_score,
        "opponentScore": opp_score,
        "score": f"{my_score}-{opp_score}",
        "gameId": live_event.get("id", "N/A"),
        "tvBroadcast": get_geo_broadcast(comp)
    }


def build_next_game(event: dict, team_id: str, tz) -> dict:
    sides = split_competitors(event, team_id)
    if not sides:
        return {}
    comp, is_home, _, opp = sides
    opponent = opp.get("team", {})
    return {
        "date": format_game_date(event["date"], tz),
        "day": get_day_of_week(event["date"], tz),
        "opponent": opponent.get("abbreviation", opponent.get("shortDisplayName", "N/A")),
        "opponentFullName": opponent.get("displayName", "N/A"),
        "location": "Home" if is_home else "Away",
        "gameTime": format_game_time(event["date"], tz),
        "tvBroadcast": get_broadcast_name(comp),
        "gameId": event.get("id", "N/A")
    }


async def proxy_endpoint(request: Request):
    team_identifier = request.query_params.get("teamName")
    if not team_identifier:
        raise HTTPException(status_code=400, detail="teamName parameter is required")

    team_id = resolve_team(TEAM_LOOKUP, team_identifier)
    logger.info(f"Resolved '{team_identifier}' to team ID: {team_id}")

    tz = get_display_tz(request)
    season = get_current_season()
    force_refresh = check_force_refresh(request, is_force_requested(request))
    cache_key = make_cache_key({"teamId": team_id, "season": season, "tz": tz.key})
    next_game_start = None

    async def build() -> dict:
        nonlocal next_game_start
        logger.info(f"Fetching live data for team {team_id}{' (forced refresh)' if force_refresh else ''}")
        team_data, games, scoreboard_events = await gather_or_raise(
            get_team_details(team_id), get_schedule(team_id, season), get_scoreboard(force_refresh)
        )
        now = datetime.now(timezone.utc)
        next_game = find_next_game(games, now)
        if next_game:
            next_game_start = parse_utc_date(next_game["date"])
        return {
            "teamId": team_id,
            "season": season,
            "team": build_team(team_id, team_data),
            "standings": build_standings(team_id, team_data),
            "lastGame": build_last_game(find_last_game(games, now), team_id, tz),
            "nextGame": build_next_game(next_game, team_id, tz),
            "liveGame": build_live_game(scoreboard_events, team_id)
        }

    def ttl_for(result: dict):
        if result.get("liveGame"):
            return LIVE_CACHE_SECONDS
        if next_game_start:
            # Expire when the next game tips off, so the live game shows up promptly
            seconds_to_start = (next_game_start - datetime.now(timezone.utc)).total_seconds()
            if 0 < seconds_to_start < CACHE.ttl_seconds:
                return max(seconds_to_start, LIVE_CACHE_SECONDS)
        return None

    result, cached = await get_or_fetch(CACHE, cache_key, build, logger, force=force_refresh, ttl_for=ttl_for)
    if cached:
        logger.info(f"Returning cached data for team {team_id}")
    return transform_data(result, cached=cached)


def format_scoreboard_game(event: dict, tz) -> dict:
    """Minimal game summary: ESPN status ('pre', 'in', 'post'), start time and both teams' scores."""
    competitors = (event.get("competitions") or [{}])[0].get("competitors", [])
    side = {c.get("homeAway"): c for c in competitors}

    def team(c: dict) -> dict:
        return {"abbreviation": c.get("team", {}).get("abbreviation", "N/A"), "score": get_score_value(c)}

    return {
        "gameId": event.get("id", "N/A"),
        "status": get_status_type(event).get("state", "N/A"),
        "startTime": format_game_time(event.get("date", ""), tz),
        "home": team(side.get("home", {})),
        "away": team(side.get("away", {})),
    }


def build_scoreboard_response(scoreboard: dict, request: Request, cached: bool) -> dict:
    """Games of a scoreboard day, optionally filtered with ?team=LAL, start times in ?tz= (default ET)"""
    tz = get_display_tz(request)
    events = sorted(scoreboard["events"], key=lambda e: (e.get("date", ""), e.get("id", "")))
    team_name = request.query_params.get("team")
    if team_name:
        team_id = resolve_team(TEAM_LOOKUP, team_name)
        events = [e for e in events
                  if any(c.get("team", {}).get("id") == team_id
                         for c in (e.get("competitions") or [{}])[0].get("competitors", []))]
    games = [format_scoreboard_game(e, tz) for e in events]
    return {"date": scoreboard["date"], "count": len(games), "games": games, "proxy-info": proxy_info(cached)}


async def scores_endpoint(request: Request):
    """All games of the current ESPN scoreboard day: finished, live and upcoming."""
    force_refresh = check_force_refresh(request, is_force_requested(request))
    scoreboard, cached = await get_current_scoreboard(force_refresh)
    return build_scoreboard_response(scoreboard, request, cached)


async def upcoming_endpoint(request: Request):
    """Games of the day after the current ESPN scoreboard day."""
    force_refresh = check_force_refresh(request, is_force_requested(request))
    current, _ = await get_current_scoreboard()
    next_day = (datetime.strptime(current["date"], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    scoreboard, cached = await get_or_fetch(
        UPCOMING_CACHE, next_day, lambda: fetch_scoreboard(next_day), logger, force=force_refresh
    )
    return build_scoreboard_response(scoreboard, request, cached)


@app.get("/debug/teams")
async def debug_teams():
    return {
        "teams": [
            {
                "id": team["id"],
                "name": team["name"],
                "aliases": team["aliases"],
                "conference": team.get("conference", "N/A"),
                "division": team.get("division", "N/A")
            }
            for team in TEAMS_DATA
        ]
    }


handle_request(app, logger, proxy_endpoint, methods=("GET",))
handle_request(app, logger, scores_endpoint, methods=("GET",), path="/scores")
handle_request(app, logger, upcoming_endpoint, methods=("GET",), path="/upcoming")
