# NBA Data Proxy — ajout au projet info-orbs-api-proxies

## Fichiers à ajouter

| Fichier | Destination dans le repo |
|---|---|
| `nbadata-proxy.py` | `src/nbadata_proxy.py` |
| `nba_teams.json` | `src/nba_teams.json` |
| Dossier `nba_logos/` | `nba_logos/` (à la racine) |

---

## Endpoint

```
GET http://localhost/nbadata/proxy?teamName=LAL
GET http://localhost/nbadata/proxy?teamName=Lakers
GET http://localhost/nbadata/proxy?teamName=lakers
GET http://localhost/nbadata/proxy?teamName=LAL&force=true   # bypass cache
```

---

## Réponse JSON

```json
{
  "teamId": "13",
  "season": "2025",
  "team": {
    "fullName": "Los Angeles Lakers",
    "shortName": "Lakers",
    "abbreviation": "LAL",
    "colors": [
      { "name": "Purple", "code": "#552583" },
      { "name": "Gold",   "code": "#FDB927" }
    ],
    "logoUrl": "https://a.espncdn.com/...",
    "logoImageFileName": "lal.png",
    "logoBackgroundColor": "#552583",
    "conference": "Western",
    "division": "Pacific",
    "standingSummary": "1st in Pacific Division"
  },
  "standings": {
    "conference": "Western",
    "conferenceRank": "1st",
    "division": "Pacific",
    "divisionRank": "1st",
    "wins": 35,
    "losses": 18,
    "winningPercentage": 0.660,
    "gamesBehind": "—",
    "homeRecord": "20-8",
    "awayRecord": "15-10",
    "lastTen": "7-3",
    "streak": "W3",
    "pointsFor": 115.2,
    "pointsAgainst": 110.8,
    "record": "35-18"
  },
  "lastGame": {
    "date": "Feb 10",
    "day": "Mon",
    "opponent": "GSW",
    "opponentFullName": "Golden State Warriors",
    "location": "Home",
    "score": "118-105",
    "result": "Won",
    "gameTime": "7:30 PM",
    "gameId": "401810640"
  },
  "liveGame": {
    "isLive": false
  },
  "nextGame": {
    "date": "Feb 12",
    "day": "Wed",
    "opponent": "PHX",
    "opponentFullName": "Phoenix Suns",
    "location": "Away",
    "gameTime": "9:00 PM",
    "tvBroadcast": "ESPN",
    "gameId": "401810650"
  },
  "proxy-info": {
    "cachedResponse": false,
    "status_code": 200,
    "timestamp": "2026-02-17T10:00:00"
  }
}
```

---

## Différences avec le proxy NFL

| Fonctionnalité | NFL | NBA |
|---|---|---|
| Saison courante | Sept → année en cours | Oct → année en cours |
| `liveGame` | ❌ | ✅ (quarter + clock) |
| `standings.wins/losses` | ❌ | ✅ |
| `standings.homeRecord` | ❌ | ✅ |
| `standings.lastTen` | ❌ | ✅ |
| `standings.streak` | ❌ | ✅ |
| `opponent` dans games | nickname | abbreviation |

---

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `NBADATA_PROXY_CACHE_LIFE` | `5` | Durée du cache en minutes |
| `NBADATA_PROXY_REQUESTS_PER_MINUTE` | `15` | Limite rate limiting |

---

## Logos NBA

Le proxy sert les logos depuis `/app/nba_logos/`.  
Noms de fichiers attendus : `lal.png`, `gsw.png`, `bos.png`… (voir `nba_teams.json`).

Télécharger depuis ESPN CDN :
```
https://a.espncdn.com/i/teamlogos/nba/500/scoreboard/{abbr}.png
```

Exemple de script pour les télécharger tous :
```bash
mkdir -p nba_logos
for abbr in atl bos bkn cha chi cle dal den det gsw hou ind lac lal mem mia mil min nop nyk okc orl phi phx por sac sas tor utah was; do
  curl -o nba_logos/${abbr}.png \
    "https://a.espncdn.com/i/teamlogos/nba/500/scoreboard/${abbr}.png"
done
```

---

## Debug

```
GET http://localhost/nbadata/debug/teams
```

Retourne la liste de toutes les équipes avec leurs aliases valides.
