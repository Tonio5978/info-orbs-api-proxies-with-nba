# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a collection of FastAPI-based API proxy services for the [InfoOrbs project](https://github.com/brettdottech/info-orbs). All proxies run inside a single Docker container managed by **supervisord**, fronted by **nginx** on port 80.

## Development Commands

```bash
# Build and start
docker-compose up -d --build

# Common operations
docker-compose down
docker-compose logs -f
docker-compose restart proxy
docker-compose exec proxy bash

# Preload timezone cache
docker-compose exec -u app proxy python -m scripts.preload_timezones
```

## Architecture

**Request flow:** Client → nginx (port 80) → supervisord-managed uvicorn process → upstream API

Each proxy is an independent FastAPI app running on its own port:

| Proxy | Port | Path prefix | Notes |
|---|---|---|---|
| timezone-proxy | 8080 | `/timezone/` | SQLite cache, real-time via timeapi.io |
| visualcrossing-proxy | 8081 | `/visualcrossing/` | Requires API key |
| twelvedata-proxy | 8082 | `/twelvedata/` | Requires API key |
| tempest-proxy | 8083 | `/tempest/` | Requires API key |
| openweather-proxy | 8084 | `/openweather/` | Requires API key |
| parqet-proxy | 8085 | `/parqet/` | |
| zoneinfo-proxy | 8086 | `/zoneinfo/` | Static tz via Python zoneinfo |
| mlbdata-proxy | 8087 | `/mlbdata/` | ESPN API, serves logos at `/mlbdata/logo/` |
| nfldata-proxy | 8088 | `/nfldata/` | ESPN API, serves logos at `/nfldata/logo/` |
| nbadata-proxy | 8089 | `/nbadata/` | ESPN API, serves logos at `/nbadata/logo/` |

**Shared infrastructure (`src/common.py`):**
- `setup_logger(app_name)` — configures uvicorn logger with app-specific prefix
- `create_app(app_name)` — creates FastAPI app with slowapi rate limiting middleware
- `fetch_data(url, logger, ...)` — async HTTP client with optional retry logic

**In-memory caching pattern:** Each proxy maintains a `{cache_key: data}` dict and a `{cache_key: expiry_datetime}` dict. Cache lifetime is controlled by `{PROXY_NAME}_PROXY_CACHE_LIFE` env var (minutes; 0 disables). All proxies support `?force=true` to bypass cache, limited per IP by `check_force_refresh` (`FORCE_REFRESH_PER_MINUTE`, default 2).

**Security conventions:** proxy processes run as the unprivileged `app` user (supervisord `user=app`). Never log raw URLs or params — use `redact_url` / `redact_params` from `common.py`. Upstream error bodies are logged server-side only; clients get a generic message. Each app exposes `GET /health` (rate-limit exempt, no upstream call).

## NBA Proxy (`src/nbadata_proxy.py`)

The NBA proxy aggregates ESPN API data into a single response per team containing:
- `team` — identity, colors, logo info
- `standings` — conference/division rank, W/L, streaks
- `lastGame` — most recent completed game
- `nextGame` — upcoming scheduled game
- `liveGame` — live game data from scoreboard (only present if team is currently playing)

Team lookup uses `src/nba_teams.json` (loaded at startup), supporting team ID, full name, or any alias. Debug endpoint: `GET /nbadata/debug/teams`.

Season detection: NBA season spans Oct–Jun; `get_current_season()` returns the end-year (e.g., `"2025"` for the 2024–25 season).

## Environment Variables

Copy `sample.env` to `.env`. Required API keys:
- `OPENWEATHER_DEFAULT_API_KEY`
- `TEMPEST_DEFAULT_API_KEY`
- `TWELVEDATA_DEFAULT_API_KEY`
- `VISUALCROSSING_DEFAULT_API_KEY`

Per-proxy overrides follow the pattern `{PROXY_NAME}_PROXY_CACHE_LIFE`, `{PROXY_NAME}_PROXY_REQUESTS_PER_MINUTE`, `{PROXY_NAME}_MAX_RETRIES`, `{PROXY_NAME}_RETRY_DELAY`.
