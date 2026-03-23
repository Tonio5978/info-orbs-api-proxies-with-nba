import os
from datetime import datetime, timedelta
from typing import Literal, Dict, Optional
import json
from fastapi import HTTPException, Request
from pydantic import BaseModel
from slowapi.util import get_remote_address
from .common import setup_logger, create_app, fetch_data

logger = setup_logger("VISUALCROSSING")
app = create_app("visualcrossing_proxy")
VISUALCROSSING_API_BASE = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
VISUALCROSSING_DEFAULT_API_KEY = os.getenv("VISUALCROSSING_DEFAULT_API_KEY")

# Cache configuration
CACHE_LIFE_MINUTES = int(os.getenv("VISUALCROSSING_PROXY_CACHE_LIFE", "5"))  # 0 disables caching
weather_cache: Dict[str, dict] = {}
cache_expiry: Dict[str, datetime] = {}

@app.on_event("startup")
async def startup_event():
    logger.info("="*50)
    logger.info(f"{'Visual Crossing Service Configuration':^50}")
    logger.info("="*50)
    logger.info(f"→ Rate limiting: {os.getenv('VISUALCROSSING_PROXY_REQUESTS_PER_MINUTE', '5')} requests/minute per IP")
    logger.info(f"→ Cache lifetime: {CACHE_LIFE_MINUTES} minutes ({'enabled' if CACHE_LIFE_MINUTES > 0 else 'disabled'})")
    logger.info("→ Force refresh: supported via &force=true parameter")
    logger.info("="*50 + "\n")

class WeatherRequest(BaseModel):
    location: str
    timeframe: str
    unit_group: Literal["us", "uk", "metric", "base"]
    include: str = "days,current"
    icon_set: str = "icons1"
    lang: str = "en"
    api_key: str

def transform_data(data: dict, cached: bool = False) -> dict:
    """Transform data and add proxy-info"""
    filtered_data = {
        "resolvedAddress": data.get("resolvedAddress"),
        "currentConditions": {
            "temp": data.get("currentConditions", {}).get("temp"),
            "icon": data.get("currentConditions", {}).get("icon")
        },
        "days": [],
        "proxy-info": {
            "cachedResponse": cached,
            "status_code": 200,
            "timestamp": datetime.utcnow().isoformat()
        }
    }

    if "days" in data:
        for day in data["days"]:
            filtered_day = {
                "description": day.get("description"),
                "icon": day.get("icon"),
                "tempmax": day.get("tempmax"),
                "tempmin": day.get("tempmin")
            }
            filtered_data["days"].append(filtered_day)

    return filtered_data

def get_cache_key(params: dict) -> str:
    """Generate a unique cache key from request parameters"""
    # Exclude 'force' from cache key since it doesn't affect the API response
    cache_params = params.copy()
    cache_params.pop('force', None)
    return json.dumps(cache_params, sort_keys=True)

async def proxy_endpoint(request: Request):
    # Get path parameters
    path_parts = request.url.path.split('/')
    if len(path_parts) < 4 or not path_parts[2] or not path_parts[3]:
        raise HTTPException(status_code=400, detail="Path must be /proxy/{location}/{timeframe}")
    
    location = path_parts[2]
    timeframe = path_parts[3]

    # Get query parameters
    unit_group = request.query_params.get("unitGroup", "us")
    include = request.query_params.get("include", "days,current")
    icon_set = request.query_params.get("iconSet", "icons1")
    lang = request.query_params.get("lang", "en")
    api_key = request.query_params.get("key")
    force_refresh = request.query_params.get("force", "").lower() == "true"
    
    if not api_key:
        if VISUALCROSSING_DEFAULT_API_KEY:
            api_key = VISUALCROSSING_DEFAULT_API_KEY
        else:
            raise HTTPException(status_code=400, detail="API key is required and no default key is configured")

    params = {
        "key": api_key,
        "unitGroup": unit_group,
        "include": include,
        "iconSet": icon_set,
        "lang": lang
    }
    cache_key = get_cache_key(params)
    
    # Check cache if enabled and not forcing refresh
    if CACHE_LIFE_MINUTES > 0 and not force_refresh:
        cached_data = weather_cache.get(cache_key)
        cache_valid = cache_expiry.get(cache_key, datetime.min) > datetime.utcnow()
        
        if cached_data and cache_valid:
            logger.info(f"Returning cached data for {location}/{timeframe}")
            return transform_data(cached_data, cached=True)

    # Fetch fresh data
    logger.info(f"Fetching live data for {location}/{timeframe}{' (forced refresh)' if force_refresh else ''}")
    try:
        url = f"{VISUALCROSSING_API_BASE}/{location}/{timeframe}"
        # Remove force parameter before making API call
        api_params = params.copy()
        api_params.pop('force', None)
        
        raw_data = await fetch_data(url, logger, method="GET", 
                                  params=api_params, app_name="visualcrossing")
        
        # Update cache if enabled
        if CACHE_LIFE_MINUTES > 0:
            weather_cache[cache_key] = raw_data
            cache_expiry[cache_key] = datetime.utcnow() + timedelta(minutes=CACHE_LIFE_MINUTES)
            logger.info(f"Cached data for {location}/{timeframe} for {CACHE_LIFE_MINUTES} minutes")
        
        return transform_data(raw_data, cached=False)
    except HTTPException as e:
        # If we have cached data and the API fails, return cached data (unless forcing refresh)
        if CACHE_LIFE_MINUTES > 0 and cache_key in weather_cache and not force_refresh:
            logger.warning(f"API failed, returning cached data for {location}/{timeframe}")
            return transform_data(weather_cache[cache_key], cached=True)
        raise e

# Custom route handler
@app.api_route("/proxy/{location}/{timeframe}", methods=["GET"])
@app.state.limiter.limit(os.getenv("VISUALCROSSING_PROXY_REQUESTS_PER_MINUTE", "5") + "/minute")
async def visualcrossing_proxy(request: Request):
    logger.info(f"{datetime.now().isoformat()} Received {request.method} request: {request.url} from {get_remote_address(request)}")
    return await proxy_endpoint(request)
