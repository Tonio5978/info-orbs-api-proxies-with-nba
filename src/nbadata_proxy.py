import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from .common import (setup_logger, create_app, fetch_data, check_force_refresh, is_force_requested, handle_request,
                     TTLCache, make_cache_key, get_or_fetch, gather_or_raise, proxy_info)
from .sports_common import (load_teams, build_team_lookup, resolve_team, parse_colors, format_ordinal, get_display_tz,
                            parse_utc_date, format_game_date, get_day_of_week, format_game_time, get_status_type,
                            find_last_game, find_next_game, split_competitors, get_score_value, get_primary_logo,
                            get_broadcast_name)
from .nba_scheduler import NbaScoreboardScheduler, needs_live_refresh

logger = setup_logger("NBADATA")
BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"

# Team data cache of /proxy (team details, standings, schedule); the live game is not cached here
CACHE = TTLCache.from_env_minutes("NBADATA_PROXY_CACHE_LIFE")
# Shorter lifetime while the team is playing, so lastGame/standings update soon after the final buzzer
LIVE_CACHE_SECONDS = int(os.getenv("NBADATA_PROXY_LIVE_CACHE_SECONDS", "60"))


async def fetch_espn(url: str) -> dict:
    return await fetch_data(url, logger, app_name="nbadata")


# Background refresh of the scoreboards (/scores, /upcoming, live games): 10 s during games, 30 min otherwise
scheduler = NbaScoreboardScheduler(
    fetch_espn, logger,
    live_seconds=int(os.getenv("NBADATA_PROXY_LIVE_REFRESH_SECONDS", "10")),
    idle_seconds=int(os.getenv("NBADATA_PROXY_IDLE_REFRESH_MINUTES", "30")) * 60,
    prematch_minutes=int(os.getenv("NBADATA_PROXY_PREMATCH_MINUTES", "10")),
    standings_live_seconds=int(os.getenv("NBADATA_PROXY_STANDINGS_LIVE_REFRESH_MINUTES", "5")) * 60,
)

# Load team data
TEAMS_DATA = load_teams(Path(__file__).parent / "nba_teams.json", logger)
TEAMS_BY_ID = {team["id"]: team for team in TEAMS_DATA}
TEAM_LOOKUP = build_team_lookup(TEAMS_DATA)

app = create_app("nbadata_proxy", default_requests_per_minute=15, banner_title="NBA Data Service Configuration", banner_lines=lambda: [
    f"Teams loaded: {len(TEAMS_DATA)}",
    f"Team data cache: {CACHE.ttl_seconds // 60} minutes ({'enabled' if CACHE.enabled else 'disabled'}), "
    f"{LIVE_CACHE_SECONDS}s while the team is playing",
    f"Scoreboards: every {scheduler.live_seconds}s during games (from {scheduler.prematch_minutes} min before tip-off), "
    f"{scheduler.idle_seconds // 60} min otherwise",
], background=scheduler.run, health_details=scheduler.health)

LOGO_DIR = Path("/app/nba_logos")
app.mount("/nbadata/logo", StaticFiles(directory=LOGO_DIR, check_dir=False), name="nba_logos")


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


def get_scoreboard() -> list:
    """Current ESPN day's events, kept up to date by the scheduler (empty until the first load)."""
    return scheduler.boards["scores"].events or []


def team_is_playing(team_id: str) -> bool:
    now = datetime.now(timezone.utc)
    return any(needs_live_refresh(e, now, scheduler.prematch_minutes)
               and any(c.get("team", {}).get("id") == team_id
                       for c in (e.get("competitions") or [{}])[0].get("competitors", []))
               for e in get_scoreboard())


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
        team_data, games = await gather_or_raise(get_team_details(team_id), get_schedule(team_id, season))
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
        }

    def ttl_for(result: dict):
        if team_is_playing(team_id):
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
    # Live game from the scoreboard refreshed in the background (every few seconds during games)
    return transform_data({**result, "liveGame": build_live_game(get_scoreboard(), team_id)}, cached=cached)


def format_scoreboard_game(event: dict, tz) -> dict:
    """Game summary: ESPN status ('pre', 'in', 'post'), start time and both teams' scores and colors.

    Live games ('in') add the current period, the clock (time remaining in the period) and the last play.
    """
    competitors = (event.get("competitions") or [{}])[0].get("competitors", [])
    side = {c.get("homeAway"): c for c in competitors}
    status = event.get("status") or (event.get("competitions") or [{}])[0].get("status") or {}
    state = status.get("type", {}).get("state", "N/A")
    is_live = state == "in"

    def team(c: dict) -> dict:
        info = c.get("team", {})
        return {
            "abbreviation": info.get("abbreviation", "N/A"),
            "score": get_score_value(c),
            "color": f"#{info['color']}" if info.get("color") else None,  # ESPN gives hex without '#'
        }

    game = {"gameId": event.get("id", "N/A"), "status": state}
    if is_live:
        game["period"] = format_period(status.get("period", 0))
        game["clock"] = status.get("displayClock", "")
        game["detail"] = status.get("type", {}).get("shortDetail", "")  # e.g. 'Halftime', 'End of 3rd Quarter'
        situation = (event.get("competitions") or [{}])[0].get("situation") or {}
        game["lastPlay"] = situation.get("lastPlay", {}).get("text")  # e.g. 'Jayson Tatum defensive rebound'
    game["startTime"] = format_game_time(event.get("date", ""), tz)
    game["home"] = team(side.get("home", {}))
    game["away"] = team(side.get("away", {}))
    return game


def build_scoreboard_response(board, request: Request):
    """Games of a scoreboard day, optionally filtered with ?team=LAL, start times in ?tz= (default ET)"""
    if board.events is None:
        # Not loaded yet since startup: never serve an empty day in place of real data
        return JSONResponse({"error": "warming_up", "message": f"{board.name} scoreboard not loaded yet, retry shortly"},
                            status_code=503, headers={"Retry-After": "5"})
    tz = get_display_tz(request)
    events = sorted(board.events, key=lambda e: (e.get("date", ""), e.get("id", "")))
    team_name = request.query_params.get("team")
    if team_name:
        team_id = resolve_team(TEAM_LOOKUP, team_name)
        events = [e for e in events
                  if any(c.get("team", {}).get("id") == team_id
                         for c in (e.get("competitions") or [{}])[0].get("competitors", []))]
    games = [format_scoreboard_game(e, tz) for e in events]
    return {"date": board.date, "count": len(games), "games": games, "proxy-info": proxy_info(True)}


async def scores_endpoint(request: Request):
    """All games of the current ESPN scoreboard day: finished, live and upcoming (no ESPN call, see nba_scheduler)."""
    return build_scoreboard_response(scheduler.boards["scores"], request)


async def upcoming_endpoint(request: Request):
    """Games of the day after the current ESPN scoreboard day."""
    return build_scoreboard_response(scheduler.boards["upcoming"], request)


def standing_stats(entry: dict) -> dict:
    return {s.get("name"): s for s in entry.get("stats", [])}


def build_standings_response(data: dict, conference: Optional[str] = None, team_id: Optional[str] = None) -> dict:
    """Conference standings from ESPN, teams sorted by playoff seed (ESPN doesn't sort them)."""
    conferences = []
    for child in data.get("children", []):
        if conference and conference.lower() not in {str(child.get("abbreviation", "")).lower(), str(child.get("name", "")).lower()}:
            continue
        teams = []
        for entry in child.get("standings", {}).get("entries", []):
            info = entry.get("team", {})
            stats = standing_stats(entry)
            value = lambda name: stats.get(name, {}).get("value")
            text = lambda name: stats.get(name, {}).get("displayValue")
            local = TEAMS_BY_ID.get(str(info.get("id")), {})
            colors = parse_colors(local.get("colors", ""))
            teams.append({
                "rank": int(value("playoffSeed") or 0),
                "teamId": info.get("id"),
                "abbreviation": info.get("abbreviation"),
                "name": info.get("displayName"),
                "color": colors[0]["code"] if colors else None,  # ESPN standings have no colors: nba_teams.json
                "wins": int(value("wins") or 0),
                "losses": int(value("losses") or 0),
                "winPercent": text("winPercent"),
                "gamesBehind": text("gamesBehind"),
                "streak": text("streak"),
                "home": text("Home"),
                "road": text("Road"),
                "vsConference": text("vs. Conf."),
                "pointsFor": text("avgPointsFor"),
                "pointsAgainst": text("avgPointsAgainst"),
            })
        # Seed first; ties (before the season starts every seed is 1) by win percentage, then name
        teams.sort(key=lambda t: (t["rank"] or 99, -(t["wins"] / max(t["wins"] + t["losses"], 1)), t["name"] or ""))
        if team_id:
            teams = [t for t in teams if str(t["teamId"]) == team_id]
        conferences.append({"name": child.get("name"), "abbreviation": child.get("abbreviation"), "teams": teams})
    season = (data.get("season") or {}).get("year")
    return {"season": str(season) if season else None, "conferences": conferences, "proxy-info": proxy_info(True)}


async def standings_endpoint(request: Request):
    """Conference standings, refreshed in the background; ?conference=east|west, ?team=LAL"""
    board = scheduler.boards["standings"]
    if board.data is None:
        return JSONResponse({"error": "warming_up", "message": "standings not loaded yet, retry shortly"},
                            status_code=503, headers={"Retry-After": "5"})
    team = request.query_params.get("team")
    return build_standings_response(board.data, request.query_params.get("conference"),
                                    resolve_team(TEAM_LOOKUP, team) if team else None)


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
handle_request(app, logger, standings_endpoint, methods=("GET",), path="/standings")
