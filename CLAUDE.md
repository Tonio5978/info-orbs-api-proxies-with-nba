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

Each proxy is an independent FastAPI app (`src/<name>_proxy.py`) running on its own port:

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
- `create_app(app_name, default_requests_per_minute, banner_title, banner_lines)` — FastAPI app with lifespan (startup banner, shared httpx client), slowapi rate limiting (`{APP_NAME}_REQUESTS_PER_MINUTE`) and `/health`
- `fetch_data(url, logger, ...)` — upstream call through the shared httpx client, with optional retry logic
- `handle_request(app, logger, endpoint, methods, path)` — registers the rate-limited `/proxy` route
- `TTLCache`, `get_or_fetch`, `make_cache_key`, `proxy_info`, `gather_or_raise` — caching and response helpers

**Sports helpers (`src/sports_common.py`):** team loading/lookup, colors, ordinals, date formatting in a display time zone (`?tz=`, default America/New_York), and ESPN event parsing (status lives on the competition in team schedules, scores are dicts there and strings in the scoreboard).

**In-memory caching pattern:** Each proxy uses a `TTLCache` from `common.py` with `get_or_fetch()`, which serves fresh entries, fetches once per key under concurrent requests (per-key lock) and falls back to the stale entry if the upstream fails. Proxies register their route with `handle_request()`; the rate limit comes from `create_app(app_name, default_requests_per_minute)`. Cache lifetime is controlled by `{PROXY_NAME}_PROXY_CACHE_LIFE` env var (minutes; 0 disables). All proxies support `?force=true` to bypass cache, limited per IP by `check_force_refresh` (`FORCE_REFRESH_PER_MINUTE`, default 2).

**Security conventions:** proxy processes run as the unprivileged `app` user (supervisord `user=app`). Never log raw URLs or params — use `redact_url` / `redact_params` from `common.py`. Upstream error bodies are logged server-side only; clients get a generic message. Each app exposes `GET /health` (rate-limit exempt, no upstream call).

## NBA Proxy (`src/nbadata_proxy.py`)

The NBA proxy aggregates ESPN API data into a single response per team containing:
- `team` — identity, colors, logo info
- `standings` — conference/division rank, W/L, streaks
- `lastGame` — most recent completed game
- `nextGame` — upcoming scheduled game
- `liveGame` — live game data from scoreboard (only present if team is currently playing)

Two more routes summarize whole days from the ESPN scoreboard, as `{date, count, games: [{gameId, status (pre/in/post), startTime, home/away: {abbreviation, score}}]}`: `GET /nbadata/scores` (current ESPN scoreboard day, which rolls over around midday ET; cached with the scoreboard shared by `/proxy`) and `GET /nbadata/upcoming` (the following day, `NBADATA_PROXY_UPCOMING_CACHE_LIFE`). Both accept `?team=` and `?tz=` (start times). Each route registered with `handle_request` gets its own rate-limit counter.

Team lookup uses `src/nba_teams.json` (loaded at startup), supporting team ID, full name, or any alias. Debug endpoint: `GET /nbadata/debug/teams`.

Season detection: NBA season spans Oct–Jun; `get_current_season()` returns the end-year (e.g., `"2025"` for the 2024–25 season).

## Environment Variables

Copy `sample.env` to `.env`. Required API keys:
- `OPENWEATHER_DEFAULT_API_KEY`
- `TEMPEST_DEFAULT_API_KEY`
- `TWELVEDATA_DEFAULT_API_KEY`
- `VISUALCROSSING_DEFAULT_API_KEY`

Per-proxy overrides follow the pattern `{PROXY_NAME}_PROXY_CACHE_LIFE`, `{PROXY_NAME}_PROXY_REQUESTS_PER_MINUTE`, `{PROXY_NAME}_MAX_RETRIES`, `{PROXY_NAME}_RETRY_DELAY`.
