from datetime import datetime, timezone
from typing import Dict, Optional
import os
import json
import sqlite3
from pathlib import Path
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from .common import setup_logger, create_app, fetch_data, handle_request

logger = setup_logger("TIMEZONE")
app = create_app("timezone_proxy")
TIME_API_BASE = "https://timeapi.io/api/timezone/zone"
CACHE_DB = Path("/var/cache/timezone_proxy/timezone_cache.db")  # More persistent location

# Ensure cache directory exists
CACHE_DB.parent.mkdir(parents=True, exist_ok=True)

# Initialize SQLite database
def init_db():
    conn = sqlite3.connect(str(CACHE_DB))
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS timezone_cache (
            timezone TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_timezone ON timezone_cache (timezone)
    """)
    conn.commit()
    conn.close()

init_db()

def get_cached_response(timezone: str) -> Optional[dict]:
    """Retrieve a cached response from the database"""
    try:
        conn = sqlite3.connect(str(CACHE_DB))
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM timezone_cache WHERE timezone = ?", (timezone,))
        result = cursor.fetchone()
        conn.close()
        return json.loads(result[0]) if result else None
    except Exception as e:
        logger.error(f"Error retrieving cache for {timezone}: {str(e)}")
        return None

def save_response_to_cache(timezone: str, data: dict):
    """Save a response to the cache database"""
    try:
        conn = sqlite3.connect(str(CACHE_DB))
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO timezone_cache (timezone, data)
            VALUES (?, ?)
        """, (timezone, json.dumps(data)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving cache for {timezone}: {str(e)}")

@app.on_event("startup")
async def startup_event():
    logger.info("="*50)
    logger.info(f"{'TimeZone Service Configuration':^50}")
    logger.info("="*50)
    logger.info(f"→ Rate limiting: {os.getenv('TIMEZONE_PROXY_REQUESTS_PER_MINUTE', '10')} requests/minute per IP")
    logger.info(f"→ Retry policy: {os.getenv('TIMEZONE_MAX_RETRIES', '3')} attempts with {os.getenv('TIMEZONE_RETRY_DELAY', '2')}s delay")
    logger.info(f"→ Cache database: {CACHE_DB}")
    logger.info("="*50 + "\n")

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

def should_bypass_cache(cached_data: dict) -> bool:
    if not cached_data.get("hasDayLightSaving") or not cached_data.get("dstInterval"):
        return False
    try:
        dst_data = cached_data["dstInterval"]
        change_time = parse_iso_datetime(
            dst_data["dstEnd"] if cached_data["isDayLightSavingActive"] else dst_data["dstStart"]
        )
        return datetime.now(timezone.utc) >= change_time
    except Exception as e:
        logger.warning(f"Cache validation failed: {str(e)}")
        return False

def create_response(original_data: dict, cached: bool, status_code: int = status.HTTP_200_OK):
    response = dict(original_data)
    next_update = None
    if original_data.get("hasDayLightSaving") and original_data.get("dstInterval"):
        try:
            dst_data = original_data["dstInterval"]
            change_str = dst_data["dstEnd"] if original_data["isDayLightSavingActive"] else dst_data["dstStart"]
            next_update = parse_iso_datetime(change_str).isoformat()
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
        timezone = request.query_params.get("timeZone")
        force = request.query_params.get("force", "").lower() == "true"
    else:
        body = await request.json()
        request_data = TimezoneRequest(**body)
        timezone = request_data.timeZone
        force = request_data.force

    if not timezone:
        raise HTTPException(
            status_code=400,
            detail={"error": "missing_parameter", "message": "timeZone parameter is required"}
        )

    if not force:
        cached_data = get_cached_response(timezone)
        if cached_data and not should_bypass_cache(cached_data):
            logger.info(f"Cache hit for {timezone}")
            return create_response(cached_data, True)

    url = f"{TIME_API_BASE}?timeZone={timezone}&futureChanges=true"
    raw_data = await fetch_data(url, logger, method="GET", app_name="timezone")
    save_response_to_cache(timezone, raw_data)
    logger.info(f"Data fetched for {timezone}")
    return create_response(raw_data, False)

handle_request(app, logger, proxy_endpoint)
