from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import HTTPException, Request
from fastapi.staticfiles import StaticFiles
from .common import (setup_logger, create_app, fetch_data, check_force_refresh, is_force_requested, handle_request,
                     TTLCache, make_cache_key, get_or_fetch, gather_or_raise, proxy_info)
from .sports_common import (load_teams, parse_colors, format_ordinal, get_display_tz, parse_utc_date,
                            format_game_date, get_day_of_week, format_game_time)

logger = setup_logger("MLBDATA")
BASE_URL = "https://statsapi.mlb.com/api/v1/"

# Cache configuration
CACHE = TTLCache.from_env_minutes("MLBDATA_PROXY_CACHE_LIFE")  # 0 disables caching

# Load team data from external JSON file
TEAMS_DATA = load_teams(Path(__file__).parent / "mlb_teams.json", logger)
TEAMS_BY_ID = {team["id"]: team for team in TEAMS_DATA}
TEAM_IDS = {alias.lower(): team["id"] for team in TEAMS_DATA for alias in team["aliases"]}

app = create_app("mlbdata_proxy", default_requests_per_minute=15, banner_title="MLB Data Service Configuration", banner_lines=lambda: [
    f"Teams loaded: {len(TEAMS_DATA)}",
    f"Cache lifetime: {CACHE.ttl_seconds // 60} minutes ({'enabled' if CACHE.enabled else 'disabled'})",
    "Force refresh: supported via &force=true parameter",
])

LOGO_DIR = Path("/app/mlb_logos")
app.mount("/mlbdata/logo", StaticFiles(directory=LOGO_DIR), name="mlb_logos")


def get_current_season() -> str:
    """Get current season year based on today's date"""
    today = datetime.now()
    return str(today.year if today.month >= 3 else today.year - 1)


def transform_data(data: dict, cached: bool = False) -> dict:
    """Add proxy-info to the MLB data response"""
    if not data:
        raise HTTPException(status_code=502, detail="Empty API response")
    transformed = dict(data)
    transformed["proxy-info"] = proxy_info(cached)
    return transformed


def get_short_team_name(full_name: str) -> str:
    """Extract short team name by removing city"""
    if not full_name:
        return "N/A"
    return full_name.split()[-1].strip()


def get_team_id(team_identifier: str) -> int:
    """Convert team name or ID string to numeric ID"""
    if team_identifier.isdigit():
        return int(team_identifier)
    lower_team = team_identifier.lower().replace(" ", "")
    if lower_team in TEAM_IDS:
        return TEAM_IDS[lower_team]
    raise HTTPException(status_code=400, detail=f"Unknown team: {team_identifier}")


async def get_team_details(team_id: int) -> dict:
    """Fetch team details from MLB API"""
    team_data = await fetch_data(f"{BASE_URL}teams/{team_id}?hydrate=division,league,sport", logger, app_name="mlbdata")
    if not team_data or "teams" not in team_data:
        raise HTTPException(status_code=502, detail="Failed to fetch team data")
    return team_data["teams"][0]


async def get_standings(league_id: str, season: str, team_id: int) -> Optional[dict]:
    """Fetch standings for a specific team"""
    standings_data = await fetch_data(f"{BASE_URL}standings?leagueId={league_id}&season={season}", logger, app_name="mlbdata")
    if standings_data and "records" in standings_data:
        for record in standings_data["records"]:
            for team_record in record["teamRecords"]:
                if team_record["team"]["id"] == team_id:
                    return team_record
    return None


async def get_schedule(team_id: int, season: str) -> list:
    """Fetch schedule for a specific team"""
    schedule_data = await fetch_data(f"{BASE_URL}schedule?sportId=1&teamId={team_id}&season={season}", logger, app_name="mlbdata")
    if not schedule_data or "dates" not in schedule_data:
        raise HTTPException(status_code=502, detail="Failed to fetch schedule data")
    return [game for date in schedule_data["dates"] for game in date["games"]]


def get_completed_games(games: list, now: datetime) -> list:
    """Final games before now, most recent first"""
    return [
        g for g in sorted(games, key=lambda x: x["gameDate"], reverse=True)
        if parse_utc_date(g["gameDate"]) < now and g["status"]["detailedState"] == "Final"
    ]


def get_last_ten_games_record(completed_games: list, team_id: int) -> dict:
    """Calculate the team's record in their last 10 completed games"""
    last_ten = completed_games[:10]
    wins = 0
    losses = 0
    for game in last_ten:
        home_team = game["teams"]["home"]
        away_team = game["teams"]["away"]
        if home_team["team"]["id"] == team_id:
            if home_team["score"] > away_team["score"]:
                wins += 1
            else:
                losses += 1
        elif away_team["team"]["id"] == team_id:
            if away_team["score"] > home_team["score"]:
                wins += 1
            else:
                losses += 1
    return {
        "record": f"{wins}-{losses}",
        "wins": wins,
        "losses": losses,
        "games": len(last_ten)  # In case there are fewer than 10 completed games
    }


def build_last_game(game: Optional[dict], team_id: int, tz) -> dict:
    if not game:
        return {"date": "N/A", "day": "N/A", "opponent": "N/A", "score": "N/A", "result": "N/A", "gameTime": "N/A"}
    home, away = game["teams"]["home"], game["teams"]["away"]
    is_home = home["team"]["id"] == team_id
    me, opp = (home, away) if is_home else (away, home)
    return {
        "date": format_game_date(game["gameDate"], tz),
        "day": get_day_of_week(game["gameDate"], tz),
        "opponent": get_short_team_name(opp["team"]["name"]),
        "score": f"{away['score']}-{home['score']}",
        "result": "Won" if me["score"] > opp["score"] else "Lost",
        "gameTime": format_game_time(game["gameDate"], tz)
    }


def build_next_game(game: Optional[dict], team_id: int, tz) -> dict:
    if not game:
        return {"date": "N/A", "day": "N/A", "opponent": "N/A", "location": "N/A", "probablePitcher": "TBD",
                "gameTime": "N/A", "tvBroadcast": "N/A"}
    home, away = game["teams"]["home"], game["teams"]["away"]
    is_home = home["team"]["id"] == team_id
    me, opp = (home, away) if is_home else (away, home)
    broadcasts = game.get("broadcasts") or [{}]
    return {
        "date": format_game_date(game["gameDate"], tz),
        "day": get_day_of_week(game["gameDate"], tz),
        "opponent": get_short_team_name(opp["team"]["name"]),
        "location": "Home" if is_home else "Away",
        "probablePitcher": me.get("probablePitcher", {}).get("fullName", "TBD"),
        "gameTime": format_game_time(game["gameDate"], tz),
        "tvBroadcast": broadcasts[0].get("name", "N/A")
    }


async def proxy_endpoint(request: Request):
    team_identifier = request.query_params.get("teamName")
    if not team_identifier:
        raise HTTPException(status_code=400, detail="teamName parameter is required")

    team_id = get_team_id(team_identifier)
    tz = get_display_tz(request)
    season = get_current_season()
    force_refresh = check_force_refresh(request, is_force_requested(request))
    cache_key = make_cache_key({"teamId": str(team_id), "season": season, "tz": tz.key})

    async def build() -> dict:
        logger.info(f"Fetching live data for team {team_id}{' (forced refresh)' if force_refresh else ''}")
        team_info, games = await gather_or_raise(get_team_details(team_id), get_schedule(team_id, season))
        full_team_name = team_info.get("name", "Unknown Team")
        team = TEAMS_BY_ID.get(team_id, {})

        result = {
            "teamId": team_id,
            "season": season,
            "team": {
                "fullName": full_team_name,
                "shortName": get_short_team_name(full_team_name),
                "colors": parse_colors(team.get("colors", "")),
                "logoUrl": f"https://www.mlbstatic.com/team-logos/{team_id}.svg",
                "logoImageFileName": team.get("logoImageFileName", ""),
                "logoBackgroundColor": team.get("logoBackgroundColor", "")
            }
        }

        # Standings (needs the league from the team details)
        league_id = team_info.get("league", {}).get("id") or (team_info.get("leagues") or [{}])[0].get("id")
        if league_id:
            standings = await get_standings(league_id, season, team_id)
            if standings:
                result["record"] = f"{standings['wins']}-{standings['losses']}"
                result["standings"] = {
                    "division": team_info.get("division", {}).get("nameShort", "N/A"),
                    "divisionRank": format_ordinal(standings.get("divisionRank", "N/A")),
                    "winningPercentage": standings["winningPercentage"],
                    "gamesBack": standings.get("gamesBack", "N/A")
                }

        now = datetime.now(timezone.utc)
        completed_games = get_completed_games(games, now)
        next_game = next(
            (g for g in sorted(games, key=lambda x: x["gameDate"])
             if parse_utc_date(g["gameDate"]) >= now and g["status"]["detailedState"] in ["Scheduled", "Pre-Game"]),
            None
        )
        result["lastGame"] = build_last_game(completed_games[0] if completed_games else None, team_id, tz)
        result["lastTen"] = get_last_ten_games_record(completed_games, team_id)
        result["nextGame"] = build_next_game(next_game, team_id, tz)
        return result

    result, cached = await get_or_fetch(CACHE, cache_key, build, logger, force=force_refresh)
    if cached:
        logger.info(f"Returning cached data for team {team_id}")
    return transform_data(result, cached=cached)


handle_request(app, logger, proxy_endpoint, methods=("GET",))
