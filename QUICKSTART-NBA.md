# NBA Proxy - Guide d'installation rapide

## 🚀 Installation en 3 étapes

### 1. Télécharger les logos NBA (optionnel mais recommandé)

```bash
bash scripts/download_nba_logos.sh
```

Cela télécharge les logos des 30 équipes NBA depuis ESPN CDN.

### 2. Lancer le projet

```bash
docker-compose up -d --build
```

### 3. Tester le proxy NBA

```bash
# Test avec les Lakers
curl "http://localhost/nbadata/proxy?teamName=LAL"

# Test avec les Celtics
curl "http://localhost/nbadata/proxy?teamName=BOS"

# Test avec nom complet
curl "http://localhost/nbadata/proxy?teamName=Lakers"
```

---

## 📋 Endpoints disponibles

| Endpoint | Description |
|---|---|
| `GET /nbadata/proxy?teamName=LAL` | Données d'équipe (scores, standings, etc.) |
| `GET /nbadata/logo/lal.png` | Logo de l'équipe (si téléchargé) |
| `GET /nbadata/debug/teams` | Liste toutes les équipes et leurs aliases |

---

## 🎯 Paramètres acceptés

| Paramètre | Type | Description | Exemple |
|---|---|---|---|
| `teamName` | string (requis) | Nom ou abréviation de l'équipe | `LAL`, `Lakers`, `lakers` |
| `force` | boolean | Bypass le cache | `force=true` |

---

## 🏀 Équipes supportées

Toutes les 30 équipes NBA sont supportées. Voici quelques exemples :

| Équipe | Abréviations acceptées |
|---|---|
| Los Angeles Lakers | `LAL`, `Lakers`, `Los Angeles` |
| Boston Celtics | `BOS`, `Celtics`, `Boston` |
| Golden State Warriors | `GSW`, `Warriors`, `Golden State` |
| Miami Heat | `MIA`, `Heat`, `Miami` |
| Chicago Bulls | `CHI`, `Bulls`, `Chicago` |

**Liste complète :** `curl http://localhost/nbadata/debug/teams`

---

## 💾 Configuration (optionnelle)

Créer un fichier `.env` (copier depuis `sample.env`) et ajuster :

```bash
# Cache (en minutes)
NBADATA_PROXY_CACHE_LIFE=5

# Rate limiting (requêtes par minute)
NBADATA_PROXY_REQUESTS_PER_MINUTE=15
```

---

## 📊 Exemple de réponse

```json
{
  "teamId": "13",
  "season": "2025",
  "team": {
    "fullName": "Los Angeles Lakers",
    "abbreviation": "LAL",
    "conference": "Western",
    "division": "Pacific"
  },
  "standings": {
    "wins": 35,
    "losses": 18,
    "conferenceRank": "3rd",
    "divisionRank": "1st"
  },
  "lastGame": {
    "opponent": "GSW",
    "score": "118-105",
    "result": "Won"
  },
  "liveGame": {
    "isLive": false
  },
  "nextGame": {
    "opponent": "PHX",
    "date": "Feb 12",
    "gameTime": "9:00 PM"
  }
}
```

---

## 🔧 Dépannage

### Le proxy ne démarre pas

```bash
# Vérifier les logs
docker-compose logs -f

# Vérifier que le port 8089 n'est pas utilisé
lsof -i :8089
```

### Erreur 404 sur les logos

```bash
# Télécharger les logos
bash scripts/download_nba_logos.sh

# Vérifier que le dossier est monté
docker-compose exec proxy ls -la /app/nba_logos/
```

### Cache ne fonctionne pas

```bash
# Forcer un refresh
curl "http://localhost/nbadata/proxy?teamName=LAL&force=true"
```

---

## 📚 Documentation complète

Voir [README-NBA.md](README-NBA.md) pour :
- Structure JSON détaillée
- Différences avec les proxies MLB/NFL
- Exemples d'utilisation avancés
- Intégration avec InfoOrbs

---

**Enjoy! 🏀**
