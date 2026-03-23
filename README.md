# API Proxies for InfoOrbs

This repository contains API proxies for the [InfoOrbs project](https://github.com/brettdottech/info-orbs).

## Proxies Provided

- **MLB Baseball Data Proxy URL:**  
  Example: `http://localhost/mlbdata/proxy`

- **NFL Football Data Proxy URL:**  
  Example: `http://localhost/nfldata/proxy`

- **NBA Basketball Data Proxy URL:**  
  Example: `http://localhost/nbadata/proxy`

- **Time Zone option 1 - ZoneInfo Timezone Proxy URL:**  
  Example: `http://localhost/zoneinfo/proxy`

  - Static Timezone info using the python's built-in [zoneinfo library](https://docs.python.org/3/library/zoneinfo.html)
  - Uses the IANA Time Zone Database (tzdata) shipped with Python or the OS
  - The timezone data is fixed at the time this app was installed or compiled
  - To update timezone rules (e.g., for new DST changes), you must update the tzdata package and reinstall

- **Time Zone option 2 - Timezone Database Proxy URL:**  
  Example: `http://localhost/timezone/proxy`

  - Real-time timezone offset from timeapi.io, requests are cached in a SQLite database
  - Data is only refreshed when a time zone update is detected
  - Database can be pre-loaded after initial installation
  - Data persists across container restarts
  - [Additional TimeZone proxy commands](/README-docker.md#timezone-proxy-commands)

- **Visual Crossing Proxy URL:**  
  Example: `http://localhost/visualcrossing/proxy`

- **Twelve Data Proxy URL:**  
  Example: `http://localhost/twelvedata/proxy`

- **Tempest Proxy URL:**  
  Example: `http://localhost/tempest/proxy`

- **OpenWeather Proxy URL:**  
  Example: `http://localhost/openweather/proxy`

- **Parqet Proxy URL:**  
  Example: `http://localhost/parqet/proxy`

> All proxies (except zoneinfo) support `force=true` parameter to bypass cache.

## Installation

**Note:** Consider aliasing `docker-compose` to `dc` for convenience:

```bash
alias dc="docker-compose"
```

If not using this alias, replace all `dc` commands with `docker-compose`.

### Clone the Repository

```bash
git clone https://github.com/dreed47/info-orbs-api-proxies.git
cd info-orbs-api-proxies
```

### Set Up Environment Variables

Either:

1. Copy `sample.env` to `.env` and modify values, or
2. Manually configure environment variables

### Build and Start

```bash
dc up -d --build
```

### Preload Timezones (Optional)

Preloads top 50 timezones (covers 95% of world population):

```bash
dc exec proxy python -m scripts.preload_timezones
```

### Common Commands

| Command              | Description     |
| -------------------- | --------------- |
| `dc down`            | Stop containers |
| `dc logs -f`         | View logs       |
| `dc ps`              | List services   |
| `dc restart proxy`   | Restart service |
| `dc exec proxy bash` | Enter container |

## License

MIT License - See [LICENSE.txt](LICENSE.txt)
