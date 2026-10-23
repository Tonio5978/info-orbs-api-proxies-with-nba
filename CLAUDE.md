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
| soccer-proxy | 8090 | `/soccer/` | ESPN soccer, background scheduler, see `README-SOCCER.md` |

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

Two more routes summarize whole days from the ESPN scoreboard, as `{date, count, games: [{gameId, status (pre/in/post), startTime, home/away: {abbreviation, score, color (#rrggbb)}}]}` — live games (`in`) add `period`, `clock` (time remaining in the period), `detail` (ESPN shortDetail, e.g. Halftime) and `lastPlay` (ESPN `competitions[0].situation.lastPlay.text`, null if absent): `GET /nbadata/scores` (current ESPN scoreboard day, which rolls over around midday ET) and `GET /nbadata/upcoming` (the following day). Both are served from `src/nba_scheduler.py`, a background scheduler like the soccer one (clients never call ESPN for them): every 10 s while a game is live or within 10 min of tip-off (late tip-offs tracked 3 h), else 30 min, always woken 10 min before the next tip-off of today's or tomorrow's board. `/proxy` caches team data (details, schedule) on demand but builds `liveGame` per request from the scheduler's board. Both accept `?team=` and `?tz=` (start times). Each route registered with `handle_request` gets its own rate-limit counter.

Final games (`post`) of `/nbadata/scores` get `topScorers` per team (5 best scorers with minutes, points, FG/3PT/FT made-attempted, assists, rebounds, offensive rebounds, blocks, +/-), from the ESPN game summary (`summary?event=<gameId>`) fetched by the scheduler with the scores refresh (`top_scorers()` in `nba_scheduler.py`, refreshed every 30 min, null until loaded).

`GET /nbadata/standings` (`?conference=`, `?team=`): ESPN `site.web.api.espn.com/apis/v2/sports/basketball/nba/standings`, a third scheduler board (30 min, 5 min during games); ESPN doesn't sort entries, the proxy sorts by `playoffSeed`; colors from `nba_teams.json`.

Team lookup uses `src/nba_teams.json` (loaded at startup), supporting team ID, full name, or any alias. Debug endpoint: `GET /nbadata/debug/teams`.

Season detection: NBA season spans Oct–Jun; `get_current_season()` returns the end-year (e.g., `"2025"` for the 2024–25 season).

## Soccer Proxy (`src/soccer_proxy.py`)

Reproduces the epl-vps.duckdns.org API for Node-RED/AWTRIX. Unlike the other proxies, requests never call ESPN:
a background scheduler (`src/soccer_scheduler.py`, started via `create_app(background=...)`) fills in-memory
payloads, and routes only serve them (with `ETag`/304, `Cache-Control: public, max-age=10`, CORS `*`; 503 until
the first load).

- Competitions come from `config/competitions.json` (`key`, `espnCode`, `country`); each key gets
  `/{key}UpcomingMatches` and `/{key}Teams`. Team aliases/country overrides: `config/aliases.json`, keyed by ESPN team id.
- ESPN rejects `dates=YYYYMMDD-YYYYMMDD` ranges (HTTP 400): the J-1..J+14 window is fetched per month
  (`dates=YYYYMM`, widened by a day for US month boundaries) and filtered locally; during live matches (or
  10 min before kickoff) only the affected days are refetched every 20 s.
- One ESPN request at a time with a pause; failures keep the last payload and back off (30 s doubling, max 30 min).
- Projection in `src/soccer_espn.py` (pure functions) must keep ESPN field names/nesting exactly; no
  `competitionKey` in matches; `period` only once a match has started; colors without `#`.
- `/scores` and `/upcoming` (ESPN `soccer/all/scoreboard`, current ESPN day and the next one, `limit=1000`): configured competitions only unless `?all=true` (league recognised by the `l:<id>` part of the event uid), `?competition=`, `?team=`, `?tz=` (default Europe/Paris, 24 h). Games in the `/nbadata/scores` style (team colors for every status) plus `goals` and `cards` from ESPN details (own goals credited to the benefiting team, shoot-out kicks excluded). Filtering runs per request on the cached raw events.
- `/{key}Standings`: ESPN `apis/v2/sports/soccer/{code}/standings` per competition (30 min, 5 min while it has a live match), in ESPN order, with `zone` from the entry note and team colors from the competition's `/teams` data; knockout cups (Coupe de France) have no `groups`.
- Team logos: `scripts/download_soccer_logos.py` saves `soccer_logos/<ESPN team id>.png` (+ `index.json`), served by nginx at `/soccer/logo/`; images are git-ignored.
- Tests: `python -m unittest discover -s tests -t .` (fixtures in `tests/fixtures/soccer/`, real ESPN responses).

## Environment Variables

Copy `sample.env` to `.env`. Required API keys:
- `OPENWEATHER_DEFAULT_API_KEY`
- `TEMPEST_DEFAULT_API_KEY`
- `TWELVEDATA_DEFAULT_API_KEY`
- `VISUALCROSSING_DEFAULT_API_KEY`

Per-proxy overrides follow the pattern `{PROXY_NAME}_PROXY_CACHE_LIFE`, `{PROXY_NAME}_PROXY_REQUESTS_PER_MINUTE`, `{PROXY_NAME}_MAX_RETRIES`, `{PROXY_NAME}_RETRY_DELAY`.
