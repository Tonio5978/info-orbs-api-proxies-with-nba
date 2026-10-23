# NBA Data Proxy — ajout au projet info-orbs-api-proxies

## Fichiers à ajouter

| Fichier | Destination dans le repo |
|---|---|
| `nbadata_proxy.py` | `src/nbadata_proxy.py` |
| `sports_common.py` | `src/sports_common.py` (helpers partagés MLB/NFL/NBA) |
| `nba_teams.json` | `src/nba_teams.json` |
| Dossier `nba_logos/` | `nba_logos/` (à la racine) |

---

## Endpoint

```
GET http://localhost/nbadata/proxy?teamName=LAL
GET http://localhost/nbadata/proxy?teamName=Lakers
GET http://localhost/nbadata/proxy?teamName=lakers
GET http://localhost/nbadata/proxy?teamName=LAL&force=true   # bypass cache
GET http://localhost/nbadata/proxy?teamName=LAL&tz=Europe/Paris   # dates/heures en heure de Paris (défaut America/New_York)
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
| `NBADATA_PROXY_REQUESTS_PER_MINUTE` | `15` | Limite rate limiting (par IP et par route) |
| `NBADATA_PROXY_LIVE_REFRESH_SECONDS` | `10` | Rafraîchissement du scoreboard pendant les matchs (`/scores`, `liveGame`) |
| `NBADATA_PROXY_IDLE_REFRESH_MINUTES` | `30` | Rafraîchissement hors matchs, et de `/upcoming` |
| `NBADATA_PROXY_PREMATCH_MINUTES` | `10` | Passage au rafraîchissement rapide avant le coup d'envoi |
| `NBADATA_PROXY_STANDINGS_LIVE_REFRESH_MINUTES` | `5` | Rafraîchissement du classement pendant les matchs (sinon 30 min) |
| `NBADATA_PROXY_LIVE_CACHE_SECONDS` | `60` | Cache des données d'équipe de `/proxy` (bilan, dernier match) pendant que l'équipe joue |

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

## Scores du jour et matchs du lendemain

```
GET http://localhost/nbadata/scores            # journée en cours : terminés, en direct et à venir
GET http://localhost/nbadata/upcoming          # matchs programmés du lendemain
GET http://localhost/nbadata/scores?team=LAL   # filtre optionnel sur une équipe (id, nom ou alias)
GET http://localhost/nbadata/upcoming?tz=Europe/Paris   # heures de début en heure de Paris (défaut America/New_York)
```

La « journée en cours » est celle du scoreboard ESPN : elle ne bascule qu'en milieu de journée (heure de New York),
donc le matin en Europe `/scores` montre encore les résultats de la nuit. `/upcoming` renvoie le jour suivant.

```json
{
  "date": "2026-10-04",
  "count": 2,
  "games": [
    {"gameId": "401914127", "status": "post", "startTime": "7:00 PM",
     "home": {"abbreviation": "DEN", "score": "97", "color": "#0e2240"}, "away": {"abbreviation": "UTAH", "score": "109", "color": "#4e008e"}},
    {"gameId": "401918010", "status": "in", "period": "Q3", "clock": "5:12", "detail": "5:12 - 3rd Quarter",
     "lastPlay": "Jayson Tatum defensive rebound", "startTime": "7:00 PM",
     "home": {"abbreviation": "LAC", "displayName": "LA Clippers", "score": "88", "color": "#12173f"},
     "away": {"abbreviation": "GS", "displayName": "Golden State Warriors", "score": "85", "color": "#fdb927"}}
  ],
  "proxy-info": {"cachedResponse": false, "status_code": 200, "timestamp": "2026-10-05T14:38:04.183521"}
}
```

`status` est l'état ESPN : `pre` (pas commencé, score `"0"`), `in` (en direct), `post` (terminé).
Match en direct (`in`) : `period` = quart-temps en cours (`Q1`…`Q4`, puis `OT`, `2OT`…), `clock` = temps **restant**
dans le quart-temps (`5:12`, puis `45.3` sous la minute), `detail` = libellé ESPN (`Halftime`, `End of 3rd Quarter`…),
utile car à la mi-temps `clock` vaut `0.0`, `lastPlay` = dernière action du match (`situation.lastPlay.text` d'ESPN,
par exemple `Jayson Tatum defensive rebound`, `Celtics Full timeout`), `null` si ESPN ne la fournit pas. Chaque équipe a sa couleur (`color`, `#rrggbb`, fournie par ESPN),
quel que soit le statut du match.

`startTime` est l'heure de début (`7:00 PM`) dans le fuseau `tz`. Attention : avec `tz=Europe/Paris`, un match du soir
aux États-Unis commence le lendemain matin en heure de Paris (`4:00 AM`), le champ ne contient que l'heure.
Les matchs sont triés par heure de début.

**Meilleurs marqueurs des matchs terminés** (`post`, `/nbadata/scores` uniquement) : chaque équipe reçoit
`topScorers`, ses 5 meilleurs marqueurs d'après la feuille de match ESPN du match (`summary?event=<gameId>`) :
```json
"home": {"abbreviation": "PHI", "displayName": "Philadelphia 76ers", "score": "114", "color": "#1d428a", "topScorers": [
  {"name": "T. Maxey", "jersey": "0", "minutes": 42, "points": 32, "fieldGoalsMade": 12, "fieldGoalsAttempted": 23,
   "threePointsMade": 2, "threePointsAttempted": 7, "freeThrowsMade": 6, "freeThrowsAttempted": 8,
   "assists": 6, "rebounds": 3, "offensiveRebounds": 1, "blocks": 0, "plusMinus": 19}, ...]}
```
- Classés par points (à égalité, par minutes jouées) ; les joueurs qui n'ont pas joué sont exclus.
- `topScorers` vaut `null` tant que la feuille de match n'est pas encore chargée (quelques secondes après la fin
  du match) ; elle est chargée en tâche de fond dès que le match est terminé, puis rechargée toutes les 30 min tant
  que le match reste dans la journée ESPN (corrections de statistiques).

**Rafraîchissement** : comme pour le football, un planificateur interne interroge ESPN en tâche de fond ; les
clients ne déclenchent aucun appel ESPN pour `/scores`, `/upcoming` et le `liveGame` de `/proxy`.
- pendant un match (et à partir de 10 min avant le coup d'envoi) : **toutes les 10 s** ;
- sinon : toutes les 30 min, mais le planificateur se réveille toujours 10 min avant le prochain coup d'envoi
  (de la journée en cours ou du lendemain), et dès le démarrage du proxy : un match n'est jamais manqué ;
- un match encore « programmé » après son heure de début (retard) reste suivi toutes les 10 s pendant 3 h ;
- `/upcoming` : toutes les 30 min, et immédiatement quand ESPN passe au jour suivant ;
- en cas d'erreur ESPN, la dernière journée valide reste servie ; au démarrage, `503` (quelques secondes) ;
- `force=true` n'a plus d'effet sur `/scores` et `/upcoming` (les données ont au plus 10 s pendant les matchs).
- `/nbadata/health` indique l'état des deux journées, le mode live et l'heure du prochain rafraîchissement.

---

## Classement (`/nbadata/standings`)

```
GET http://localhost/nbadata/standings                     # conférences Est et Ouest
GET http://localhost/nbadata/standings?conference=east     # une conférence (east / west)
GET http://localhost/nbadata/standings?team=LAL            # une équipe (id, nom ou alias)
```
```json
{"season": "2027", "conferences": [{"name": "Eastern Conference", "abbreviation": "East", "teams": [
  {"rank": 1, "teamId": "8", "abbreviation": "DET", "name": "Detroit Pistons", "color": "#C8102E", "wins": 60, "losses": 22,
   "winPercent": ".732", "gamesBehind": "-", "streak": "W3", "home": "33-8", "road": "27-14", "vsConference": "38-14",
   "pointsFor": "117.2", "pointsAgainst": "108.9"}]}], "proxy-info": {...}}
```
- Source : `site.web.api.espn.com/apis/v2/sports/basketball/nba/standings`, rechargé en tâche de fond toutes les
  30 min, toutes les 5 min pendant les matchs (`NBADATA_PROXY_STANDINGS_LIVE_REFRESH_MINUTES`).
- `rank` = tête de série (`playoffSeed`) ; ESPN ne trie pas les équipes, le proxy les trie par `rank`.
- `color` vient de `nba_teams.json` (le classement ESPN n'a pas de couleurs).
- Avant le début de la saison régulière, ESPN donne la nouvelle saison avec tous les bilans à 0-0 et toutes les têtes
  de série à 1 : les équipes sont alors listées par ordre alphabétique.

---

## Debug

```
GET http://localhost/nbadata/debug/teams
```

Retourne la liste de toutes les équipes avec leurs aliases valides.
