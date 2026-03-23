from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Dict, Optional
import os
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from .common import setup_logger, create_app, handle_request

logger = setup_logger("ZONEINFO")
app = create_app("zoneinfo_proxy")

@app.on_event("startup")
async def startup_event():
    logger.info("="*50)
    logger.info(f"{'ZoneInfo Service Configuration':^50}")
    logger.info("="*50)
    logger.info(f"→ Rate limiting: {os.getenv('ZONEINFO_PROXY_REQUESTS_PER_MINUTE', '10')} requests/minute per IP")
    logger.info("="*50 + "\n")

class TimezoneRequest(BaseModel):
    timeZone: str

def format_offset_nanoseconds(offset: timedelta) -> Dict:
    """Convert timedelta to detailed offset structure"""
    total_seconds = int(offset.total_seconds())
    return {
        "seconds": total_seconds,
        "milliseconds": total_seconds * 1000,
        "ticks": total_seconds * 10_000_000,  # 1 tick = 100ns
        "nanoseconds": total_seconds * 1_000_000_000
    }

def calculate_dst_interval(zone: ZoneInfo, now: datetime) -> Optional[Dict]:
    """Calculate DST interval details if applicable"""
    if not zone.dst(now):
        return None

    # Find next transition (approximation since zoneinfo doesn't expose transitions directly)
    dst_start, dst_end = None, None
    current_year = now.year
    
    # Try to find transitions by checking each day (simplified approach)
    for month in range(1, 13):
        for day in range(1, 29):
            test_date = datetime(current_year, month, day, tzinfo=zone)
            if zone.dst(test_date) and not zone.dst(test_date - timedelta(days=1)):
                dst_start = test_date
            elif not zone.dst(test_date) and zone.dst(test_date - timedelta(days=1)):
                dst_end = test_date
    
    if not dst_start or not dst_end:
        return None

    dst_offset = zone.dst(now)
    standard_offset = zone.utcoffset(now) - dst_offset

    return {
        "dstName": now.strftime("%Z"),
        "dstOffsetToUtc": format_offset_nanoseconds(zone.utcoffset(now)),
        "dstOffsetToStandardTime": format_offset_nanoseconds(dst_offset),
        "dstStart": dst_start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dstEnd": dst_end.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dstDuration": calculate_duration(dst_end - dst_start)
    }

def calculate_duration(delta: timedelta) -> Dict:
    """Calculate detailed duration structure"""
    total_ns = int(delta.total_seconds() * 1_000_000_000)
    return {
        "days": delta.days,
        "nanosecondOfDay": delta.seconds * 1_000_000_000 + delta.microseconds * 1000,
        "hours": delta.seconds // 3600,
        "minutes": (delta.seconds % 3600) // 60,
        "seconds": delta.seconds % 60,
        "milliseconds": delta.microseconds // 1000,
        "subsecondTicks": delta.microseconds * 10,  # 1 tick = 100ns
        "subsecondNanoseconds": delta.microseconds * 1000,
        "bclCompatibleTicks": int(delta.total_seconds() * 10_000_000),
        "totalDays": delta.total_seconds() / 86400,
        "totalHours": delta.total_seconds() / 3600,
        "totalMinutes": delta.total_seconds() / 60,
        "totalSeconds": delta.total_seconds(),
        "totalMilliseconds": delta.total_seconds() * 1000,
        "totalTicks": int(delta.total_seconds() * 10_000_000),
        "totalNanoseconds": total_ns
    }

def get_zoneinfo_data(timezone: str) -> Dict:
    """Get detailed timezone information using Python's zoneinfo module."""
    try:
        zone = ZoneInfo(timezone)
        now = datetime.now(zone)
        
        has_dst = zone.dst(now) is not None
        is_dst_active = has_dst and zone.dst(now).total_seconds() > 0
        standard_offset = zone.utcoffset(now) - (zone.dst(now) if zone.dst(now) else timedelta(0))

        return {
            "timeZone": timezone,
            "currentLocalTime": now.isoformat(),
            "currentUtcOffset": format_offset_nanoseconds(zone.utcoffset(now)),
            "standardUtcOffset": format_offset_nanoseconds(standard_offset),
            "hasDayLightSaving": has_dst,
            "isDayLightSavingActive": is_dst_active,
            "dstInterval": calculate_dst_interval(zone, now),
            "_cached_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "_source": "python-zoneinfo"
        }
    except ZoneInfoNotFoundError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_timezone", "message": str(e)}
        )

def create_response(original_data: dict, status_code: int = status.HTTP_200_OK):
    response = dict(original_data)
    next_update = None
    
    if original_data.get("dstInterval"):
        try:
            next_update = original_data["dstInterval"]["dstEnd"] if original_data["isDayLightSavingActive"] \
                         else original_data["dstInterval"]["dstStart"]
        except Exception as e:
            logger.warning(f"Failed to determine next update: {str(e)}")
    
    response["proxy-info"] = {
        "status_code": status_code,
        "cachedResponse": False,  # Always false since we don't cache
        "nextTimeZoneUpdate": next_update,
        "source": "python-zoneinfo"
    }
    return response

async def proxy_endpoint(request: Request):
    if request.method == "GET":
        timezone = request.query_params.get("timeZone")
    else:
        body = await request.json()
        request_data = TimezoneRequest(**body)
        timezone = request_data.timeZone

    if not timezone:
        raise HTTPException(
            status_code=400,
            detail={"error": "missing_parameter", "message": "timeZone parameter is required"}
        )

    zone_data = get_zoneinfo_data(timezone)
    return create_response(zone_data)

handle_request(app, logger, proxy_endpoint)
