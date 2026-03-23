import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import json
from zoneinfo import ZoneInfo
from fastapi import HTTPException, Request
from pydantic import BaseModel
from slowapi.util import get_remote_address
from .common import setup_logger, create_app, fetch_data

logger = setup_logger("NFLDATA")
app = create_app("nfldata_proxy")
BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"

LOGO_DIR = Path("/app/nfl_logos")
app.mount("/nfldata/logo", StaticFiles(directory=LOGO_DIR), name="nfl_logos")

# Cache configuration
CACHE_LIFE_MINUTES = int(os.getenv("NFLDATA_PROXY_CACHE_LIFE", "5"))
nfl_cache: Dict[str, dict] = {}
cache_expiry: Dict[str, datetime] = {}

# Load team data
TEAMS_DATA_FILE = Path(__file__).parent / "nfl_teams.json"
try:
    with open(TEAMS_DATA_FILE) as f:
        TEAMS_DATA = json.load(f)
    TEAM_IDS = {alias.lower(): team["id"] for team in TEAMS_DATA for alias in team["aliases"]}
    TEAM_COLORS = {team["id"]: team["colors"] for team in TEAMS_DATA}
    TEAM_LOGO_FILENAMES = {team["id"]: team["logoImageFileName"] for team in TEAMS_DATA}
    TEAM_LOGO_BG_COLORS = {team["id"]: team["logoBackgroundColor"] for team in TEAMS_DATA}
    TEAM_CONFERENCES = {team["id"]: team["conference"] for team in TEAMS_DATA}
    TEAM_DIVISIONS = {team["id"]: team["division"] for team in TEAMS_DATA}
except Exception as e:
    logger.error(f"Failed to load team data: {str(e)}")
    raise RuntimeError("Could not initialize team data")

@app.on_event("startup")
async def startup_event():
    logger.info("="*50)
    logger.info(f"{'NFL Data Service Configuration':^50}")
    logger.info("="*50)
    logger.info(f"→ Teams loaded: {len(TEAMS_DATA)}")
    logger.info(f"→ Rate limiting: {os.getenv('NFLDATA_PROXY_REQUESTS_PER_MINUTE', '15')}/minute")
    logger.info(f"→ Cache lifetime: {CACHE_LIFE_MINUTES} minutes ({'enabled' if CACHE_LIFE_MINUTES > 0 else 'disabled'})")
    logger.info("="*50 + "\n")

class NFLRequest(BaseModel):
    teamName: str

def get_current_season() -> str:
    today = datetime.now()
    return str(today.year if today.month >= 9 else today.year - 1)

def format_division_rank(rank: str) -> str:
    try:
        num = int(rank)
        if 11 <= (num % 100) <= 13:
            return f"{num}th"
        return {1: f"{num}st", 2: f"{num}nd", 3: f"{num}rd"}.get(num % 10, f"{num}th")
    except (ValueError, TypeError):
        return rank

def parse_colors(color_str: str) -> List[Dict[str, str]]:
    if not color_str or color_str == "N/A":
        return []
    colors = []
    for color_part in color_str.split(","):
        color_part = color_part.strip()
        if "(" in color_part and ")" in color_part:
            name_part, code_part = color_part.split("(", 1)
            colors.append({"name": name_part.strip(), "code": code_part.split(")")[0].strip()})
        else:
            colors.append({"name": color_part, "code": "#000000"})
    return colors

def parse_nfl_date(date_str: str) -> datetime:
    formats = ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ", "%Y-%m-%d"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Time data '{date_str}' doesn't match expected formats")

def format_game_date(date_str: str) -> str:
    if not date_str or date_str == "N/A":
        return "N/A"
    try:
        date_obj = parse_nfl_date(date_str) if isinstance(date_str, str) else date_str
        return date_obj.strftime("%b %-d")
    except (ValueError, AttributeError):
        return "N/A"

def transform_data(data: dict, cached: bool = False) -> dict:
    if not data:
        raise HTTPException(status_code=502, detail="Empty API response")
    
    # Ensure all required fields exist with proper fallbacks
    transformed = {
        "teamId": data.get("teamId", "N/A"),
        "season": data.get("season", "N/A"),
        "team": data.get("team", {}),
        "standings": data.get("standings", {}),
        "lastGame": data.get("lastGame", {}),
        "nextGame": data.get("nextGame", {}),
        "proxy-info": {
            "cachedResponse": cached,
            "status_code": 200,
            "timestamp": datetime.utcnow().isoformat()
        }
    }
    
    # Ensure nested structures have all required fields
    for field in ["team", "standings", "lastGame", "nextGame"]:
        if field not in transformed:
            transformed[field] = {}
            
    return transformed

def get_day_of_week(date_str: str) -> str:
    if not date_str or date_str == "N/A":
        return "N/A"
    try:
        date_obj = parse_nfl_date(date_str) if isinstance(date_str, str) else date_str
        return date_obj.strftime("%a")
    except (ValueError, AttributeError):
        return "N/A"

def format_game_time(time_str: str) -> str:
    if not time_str or time_str == "N/A":
        return "N/A"
    try:
        et_time = parse_nfl_date(time_str).astimezone(ZoneInfo("America/New_York"))
        hour = et_time.hour
        period = "AM" if hour < 12 else "PM"
        hour_12 = hour % 12 or 12
        return f"{hour_12}:{et_time.minute:02d} {period}"
    except ValueError:
        return time_str

def get_cache_key(params: dict) -> str:
    return json.dumps({k: v for k, v in params.items() if k != 'force'}, sort_keys=True)

async def get_team_id(team_identifier: str) -> str:
    if team_identifier in TEAM_IDS.values():
        return team_identifier
    lower_team = team_identifier.lower().strip()
    for team in TEAMS_DATA:
        if (lower_team == team["id"].lower() or 
            lower_team in [a.lower() for a in team["aliases"]] or
            lower_team == team["name"].lower()):
            return team["id"]
    raise HTTPException(
        status_code=400, 
        detail=f"Unknown team: {team_identifier}. Try /debug/teams for valid options"
    )

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

async def proxy_endpoint(request: Request):
    team_identifier = request.query_params.get("teamName")
    if not team_identifier:
        raise HTTPException(status_code=400, detail="teamName parameter is required")

    try:
        team_id = await get_team_id(team_identifier)
        logger.info(f"Resolved '{team_identifier}' to team ID: {team_id}")
    except ValueError as e:
        logger.error(f"Failed to resolve team: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

    season = get_current_season()
    force_refresh = request.query_params.get("force", "").lower() == "true"
    params = {"teamId": team_id, "season": season}
    cache_key = get_cache_key(params)
    
    if CACHE_LIFE_MINUTES > 0 and not force_refresh:
        cached_data = nfl_cache.get(cache_key)
        if cached_data and cache_expiry.get(cache_key, datetime.min) > datetime.utcnow():
            logger.info(f"Returning cached data for team {team_id}")
            return transform_data(cached_data, cached=True)

    logger.info(f"Fetching live data for team {team_id}{' (forced refresh)' if force_refresh else ''}")
    try:
        result = {
            "teamId": team_id,
            "season": season,
            "team": {},
            "standings": {},
            "lastGame": {},
            "nextGame": {}
        }

        # Team Details
        team_data = await get_team_details(team_id)
        team_info = team_data.get("team", {})
        logos = team_info.get("logos", [])
        primary_logo = next((logo for logo in logos if "default" in logo.get("rel", [])), logos[0] if logos else {})
        
        result["team"] = {
            "fullName": team_info.get("displayName", "Unknown Team"),
            "shortName": team_info.get("nickname", team_info.get("shortDisplayName", "")),
            "colors": parse_colors(TEAM_COLORS.get(team_id, "")),
            "logoUrl": primary_logo.get("href", ""),
            "logoImageFileName": TEAM_LOGO_FILENAMES.get(team_id, ""),
            "logoBackgroundColor": TEAM_LOGO_BG_COLORS.get(team_id, ""),
            "abbreviation": team_info.get("abbreviation", ""),
            "standingSummary": team_data.get("standingSummary", "N/A"),
            "conference": TEAM_CONFERENCES.get(team_id, "N/A"),
            "division": TEAM_DIVISIONS.get(team_id, "N/A")
        }

        # Standings data
        record_items = team_info.get("record", {}).get("items", [])
        total_record = next((item for item in record_items if item.get("type") == "total"), {})
        
        if total_record:
            stats = {stat["name"]: stat["value"] for stat in total_record.get("stats", [])}
            
            result["standings"] = {
                "conference": TEAM_CONFERENCES.get(team_id, "N/A"),
                "conferenceRank": format_division_rank(stats.get("playoffSeed", "N/A")),
                "division": TEAM_DIVISIONS.get(team_id, "N/A"),
                "divisionRank": format_division_rank(stats.get("divisionRank", "N/A")),  # Added division rank
                "winningPercentage": stats.get("winPercent", "N/A"),
                "pointsFor": stats.get("pointsFor", "N/A"),
                "pointsAgainst": stats.get("pointsAgainst", "N/A"),
                "record": total_record.get("summary", "N/A")
            }

        # Schedule processing
        games = await get_schedule(team_id, season)
        today = datetime.now(timezone.utc)

        # Last Game - find the most recent completed game
        last_game = next(
            (g for g in sorted(games, key=lambda x: x["date"], reverse=True)
            if parse_nfl_date(g["date"]) < today
            and g.get("status", {}).get("type", {}).get("completed", False)), None)
        
        if last_game and last_game.get("competitions"):
            comp = last_game["competitions"][0]
            competitors = comp.get("competitors", [])
            if len(competitors) >= 2:
                home = competitors[0]
                away = competitors[1]
                is_home = home.get("team", {}).get("id") == team_id
                opponent = away["team"] if is_home else home["team"]
                home_score = home.get("score", "0")
                away_score = away.get("score", "0")
                
                result["lastGame"] = {
                    "date": format_game_date(last_game["date"]),
                    "day": get_day_of_week(last_game["date"]),
                    "opponent": opponent.get("nickname", opponent.get("shortDisplayName", "N/A")),
                    "score": f"{home_score}-{away_score}" if is_home else f"{away_score}-{home_score}",
                    "result": "Won" if ((is_home and home.get("winner")) or 
                                      (not is_home and away.get("winner"))) else "Lost",
                    "gameTime": format_game_time(last_game["date"]),
                    "gameId": last_game.get("id", "N/A")
                }

        # Next Game - find the first upcoming game
        next_game = next(
            (g for g in sorted(games, key=lambda x: x["date"])
            if parse_nfl_date(g["date"]) >= today
            and not g.get("status", {}).get("type", {}).get("completed", True)), None)
        
        if next_game and next_game.get("competitions"):
            comp = next_game["competitions"][0]
            competitors = comp.get("competitors", [])
            if len(competitors) >= 2:
                home = competitors[0]
                away = competitors[1]
                is_home = home.get("team", {}).get("id") == team_id
                opponent = away["team"] if is_home else home["team"]
                
                result["nextGame"] = {
                    "date": format_game_date(next_game["date"]),
                    "day": get_day_of_week(next_game["date"]),
                    "opponent": opponent.get("nickname", opponent.get("shortDisplayName", "N/A")),
                    "location": "Home" if is_home else "Away",
                    "gameTime": format_game_time(next_game["date"]),
                    "tvBroadcast": comp.get("broadcasts", [{}])[0].get("names", ["N/A"])[0],
                    "gameId": next_game.get("id", "N/A")
                }

        # Update cache
        if CACHE_LIFE_MINUTES > 0:
            nfl_cache[cache_key] = result
            cache_expiry[cache_key] = datetime.utcnow() + timedelta(minutes=CACHE_LIFE_MINUTES)
            logger.info(f"Cached data for team {team_id} for {CACHE_LIFE_MINUTES} minutes")
        
        return transform_data(result, cached=False)
    except HTTPException as e:
        if CACHE_LIFE_MINUTES > 0 and cache_key in nfl_cache and not force_refresh:
            logger.warning(f"API failed, returning cached data for team {team_id}")
            return transform_data(nfl_cache[cache_key], cached=True)
        raise e

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

@app.api_route("/proxy", methods=["GET"])
@app.state.limiter.limit(os.getenv("NFLDATA_PROXY_REQUESTS_PER_MINUTE", "15") + "/minute")
async def nfldata_proxy(request: Request):
    logger.info(f"{datetime.now().isoformat()} Received request for team: {request.query_params.get('teamName')}")
    return await proxy_endpoint(request)
