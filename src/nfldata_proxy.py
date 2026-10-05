from datetime import datetime, timezone
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.staticfiles import StaticFiles
from .common import (setup_logger, create_app, fetch_data, check_force_refresh, is_force_requested, handle_request,
                     TTLCache, make_cache_key, get_or_fetch, gather_or_raise, proxy_info)
from .sports_common import (load_teams, build_team_lookup, resolve_team, parse_colors, format_ordinal, get_display_tz,
                            format_game_date, get_day_of_week, format_game_time, find_last_game, find_next_game,
                            split_competitors, get_score_value, get_primary_logo, get_broadcast_name)

logger = setup_logger("NFLDATA")
BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"

# Cache configuration
CACHE = TTLCache.from_env_minutes("NFLDATA_PROXY_CACHE_LIFE")

# Load team data
TEAMS_DATA = load_teams(Path(__file__).parent / "nfl_teams.json", logger)
TEAMS_BY_ID = {team["id"]: team for team in TEAMS_DATA}
TEAM_LOOKUP = build_team_lookup(TEAMS_DATA)

app = create_app("nfldata_proxy", default_requests_per_minute=15, banner_title="NFL Data Service Configuration", banner_lines=lambda: [
    f"Teams loaded: {len(TEAMS_DATA)}",
    f"Cache lifetime: {CACHE.ttl_seconds // 60} minutes ({'enabled' if CACHE.enabled else 'disabled'})",
])

LOGO_DIR = Path("/app/nfl_logos")
app.mount("/nfldata/logo", StaticFiles(directory=LOGO_DIR), name="nfl_logos")


def get_current_season() -> str:
    """NFL season starts in September and is named after its start year."""
    today = datetime.now()
    return str(today.year if today.month >= 9 else today.year - 1)


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
        "proxy-info": proxy_info(cached)
    }


async def get_team_details(team_id: str) -> dict:
    team_data = await fetch_data(f"{BASE_URL}teams/{team_id}", logger, app_name="nfldata")
    if not team_data or "team" not in team_data:
        raise HTTPException(status_code=502, detail="Failed to fetch team data")
    return team_data


async def get_schedule(team_id: str, season: str) -> list:
    schedule_data = await fetch_data(f"{BASE_URL}teams/{team_id}/schedule?season={season}", logger, app_name="nfldata")
    if not schedule_data or "events" not in schedule_data:
        raise HTTPException(status_code=502, detail="Failed to fetch schedule data")
    return schedule_data["events"]


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
    total_record = next((item for item in record_items if item.get("type") == "total"), {})
    if not total_record:
        return {}
    team = TEAMS_BY_ID[team_id]
    stats = {stat["name"]: stat["value"] for stat in total_record.get("stats", [])}
    return {
        "conference": team.get("conference", "N/A"),
        "conferenceRank": format_ordinal(stats.get("playoffSeed", "N/A")),
        "division": team.get("division", "N/A"),
        "divisionRank": format_ordinal(stats.get("divisionRank", "N/A")),
        "winningPercentage": stats.get("winPercent", "N/A"),
        "pointsFor": stats.get("pointsFor", "N/A"),
        "pointsAgainst": stats.get("pointsAgainst", "N/A"),
        "record": total_record.get("summary", "N/A")
    }


def build_last_game(event: dict, team_id: str, tz) -> dict:
    sides = split_competitors(event, team_id)
    if not sides:
        return {}
    _, _, me, opp = sides
    opponent = opp.get("team", {})
    return {
        "date": format_game_date(event["date"], tz),
        "day": get_day_of_week(event["date"], tz),
        "opponent": opponent.get("nickname", opponent.get("shortDisplayName", "N/A")),
        "score": f"{get_score_value(me)}-{get_score_value(opp)}",
        "result": "Won" if me.get("winner") else "Lost",
        "gameTime": format_game_time(event["date"], tz),
        "gameId": event.get("id", "N/A")
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
        "opponent": opponent.get("nickname", opponent.get("shortDisplayName", "N/A")),
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

    async def build() -> dict:
        logger.info(f"Fetching live data for team {team_id}{' (forced refresh)' if force_refresh else ''}")
        team_data, games = await gather_or_raise(get_team_details(team_id), get_schedule(team_id, season))
        now = datetime.now(timezone.utc)
        return {
            "teamId": team_id,
            "season": season,
            "team": build_team(team_id, team_data),
            "standings": build_standings(team_id, team_data),
            "lastGame": build_last_game(find_last_game(games, now), team_id, tz),
            "nextGame": build_next_game(find_next_game(games, now), team_id, tz)
        }

    result, cached = await get_or_fetch(CACHE, cache_key, build, logger, force=force_refresh)
    if cached:
        logger.info(f"Returning cached data for team {team_id}")
    return transform_data(result, cached=cached)


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
