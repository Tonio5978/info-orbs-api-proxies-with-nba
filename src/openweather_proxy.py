import os
from fastapi import HTTPException, Request
from .common import (setup_logger, create_app, fetch_data, check_force_refresh, is_force_requested, handle_request,
                     TTLCache, make_cache_key, get_or_fetch, proxy_info)

logger = setup_logger("OPENWEATHER")
OPENWEATHER_API_BASE = "https://api.openweathermap.org/data/3.0/onecall"
OPENWEATHER_DEFAULT_API_KEY = os.getenv("OPENWEATHER_DEFAULT_API_KEY")

# Cache configuration
CACHE = TTLCache.from_env_minutes("OPENWEATHER_PROXY_CACHE_LIFE")  # 0 disables caching

app = create_app("openweather_proxy", banner_title="OpenWeather Service Configuration", banner_lines=lambda: [
    f"Cache lifetime: {CACHE.ttl_seconds // 60} minutes ({'enabled' if CACHE.enabled else 'disabled'})",
    "Force refresh: supported via &force=true parameter",
])


def transform_data(data: dict, cached: bool = False) -> dict:
    """Add proxy-info to the full OpenWeather response"""
    if not data:
        raise HTTPException(status_code=502, detail="Empty API response")
    transformed = dict(data)
    transformed["proxy-info"] = proxy_info(cached)
    return transformed


async def proxy_endpoint(request: Request):
    lat = request.query_params.get("lat")
    lon = request.query_params.get("lon")
    if not lat or not lon:
        raise HTTPException(status_code=400, detail="Latitude and longitude parameters are required")

    appid = request.query_params.get("appid") or OPENWEATHER_DEFAULT_API_KEY
    if not appid:
        raise HTTPException(status_code=400, detail="API key is required and no default key is configured")

    params = {
        "lat": lat,
        "lon": lon,
        "units": request.query_params.get("units", "imperial"),
        "exclude": request.query_params.get("exclude", "minutely,hourly,alerts"),
        "lang": request.query_params.get("lang", "en"),
        "cnt": request.query_params.get("cnt", "3"),
        "appid": appid
    }
    force_refresh = check_force_refresh(request, is_force_requested(request))

    async def fetch():
        logger.info(f"Fetching live data for location {lat},{lon}{' (forced refresh)' if force_refresh else ''}")
        return await fetch_data(OPENWEATHER_API_BASE, logger, method="GET", params=params, app_name="openweather")

    data, cached = await get_or_fetch(CACHE, make_cache_key(params), fetch, logger, force=force_refresh)
    if cached:
        logger.info(f"Returning cached data for location {lat},{lon}")
    return transform_data(data, cached=cached)


handle_request(app, logger, proxy_endpoint, methods=("GET",))
