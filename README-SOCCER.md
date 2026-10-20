# Soccer Proxy — scores de football (ESPN) pour Node-RED / AWTRIX

Reproduit l'API de `epl-vps.duckdns.org` : il relaie l'API publique non officielle d'ESPN (sans clé),
réduit les réponses aux champs utiles et les garde en cache mémoire. Il tourne dans le même conteneur
que les autres proxys (port interne 8090), derrière nginx sur le port 80, préfixe **`/soccer/`**.

## Routes

Pour chaque compétition de `config/competitions.json` (clé `{cle}`) :

| Route | Contenu |
|---|---|
| `GET /soccer/{cle}UpcomingMatches` | Matchs de J-1 à J+14 (champs du scoreboard ESPN, réduits) |
| `GET /soccer/{cle}Teams` | Équipes de la compétition, avec `aliases`, `competitionKey`, `country` |
| `GET /soccer/{cle}Standings` | Classement de la compétition (`?team=` pour une équipe) |
| `GET /soccer/scores` | Tous les matchs de la journée ESPN en cours, toutes compétitions configurées (terminés, en direct, à venir) |
| `GET /soccer/upcoming` | Les matchs du lendemain |
| `GET /soccer/health` | État du cache par compétition et des deux journées |

Clés livrées (les mêmes que l'API d'origine, plus `bundesliga`) :

| Clé | Code ESPN | Pays |
|---|---|---|
| `ligue1` | `fra.1` | france |
| `ligue2` | `fra.2` | france |
| `coupeDeFrance` | `fra.coupe_de_france` | france |
| `cl` | `uefa.champions` | — |
| `el` | `uefa.europa` | — |
| `ecl` | `uefa.europa.conf` | — |
| `epl` | `eng.1` | england |
| `laliga` | `esp.1` | spain |
| `serieA` | `ita.1` | italy |
| `bundesliga` | `ger.1` | germany |

Exemples :
```bash
curl -s http://localhost/soccer/ligue1UpcomingMatches | jq '.events[] | {date, name, state: .status.type.state}'
curl -s http://localhost/soccer/clTeams | jq '.sports[0].leagues[0].teams[0]'
curl -s http://localhost/soccer/health | jq
```

Les routes sont sensibles à la casse (`/soccer/serieAUpcomingMatches`, pas `serieaUpcomingMatches`).

## Format

`/…UpcomingMatches` — noms et imbrication du scoreboard ESPN, seuls ces champs sont gardés :
```json
{"events": [{
  "id": "401876437", "uid": "s:600~l:710~e:401876437", "date": "2026-10-16T18:45Z", "name": "Toulouse at Le Mans",
  "status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED", "completed": false}, "displayClock": "0'"},
  "venue": {"displayName": "Stade Marie-Marvingt"},
  "competitions": [{
    "altGameNote": "Ligue 1",
    "competitors": [
      {"homeAway": "home", "score": "0", "team": {"id": "2697", "displayName": "Le Mans", "abbreviation": "MNS", "color": "d62b11", "alternateColor": "d62b11"}},
      {"homeAway": "away", "score": "0", "team": {"id": "179", "displayName": "Toulouse", "abbreviation": "TOU", "color": "560080", "alternateColor": "ffff00"}}
    ],
    "details": []
  }]
}]}
```
- `status.period` n'existe que pour les matchs commencés (`in`) ou terminés (`post`).
- `status.type.name` est la valeur ESPN telle quelle : en plus de `STATUS_SCHEDULED`, `STATUS_IN_PROGRESS`,
  `STATUS_HALFTIME`, `STATUS_FULL_TIME`, `STATUS_FINAL_AET`, `STATUS_FINAL_PEN`, ESPN envoie aussi par exemple
  `STATUS_FIRST_HALF`, `STATUS_SECOND_HALF`, `STATUS_ABANDONED`, `STATUS_POSTPONED`.
- Couleurs en hexadécimal **sans `#`**, scores en chaîne.
- `details[]` (buts, cartons…) : `type.text`, `scoringPlay`, `penaltyKick`, `ownGoal`, `team.id`, `clock.displayValue`,
  `athletesInvolved[].shortName/jersey`, et `corrected` / `score` seulement quand ESPN les fournit.
- Les matchs sont triés par date.

`/…Teams` — enveloppe ESPN `sports[0].leagues[0].teams[].team`, avec `id`, `displayName`, `abbreviation`, `color`,
`alternateColor` (ESPN) plus :
- `aliases` : surnoms en minuscules de `config/aliases.json`, plus le nom sans accents s'il en a (`atletico madrid`) ;
- `competitionKey` : la clé de la compétition ;
- `country` : pays de la compétition ; pour les coupes européennes, pays du championnat configuré où joue l'équipe
  (PSG → `france`), sinon `country` de `aliases.json`, sinon `null` (jamais deviné).

## Scores du jour et matchs du lendemain (`/soccer/scores`, `/soccer/upcoming`)

Même principe que `/nbadata/scores`, à partir du scoreboard ESPN toutes compétitions
(`soccer/all/scoreboard`) : journée ESPN en cours pour `/scores`, jour suivant pour `/upcoming`.

```bash
curl -s http://localhost/soccer/scores                       # compétitions de competitions.json uniquement
curl -s "http://localhost/soccer/scores?all=true"            # tout le football du jour chez ESPN (~100 matchs)
curl -s "http://localhost/soccer/scores?competition=cl"      # une compétition (clé de competitions.json)
curl -s "http://localhost/soccer/upcoming?team=psg"          # une équipe : abréviation, nom (avec ou sans accents) ou alias
curl -s "http://localhost/soccer/scores?tz=UTC"              # fuseau de startTime (défaut Europe/Paris)
```

```json
{"date": "2026-10-20", "count": 9, "games": [
  {"gameId": "401915399", "status": "in", "competition": "cl", "competitionName": "UEFA Champions League, League Phase",
   "period": 1, "clock": "45'+1'", "detail": "45'+1'", "startTime": "21:00",
   "home": {"abbreviation": "PSG", "score": "1", "color": "#011F68"}, "away": {"abbreviation": "BAR", "score": "0", "color": "#a50044"},
   "goals": [{"minute": "38'", "team": "PSG", "player": "D. Doué", "type": "Goal", "penalty": false, "ownGoal": false}],
   "cards": [{"minute": "17'", "team": "BAR", "player": "J. Cancelo", "card": "yellow"}]},
  {"gameId": "401915403", "status": "post", "competition": "cl", "competitionName": "UEFA Champions League, League Phase",
   "startTime": "18:45", "home": {"abbreviation": "FEN", "score": "2", "color": "#ffff00"},
   "away": {"abbreviation": "SLP", "score": "2", "color": "#ffffff"}, "goals": [...], "cards": [...]}
]}
```
- `status` : `pre`, `in`, `post` (ESPN). `competition` : clé de `competitions.json`, `null` pour les autres compétitions
  (avec `?all=true`) ; `competitionName` : libellé ESPN.
- Chaque équipe a sa couleur (`color`, `#rrggbb`), quel que soit le statut du match.
- Match en direct : en plus, `period` (1 ou 2, puis prolongations), `clock` (`67'`, `45'+2'`), `detail` (`HT` à la mi-temps).
- `startTime` : heure de coup d'envoi en 24 h, heure de Paris par défaut.
- `goals` : minute, équipe, buteur, type ESPN (`Goal`, `Goal - Header`, `Penalty - Scored`, `Own Goal`…), `penalty`,
  `ownGoal`. Un but contre son camp compte pour l'équipe qui en bénéficie (comme le score) ; `player` est alors le
  joueur adverse. Les tirs au but ne sont pas comptés comme des buts.
- `cards` : minute, équipe, joueur, `card` = `yellow` ou `red`. `player` peut être `null` quand ESPN ne nomme personne
  (carton à un membre du staff).
- ESPN n'a pas de « dernière action » textuelle en football (`situation.lastPlay`, présent en NBA, vaut toujours `null`).
- Rafraîchissement : `/scores` toutes les 20 s quand un match d'une compétition configurée est en cours ou va commencer,
  sinon toutes les 5 min ; `/upcoming` toutes les 30 min. Les matchs des autres compétitions (`?all=true`) peuvent donc
  avoir jusqu'à 5 min de retard quand aucune de vos compétitions ne joue.

## Classements (`/soccer/{cle}Standings`)

```bash
curl -s http://localhost/soccer/ligue1Standings
curl -s "http://localhost/soccer/eplStandings?team=arsenal"
```
```json
{"competition": "ligue1", "name": "French Ligue 1", "season": "2026-27 French Ligue 1", "groups": [{"name": "French Ligue 1 2026-27", "teams": [
  {"rank": 2, "teamId": "160", "abbreviation": "PSG", "name": "Paris Saint-Germain", "color": "#011F68", "played": 7,
   "wins": 4, "draws": 2, "losses": 1, "goalsFor": 14, "goalsAgainst": 9, "goalDifference": 5, "points": 14,
   "zone": {"description": "Champions League", "color": "#81D6AC"}}]}]}
```
- Source : `site.api.espn.com/apis/v2/sports/soccer/{code}/standings`, rechargé en tâche de fond toutes les 30 min,
  toutes les 5 min quand un match de la compétition est en cours (`SOCCER_PROXY_STANDINGS_LIVE_REFRESH_MINUTES`).
- Ordre d'ESPN (par `rank`). `zone` : zone de qualification ou de relégation avec sa couleur ESPN (`null` sinon).
- `color` : couleur de l'équipe, reprise de `/…Teams` (le classement ESPN n'en a pas).
- `groups` : un groupe pour un championnat, la phase de ligue pour les coupes européennes, **aucun pour la Coupe de
  France** (coupe à élimination directe, ESPN ne publie pas de classement).

## Configuration

**`config/competitions.json`** — une entrée par compétition (une clé = deux routes) :
```json
{"key": "ligue1", "espnCode": "fra.1", "name": "Ligue 1", "country": "france"}
```
`key` : lettres et chiffres, commence par une lettre. `country: null` pour une compétition internationale.

**`config/aliases.json`** — par identifiant d'équipe ESPN (visible dans `/…Teams`) :
```json
"160": {"name": "Paris Saint-Germain", "aliases": ["psg", "paris sg", "paris"]},
"887": {"name": "AEK Athens", "country": "greece", "aliases": ["aek"]}
```
`name` sert seulement à la lecture du fichier. `country` (optionnel) force le pays de l'équipe. Le fichier livré
reprend les alias publiés par l'API d'origine (Ligue 1, Ligue 2, Premier League, LaLiga, Serie A) ; les autres
équipes (Bundesliga, coupes européennes) n'ont que l'alias automatique sans accents, à compléter si besoin.

Le dossier `config/` est monté en lecture seule dans le conteneur (`docker-compose.yml`) : après modification,
`docker compose restart proxy` suffit (pas de reconstruction).

**Variables d'environnement** (`.env`, toutes optionnelles) :

| Variable | Défaut | Rôle |
|---|---|---|
| `SOCCER_PROXY_DAYS_BEFORE` | `1` | Début de la fenêtre (J-1) |
| `SOCCER_PROXY_DAYS_AFTER` | `14` | Fin de la fenêtre (J+14) |
| `SOCCER_PROXY_TZ` | `Europe/Paris` | Fuseau qui définit « aujourd'hui » |
| `SOCCER_PROXY_LIVE_REFRESH_SECONDS` | `20` | Rafraîchissement pendant un match (ou avant le coup d'envoi) |
| `SOCCER_PROXY_PREMATCH_MINUTES` | `10` | Passage en rafraîchissement rapide avant le coup d'envoi |
| `SOCCER_PROXY_IDLE_REFRESH_MINUTES` | `30` | Rafraîchissement complet de la fenêtre |
| `SOCCER_PROXY_TEAMS_REFRESH_HOURS` | `24` | Rafraîchissement des équipes |
| `SOCCER_PROXY_REQUEST_PAUSE_MS` | `1000` | Pause entre deux requêtes ESPN |
| `SOCCER_PROXY_RETRY_BASE_SECONDS` | `30` | Premier délai de réessai après une erreur (doublé à chaque échec, max 30 min) |
| `SOCCER_PROXY_SCORES_REFRESH_MINUTES` | `5` | `/scores` quand aucune compétition configurée ne joue |
| `SOCCER_PROXY_UPCOMING_REFRESH_MINUTES` | `30` | `/upcoming` |
| `SOCCER_PROXY_STANDINGS_LIVE_REFRESH_MINUTES` | `5` | Classements pendant les matchs de la compétition (sinon 30 min) |
| `SOCCER_PROXY_REQUESTS_PER_MINUTE` | `60` | Limite par IP et par route |
| `SOCCER_PROXY_CONFIG_DIR` | `/app/config` | Dossier de `competitions.json` et `aliases.json` |

## Fonctionnement

- **Les clients ne déclenchent jamais d'appel ESPN** : un planificateur interne remplit le cache, les routes ne
  font que le lire.
- **Plage de dates** : ESPN refuse aujourd'hui toute plage `dates=AAAAMMJJ-AAAAMMJJ` (HTTP 400 « Failed to get
  events endpoint »). Le proxy interroge donc **par mois** (`dates=AAAAMM`, deux mois en fin ou début de mois,
  fenêtre élargie d'un jour car les mois d'ESPN suivent l'heure américaine) et filtre de J-1 à J+14.
- **Cadence** par compétition : toute la fenêtre toutes les 30 min ; dès qu'un match est en cours ou commence dans
  moins de 10 min, rafraîchissement **toutes les 20 s des seuls jours concernés** (`dates=AAAAMMJJ`, léger) ;
  équipes une fois par jour.
- **Un seul appel ESPN à la fois**, avec une pause d'1 s entre deux appels. Démarrage complet en ~30 s.
- **Panne ESPN** : la dernière réponse valide reste servie (jamais remplacée par une liste vide), l'erreur est
  journalisée et visible dans `/soccer/health`, nouvel essai après 30 s, 1 min, 2 min… (max 30 min).
- **Au démarrage**, tant qu'une compétition n'est pas encore chargée : `503` avec `Retry-After: 10`.
- **En-têtes** : `Cache-Control: public, max-age=10`, `ETag` (réponse `304` si `If-None-Match` correspond),
  `Access-Control-Allow-Origin: *`, `Content-Type: application/json`.
- **Coupe de France** : ESPN ne la couvre qu'à partir des 32ᵉ de finale (décembre-janvier) ; la route renvoie
  `{"events": []}` avant.

Différences avec l'API d'origine (constatées le 14/10/2026) : les matchs communs sont identiques, mais l'origine
renvoie des matchs jusqu'à la fin du mois en cours (31/10) alors que ce proxy s'arrête à J+14 comme spécifié
(réglable avec `SOCCER_PROXY_DAYS_AFTER`) ; l'origine n'a pas de `/…Teams` pour les coupes ni de `/health`, et ses
équipes n'ont pas `color` / `alternateColor`.

## Logos des équipes

Le script `scripts/download_soccer_logos.py` télécharge depuis le CDN d'ESPN le logo de chaque équipe des
compétitions de `config/competitions.json` dans **`soccer_logos/<id équipe ESPN>.png`** (l'`id` des routes
`/…Teams` et `/…UpcomingMatches`), servis par nginx sur **`/soccer/logo/<id>.png`** :

```bash
python scripts/download_soccer_logos.py                       # toutes les compétitions, PNG 500x500 (~13 Mo)
python scripts/download_soccer_logos.py --size 64             # redimensionnés par ESPN (~5 Ko chacun)
python scripts/download_soccer_logos.py --competitions ligue1,cl --variant dark --force
docker compose exec proxy python scripts/download_soccer_logos.py   # depuis le conteneur
curl -o psg.png http://localhost/soccer/logo/160.png
```

- `--variant dark` : version du logo prévue pour un fond sombre ; `--force` : retélécharge les fichiers présents.
- Une équipe présente dans plusieurs compétitions (PSG : ligue1, coupeDeFrance, cl) n'est téléchargée qu'une fois.
- `soccer_logos/index.json` liste chaque équipe : `{"160": {"displayName": "Paris Saint-Germain", "abbreviation": "PSG", "competitions": ["ligue1", "coupeDeFrance", "cl"], "file": "160.png"}}`.
  `file` vaut `null` quand ESPN n'a pas de logo (surtout des clubs amateurs de Coupe de France).
- Relancer le script après un changement de `competitions.json` ou en début de saison (promus, nouvelles équipes
  qualifiées) : seuls les logos manquants sont téléchargés.
- Les images ne sont pas versionnées (`.gitignore`) ; le dossier est monté dans le conteneur (`docker-compose.yml`).

## Node-RED / AWTRIX

Dans le flow, remplacer l'URL de base `https://epl-vps.duckdns.org/` par `http://<IP du serveur>/soccer/`,
par exemple un nœud *http request* sur `http://192.168.1.10/soccer/ligue1UpcomingMatches` déclenché toutes les
20-30 s. Le cache côté serveur étant rafraîchi toutes les 20 s pendant les matchs, interroger plus souvent n'apporte rien.

## Exposition hors du réseau local

Pas d'authentification (usage LAN). Pour exposer le service, placer un reverse proxy HTTPS devant le port 80 et
limiter l'accès, par exemple avec Caddy :
```
scores.example.com {
    basicauth /soccer/* {
        nodered <hash généré par: caddy hash-password>
    }
    reverse_proxy 192.168.1.10:80
}
```
Ou, sans authentification, n'autoriser que certaines adresses dans un bloc nginx placé devant
(`allow 203.0.113.4; deny all;`). Ne pas publier directement le port 80 du conteneur sur Internet.

## Tests

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t .
```
Les tests tournent sur des réponses ESPN réelles enregistrées dans `tests/fixtures/soccer/` (aucun appel réseau) :
projection (champs exacts, `period` absent si programmé, couleurs sans `#`, scores en chaîne, détails des buts et
cartons), fenêtre de dates (fins de mois, changement d'année, fuseau), équipes (alias, pays), planificateur (cadence
live / normale, rafraîchissement du seul jour en direct, conservation des données et délais croissants en cas
d'erreur, un appel à la fois avec pause) et routes (en-têtes, `ETag`/`304`, `503` au démarrage).
