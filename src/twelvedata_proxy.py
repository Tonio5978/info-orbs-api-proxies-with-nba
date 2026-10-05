import os
from fastapi import HTTPException, Request
from .common import (setup_logger, create_app, fetch_data, check_force_refresh, is_force_requested, handle_request,
                     TTLCache, make_cache_key, get_or_fetch, proxy_info)

logger = setup_logger("TWELVEDATA")
TWELVEDATA_API_BASE = "https://api.twelvedata.com/quote"
TWELVEDATA_DEFAULT_API_KEY = os.getenv("TWELVEDATA_DEFAULT_API_KEY")

# Cache configuration
CACHE = TTLCache.from_env_minutes("TWELVEDATA_PROXY_CACHE_LIFE")  # 0 disables caching

app = create_app("twelvedata_proxy", default_requests_per_minute=15, banner_title="TwelveData Service Configuration", banner_lines=lambda: [
    f"Cache lifetime: {CACHE.ttl_seconds // 60} minutes ({'enabled' if CACHE.enabled else 'disabled'})",
    "Force refresh: supported via &force=true parameter",
])


def transform_data(data: dict, cached: bool = False) -> dict:
    """Add proxy-info to the full TwelveData response"""
    if not data:
        raise HTTPException(status_code=502, detail="Empty API response")
    transformed = dict(data)
    transformed["proxy-info"] = proxy_info(cached)
    return transformed


async def proxy_endpoint(request: Request):
    symbol = request.query_params.get("symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol parameter is required")

    apikey = request.query_params.get("apikey") or TWELVEDATA_DEFAULT_API_KEY
    if not apikey:
        raise HTTPException(status_code=400, detail="API key is required and no default key is configured")

    params = {"symbol": symbol, "apikey": apikey}
    force_refresh = check_force_refresh(request, is_force_requested(request))

    async def fetch():
        logger.info(f"Fetching live data for symbol {symbol}{' (forced refresh)' if force_refresh else ''}")
        return await fetch_data(TWELVEDATA_API_BASE, logger, method="GET", params=params, app_name="twelvedata")

    data, cached = await get_or_fetch(CACHE, make_cache_key(params), fetch, logger, force=force_refresh)
    if cached:
        logger.info(f"Returning cached data for symbol {symbol}")
    return transform_data(data, cached=cached)


handle_request(app, logger, proxy_endpoint, methods=("GET",))
