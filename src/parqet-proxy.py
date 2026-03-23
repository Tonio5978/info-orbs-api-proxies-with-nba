import os
from datetime import datetime, timedelta
from typing import Literal, Dict, Optional
import json
from fastapi import HTTPException, Request
from pydantic import BaseModel
from .common import setup_logger, create_app, fetch_data, handle_request

logger = setup_logger("PARQET")
app = create_app("parqet_proxy")
PARQET_API_BASE = "https://api.parqet.com/v1/portfolios/assemble?useInclude=true&include=ttwror&include=performance_charts&resolution=200"

# Cache configuration
CACHE_LIFE_MINUTES = int(os.getenv("PARQET_PROXY_CACHE_LIFE", "5"))  # 0 disables caching
portfolio_cache: Dict[str, dict] = {}
cache_expiry: Dict[str, datetime] = {}

@app.on_event("startup")
async def startup_event():
    logger.info("="*50)
    logger.info(f"{'Parqet Service Configuration':^50}")
    logger.info("="*50)
    logger.info(f"→ Rate limiting: {os.getenv('PARQET_PROXY_REQUESTS_PER_MINUTE', '5')} requests/minute per IP")
    logger.info(f"→ Cache lifetime: {CACHE_LIFE_MINUTES} minutes ({'enabled' if CACHE_LIFE_MINUTES > 0 else 'disabled'})")
    logger.info("→ Force refresh: supported via &force=true parameter")
    logger.info("="*50 + "\n")

class PortfolioRequest(BaseModel):
    id: str
    timeframe: Literal["today", "1d", "1w", "1m", "3m", "6m", "1y", "5y", "10y", "mtd", "ytd", "max"]
    perf: Literal["returnGross", "returnNet", "totalReturnGross", "totalReturnNet", "ttwror", "izf"]
    perfChart: Literal["perfHistory", "perfHistoryUnrealized", "ttwror", "drawdown"]

def transform_data(data: dict, perf: str, perf_chart: str, cached: bool = False) -> dict:
    """Transform data and add proxy-info"""
    filtered_data = {
        "holdings": [],
        "performance": {},
        "chart": [],
        "proxy-info": {
            "cachedResponse": cached,
            "status_code": 200,
            "timestamp": datetime.utcnow().isoformat()
        }
    }

    if "holdings" in data:
        for holding in data["holdings"]:
            asset_type = holding.get("assetType", "").lower()
            if asset_type not in ["security", "crypto"]:
                continue
            is_sold = holding.get("position", {}).get("isSold")
            shares = holding.get("position", {}).get("shares")
            if is_sold or shares == 0:
                continue
            filtered_holding = {
                "assetType": asset_type,
                "currency": holding.get("currency"),
                "id": holding.get("asset", {}).get("identifier"),
                "name": holding.get("sharedAsset", {}).get("name"),
                "priceStart": holding.get("performance", {}).get("priceAtIntervalStart"),
                "valueStart": holding.get("performance", {}).get("purchaseValueForInterval"),
                "priceNow": holding.get("position", {}).get("currentPrice"),
                "valueNow": holding.get("position", {}).get("currentValue"),
                "shares": holding.get("position", {}).get("shares"),
                "perf": data.get("performance", {}).get(perf, 0)
            }
            filtered_data["holdings"].append(filtered_holding)

    performance_data = data.get("performance", {})
    filtered_data["performance"] = {
        "valueStart": performance_data.get("purchaseValueForInterval"),
        "valueNow": performance_data.get("value"),
        "perf": performance_data.get(perf, 0)
    }

    if "charts" in data:
        first = True
        for chart in data["charts"]:
            if first:
                first = False
                continue
            filtered_data["chart"].append(chart.get("values", {}).get(perf_chart, 0))

    return filtered_data

def get_cache_key(request_data: dict) -> str:
    """Generate a unique cache key from request parameters"""
    return json.dumps(request_data.dict(), sort_keys=True)

async def proxy_endpoint(request: Request):
    force_refresh = request.query_params.get("force", "").lower() == "true"
    
    if request.method == "GET":
        id = request.query_params.get("id")
        timeframe = request.query_params.get("timeframe")
        perf = request.query_params.get("perf")
        perf_chart = request.query_params.get("perfChart")
        if not all([id, timeframe, perf, perf_chart]):
            raise HTTPException(status_code=400, detail="Missing required query parameters")
        request_data = PortfolioRequest(id=id, timeframe=timeframe, perf=perf, perfChart=perf_chart)
    else:
        body = await request.json()
        request_data = PortfolioRequest(**body)

    cache_key = get_cache_key(request_data)
    
    # Check cache if enabled and not forcing refresh
    if CACHE_LIFE_MINUTES > 0 and not force_refresh:
        cached_data = portfolio_cache.get(cache_key)
        cache_valid = cache_expiry.get(cache_key, datetime.min) > datetime.utcnow()
        
        if cached_data and cache_valid:
            logger.info(f"Returning cached data for portfolio {request_data.id}")
            return transform_data(cached_data, request_data.perf, request_data.perfChart, cached=True)

    # Fetch fresh data
    logger.info(f"Fetching live data for portfolio {request_data.id}{' (forced refresh)' if force_refresh else ''}")
    try:
        payload = {
            "portfolioIds": [request_data.id],
            "holdingIds": [],
            "assetTypes": [],
            "timeframe": request_data.timeframe
        }
        raw_data = await fetch_data(PARQET_API_BASE, logger, method="POST", json=payload, app_name="parqet")
        
        # Update cache if enabled
        if CACHE_LIFE_MINUTES > 0:
            portfolio_cache[cache_key] = raw_data
            cache_expiry[cache_key] = datetime.utcnow() + timedelta(minutes=CACHE_LIFE_MINUTES)
            logger.info(f"Cached data for portfolio {request_data.id} for {CACHE_LIFE_MINUTES} minutes")
        
        return transform_data(raw_data, request_data.perf, request_data.perfChart, cached=False)
    except HTTPException as e:
        # If we have cached data and the API fails, return cached data (unless forcing refresh)
        if CACHE_LIFE_MINUTES > 0 and cache_key in portfolio_cache and not force_refresh:
            logger.warning(f"API failed, returning cached data for portfolio {request_data.id}")
            return transform_data(portfolio_cache[cache_key], request_data.perf, request_data.perfChart, cached=True)
        raise e

handle_request(app, logger, proxy_endpoint)
