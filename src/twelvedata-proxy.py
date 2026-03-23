import os
from datetime import datetime, timedelta
from typing import Dict, Optional
import json
from fastapi import HTTPException, Request
from pydantic import BaseModel
from slowapi.util import get_remote_address
from .common import setup_logger, create_app, fetch_data

logger = setup_logger("TWELVEDATA")
app = create_app("twelvedata_proxy")
TWELVEDATA_API_BASE = "https://api.twelvedata.com/quote"
TWELVEDATA_DEFAULT_API_KEY = os.getenv("TWELVEDATA_DEFAULT_API_KEY")

# Cache configuration
CACHE_LIFE_MINUTES = int(os.getenv("TWELVEDATA_PROXY_CACHE_LIFE", "5"))  # 0 disables caching
quote_cache: Dict[str, dict] = {}
cache_expiry: Dict[str, datetime] = {}

@app.on_event("startup")
async def startup_event():
    logger.info("="*50)
    logger.info(f"{'TwelveData Service Configuration':^50}")
    logger.info("="*50)
    logger.info(f"→ Rate limiting: {os.getenv('TWELVEDATA_PROXY_REQUESTS_PER_MINUTE', '15')} requests/minute per IP")
    logger.info(f"→ Cache lifetime: {CACHE_LIFE_MINUTES} minutes ({'enabled' if CACHE_LIFE_MINUTES > 0 else 'disabled'})")
    logger.info("→ Force refresh: supported via &force=true parameter")
    logger.info("="*50 + "\n")

class QuoteRequest(BaseModel):
    symbol: str
    apikey: str

def transform_data(data: dict, cached: bool = False) -> dict:
    """Add proxy-info to the full TwelveData response"""
    if not data:
        raise HTTPException(status_code=502, detail="Empty API response")
    
    # Include the full original response plus proxy-info
    transformed = dict(data)
    transformed["proxy-info"] = {
        "cachedResponse": cached,
        "status_code": 200,
        "timestamp": datetime.utcnow().isoformat()
    }
    return transformed

def get_cache_key(params: dict) -> str:
    """Generate a unique cache key from request parameters"""
    # Exclude 'force' from cache key since it doesn't affect the API response
    cache_params = params.copy()
    cache_params.pop('force', None)
    return json.dumps(cache_params, sort_keys=True)

async def proxy_endpoint(request: Request):
    # Get query parameters
    symbol = request.query_params.get("symbol")
    apikey = request.query_params.get("apikey")
    force_refresh = request.query_params.get("force", "").lower() == "true"
    
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol parameter is required")
    
    if not apikey:
        if TWELVEDATA_DEFAULT_API_KEY:
            apikey = TWELVEDATA_DEFAULT_API_KEY
        else:
            raise HTTPException(status_code=400, detail="API key is required and no default key is configured")

    params = {
        "symbol": symbol,
        "apikey": apikey
    }
    cache_key = get_cache_key(params)
    
    # Check cache if enabled and not forcing refresh
    if CACHE_LIFE_MINUTES > 0 and not force_refresh:
        cached_data = quote_cache.get(cache_key)
        cache_valid = cache_expiry.get(cache_key, datetime.min) > datetime.utcnow()
        
        if cached_data and cache_valid:
            logger.info(f"Returning cached data for symbol {symbol}")
            return transform_data(cached_data, cached=True)

    # Fetch fresh data
    logger.info(f"Fetching live data for symbol {symbol}{' (forced refresh)' if force_refresh else ''}")
    try:
        # Remove force parameter before making API call
        api_params = params.copy()
        api_params.pop('force', None)
        
        raw_data = await fetch_data(TWELVEDATA_API_BASE, logger, method="GET", 
                                  params=api_params, app_name="twelvedata")
        
        # Update cache if enabled
        if CACHE_LIFE_MINUTES > 0:
            quote_cache[cache_key] = raw_data
            cache_expiry[cache_key] = datetime.utcnow() + timedelta(minutes=CACHE_LIFE_MINUTES)
            logger.info(f"Cached data for symbol {symbol} for {CACHE_LIFE_MINUTES} minutes")
        
        return transform_data(raw_data, cached=False)
    except HTTPException as e:
        # If we have cached data and the API fails, return cached data (unless forcing refresh)
        if CACHE_LIFE_MINUTES > 0 and cache_key in quote_cache and not force_refresh:
            logger.warning(f"API failed, returning cached data for symbol {symbol}")
            return transform_data(quote_cache[cache_key], cached=True)
        raise e

# Custom route handler
@app.api_route("/proxy", methods=["GET"])
@app.state.limiter.limit(os.getenv("TWELVEDATA_PROXY_REQUESTS_PER_MINUTE", "15") + "/minute")
async def twelvedata_proxy(request: Request):
    logger.info(f"{datetime.now().isoformat()} Received {request.method} request: {request.url} from {get_remote_address(request)}")
    return await proxy_endpoint(request)
