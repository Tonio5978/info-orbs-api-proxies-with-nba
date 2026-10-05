from typing import Literal
from fastapi import HTTPException, Request
from pydantic import BaseModel
from .common import (setup_logger, create_app, fetch_data, check_force_refresh, is_force_requested, handle_request,
                     TTLCache, make_cache_key, get_or_fetch, proxy_info)

logger = setup_logger("PARQET")
PARQET_API_BASE = "https://api.parqet.com/v1/portfolios/assemble?useInclude=true&include=ttwror&include=performance_charts&resolution=200"

# Cache configuration
CACHE = TTLCache.from_env_minutes("PARQET_PROXY_CACHE_LIFE")  # 0 disables caching

app = create_app("parqet_proxy", banner_title="Parqet Service Configuration", banner_lines=lambda: [
    f"Cache lifetime: {CACHE.ttl_seconds // 60} minutes ({'enabled' if CACHE.enabled else 'disabled'})",
    "Force refresh: supported via &force=true parameter",
])


class PortfolioRequest(BaseModel):
    id: str
    timeframe: Literal["today", "1d", "1w", "1m", "3m", "6m", "1y", "5y", "10y", "mtd", "ytd", "max"]
    perf: Literal["returnGross", "returnNet", "totalReturnGross", "totalReturnNet", "ttwror", "izf"]
    perfChart: Literal["perfHistory", "perfHistoryUnrealized", "ttwror", "drawdown"]


def transform_data(data: dict, perf: str, perf_chart: str, cached: bool = False) -> dict:
    """Keep open security/crypto holdings, overall performance and the chart, and add proxy-info"""
    filtered_data = {
        "holdings": [],
        "performance": {},
        "chart": [],
        "proxy-info": proxy_info(cached)
    }

    for holding in data.get("holdings", []):
        asset_type = holding.get("assetType", "").lower()
        if asset_type not in ["security", "crypto"]:
            continue
        position = holding.get("position", {})
        if position.get("isSold") or position.get("shares") == 0:
            continue
        filtered_data["holdings"].append({
            "assetType": asset_type,
            "currency": holding.get("currency"),
            "id": holding.get("asset", {}).get("identifier"),
            "name": holding.get("sharedAsset", {}).get("name"),
            "priceStart": holding.get("performance", {}).get("priceAtIntervalStart"),
            "valueStart": holding.get("performance", {}).get("purchaseValueForInterval"),
            "priceNow": position.get("currentPrice"),
            "valueNow": position.get("currentValue"),
            "shares": position.get("shares"),
            "perf": data.get("performance", {}).get(perf, 0)
        })

    performance_data = data.get("performance", {})
    filtered_data["performance"] = {
        "valueStart": performance_data.get("purchaseValueForInterval"),
        "valueNow": performance_data.get("value"),
        "perf": performance_data.get(perf, 0)
    }

    # The first chart point is skipped
    filtered_data["chart"] = [chart.get("values", {}).get(perf_chart, 0) for chart in data.get("charts", [])[1:]]

    return filtered_data


async def proxy_endpoint(request: Request):
    force_refresh = check_force_refresh(request, is_force_requested(request))

    if request.method == "GET":
        params = {name: request.query_params.get(name) for name in ["id", "timeframe", "perf", "perfChart"]}
        if not all(params.values()):
            raise HTTPException(status_code=400, detail="Missing required query parameters")
        request_data = PortfolioRequest(**params)
    else:
        request_data = PortfolioRequest(**(await request.json()))

    async def fetch():
        logger.info(f"Fetching live data for portfolio {request_data.id}{' (forced refresh)' if force_refresh else ''}")
        payload = {
            "portfolioIds": [request_data.id],
            "holdingIds": [],
            "assetTypes": [],
            "timeframe": request_data.timeframe
        }
        return await fetch_data(PARQET_API_BASE, logger, method="POST", json=payload, app_name="parqet")

    data, cached = await get_or_fetch(CACHE, make_cache_key(request_data.model_dump()), fetch, logger, force=force_refresh)
    if cached:
        logger.info(f"Returning cached data for portfolio {request_data.id}")
    return transform_data(data, request_data.perf, request_data.perfChart, cached=cached)


handle_request(app, logger, proxy_endpoint)
