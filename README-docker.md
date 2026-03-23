## Installation

<i>**note:** You may find it useful to alias docker-compose to dc (e.g. alias dc="docker-compose") to save typing. If you choose to not use this shortcut just replace all instacnes of "dc" to docker-compose" in this document.</i>

### Build and Start

```bash
dc up -d --build
```

### Other commands

`dc down` -- Stop cleanly  
`dc logs -f` -- Tail all service logs  
`dc ps` -- Check running services  
`dc restart proxy` -- Restart just one service  
`dc exec proxy bash` -- Enter container shell

## TimeZone Proxy Commands

### Preload top 50 timezones

```bash
dc exec proxy python -m scripts.preload_timezones
```

### Check sqldb for existing record

```bash
dc exec proxy sqlite3 /var/cache/timezone_proxy/timezone_cache.db \
 "SELECT json_extract(data, '$.dstInterval') FROM timezone_cache LIMIT 1"
```

### Check sqldb for total record count

```bash
dc exec proxy sqlite3 /var/cache/timezone_proxy/timezone_cache.db \
 "SELECT COUNT(*) FROM timezone_cache;"
```
