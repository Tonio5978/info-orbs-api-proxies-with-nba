#!/usr/bin/env python3
"""Download the logos of the soccer teams of config/competitions.json from the ESPN CDN.

Logos are saved as soccer_logos/<ESPN team id>.png (the id used by the /soccer/... routes) and served by
nginx at /soccer/logo/<id>.png. An index.json maps each id to the team name, abbreviation and competitions.

Run from the project root:
    python scripts/download_soccer_logos.py                      # all competitions, 500x500 originals
    python scripts/download_soccer_logos.py --size 64            # resized by the ESPN CDN (smaller files)
    python scripts/download_soccer_logos.py --competitions ligue1,cl --variant dark --force
Inside the container (as root, the logos folder is mounted from the host):
    docker compose exec proxy python scripts/download_soccer_logos.py
"""
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/teams"
RESIZE_URL = "https://a.espncdn.com/combiner/i?img={path}&w={size}&h={size}"
MIN_LOGO_BYTES = 500  # Smaller bodies are placeholders, not logos


def pick_logo(team: dict, variant: str) -> Optional[str]:
    """URL of the team's 'default' (or 'dark') logo, falling back to the first one."""
    logos = team.get("logos") or []
    for logo in logos:
        if variant in logo.get("rel", []):
            return logo.get("href")
    return logos[0].get("href") if logos else None


def logo_url(href: str, size: Optional[int]) -> str:
    """Original URL, or the ESPN CDN resize service for a size x size image."""
    if not size:
        return href
    return RESIZE_URL.format(path=urlsplit(href).path, size=size)


def collect_teams(competitions: List[dict], fetch_json, pause: float) -> Dict[str, dict]:
    """Teams of all competitions, deduplicated by ESPN id (PSG plays in ligue1 and cl)."""
    teams: Dict[str, dict] = {}
    for n, competition in enumerate(competitions):
        if n:
            time.sleep(pause)
        data = fetch_json(TEAMS_URL.format(code=competition["espnCode"]))
        entries = ((data.get("sports") or [{}])[0].get("leagues") or [{}])[0].get("teams", [])
        print(f"  {competition['key']:<15} {len(entries):3} équipes")
        for entry in entries:
            team = entry.get("team", {})
            team_id = str(team.get("id"))
            info = teams.setdefault(team_id, {"team": team, "competitions": []})
            info["competitions"].append(competition["key"])
    return teams


def main() -> int:
    parser = argparse.ArgumentParser(description="Télécharge les logos des équipes de football (ESPN)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "soccer_logos")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "competitions.json")
    parser.add_argument("--competitions", help="clés séparées par des virgules (défaut : toutes)")
    parser.add_argument("--size", type=int, help="taille en pixels (carré), redimensionné par ESPN ; défaut : 500 d'origine")
    parser.add_argument("--variant", choices=["default", "dark"], default="default", help="logo pour fond clair ou sombre")
    parser.add_argument("--force", action="store_true", help="retélécharger les logos déjà présents")
    parser.add_argument("--pause", type=float, default=0.2, help="pause entre deux requêtes ESPN (s)")
    args = parser.parse_args()

    competitions = json.loads(args.config.read_text(encoding="utf-8"))
    if args.competitions:
        wanted = {k.strip() for k in args.competitions.split(",")}
        unknown = wanted - {c["key"] for c in competitions}
        if unknown:
            print(f"Compétitions inconnues : {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        competitions = [c for c in competitions if c["key"] in wanted]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "InfoOrbs-Soccer-Proxy/1.0"}) as client:
        def fetch_json(url):
            r = client.get(url)
            r.raise_for_status()
            return r.json()

        print(f"Lecture des équipes de {len(competitions)} compétitions…")
        teams = collect_teams(competitions, fetch_json, args.pause)
        print(f"\nTéléchargement de {len(teams)} logos → {args.output_dir}")

        downloaded = skipped = 0
        failed, no_logo = [], []
        index = {}
        for team_id, info in sorted(teams.items(), key=lambda kv: kv[1]["team"].get("displayName", "")):
            team = info["team"]
            name = team.get("displayName", team_id)
            dest = args.output_dir / f"{team_id}.png"
            index[team_id] = {"displayName": name, "abbreviation": team.get("abbreviation"),
                              "competitions": info["competitions"], "file": dest.name}
            if dest.exists() and not args.force:
                skipped += 1
                continue
            href = pick_logo(team, args.variant)
            if not href:
                no_logo.append(f"{name} ({team_id})")  # Mostly amateur clubs of the Coupe de France
                index[team_id]["file"] = None
                continue
            try:
                time.sleep(args.pause)
                r = client.get(logo_url(href, args.size))
                r.raise_for_status()
                if not r.headers.get("content-type", "").startswith("image/") or len(r.content) < MIN_LOGO_BYTES:
                    raise ValueError(f"réponse inattendue ({r.headers.get('content-type')}, {len(r.content)} octets)")
                dest.write_bytes(r.content)
                downloaded += 1
            except Exception as e:
                failed.append(f"{name} ({team_id}) : {e}")
                index[team_id]["file"] = None if not dest.exists() else dest.name

        # Merge with the existing index, so a run on a few competitions (--competitions) keeps the other teams
        index_path = args.output_dir / "index.json"
        try:
            merged = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
        except ValueError:
            merged = {}
        for team_id, entry in index.items():
            previous = merged.get(team_id, {})
            known = previous.get("competitions", [])
            entry["competitions"] = known + [c for c in entry["competitions"] if c not in known]
            merged[team_id] = entry
        index_path.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n  Téléchargés : {downloaded}\n  Déjà présents : {skipped}\n  Sans logo chez ESPN : {len(no_logo)}"
          f"\n  Échecs : {len(failed)}")
    if no_logo:
        print("    " + ", ".join(no_logo))
    for line in failed:
        print(f"    ✗ {line}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
