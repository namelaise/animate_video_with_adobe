"""
fetch_stats.py — Récupère les stats TikTok des vidéos postées et les corrèle aux prompts utilisés.

Nécessite : scope video.list (approuvé en production)
Usage     : python fetch_stats.py
            python fetch_stats.py --report       # rapport par prompt uniquement
            python fetch_stats.py --update-only  # màj upload_history.json sans affichage
"""

import os
import json
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# Permet l'import de tiktok_account_manager même quand le script est lancé depuis la racine
sys.path.insert(0, str(Path(__file__).resolve().parent))

load_dotenv()

BASE_DIR        = Path(os.getenv("BASE_DIR", Path(__file__).parent.parent))
HISTORY_FILE    = BASE_DIR / "state" / "upload_history.json"
TOKEN_FILE      = BASE_DIR / "config" / "tiktok_tokens.json"

FIELDS = "id,create_time,view_count,like_count,comment_count,share_count,title,duration"
VIDEO_LIST_URL  = "https://open.tiktokapis.com/v2/video/list/"
VIDEO_QUERY_URL = "https://open.tiktokapis.com/v2/video/query/"


# ── Token ──────────────────────────────────────────────────────────────────────

def get_token() -> str:
    # Priorité 1 : compte actif via le gestionnaire multi-compte
    try:
        from tiktok_account_manager import get_access_token
        tok = (get_access_token() or "").strip()
        if tok:
            return tok
    except Exception:
        pass
    # Fallback : .env / fichier tokens (compat ancien)
    tok = (os.getenv("TIKTOK_USER_ACCESS_TOKEN") or "").strip()
    if not tok and TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            tok = data.get("access_token", "")
        except Exception:
            pass
    if not tok:
        sys.exit("[ERR] TIKTOK_USER_ACCESS_TOKEN manquant. Lance d'abord auth_tiktok_refresh.py")
    return tok


# ── API calls ─────────────────────────────────────────────────────────────────

def list_videos(token: str, max_count: int = 20, cursor: int = 0) -> list[dict]:
    """Retourne les N dernières vidéos du compte (nécessite video.list)."""
    r = requests.post(
        VIDEO_LIST_URL,
        params={"fields": FIELDS},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"max_count": max_count, "cursor": cursor},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    err = data.get("error", {})
    if err.get("code") not in (None, "ok", ""):
        sys.exit(f"[ERR] TikTok API: {err}")
    return data.get("data", {}).get("videos", [])


def query_videos(token: str, video_ids: list[str]) -> list[dict]:
    """Récupère les stats de video_ids spécifiques (nécessite video.list)."""
    if not video_ids:
        return []
    r = requests.post(
        VIDEO_QUERY_URL,
        params={"fields": FIELDS},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"filters": {"video_ids": video_ids}},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    err = data.get("error", {})
    if err.get("code") not in (None, "ok", ""):
        print(f"[WARN] query_videos erreur: {err}")
        return []
    return data.get("data", {}).get("videos", [])


# ── Matching publish_id → video_id ────────────────────────────────────────────

def match_videos_by_timestamp(
    history: list[dict],
    api_videos: list[dict],
    tolerance_seconds: int = 7200,   # ±2h (inbox → publication manuelle)
) -> int:
    """
    Pour les entrées sans video_id, essaie de les matcher avec les vidéos API
    par proximité de timestamp. Modifie history in-place, retourne le nb de matches.
    """
    matched = 0
    unmatched = [e for e in history if e.get("upload_ok") and not e.get("video_id")]

    for entry in unmatched:
        try:
            upload_ts = datetime.fromisoformat(entry["timestamp"]).timestamp()
        except Exception:
            continue

        best_vid = None
        best_delta = tolerance_seconds + 1

        for vid in api_videos:
            create_ts = vid.get("create_time", 0)
            delta = abs(create_ts - upload_ts)
            if delta < best_delta:
                best_delta = delta
                best_vid = vid

        if best_vid:
            entry["video_id"]      = best_vid.get("id")
            entry["view_count"]    = best_vid.get("view_count")
            entry["like_count"]    = best_vid.get("like_count")
            entry["comment_count"] = best_vid.get("comment_count")
            entry["share_count"]   = best_vid.get("share_count")
            entry["title"]         = best_vid.get("title", "")
            entry["stats_fetched_at"] = datetime.now().isoformat(timespec="seconds")
            matched += 1

    return matched


def refresh_known_video_stats(token: str, history: list[dict]) -> int:
    """Rafraîchit les stats des entrées qui ont déjà un video_id."""
    known_ids = list({e["video_id"] for e in history if e.get("video_id")})
    if not known_ids:
        return 0

    # Requêtes par lots de 20 (limite API)
    updated = 0
    for i in range(0, len(known_ids), 20):
        batch = known_ids[i:i+20]
        vids = query_videos(token, batch)
        stats_by_id = {v["id"]: v for v in vids}
        for entry in history:
            vid = stats_by_id.get(entry.get("video_id"))
            if vid:
                entry["view_count"]    = vid.get("view_count")
                entry["like_count"]    = vid.get("like_count")
                entry["comment_count"] = vid.get("comment_count")
                entry["share_count"]   = vid.get("share_count")
                entry["stats_fetched_at"] = datetime.now().isoformat(timespec="seconds")
                updated += 1

    return updated


# ── Rapport ───────────────────────────────────────────────────────────────────

def print_report(history: list[dict]):
    """Affiche un rapport par template de prompt."""
    from collections import defaultdict

    by_template: dict[str, list[dict]] = defaultdict(list)
    for entry in history:
        if entry.get("upload_ok") and entry.get("view_count") is not None:
            by_template[entry.get("prompt_template", "(inconnu)")].append(entry)

    if not by_template:
        print("\n⚠️  Aucune donnée de stats disponible.")
        print("   → Les stats arrivent ~24h après publication (mode INBOX = publication manuelle).")
        print("   → Relance fetch_stats.py quand les vidéos sont publiées.")
        return

    print("\n" + "═" * 70)
    print("  CLASSEMENT DES PROMPTS PAR VUES MOYENNES")
    print("═" * 70)

    rows = []
    for template, entries in by_template.items():
        views   = [e["view_count"]    or 0 for e in entries]
        likes   = [e["like_count"]    or 0 for e in entries]
        shares  = [e["share_count"]   or 0 for e in entries]
        rows.append({
            "template"  : template,
            "videos"    : len(entries),
            "avg_views" : sum(views)  // len(views),
            "avg_likes" : sum(likes)  // len(likes),
            "avg_shares": sum(shares) // len(shares),
            "max_views" : max(views),
        })

    rows.sort(key=lambda r: r["avg_views"], reverse=True)

    for i, r in enumerate(rows, 1):
        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f" {i}."))
        print(f"\n{medal}  {r['template']}")
        print(f"     Vidéos : {r['videos']}  |  "
              f"Vues moy. : {r['avg_views']:,}  |  "
              f"Max vues : {r['max_views']:,}  |  "
              f"Likes moy. : {r['avg_likes']:,}  |  "
              f"Partages moy. : {r['avg_shares']:,}")

    print("\n" + "═" * 70)

    # Toutes les vidéos triées par vues
    print("\nDÉTAIL PAR VIDÉO :")
    all_entries = [e for entries in by_template.values() for e in entries]
    all_entries.sort(key=lambda e: e.get("view_count") or 0, reverse=True)
    for e in all_entries:
        ts = e["timestamp"][:10]
        title = (e.get("title") or "")[:35]
        tmpl  = (e.get("prompt_template") or "?")[:25]
        print(f"  {ts}  {e['view_count']:>8,} vues  "
              f"❤ {e['like_count'] or 0:<6}  "
              f"↗ {e['share_count'] or 0:<5}  "
              f"[{tmpl}]  {title}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Récupère les stats TikTok des vidéos postées")
    ap.add_argument("--report",      action="store_true", help="Affiche uniquement le rapport (pas d'appel API)")
    ap.add_argument("--update-only", action="store_true", help="Màj silencieuse, pas de rapport")
    ap.add_argument("--max",         type=int, default=20, help="Nb max de vidéos à récupérer via list (défaut 20)")
    args = ap.parse_args()

    # Charger l'historique local
    if not HISTORY_FILE.exists():
        print(f"[INFO] Aucun historique trouvé ({HISTORY_FILE}).")
        print("       Les vidéos sont trackées automatiquement à chaque upload par main_v3.py.")
        return

    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    total_uploads = len([e for e in history if e.get("upload_ok")])
    print(f"[INFO] {total_uploads} upload(s) dans l'historique.")

    if args.report:
        print_report(history)
        return

    # Appels API
    token = get_token()
    print(f"[INFO] Récupération des {args.max} dernières vidéos TikTok…")

    try:
        api_videos = list_videos(token, max_count=args.max)
        print(f"[INFO] {len(api_videos)} vidéo(s) récupérée(s) depuis l'API.")
    except Exception as e:
        print(f"[ERR] list_videos échoué: {e}")
        print("      → Vérifie que le scope video.list est approuvé et que le token est valide.")
        return

    # Matcher les nouvelles entrées par timestamp
    new_matches = match_videos_by_timestamp(history, api_videos)
    print(f"[INFO] {new_matches} nouvelle(s) vidéo(s) matchée(s) par timestamp.")

    # Rafraîchir les stats des vidéos déjà connues
    refreshed = refresh_known_video_stats(token, history)
    print(f"[INFO] {refreshed} entrée(s) mise(s) à jour.")

    # Sauvegarder
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] Historique sauvé → {HISTORY_FILE}")

    if not args.update_only:
        print_report(history)


if __name__ == "__main__":
    main()
