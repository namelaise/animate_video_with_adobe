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
SCRAPER_CONFIG_FILE = BASE_DIR / "scraper_config.json"

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("scraper")
log.setLevel(logging.INFO)
# Configurer les handlers uniquement si pas déjà fait (évite les doublons lors de l'import)
if not log.handlers:
    _fmt = logging.Formatter('[%(asctime)s] %(levelname)-8s [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    _fh = logging.FileHandler(LOG_DIR / "scraper.log", mode="w", encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_sh)
    log.addHandler(_fh)
    log.propagate = False

_FALLBACK_ACCOUNTS = [
    "https://www.tiktok.com/@martinspam001",
    "https://www.tiktok.com/@sticker.art01",
    "https://www.tiktok.com/@le_boss_des_pranks",
    "https://www.tiktok.com/@allomartin",
    "https://www.tiktok.com/@mrmartinrireetchansons",
]
_FALLBACK_LIMIT = 5


def load_scraper_config() -> dict:
    """Lit scraper_config.json. Retourne les valeurs par défaut si absent/corrompu."""
    try:
        if SCRAPER_CONFIG_FILE.exists():
            data = json.loads(SCRAPER_CONFIG_FILE.read_text(encoding="utf-8"))
            accounts = data.get("accounts") or _FALLBACK_ACCOUNTS
            limit = int(data.get("daily_limit") or _FALLBACK_LIMIT)
            return {
                "accounts": accounts,
                "daily_limit": limit,
                "min_duration": int(data.get("min_duration", 10)),
                "max_duration": int(data.get("max_duration", 600)),
            }
    except Exception:
        log.warning("scraper_config.json illisible, utilisation des valeurs par défaut.")
    return {
        "accounts": _FALLBACK_ACCOUNTS,
        "daily_limit": _FALLBACK_LIMIT,
        "min_duration": 10,
        "max_duration": 600,
    }


_config = load_scraper_config()
DEFAULT_ACCOUNTS = _config["accounts"]
DEFAULT_DAILY_LIMIT = _config["daily_limit"]


def load_history() -> set:
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            return set(data.get("downloaded_ids", []))
        except Exception:
            log.warning("Historique corrompu, reset.")
    return set()


def _atomic_write(path: Path, content: str):
    """Écrit dans un fichier temporaire puis renomme atomiquement (évite la corruption)."""
    import tempfile
    dir_ = path.parent
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def save_history(ids: set):
    _atomic_write(
        HISTORY_FILE,
        json.dumps({"downloaded_ids": sorted(ids)}, indent=2, ensure_ascii=False),
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
    _atomic_write(
        DAILY_LOG_FILE,
        json.dumps({"date": today, "count": count}, indent=2),
    )


YTDLP_CMD = [sys.executable, "-m", "yt_dlp"]
YTDLP_UPDATE_CACHE = BASE_DIR / ".ytdlp_last_update"


def ensure_ytdlp():
    # Verifier si yt-dlp est disponible
    try:
        subprocess.run(YTDLP_CMD + ["--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        log.error("yt-dlp n'est pas installe. Installation en cours...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "yt-dlp"], check=True)
            log.info("yt-dlp installe avec succes.")
        except Exception as e:
            log.error(f"Impossible d'installer yt-dlp: {e}")
            return False

    # Mise a jour une seule fois par jour (evite ~60s de pip a chaque lancement)
    today = date.today().isoformat()
    last_update = ""
    if YTDLP_UPDATE_CACHE.exists():
        try:
            last_update = YTDLP_UPDATE_CACHE.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    if last_update != today:
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                YTDLP_UPDATE_CACHE.write_text(today, encoding="utf-8")
                if "already satisfied" not in r.stdout.lower():
                    log.info("yt-dlp mis a jour.")
        except Exception:
            pass  # pas bloquant
    return True


def _run_ytdlp_with_cookie_fallback(base_args: list[str], url: str) -> subprocess.CompletedProcess:
    """Lance yt-dlp en essayant cookies.txt > firefox > edge > chrome > sans cookies."""
    cookies_file = BASE_DIR / "cookies.txt"
    candidates = []
    if cookies_file.exists():
        candidates.append(["--cookies", str(cookies_file)])
    for browser in ("firefox", "edge", "chrome"):
        candidates.append(["--cookies-from-browser", browser])
    candidates.append([])  # sans cookies en dernier recours

    last_result = None
    for cookie_args in candidates:
        cmd = YTDLP_CMD + base_args + cookie_args + [url]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace"
        )
        last_result = result
        stderr_low = result.stderr.lower()
        cookie_error = ("could not copy" in stderr_low) or ("cookie" in stderr_low and "error" in stderr_low)
        if cookie_error:
            source = cookie_args[1] if len(cookie_args) >= 2 else "aucun"
            log.warning(f"Cookies inaccessibles ({source}), essai suivant...")
            continue
        return result
    return last_result


def list_videos_from_account(account_url: str, max_videos: int = 30) -> list[dict]:
    """Recupere la liste des videos d'un compte TikTok via yt-dlp --flat-playlist."""
    base_args = [
        "--flat-playlist",
        "-j",
        "--playlist-end", str(max_videos),
        "--no-warnings",
    ]
    try:
        result = _run_ytdlp_with_cookie_fallback(base_args, account_url)
        if result.returncode != 0 and result.stderr.strip():
            log.warning(f"yt-dlp stderr ({account_url}): {result.stderr.strip()[:500]}")

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

        if not videos and result.stdout.strip():
            log.warning(f"stdout non-vide mais aucun JSON parse ({account_url}): {result.stdout.strip()[:300]}")

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
    base_args = [
        "--no-warnings",
        "--merge-output-format", "mp4",
        "-o", output_template,
    ]
    try:
        result = _run_ytdlp_with_cookie_fallback(base_args, video_url)
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
                cfg = load_scraper_config()
                dur = v.get("duration")
                if dur is not None and (dur < cfg["min_duration"] or dur > cfg["max_duration"]):
                    log.info(f"  Skip {vid} (duree={dur}s hors limites [{cfg['min_duration']}-{cfg['max_duration']}s])")
                    continue
                v["_account_url"] = account_url
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
            acct = v.get("_account_url", "")
            username = acct.rstrip("/").split("@")[-1] if "@" in acct else "user"
            url = f"https://www.tiktok.com/@{username}/video/{vid}"

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
