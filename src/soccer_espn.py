"""Pure functions for the soccer proxy: ESPN scoreboard/teams projection, date window, aliases.

No I/O here, so everything can be unit-tested on recorded ESPN responses (tests/fixtures/soccer).
"""
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

UTC_FORMATS = ["%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"]


def parse_espn_date(value: str) -> Optional[datetime]:
    for fmt in UTC_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


# ── Date window ───────────────────────────────────────────────────────────────

def window_bounds(now: datetime, tz: ZoneInfo, days_before: int, days_after: int):
    """[start, end) of the window from day J-days_before 00:00 to the end of day J+days_after, in the local time zone."""
    today = now.astimezone(tz).date()
    start = datetime.combine(today - timedelta(days=days_before), datetime.min.time(), tz)
    end = datetime.combine(today + timedelta(days=days_after + 1), datetime.min.time(), tz)
    return start, end


def months_to_fetch(now: datetime, tz: ZoneInfo, days_before: int, days_after: int) -> List[str]:
    """ESPN 'YYYYMM' months covering the window.

    ESPN rejects date ranges, so the window is fetched month by month and filtered locally.
    The window is widened by one day on each side because ESPN's month boundaries follow
    US time, not ours: a match on the 1st at 00:30 in Paris is still the 31st for ESPN.
    """
    today = now.astimezone(tz).date()
    first = today - timedelta(days=days_before + 1)
    last = today + timedelta(days=days_after + 1)
    months, day = [], first
    while day <= last:
        key = day.strftime("%Y%m")
        if key not in months:
            months.append(key)
        day += timedelta(days=1)
    return months


def in_window(event: dict, start: datetime, end: datetime) -> bool:
    when = parse_espn_date(event.get("date", ""))
    return when is not None and start <= when < end


def event_days(event: dict, tz: ZoneInfo) -> List[str]:
    """ESPN 'YYYYMMDD' days that may contain this event (UTC, US Eastern and local day of its kickoff)."""
    when = parse_espn_date(event.get("date", ""))
    if when is None:
        return []
    days = []
    for zone in (timezone.utc, ZoneInfo("America/New_York"), tz):
        day = when.astimezone(zone).strftime("%Y%m%d")
        if day not in days:
            days.append(day)
    return days


def needs_live_refresh(event: dict, now: datetime, prematch_minutes: int) -> bool:
    """True if the match is in progress, or scheduled to start within `prematch_minutes`.

    A scheduled match whose kickoff passed more than 3 hours ago is ignored (ESPN never
    started it), so it can't keep the competition in fast refresh forever.
    """
    state = event.get("status", {}).get("type", {}).get("state")
    if state == "in":
        return True
    if state != "pre":
        return False
    when = parse_espn_date(event.get("date", ""))
    return when is not None and now - timedelta(hours=3) <= when <= now + timedelta(minutes=prematch_minutes)


# ── Matches projection (/…UpcomingMatches) ────────────────────────────────────

def _strip_hash(color):
    return color.lstrip("#") if isinstance(color, str) else color


def _score(value):
    if value is None:
        return None
    if isinstance(value, dict):  # Not seen in the soccer scoreboard, but other ESPN endpoints use {value, displayValue}
        value = value.get("displayValue", value.get("value"))
    return str(value)


def project_competitor(competitor: dict) -> dict:
    team = competitor.get("team", {})
    return {
        "homeAway": competitor.get("homeAway"),
        "score": _score(competitor.get("score")),
        "team": {
            "id": team.get("id"),
            "displayName": team.get("displayName"),
            "abbreviation": team.get("abbreviation"),
            "color": _strip_hash(team.get("color")),
            "alternateColor": _strip_hash(team.get("alternateColor")),
        },
    }


def project_detail(detail: dict) -> dict:
    """Key event of a match (goal, card...). Only the fields used by the clients; optional ones only when ESPN sends them."""
    projected = {"type": {"text": detail.get("type", {}).get("text")}}
    for flag in ("scoringPlay", "penaltyKick", "ownGoal"):
        if flag in detail:
            projected[flag] = detail[flag]
    projected["team"] = {"id": detail.get("team", {}).get("id")}
    projected["clock"] = {"displayValue": detail.get("clock", {}).get("displayValue")}
    projected["athletesInvolved"] = [
        {"shortName": athlete.get("shortName"), "jersey": athlete.get("jersey")}
        for athlete in detail.get("athletesInvolved", [])
    ]
    if "corrected" in detail:
        projected["corrected"] = detail["corrected"]
    if "score" in detail:
        projected["score"] = _score(detail["score"])
    return projected


def project_event(event: dict) -> dict:
    """ESPN scoreboard event reduced to the fields of the API (same names and nesting as ESPN)."""
    status = event.get("status", {})
    status_type = status.get("type", {})
    projected_status = {
        "type": {
            "state": status_type.get("state"),
            "name": status_type.get("name"),
            "completed": status_type.get("completed"),
        },
        "displayClock": status.get("displayClock"),
    }
    # No period for scheduled matches
    if status_type.get("state") != "pre" and "period" in status:
        projected_status["period"] = status["period"]

    competition = (event.get("competitions") or [{}])[0]
    venue = event.get("venue") or competition.get("venue") or {}
    return {
        "id": event.get("id"),
        "uid": event.get("uid"),
        "date": event.get("date"),
        "name": event.get("name"),
        "status": projected_status,
        "venue": {"displayName": venue.get("displayName")},
        "competitions": [{
            "altGameNote": competition.get("altGameNote"),
            "competitors": [project_competitor(c) for c in competition.get("competitors", [])],
            "details": [project_detail(d) for d in competition.get("details", [])],
        }],
    }


def build_matches_payload(events: Iterable[dict], start: datetime, end: datetime) -> dict:
    """Projected events of the window, in kickoff order."""
    kept = [e for e in events if in_window(e, start, end)]
    kept.sort(key=lambda e: (e.get("date") or "", e.get("id") or ""))
    return {"events": kept}


# ── Day scoreboards (/scores, /upcoming) ──────────────────────────────────────

def league_id_from_uid(uid: str) -> Optional[str]:
    """ESPN league id from an event uid ('s:600~l:775~e:401915402' -> '775')."""
    for part in (uid or "").split("~"):
        if part.startswith("l:"):
            return part[2:]
    return None


def format_local_time(date_str: str, tz: ZoneInfo) -> Optional[str]:
    """Kickoff as 24-hour local time ('21:00')."""
    when = parse_espn_date(date_str)
    return when.astimezone(tz).strftime("%H:%M") if when else None


def goals_and_cards(event: dict) -> tuple:
    """Goals and yellow/red cards of a match from its ESPN details.

    For an own goal ESPN credits the team that benefits (consistent with the score); the player
    belongs to the other team. Penalty shoot-out kicks are not goals and are left out.
    """
    competition = (event.get("competitions") or [{}])[0]
    abbreviations = {str(c.get("team", {}).get("id")): c.get("team", {}).get("abbreviation")
                     for c in competition.get("competitors", [])}
    goals, cards = [], []
    for detail in competition.get("details", []):
        athletes = detail.get("athletesInvolved") or [{}]
        common = {
            "minute": detail.get("clock", {}).get("displayValue"),
            "team": abbreviations.get(str(detail.get("team", {}).get("id"))),
            "player": athletes[0].get("shortName") or athletes[0].get("displayName"),
        }
        if detail.get("scoringPlay") and not detail.get("shootout"):
            goals.append({**common, "type": detail.get("type", {}).get("text"),
                          "penalty": bool(detail.get("penaltyKick")), "ownGoal": bool(detail.get("ownGoal"))})
        elif detail.get("redCard") or detail.get("yellowCard"):
            cards.append({**common, "card": "red" if detail.get("redCard") else "yellow"})
    return goals, cards


def format_day_game(event: dict, competition_key: Optional[str], tz: ZoneInfo) -> dict:
    """Match summary in the /nbadata/scores style: status, live period/clock, teams, goals and cards.

    Live matches ('in') get period, clock and detail ('HT' at half time); other matches get
    each team's color ('#rrggbb').
    """
    status = event.get("status") or {}
    state = status.get("type", {}).get("state")
    competition = (event.get("competitions") or [{}])[0]
    side = {c.get("homeAway"): c for c in competition.get("competitors", [])}

    def team(c: dict) -> dict:
        info = c.get("team", {})
        summary = {"abbreviation": info.get("abbreviation"), "score": _score(c.get("score"))}
        if state != "in":
            summary["color"] = f"#{_strip_hash(info['color'])}" if info.get("color") else None
        return summary

    game = {"gameId": event.get("id"), "status": state, "competition": competition_key,
            "competitionName": competition.get("altGameNote")}
    if state == "in":
        game["period"] = status.get("period")
        game["clock"] = status.get("displayClock")
        game["detail"] = status.get("type", {}).get("shortDetail")
    game["startTime"] = format_local_time(event.get("date", ""), tz)
    game["home"] = team(side.get("home", {}))
    game["away"] = team(side.get("away", {}))
    game["goals"], game["cards"] = goals_and_cards(event)
    return game


def team_matches(competitor: dict, wanted: str, aliases_config: Dict[str, dict]) -> bool:
    """?team= filter: ESPN id, abbreviation, name (with or without accents) or alias from aliases.json."""
    info = competitor.get("team", {})
    wanted = strip_accents(wanted).lower().strip()
    names = {str(info.get("id")), info.get("abbreviation") or "", info.get("displayName") or "", info.get("shortDisplayName") or ""}
    names |= set(aliases_config.get(str(info.get("id")), {}).get("aliases", []))
    return wanted in {strip_accents(n).lower() for n in names if n}


def build_day_payload(day: str, events: Iterable[dict], league_keys: Dict[str, str], tz: ZoneInfo,
                      include_all: bool = False, competition: Optional[str] = None, team: Optional[str] = None,
                      aliases_config: Optional[Dict[str, dict]] = None) -> dict:
    """Games of a scoreboard day: configured competitions only, unless include_all."""
    games = []
    for event in sorted(events, key=lambda e: (e.get("date") or "", e.get("id") or "")):
        key = league_keys.get(league_id_from_uid(event.get("uid")))
        if not include_all and key is None:
            continue
        if competition and key != competition:
            continue
        if team and not any(team_matches(c, team, aliases_config or {})
                            for c in (event.get("competitions") or [{}])[0].get("competitors", [])):
            continue
        games.append(format_day_game(event, key, tz))
    return {"date": day, "count": len(games), "games": games}


# ── Teams projection (/…Teams) ────────────────────────────────────────────────

def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def team_aliases(team_id: str, display_name: str, aliases_config: Dict[str, dict]) -> List[str]:
    """Aliases from config/aliases.json (lowercase), plus the accent-free name when it differs."""
    result = []
    for alias in aliases_config.get(str(team_id), {}).get("aliases", []):
        alias = str(alias).lower().strip()
        if alias and alias not in result:
            result.append(alias)
    if display_name:
        plain = strip_accents(display_name).lower()
        if plain != display_name.lower() and plain not in result:
            result.append(plain)
    return result


def espn_teams(teams_response: dict) -> List[dict]:
    """Team objects of an ESPN /teams response (sports[0].leagues[0].teams[].team)."""
    leagues = ((teams_response.get("sports") or [{}])[0].get("leagues") or [{}])
    return [entry.get("team", {}) for entry in leagues[0].get("teams", [])]


def team_country(team_id: str, competition_country: Optional[str], aliases_config: Dict[str, dict],
                 national_countries: Dict[str, str]) -> Optional[str]:
    """Country of a team: aliases.json override, else the competition's country, else the country of
    the national league the team plays in (for European cups), else None (never guessed)."""
    override = aliases_config.get(str(team_id), {}).get("country")
    country = override or competition_country or national_countries.get(str(team_id))
    return country.lower() if country else None


def build_teams_payload(teams_response: dict, competition_key: str, competition_country: Optional[str],
                        aliases_config: Dict[str, dict], national_countries: Dict[str, str]) -> dict:
    """ESPN teams envelope, each team with its ESPN fields plus aliases, competitionKey and country."""
    teams = []
    for team in espn_teams(teams_response):
        team_id = str(team.get("id"))
        teams.append({"team": {
            "id": team.get("id"),
            "displayName": team.get("displayName"),
            "abbreviation": team.get("abbreviation"),
            "color": _strip_hash(team.get("color")),
            "alternateColor": _strip_hash(team.get("alternateColor")),
            "competitionKey": competition_key,
            "country": team_country(team_id, competition_country, aliases_config, national_countries),
            "aliases": team_aliases(team_id, team.get("displayName") or "", aliases_config),
        }})
    return {"sports": [{"leagues": [{"teams": teams}]}]}
