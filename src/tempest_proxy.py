import os
from fastapi import HTTPException, Request
from .common import (setup_logger, create_app, fetch_data, check_force_refresh, is_force_requested, handle_request,
                     TTLCache, make_cache_key, get_or_fetch, proxy_info)

logger = setup_logger("TEMPEST")
WEATHER_API_BASE = "https://swd.weatherflow.com/swd/rest/better_forecast"
TEMPEST_DEFAULT_API_KEY = os.getenv("TEMPEST_DEFAULT_API_KEY")
REQUIRED_PARAMS = ["station_id", "units_temp", "units_wind", "units_pressure", "units_precip", "units_distance"]

# Cache configuration
CACHE = TTLCache.from_env_minutes("TEMPEST_PROXY_CACHE_LIFE")  # 0 disables caching

app = create_app("tempest_proxy", banner_title="TEMPEST Service Configuration", banner_lines=lambda: [
    f"Cache lifetime: {CACHE.ttl_seconds // 60} minutes ({'enabled' if CACHE.enabled else 'disabled'})",
    "Force refresh: supported via &force=true parameter",
])

CURRENT_FIELDS = ["air_temperature", "icon", "conditions", "feels_like", "relative_humidity",
                  "station_pressure", "precip_probability", "wind_gust"]
DAILY_FIELDS = ["day_start_local", "air_temp_high", "air_temp_low", "conditions", "day_num", "month_num",
                "precip_probability", "precip_type", "icon", "precip_icon"]


def transform_data(data: dict, cached: bool = False) -> dict:
    """Keep the current conditions and a 4-day forecast, and add proxy-info"""
    filtered_data = {
        "current_conditions": {},
        "forecast": {"daily": []},
        "proxy-info": proxy_info(cached)
    }
    if "current_conditions" in data:
        cc = data["current_conditions"]
        filtered_data["current_conditions"] = {field: cc.get(field) for field in CURRENT_FIELDS}
    if "forecast" in data and "daily" in data["forecast"]:
        filtered_data["forecast"]["daily"] = [
            {field: daily.get(field) for field in DAILY_FIELDS}
            for daily in data["forecast"]["daily"][:4]
        ]
    return filtered_data


async def proxy_endpoint(request: Request):
    if request.method == "GET":
        request_data = {name: request.query_params.get(name) for name in REQUIRED_PARAMS}
        if not all(request_data.values()):
            raise HTTPException(status_code=400, detail="Missing required query parameters")
        request_data["api_key"] = request.query_params.get("api_key")
    else:
        request_data = dict(await request.json())

    request_data["api_key"] = request_data.get("api_key") or TEMPEST_DEFAULT_API_KEY
    if not request_data["api_key"]:
        raise HTTPException(status_code=400, detail="API key is required and no default key is configured")
    request_data.pop("force", None)
    station_id = request_data.get("station_id")
    force_refresh = check_force_refresh(request, is_force_requested(request))

    async def fetch():
        logger.info(f"Fetching live data for station {station_id}{' (forced refresh)' if force_refresh else ''}")
        return await fetch_data(WEATHER_API_BASE, logger, method="GET", params=request_data, app_name="tempest")

    data, cached = await get_or_fetch(CACHE, make_cache_key(request_data), fetch, logger, force=force_refresh)
    if cached:
        logger.info(f"Returning cached data for station {station_id}")
    return transform_data(data, cached=cached)


handle_request(app, logger, proxy_endpoint)
