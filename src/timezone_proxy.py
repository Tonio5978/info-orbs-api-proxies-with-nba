import asyncio
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import HTTPException, Request, status
from pydantic import BaseModel
from .common import setup_logger, create_app, fetch_data, handle_request, check_force_refresh

logger = setup_logger("TIMEZONE")
TIME_API_BASE = "https://timeapi.io/api/timezone/zone"
CACHE_DB = Path(os.getenv("TIMEZONE_CACHE_DB", "/var/cache/timezone_proxy/timezone_cache.db"))

app = create_app("timezone_proxy", banner_title="TimeZone Service Configuration", banner_lines=lambda: [
    f"Retry policy: {os.getenv('TIMEZONE_MAX_RETRIES', '0')} retries with {os.getenv('TIMEZONE_RETRY_DELAY', '0')}s delay",
    f"Cache database: {CACHE_DB}",
])


def init_db():
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(CACHE_DB))) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS timezone_cache (
                timezone TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timezone ON timezone_cache (timezone)")


init_db()


def get_cached_response(tz_name: str) -> Optional[dict]:
    """Retrieve a cached response from the database"""
    try:
        with closing(sqlite3.connect(str(CACHE_DB))) as conn:
            row = conn.execute("SELECT data FROM timezone_cache WHERE timezone = ?", (tz_name,)).fetchone()
        return json.loads(row[0]) if row else None
    except Exception as e:
        logger.error(f"Error retrieving cache for {tz_name}: {str(e)}")
        return None


def save_response_to_cache(tz_name: str, data: dict):
    """Save a response to the cache database"""
    try:
        with closing(sqlite3.connect(str(CACHE_DB))) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO timezone_cache (timezone, data) VALUES (?, ?)", (tz_name, json.dumps(data)))
    except Exception as e:
        logger.error(f"Error saving cache for {tz_name}: {str(e)}")


class TimezoneRequest(BaseModel):
    timeZone: str
    force: Optional[bool] = False


def parse_iso_datetime(dt_str: str) -> datetime:
    if dt_str.endswith('Z'):
        dt_str = dt_str[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        if '.' in dt_str:
            return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S.%f%z")
        return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S%z")


def get_next_change(data: dict) -> Optional[datetime]:
    """Next DST transition (end of DST if active, otherwise its start)"""
    if not data.get("hasDayLightSaving") or not data.get("dstInterval"):
        return None
    dst_data = data["dstInterval"]
    return parse_iso_datetime(dst_data["dstEnd"] if data["isDayLightSavingActive"] else dst_data["dstStart"])


def should_bypass_cache(cached_data: dict) -> bool:
    """Cached data is outdated once the DST transition it describes has passed"""
    try:
        change_time = get_next_change(cached_data)
        return change_time is not None and datetime.now(timezone.utc) >= change_time
    except Exception as e:
        logger.warning(f"Cache validation failed: {str(e)}")
        return False


def create_response(original_data: dict, cached: bool, status_code: int = status.HTTP_200_OK):
    response = dict(original_data)
    next_update = None
    try:
        change_time = get_next_change(original_data)
        next_update = change_time.isoformat() if change_time else None
    except Exception as e:
        logger.warning(f"Failed to calculate next update: {str(e)}")

    response["proxy-info"] = {
        "status_code": status_code,
        "cachedResponse": cached,
        "nextTimeZoneUpdate": next_update
    }
    return response


async def proxy_endpoint(request: Request):
    if request.method == "GET":
        tz_name = request.query_params.get("timeZone")
        force = request.query_params.get("force", "").lower() == "true"
    else:
        request_data = TimezoneRequest(**(await request.json()))
        tz_name = request_data.timeZone
        force = request_data.force

    if not tz_name:
        raise HTTPException(
            status_code=400,
            detail={"error": "missing_parameter", "message": "timeZone parameter is required"}
        )

    force = check_force_refresh(request, bool(force))
    if not force:
        # SQLite is blocking: run it in a worker thread to keep the event loop free
        cached_data = await asyncio.to_thread(get_cached_response, tz_name)
        if cached_data and not should_bypass_cache(cached_data):
            logger.info(f"Cache hit for {tz_name}")
            return create_response(cached_data, True)

    # Pass the timezone as an encoded query parameter so it can't inject extra parameters
    raw_data = await fetch_data(TIME_API_BASE, logger, method="GET",
                                params={"timeZone": tz_name, "futureChanges": "true"}, app_name="timezone")
    await asyncio.to_thread(save_response_to_cache, tz_name, raw_data)
    logger.info(f"Data fetched for {tz_name}")
    return create_response(raw_data, False)


handle_request(app, logger, proxy_endpoint)
