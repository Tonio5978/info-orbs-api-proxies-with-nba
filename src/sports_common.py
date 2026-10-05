"""Helpers shared by the sports proxies (MLB, NFL, NBA), plus ESPN-specific parsing for NFL and NBA."""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request

DEFAULT_TZ = "America/New_York"
UTC_FORMATS = ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ", "%Y-%m-%d"]


# ── Team data ─────────────────────────────────────────────────────────────────

def load_teams(path: Path, logger: logging.Logger) -> List[dict]:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load team data: {str(e)}")
        raise RuntimeError("Could not initialize team data")


def build_team_lookup(teams: List[dict]) -> Dict[str, str]:
    """Map lowercased id, name and aliases to the team id."""
    lookup = {}
    for team in teams:
        for name in [team["id"], team["name"], *team["aliases"]]:
            lookup[str(name).lower().strip()] = team["id"]
    return lookup


def resolve_team(lookup: Dict[str, str], team_identifier: str) -> str:
    team_id = lookup.get(team_identifier.lower().strip())
    if team_id is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown team: {team_identifier}. Try /debug/teams for valid options"
        )
    return team_id


def parse_colors(color_str: str) -> List[Dict[str, str]]:
    """Parse 'Red (#C8102E), Black (#000000)' into [{'name': 'Red', 'code': '#C8102E'}, ...]"""
    if not color_str or color_str == "N/A":
        return []
    colors = []
    for color_part in color_str.split(","):
        color_part = color_part.strip()
        if "(" in color_part and ")" in color_part:
            name_part, code_part = color_part.split("(", 1)
            colors.append({"name": name_part.strip(), "code": code_part.split(")")[0].strip()})
        else:
            colors.append({"name": color_part, "code": "#000000"})  # Default black if no code provided
    return colors


def format_ordinal(rank):
    """Convert a numeric rank (int, float or string) to an ordinal string: 1 -> '1st'. Non-numeric values are returned unchanged."""
    try:
        num = int(float(rank))
    except (ValueError, TypeError):
        return rank
    if 11 <= (num % 100) <= 13:
        return f"{num}th"
    return {1: f"{num}st", 2: f"{num}nd", 3: f"{num}rd"}.get(num % 10, f"{num}th")


# ── Dates & time zones ────────────────────────────────────────────────────────

def get_display_tz(request: Request) -> ZoneInfo:
    """Time zone used to display game dates/times: ?tz=Europe/Paris, default America/New_York."""
    tz_name = request.query_params.get("tz") or DEFAULT_TZ
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=400, detail=f"Unknown time zone: {tz_name}")


def parse_utc_date(date_str: str) -> datetime:
    for fmt in UTC_FORMATS:
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Time data '{date_str}' doesn't match expected formats")


def format_game_date(date_str: str, tz: ZoneInfo) -> str:
    """'Apr 2' in the display time zone."""
    if not date_str or date_str == "N/A":
        return "N/A"
    try:
        local = parse_utc_date(date_str).astimezone(tz)
        return f"{local:%b} {local.day}"  # Portable equivalent of %-d
    except ValueError:
        return "N/A"


def get_day_of_week(date_str: str, tz: ZoneInfo) -> str:
    """Abbreviated day of week ('Mon') in the display time zone."""
    if not date_str or date_str == "N/A":
        return "N/A"
    try:
        return parse_utc_date(date_str).astimezone(tz).strftime("%a")
    except ValueError:
        return "N/A"


def format_game_time(date_str: str, tz: ZoneInfo) -> str:
    """12-hour time ('7:30 PM') in the display time zone."""
    if not date_str or date_str == "N/A":
        return "N/A"
    try:
        local = parse_utc_date(date_str).astimezone(tz)
    except ValueError:
        return date_str
    period = "AM" if local.hour < 12 else "PM"
    return f"{local.hour % 12 or 12}:{local.minute:02d} {period}"


# ── ESPN (NFL, NBA) ───────────────────────────────────────────────────────────

def get_status_type(event: dict) -> dict:
    """Status of an ESPN event.

    The scoreboard puts it on the event, the team schedule only on the competition.
    """
    status = event.get("status") or (event.get("competitions") or [{}])[0].get("status") or {}
    return status.get("type", {})


def is_completed(event: dict) -> bool:
    return bool(get_status_type(event).get("completed", False))


def find_last_game(events: List[dict], now: datetime) -> Optional[dict]:
    """Most recent completed game before now."""
    return next(
        (e for e in sorted(events, key=lambda x: x["date"], reverse=True)
         if parse_utc_date(e["date"]) < now and is_completed(e)),
        None
    )


def find_next_game(events: List[dict], now: datetime) -> Optional[dict]:
    """First game scheduled from now on that isn't completed."""
    return next(
        (e for e in sorted(events, key=lambda x: x["date"])
         if parse_utc_date(e["date"]) >= now and not is_completed(e)),
        None
    )


def split_competitors(event: dict, team_id: str) -> Optional[Tuple[dict, bool, dict, dict]]:
    """Return (competition, is_home, my_side, opponent_side), or None if the event has no usable competition."""
    if not event or not event.get("competitions"):
        return None
    comp = event["competitions"][0]
    competitors = comp.get("competitors", [])
    if len(competitors) < 2:
        return None
    home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
    away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
    is_home = home.get("team", {}).get("id") == team_id
    return comp, is_home, (home if is_home else away), (away if is_home else home)


def get_score_value(competitor: dict) -> str:
    """ESPN returns the score as a dict in schedules and as a string in the scoreboard."""
    score = competitor.get("score", "0")
    if isinstance(score, dict):
        return score.get("displayValue", "0")
    return str(score)


def get_broadcast_name(comp: dict) -> str:
    """First TV broadcast of a scheduled game: 'media.shortName' in team schedules, 'names' in the scoreboard."""
    broadcast = (comp.get("broadcasts") or [{}])[0]
    return broadcast.get("media", {}).get("shortName") or (broadcast.get("names") or ["N/A"])[0]


def get_primary_logo(team_info: dict) -> dict:
    logos = team_info.get("logos", [])
    return next((logo for logo in logos if "default" in logo.get("rel", [])), logos[0] if logos else {})
