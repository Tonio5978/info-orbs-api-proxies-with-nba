import os
from urllib.parse import quote
from fastapi import HTTPException, Request
from .common import (setup_logger, create_app, fetch_data, check_force_refresh, is_force_requested, handle_request,
                     TTLCache, make_cache_key, get_or_fetch, proxy_info)

logger = setup_logger("VISUALCROSSING")
VISUALCROSSING_API_BASE = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
VISUALCROSSING_DEFAULT_API_KEY = os.getenv("VISUALCROSSING_DEFAULT_API_KEY")

# Cache configuration
CACHE = TTLCache.from_env_minutes("VISUALCROSSING_PROXY_CACHE_LIFE")  # 0 disables caching

app = create_app("visualcrossing_proxy", banner_title="Visual Crossing Service Configuration", banner_lines=lambda: [
    f"Cache lifetime: {CACHE.ttl_seconds // 60} minutes ({'enabled' if CACHE.enabled else 'disabled'})",
    "Force refresh: supported via &force=true parameter",
])


def transform_data(data: dict, cached: bool = False) -> dict:
    """Keep only the fields used by the clients and add proxy-info"""
    return {
        "resolvedAddress": data.get("resolvedAddress"),
        "currentConditions": {
            "temp": data.get("currentConditions", {}).get("temp"),
            "icon": data.get("currentConditions", {}).get("icon")
        },
        "days": [
            {
                "description": day.get("description"),
                "icon": day.get("icon"),
                "tempmax": day.get("tempmax"),
                "tempmin": day.get("tempmin")
            }
            for day in data.get("days", [])
        ],
        "proxy-info": proxy_info(cached)
    }


async def proxy_endpoint(request: Request):
    location = request.path_params["location"]
    timeframe = request.path_params["timeframe"]
    if location in (".", "..") or timeframe in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid location or timeframe")

    api_key = request.query_params.get("key") or VISUALCROSSING_DEFAULT_API_KEY
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required and no default key is configured")

    params = {
        "key": api_key,
        "unitGroup": request.query_params.get("unitGroup", "us"),
        "include": request.query_params.get("include", "days,current"),
        "iconSet": request.query_params.get("iconSet", "icons1"),
        "lang": request.query_params.get("lang", "en")
    }
    force_refresh = check_force_refresh(request, is_force_requested(request))

    async def fetch():
        logger.info(f"Fetching live data for {location}/{timeframe}{' (forced refresh)' if force_refresh else ''}")
        # Encode path segments so they can't alter the upstream path
        url = f"{VISUALCROSSING_API_BASE}/{quote(location, safe=',')}/{quote(timeframe, safe=',')}"
        return await fetch_data(url, logger, method="GET", params=params, app_name="visualcrossing")

    # Location and timeframe are path parameters, so include them explicitly in the cache key
    cache_key = make_cache_key({**params, "location": location, "timeframe": timeframe})
    data, cached = await get_or_fetch(CACHE, cache_key, fetch, logger, force=force_refresh)
    if cached:
        logger.info(f"Returning cached data for {location}/{timeframe}")
    return transform_data(data, cached=cached)


handle_request(app, logger, proxy_endpoint, methods=("GET",), path="/proxy/{location}/{timeframe}")
