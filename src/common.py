import logging
import sys
import os
import asyncio
from typing import Callable, Optional
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded


def setup_logger(app_name: str) -> logging.Logger:
    """Set up a logger with an app-specific prefix for both app and access logs."""
    # Get the base Uvicorn logger
    logger = logging.getLogger("uvicorn")
    logger.handlers.clear()  # Clear any existing handlers

    # Create and configure a handler with the app-specific prefix
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(f"{app_name}:%(levelname)s:%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    # Ensure Uvicorn's access logger uses the same configuration
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()  # Clear default access handlers
    access_logger.addHandler(handler)  # Use the same handler with app prefix
    access_logger.setLevel(logging.INFO)

    return logger


def create_app(app_name: str, rate_limit: str = None) -> FastAPI:
    """Create a FastAPI app with rate limiting and middleware."""
    app = FastAPI(title=app_name)
    default_rate_limit = rate_limit or os.getenv(f"{app_name.upper()}_REQUESTS_PER_MINUTE", "5") + "/minute"
    limiter = Limiter(key_func=get_remote_address, default_limits=[default_rate_limit])
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
        retry_after = 60
        limit = default_rate_limit
        try:
            detail = exc.detail if hasattr(exc, 'detail') else str(exc)
            if hasattr(detail, 'retry_after'):
                retry_after = detail.retry_after
            if isinstance(detail, str) and "per" in detail:
                limit = detail.split(":")[-1].strip()
        except Exception as e:
            logger.error(f"Error parsing rate limit details: {str(e)}")
        
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "message": f"Try again in {retry_after} seconds",
                "limit": limit
            },
            headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": limit}
        )

    return app


async def fetch_data(
    url: str,
    logger: logging.Logger,
    method: str = "GET",
    params: dict = None,
    json: dict = None,
    timeout: int = 10,
    app_name: str = ""
) -> dict:
    """Generic function to fetch data from an API with optional retries."""
    logger.info(f"Sending {method} request to {url} with params={params} json={json}")
    
    max_retries = int(os.getenv(f"{app_name.upper()}_MAX_RETRIES", "0"))
    retry_delay = int(os.getenv(f"{app_name.upper()}_RETRY_DELAY", "0"))
    
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == "GET":
                    response = await client.get(url, params=params)
                elif method == "POST":
                    response = await client.post(url, json=json)
                else:
                    raise ValueError(f"Unsupported method: {method}")
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            last_error = e
            if max_retries > 0 and e.response.status_code == 502 and attempt < max_retries:
                logger.warning(f"502 Bad Gateway - Attempt {attempt + 1}/{max_retries + 1}")
                await asyncio.sleep(retry_delay)
                continue
            raise HTTPException(status_code=e.response.status_code, detail=f"API error: {e.response.text}")
        except httpx.RequestError as e:
            last_error = e
            if max_retries > 0 and attempt < max_retries:
                logger.warning(f"Network error - Attempt {attempt + 1}/{max_retries + 1}")
                await asyncio.sleep(retry_delay)
                continue
            raise HTTPException(status_code=502, detail=f"Proxy error: {str(e)}")
    raise last_error if last_error else HTTPException(502, "Unknown proxy error")


def handle_request(
    app: FastAPI,
    logger: logging.Logger,
    endpoint_func: Callable,
    rate_limit: str = None
):
    """Decorator to handle GET/POST requests with logging and rate limiting."""
    limit = rate_limit or os.getenv(f"{app.title.upper()}_REQUESTS_PER_MINUTE", "5") + "/minute"
    
    @app.api_route("/proxy", methods=["GET", "POST"])
    @app.state.limiter.limit(limit)
    async def proxy_request(request: Request):
        logger.info(f"{datetime.now().isoformat()} Received {request.method} request: {request.url} from {get_remote_address(request)}")
        return await endpoint_func(request)
    
    return proxy_request
