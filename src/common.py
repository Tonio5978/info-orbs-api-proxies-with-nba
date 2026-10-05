import logging
import sys
import os
import asyncio
import json as jsonlib
import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from datetime import datetime, timezone
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
    """Return the URL (or path?query) with sensitive query parameter values masked."""
    parts = urlsplit(str(url))
    if not parts.query:
        return str(url)
    if not any(k.lower() in SENSITIVE_PARAMS for k, _ in parse_qsl(parts.query, keep_blank_values=True)):
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


def is_force_requested(request: Request) -> bool:
    return request.query_params.get("force", "").lower() == "true"


# ── Caching ───────────────────────────────────────────────────────────────────

class TTLCache:
    """In-memory cache with per-entry expiry.

    Expired entries are kept for `stale_seconds` so they can be served when the
    upstream API fails, then purged. A per-key lock lets concurrent requests for
    the same key wait for a single upstream fetch instead of each triggering one.
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 500, stale_seconds: float = 86400):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.stale_seconds = stale_seconds
        self._entries: Dict[str, Tuple[float, Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    @classmethod
    def from_env_minutes(cls, env_var: str, default_minutes: str = "5") -> "TTLCache":
        """Cache whose lifetime comes from an env var in minutes (0 disables caching)."""
        return cls(int(os.getenv(env_var, default_minutes)) * 60)

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def get(self, key: str) -> Optional[Any]:
        """Return the value if present and not expired."""
        entry = self._entries.get(key)
        if entry and entry[0] > time.monotonic():
            return entry[1]
        return None

    def get_stale(self, key: str) -> Optional[Any]:
        """Return the value even if expired (fallback when the upstream API fails)."""
        entry = self._entries.get(key)
        return entry[1] if entry else None

    def set(self, key: str, value: Any, ttl_seconds: Optional[float] = None):
        if not self.enabled:
            return
        if len(self._entries) >= self.max_entries:
            self._purge()
        self._entries[key] = (time.monotonic() + (ttl_seconds or self.ttl_seconds), value)

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _purge(self):
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._entries.items() if exp + self.stale_seconds < now]:
            del self._entries[key]
        # Still full: drop the entries closest to expiry
        overflow = len(self._entries) - self.max_entries + 1
        if overflow > 0:
            for key in sorted(self._entries, key=lambda k: self._entries[k][0])[:overflow]:
                del self._entries[key]
        for key in [k for k, lock in self._locks.items() if k not in self._entries and not lock.locked()]:
            del self._locks[key]


def make_cache_key(params: dict) -> str:
    """Stable cache key from request parameters ('force' never affects the response)."""
    return jsonlib.dumps({k: v for k, v in params.items() if k != "force"}, sort_keys=True, default=str)


async def get_or_fetch(
    cache: TTLCache,
    key: str,
    fetch: Callable[[], Awaitable[Any]],
    logger: logging.Logger,
    force: bool = False,
    ttl_for: Optional[Callable[[Any], Optional[float]]] = None,
) -> Tuple[Any, bool]:
    """Return (data, served_from_cache).

    Serves a fresh cache entry unless `force`, otherwise fetches once per key even
    under concurrent requests. On upstream failure, falls back to the stale entry
    (unless `force`). `ttl_for(data)` can override the lifetime of a new entry.
    """
    if cache.enabled and not force:
        cached = cache.get(key)
        if cached is not None:
            return cached, True

    async with cache.lock(key):
        # Another request may have refreshed the entry while we were waiting
        if cache.enabled and not force:
            cached = cache.get(key)
            if cached is not None:
                return cached, True
        try:
            data = await fetch()
        except HTTPException:
            stale = cache.get_stale(key)
            if stale is not None and not force:
                logger.warning(f"Upstream failed, returning stale cached data for {key}")
                return stale, True
            raise
        cache.set(key, data, ttl_for(data) if ttl_for else None)
        return data, False


def utc_timestamp() -> str:
    """Naive UTC ISO timestamp, same format as the historical datetime.utcnow().isoformat()."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def proxy_info(cached: bool) -> dict:
    """Standard 'proxy-info' block added to proxy responses."""
    return {"cachedResponse": cached, "status_code": 200, "timestamp": utc_timestamp()}


async def gather_or_raise(*aws):
    """Run awaitables concurrently; once all are done, raise the first exception if any."""
    results = await asyncio.gather(*aws, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


# ── App setup ─────────────────────────────────────────────────────────────────

class RedactAccessLogFilter(logging.Filter):
    """Mask API keys in uvicorn access log lines (args: client, method, path?query, http version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            args[2] = redact_url(args[2])
            record.args = tuple(args)
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
    if not any(isinstance(f, RedactAccessLogFilter) for f in access_logger.filters):
        access_logger.addFilter(RedactAccessLogFilter())

    return logger


# Shared HTTP client (connection pooling, keep-alive), opened and closed by the app lifespan
_http_client: Optional[httpx.AsyncClient] = None


def create_app(
    app_name: str,
    default_requests_per_minute: int = 5,
    banner_title: Optional[str] = None,
    banner_lines: Optional[Callable[[], List[str]]] = None,
) -> FastAPI:
    """Create a FastAPI app with rate limiting, a shared HTTP client and a /health endpoint.

    The per-IP rate limit comes from {APP_NAME}_REQUESTS_PER_MINUTE (e.g. NBADATA_PROXY_REQUESTS_PER_MINUTE)
    and applies to every route except /health; it's stored in app.state.rate_limit for handle_request.
    """
    rate_limit = os.getenv(f"{app_name.upper()}_REQUESTS_PER_MINUTE", str(default_requests_per_minute)) + "/minute"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _http_client
        logger = logging.getLogger("uvicorn")
        if banner_title:
            logger.info("=" * 50)
            logger.info(f"{banner_title:^50}")
            logger.info("=" * 50)
            logger.info(f"→ Rate limiting: {rate_limit} per IP")
            for line in (banner_lines() if banner_lines else []):
                logger.info(f"→ {line}")
            logger.info("=" * 50 + "\n")
        _http_client = httpx.AsyncClient()
        try:
            yield
        finally:
            await _http_client.aclose()
            _http_client = None

    app = FastAPI(title=app_name, lifespan=lifespan)
    limiter = Limiter(key_func=get_remote_address, default_limits=[rate_limit])
    app.state.limiter = limiter
    app.state.rate_limit = rate_limit
    app.add_middleware(SlowAPIMiddleware)

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
        retry_after = 60
        limit = rate_limit
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
    """Fetch JSON from an upstream API with optional retries (env {APP_NAME}_MAX_RETRIES / _RETRY_DELAY)."""
    logger.info(f"Sending {method} request to {redact_url(url)} with params={redact_params(params)} json={redact_params(json)}")

    max_retries = int(os.getenv(f"{app_name.upper()}_MAX_RETRIES", "0"))
    retry_delay = int(os.getenv(f"{app_name.upper()}_RETRY_DELAY", "0"))

    for attempt in range(max_retries + 1):
        try:
            if _http_client is not None:
                response = await _send(_http_client, method, url, params, json, timeout)
            else:
                # Outside the app lifespan (scripts, tests): use a one-off client
                async with httpx.AsyncClient() as client:
                    response = await _send(client, method, url, params, json, timeout)
            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                logger.error(f"Upstream returned non-JSON response from {redact_url(url)}")
                raise HTTPException(status_code=502, detail="Upstream API returned an invalid response")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 502 and attempt < max_retries:
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
            if attempt < max_retries:
                logger.warning(f"Network error - Attempt {attempt + 1}/{max_retries + 1}")
                await asyncio.sleep(retry_delay)
                continue
            logger.error(f"Upstream request to {redact_url(url)} failed: {type(e).__name__}")
            raise HTTPException(status_code=502, detail="Upstream API unreachable")
    raise HTTPException(status_code=502, detail="Unknown proxy error")


async def _send(client: httpx.AsyncClient, method: str, url: str, params: Optional[dict], json: Optional[dict], timeout: int) -> httpx.Response:
    if method == "GET":
        return await client.get(url, params=params, timeout=timeout)
    if method == "POST":
        return await client.post(url, json=json, timeout=timeout)
    raise ValueError(f"Unsupported method: {method}")


def handle_request(
    app: FastAPI,
    logger: logging.Logger,
    endpoint_func: Callable,
    methods: Tuple[str, ...] = ("GET", "POST"),
    path: str = "/proxy",
):
    """Register the proxy route with request logging and the app's rate limit."""

    @app.api_route(path, methods=list(methods))
    @app.state.limiter.limit(app.state.rate_limit)
    async def proxy_request(request: Request):
        logger.info(f"{datetime.now().isoformat()} Received {request.method} request: {redact_url(request.url)} from {get_remote_address(request)}")
        return await endpoint_func(request)

    return proxy_request
