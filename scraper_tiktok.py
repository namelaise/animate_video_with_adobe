# scraper_tiktok.py
# Télécharge automatiquement les nouvelles vidéos depuis des comptes TikTok
# via yt-dlp, en évitant les doublons grâce à un historique persistant.
#
# Usage:
#   python scraper_tiktok.py                    # télécharge jusqu'à 5 nouvelles vidéos
#   python scraper_tiktok.py --limit 3          # limite à 3 vidéos
#   python scraper_tiktok.py --accounts url1 url2  # comptes spécifiques

import os
import sys
import json
import argparse
import subprocess
import logging
from pathlib import Path
from datetime import datetime, date

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(os.getenv("BASE_DIR", os.path.dirname(os.path.abspath(__file__))))
DOWNLOAD_DIR = BASE_DIR / "download"
HISTORY_FILE = BASE_DIR / "scraper_history.json"
DAILY_LOG_FILE = BASE_DIR / "scraper_daily.json"

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "scraper.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("scraper")

DEFAULT_ACCOUNTS = [
    "https://www.tiktok.com/@martinspam001",
    "https://www.tiktok.com/@sticker.art01",
    "https://www.tiktok.com/@le_boss_des_pranks",
    "https://www.tiktok.com/@allomartin",
    "https://www.tiktok.com/@mrmartinrireetchansons",
]

DEFAULT_DAILY_LIMIT = 5


def load_history() -> set:
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            return set(data.get("downloaded_ids", []))
        except Exception:
            log.warning("Historique corrompu, reset.")
    return set()


def save_history(ids: set):
    HISTORY_FILE.write_text(
        json.dumps({"downloaded_ids": sorted(ids)}, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def get_daily_count() -> int:
    today = date.today().isoformat()
    if DAILY_LOG_FILE.exists():
        try:
            data = json.loads(DAILY_LOG_FILE.read_text(encoding="utf-8"))
            if data.get("date") == today:
                return data.get("count", 0)
        except Exception:
            pass
    return 0


def set_daily_count(count: int):
    today = date.today().isoformat()
    DAILY_LOG_FILE.write_text(
        json.dumps({"date": today, "count": count}, indent=2),
        encoding="utf-8"
    )


def ensure_ytdlp():
    try:
        subprocess.run(["yt-dlp", "--version"], capture_output=True, check=True)
        return True
    except FileNotFoundError:
        log.error("yt-dlp n'est pas installe. Installation en cours...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "yt-dlp"], check=True)
            log.info("yt-dlp installe avec succes.")
            return True
        except Exception as e:
            log.error(f"Impossible d'installer yt-dlp: {e}")
            return False


def list_videos_from_account(account_url: str, max_videos: int = 30) -> list[dict]:
    """Recupere la liste des videos d'un compte TikTok via yt-dlp --flat-playlist."""
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--print-json",
        "--playlist-end", str(max_videos),
        "--no-warnings",
        account_url,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace"
        )
        videos = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                info = json.loads(line)
                videos.append({
                    "id": info.get("id", ""),
                    "url": info.get("url") or info.get("webpage_url") or "",
                    "title": info.get("title", ""),
                    "duration": info.get("duration"),
                })
            except json.JSONDecodeError:
                continue
        return videos
    except subprocess.TimeoutExpired:
        log.warning(f"Timeout lors du listing de {account_url}")
        return []
    except Exception as e:
        log.error(f"Erreur listing {account_url}: {e}")
        return []


def download_video(video_url: str, video_id: str) -> bool:
    """Telecharge une video TikTok dans le dossier download/."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    output_template = str(DOWNLOAD_DIR / f"{video_id}.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--merge-output-format", "mp4",
        "-o", output_template,
        video_url,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace"
        )
        if result.returncode == 0:
            log.info(f"Telecharge: {video_id}")
            return True
        else:
            log.error(f"Echec telechargement {video_id}: {result.stderr[:500]}")
            return False
    except subprocess.TimeoutExpired:
        log.warning(f"Timeout telechargement {video_id}")
        return False
    except Exception as e:
        log.error(f"Erreur telechargement {video_id}: {e}")
        return False


def scrape_accounts(accounts: list[str], limit: int = DEFAULT_DAILY_LIMIT) -> int:
    """
    Scrape les comptes TikTok et telecharge les nouvelles videos.
    Retourne le nombre de videos telechargees.
    """
    if not ensure_ytdlp():
        return 0

    history = load_history()
    daily_count = get_daily_count()
    remaining = max(0, limit - daily_count)

    if remaining <= 0:
        log.info(f"Quota quotidien atteint ({daily_count}/{limit}). Pas de telechargement.")
        return 0

    log.info(f"Quota du jour: {daily_count}/{limit} — {remaining} video(s) restante(s)")

    # Collecter les videos de tous les comptes
    all_new_videos = []
    for account_url in accounts:
        account_name = account_url.rstrip("/").split("@")[-1] if "@" in account_url else account_url
        log.info(f"Scan du compte: @{account_name}")
        videos = list_videos_from_account(account_url, max_videos=15)
        log.info(f"  {len(videos)} video(s) trouvee(s)")

        for v in videos:
            vid = v["id"]
            if vid and vid not in history:
                # Filtrer les videos trop courtes (< 10s) ou trop longues (> 10min)
                dur = v.get("duration")
                if dur is not None and (dur < 10 or dur > 600):
                    log.info(f"  Skip {vid} (duree={dur}s hors limites)")
                    continue
                all_new_videos.append(v)

    if not all_new_videos:
        log.info("Aucune nouvelle video trouvee sur les comptes.")
        return 0

    # Melanger pour varier les sources
    import random
    random.shuffle(all_new_videos)

    downloaded = 0
    for v in all_new_videos:
        if downloaded >= remaining:
            break

        vid = v["id"]
        url = v["url"]
        if not url:
            url = f"https://www.tiktok.com/@user/video/{vid}"

        log.info(f"Telechargement {downloaded + 1}/{remaining}: {vid} ({v.get('title', '')[:50]})")
        if download_video(url, vid):
            history.add(vid)
            save_history(history)
            downloaded += 1
            daily_count += 1
            set_daily_count(daily_count)

    log.info(f"Scraping termine: {downloaded} video(s) telechargee(s)")
    return downloaded


def main():
    ap = argparse.ArgumentParser(description="Scraper TikTok pour Mr Martin")
    ap.add_argument("--accounts", nargs="+", default=None,
                    help="URLs des comptes TikTok a scraper")
    ap.add_argument("--limit", type=int, default=DEFAULT_DAILY_LIMIT,
                    help=f"Nombre max de videos par jour (defaut: {DEFAULT_DAILY_LIMIT})")
    args = ap.parse_args()

    accounts = args.accounts or DEFAULT_ACCOUNTS
    scrape_accounts(accounts, limit=args.limit)


if __name__ == "__main__":
    main()
