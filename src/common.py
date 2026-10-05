import logging
import sys
import os
import asyncio
from typing import Callable, Optional
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from limits import parse as parse_limit
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded


# Query/body parameter names whose values must never appear in logs
SENSITIVE_PARAMS = {"appid", "apikey", "api_key", "key", "token", "access_token"}


def redact_params(params: Optional[dict]) -> Optional[dict]:
    """Return a copy of params with sensitive values masked."""
    if not params:
        return params
    return {k: ("***" if k.lower() in SENSITIVE_PARAMS else v) for k, v in params.items()}


def redact_url(url) -> str:
    """Return the URL with sensitive query parameter values masked."""
    parts = urlsplit(str(url))
    if not parts.query:
        return str(url)
    query = urlencode(
        [(k, "***" if k.lower() in SENSITIVE_PARAMS else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)],
        safe="*",
    )
    return urlunsplit(parts._replace(query=query))


# Separate, stricter limit for ?force=true, which bypasses the cache and always hits the upstream API
FORCE_REFRESH_LIMIT = parse_limit(os.getenv("FORCE_REFRESH_PER_MINUTE", "2") + "/minute")
_force_limiter = MovingWindowRateLimiter(MemoryStorage())


def check_force_refresh(request: Request, requested: bool) -> bool:
    """Return True if a forced refresh is requested and allowed, raise 429 if the client exceeds the force limit."""
    if not requested:
        return False
    if not _force_limiter.hit(FORCE_REFRESH_LIMIT, "force", get_remote_address(request)):
        raise HTTPException(
            status_code=429,
            detail=f"Too many forced refreshes, limit is {FORCE_REFRESH_LIMIT}. Retry without force=true to get cached data.",
            headers={"Retry-After": "60"},
        )
    return True


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
            logging.getLogger("uvicorn").error(f"Error parsing rate limit details: {str(e)}")
        
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "message": f"Try again in {retry_after} seconds",
                "limit": limit
            },
            headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": limit}
        )

    @app.get("/health")
    @limiter.exempt
    async def health():
        """Liveness check that doesn't call any upstream API."""
        return {"status": "ok", "service": app_name}

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
    logger.info(f"Sending {method} request to {redact_url(url)} with params={redact_params(params)} json={redact_params(json)}")

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
                try:
                    return response.json()
                except ValueError:
                    logger.error(f"Upstream returned non-JSON response from {redact_url(url)}")
                    raise HTTPException(status_code=502, detail="Upstream API returned an invalid response")
        except httpx.HTTPStatusError as e:
            last_error = e
            if max_retries > 0 and e.response.status_code == 502 and attempt < max_retries:
                logger.warning(f"502 Bad Gateway - Attempt {attempt + 1}/{max_retries + 1}")
                await asyncio.sleep(retry_delay)
                continue
            # Log upstream details server-side only, they may contain internal information
            body = e.response.text[:500]
            # Some APIs echo the key back in their error message
            secrets = [v for k, v in parse_qsl(urlsplit(str(e.request.url)).query) if k.lower() in SENSITIVE_PARAMS and v]
            for secret in secrets:
                body = body.replace(secret, "***")
            logger.error(f"Upstream HTTP {e.response.status_code} from {redact_url(e.request.url)}: {body}")
            raise HTTPException(status_code=e.response.status_code, detail=f"Upstream API returned HTTP {e.response.status_code}")
        except httpx.RequestError as e:
            last_error = e
            if max_retries > 0 and attempt < max_retries:
                logger.warning(f"Network error - Attempt {attempt + 1}/{max_retries + 1}")
                await asyncio.sleep(retry_delay)
                continue
            logger.error(f"Upstream request to {redact_url(url)} failed: {type(e).__name__}")
            raise HTTPException(status_code=502, detail="Upstream API unreachable")
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
        logger.info(f"{datetime.now().isoformat()} Received {request.method} request: {redact_url(request.url)} from {get_remote_address(request)}")
        return await endpoint_func(request)
    
    return proxy_request
