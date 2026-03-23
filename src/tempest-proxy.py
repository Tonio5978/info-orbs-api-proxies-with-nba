import os
from datetime import datetime, timedelta
from typing import Literal, Dict, Optional
import json
from fastapi import HTTPException, Request
from pydantic import BaseModel
from .common import setup_logger, create_app, fetch_data, handle_request

logger = setup_logger("TEMPEST")
app = create_app("tempest_proxy")
WEATHER_API_BASE = "https://swd.weatherflow.com/swd/rest/better_forecast"
TEMPEST_DEFAULT_API_KEY = os.getenv("TEMPEST_DEFAULT_API_KEY")

# Cache configuration
CACHE_LIFE_MINUTES = int(os.getenv("TEMPEST_PROXY_CACHE_LIFE", "5"))  # 0 disables caching
weather_cache: Dict[str, dict] = {}
cache_expiry: Dict[str, datetime] = {}

@app.on_event("startup")
async def startup_event():
    logger.info("="*50)
    logger.info(f"{'TEMPEST Service Configuration':^50}")
    logger.info("="*50)
    logger.info(f"→ Rate limiting: {os.getenv('TEMPEST_PROXY_REQUESTS_PER_MINUTE', '5')} requests/minute per IP")
    logger.info(f"→ Cache lifetime: {CACHE_LIFE_MINUTES} minutes ({'enabled' if CACHE_LIFE_MINUTES > 0 else 'disabled'})")
    logger.info("→ Force refresh: supported via &force=true parameter")
    logger.info("="*50 + "\n")

class WeatherRequest(BaseModel):
    station_id: str
    units_temp: Literal["c", "f"]
    units_wind: Literal["mph", "kph", "m/s"]
    units_pressure: Literal["mb", "inHg"]
    units_precip: Literal["in", "mm"]
    units_distance: Literal["mi", "km"]
    api_key: str

def transform_data(data: dict, cached: bool = False) -> dict:
    """Transform data and add proxy-info"""
    filtered_data = {
        "current_conditions": {},
        "forecast": {"daily": []},
        "proxy-info": {
            "cachedResponse": cached,
            "status_code": 200,
            "timestamp": datetime.utcnow().isoformat()
        }
    }

    if "current_conditions" in data:
        cc = data["current_conditions"]
        filtered_data["current_conditions"] = {
            "air_temperature": cc.get("air_temperature"),
            "icon": cc.get("icon"),
            "conditions": cc.get("conditions"),
            "feels_like": cc.get("feels_like"),
            "relative_humidity": cc.get("relative_humidity"),
            "station_pressure": cc.get("station_pressure"),
            "precip_probability": cc.get("precip_probability"),
            "wind_gust": cc.get("wind_gust")
        }

    if "forecast" in data and "daily" in data["forecast"]:
        for daily_forecast in data["forecast"]["daily"][:4]:
            filtered_daily = {
                "day_start_local": daily_forecast.get("day_start_local"),
                "air_temp_high": daily_forecast.get("air_temp_high"),
                "air_temp_low": daily_forecast.get("air_temp_low"),
                "conditions": daily_forecast.get("conditions"),
                "day_num": daily_forecast.get("day_num"),
                "month_num": daily_forecast.get("month_num"),
                "precip_probability": daily_forecast.get("precip_probability"),
                "precip_type": daily_forecast.get("precip_type"),
                "icon": daily_forecast.get("icon"),
                "precip_icon": daily_forecast.get("precip_icon")
            }
            filtered_data["forecast"]["daily"].append(filtered_daily)

    return filtered_data

def get_cache_key(params: dict) -> str:
    """Generate a unique cache key from request parameters"""
    # Exclude 'force' from cache key since it doesn't affect the API response
    cache_params = params.copy()
    cache_params.pop('force', None)
    return json.dumps(cache_params, sort_keys=True)

async def proxy_endpoint(request: Request):
    force_refresh = request.query_params.get("force", "").lower() == "true"
    
    if request.method == "GET":
        station_id = request.query_params.get("station_id")
        units_temp = request.query_params.get("units_temp")
        units_wind = request.query_params.get("units_wind")
        units_pressure = request.query_params.get("units_pressure")
        units_precip = request.query_params.get("units_precip")
        units_distance = request.query_params.get("units_distance")
        api_key = request.query_params.get("api_key")
        
        if not all([station_id, units_temp, units_wind, units_pressure, 
                   units_precip, units_distance]):
            raise HTTPException(status_code=400, detail="Missing required query parameters")
        
        if not api_key:
            if TEMPEST_DEFAULT_API_KEY:
                api_key = TEMPEST_DEFAULT_API_KEY
            else:
                raise HTTPException(status_code=400, detail="API key is required and no default key is configured")
        
        request_data = {
            "station_id": station_id,
            "units_temp": units_temp,
            "units_wind": units_wind,
            "units_pressure": units_pressure,
            "units_precip": units_precip,
            "units_distance": units_distance,
            "api_key": api_key
        }
    else:
        body = await request.json()
        request_data = body
        if not request_data.get("api_key"):
            if TEMPEST_DEFAULT_API_KEY:
                request_data["api_key"] = TEMPEST_DEFAULT_API_KEY
            else:
                raise HTTPException(status_code=400, detail="API key is required and no default key is configured")

    params = request_data.copy()
    cache_key = get_cache_key(params)
    
    # Check cache if enabled and not forcing refresh
    if CACHE_LIFE_MINUTES > 0 and not force_refresh:
        cached_data = weather_cache.get(cache_key)
        cache_valid = cache_expiry.get(cache_key, datetime.min) > datetime.utcnow()
        
        if cached_data and cache_valid:
            logger.info(f"Returning cached data for station {request_data['station_id']}")
            return transform_data(cached_data, cached=True)

    # Fetch fresh data
    logger.info(f"Fetching live data for station {request_data['station_id']}{' (forced refresh)' if force_refresh else ''}")
    try:
        # Remove force parameter before making API call
        api_params = params.copy()
        api_params.pop('force', None)
        
        raw_data = await fetch_data(WEATHER_API_BASE, logger, method="GET", 
                                  params=api_params, app_name="tempest")
        
        # Update cache if enabled
        if CACHE_LIFE_MINUTES > 0:
            weather_cache[cache_key] = raw_data
            cache_expiry[cache_key] = datetime.utcnow() + timedelta(minutes=CACHE_LIFE_MINUTES)
            logger.info(f"Cached data for station {request_data['station_id']} for {CACHE_LIFE_MINUTES} minutes")
        
        return transform_data(raw_data, cached=False)
    except HTTPException as e:
        # If we have cached data and the API fails, return cached data (unless forcing refresh)
        if CACHE_LIFE_MINUTES > 0 and cache_key in weather_cache and not force_refresh:
            logger.warning(f"API failed, returning cached data for station {request_data['station_id']}")
            return transform_data(weather_cache[cache_key], cached=True)
        raise e

handle_request(app, logger, proxy_endpoint)
