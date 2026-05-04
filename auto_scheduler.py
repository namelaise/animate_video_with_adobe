
import socket
import os
import time
import threading
import shutil
import glob
import subprocess
import logging
from datetime import datetime
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

# Force UTF-8 sur stdout/stderr (Windows cp1252 bloque les emojis dans les logs)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).parent / "pipeline"))
sys.path.insert(0, str(Path(__file__).parent / "tiktok"))
from scraper_tiktok import scrape_accounts, DEFAULT_ACCOUNTS, DEFAULT_DAILY_LIMIT, get_daily_count


# ==============================
# Configuration
# ==============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Toujours utiliser le Python du venv (même si le scheduler est lancé sans activate)
_VENV_PYTHON = os.path.join(BASE_DIR, "venv", "Scripts", "python.exe")
PYTHON_EXE = _VENV_PYTHON if os.path.isfile(_VENV_PYTHON) else sys.executable

DOWNLOAD_CANDIDATES = [
    os.path.join(BASE_DIR, 'download'),
    os.path.join(BASE_DIR, 'downloads'),
    os.path.join(BASE_DIR, 'Download'),
    os.path.join(BASE_DIR, 'Download.mp4'),
]
MAIN_INPUT_PATH = os.path.join(BASE_DIR, 'Download.mp4')
COMPOSITE_DIR = os.path.join(BASE_DIR, 'video_composite')
POSTED_DIR = os.path.join(COMPOSITE_DIR, 'posted')
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

GENERATE_INTERVAL = 10   # secondes (ici mis à 10 pour test)
POST_INTERVAL = 10       # secondes (idem)
REJECTED_DIR = os.path.join(BASE_DIR, 'moderation_rejected')
MAX_RETRIES = 3  # Nombre max d'échecs pipeline avant abandon définitif d'une vidéo

VIDEO_EXTS = ('.mp4', '.mov', '.mkv', '.avi')

# ==============================
# Logging (console + fichier)
# ==============================
os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger("scheduler")
log.setLevel(logging.INFO)

formatter = logging.Formatter('[%(asctime)s] %(levelname)-8s [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# Handler console
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
log.addHandler(console_handler)

file_handler = RotatingFileHandler(
    LOG_FILE,
    mode="w",                  # logs frais a chaque lancement
    maxBytes=2 * 1024 * 1024,  # 2 MB max
    backupCount=1,
    encoding="utf-8"
)
file_handler.setFormatter(formatter)
log.addHandler(file_handler)
log.propagate = False



# ==============================
# Fonctions
# ==============================
def ensure_dirs():
    os.makedirs(COMPOSITE_DIR, exist_ok=True)
    os.makedirs(POSTED_DIR, exist_ok=True)


def find_download_videos():
    """Retourne la liste des fichiers vidéos trouvés dans ./download, triées par date de création."""
    download_dir = os.path.join(BASE_DIR, 'download')
    if not os.path.isdir(download_dir):
        return []
    videos = []
    for ext in VIDEO_EXTS:
        videos.extend(glob.glob(os.path.join(download_dir, f'*{ext}')))
    # Trie par date de création (du plus ancien au plus récent)
    videos.sort(key=lambda f: os.path.getctime(f))
    return videos


def pick_video_for_processing():
    vids = find_download_videos()
    if not vids:
        return None
    return vids[0]


def prepare_main_input(src_video):
    """Copie la vidéo sélectionnée vers MAIN_INPUT_PATH. Ne supprime PAS la source ici."""
    try:
        if os.path.abspath(src_video) == os.path.abspath(MAIN_INPUT_PATH):
            log.info('La vidéo sélectionnée est déjà Download.mp4')
            return True
        shutil.copy2(src_video, MAIN_INPUT_PATH)
        log.info(f'Copié {src_video} -> {MAIN_INPUT_PATH}')
        return True
    except Exception as e:
        log.exception('Erreur lors de la copie vers Download.mp4')
        return False


def _pump_stream(pipe, log_fn):
    for line in iter(pipe.readline, ''):
        if not line:
            break
        log_fn(line.rstrip('\n'))
    pipe.close()


def run_main_script():
    """Lance main.py et STREAM les logs en temps réel dans la console + fichier."""
    try:
        cmd = [
            PYTHON_EXE,
            "-X", "utf8",
            "-u",
            os.path.join(BASE_DIR, 'main_v3.py')
        ]
        log.info('Lancement de main.py avec %s', PYTHON_EXE)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        # Lire la concurrence Adobe depuis gui_config.json
        _gui_cfg = os.path.join(BASE_DIR, "gui_config.json")
        try:
            import json as _json
            if os.path.exists(_gui_cfg):
                _conc = _json.loads(open(_gui_cfg, encoding="utf-8").read()).get("adobe_concurrency", 8)
                env["ADOBE_CONCURRENCY"] = str(int(_conc))
        except Exception:
            pass

        p = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env
        )

        t_out = threading.Thread(target=_pump_stream, args=(p.stdout, lambda s: log.info("main.py: %s", s)))
        t_err = threading.Thread(target=_pump_stream, args=(p.stderr, lambda s: log.error("main.py: %s", s)))
        t_out.daemon = True
        t_err.daemon = True
        t_out.start()
        t_err.start()

        rc = p.wait()
        t_out.join(timeout=1)
        t_err.join(timeout=1)

        log.info('main.py terminé avec code %s', rc)
        return rc  # 0=succès, 1=erreur retry, 2=rejet permanent

    except Exception:
        log.exception('Erreur lors de l\'exécution streaming de main.py')
        return 1



SCRAPE_INTERVAL = 3600  # Re-verifier toutes les heures si le quota n'est pas atteint


def scrape_if_needed():
    """
    Telecharge des videos si le quota quotidien n'est pas atteint.
    Attend la connexion internet avant de scraper.
    """
    daily = get_daily_count()
    if daily >= DEFAULT_DAILY_LIMIT:
        log.info(f'Quota scraping atteint ({daily}/{DEFAULT_DAILY_LIMIT}), pas de telechargement.')
        return 0

    # Verifier aussi combien de videos sont deja en attente dans download/
    pending_downloads = find_download_videos()
    if len(pending_downloads) >= DEFAULT_DAILY_LIMIT:
        log.info(f'{len(pending_downloads)} video(s) deja en attente dans download/, scraping differe.')
        return 0

    log.info('Lancement du scraping TikTok...')
    wait_for_internet(poll_every=30)
    try:
        downloaded = scrape_accounts(DEFAULT_ACCOUNTS, limit=DEFAULT_DAILY_LIMIT)
        log.info(f'Scraping termine: {downloaded} video(s) telechargee(s)')
        return downloaded
    except Exception:
        log.exception('Erreur lors du scraping TikTok')
        return 0


def scrape_loop():
    """Boucle de scraping: verifie periodiquement si des videos doivent etre telechargees."""
    while True:
        try:
            scrape_if_needed()
        except Exception:
            log.exception('Erreur inattendue dans scrape_loop')
        time.sleep(SCRAPE_INTERVAL)


def generation_loop():
    _state_file = Path(BASE_DIR) / "pipeline_state.json"
    _retry_counts: dict = {}  # {video_name: nb_echecs}

    while True:
        try:
            log.info('Début du cycle de génération')

            # ── Reprise d'un pipeline interrompu (crash système) ──────────────
            if os.path.exists(MAIN_INPUT_PATH) and _state_file.exists():
                log.info('♻️  Download.mp4 + pipeline_state.json détectés — reprise du pipeline précédent')
                wait_for_internet(poll_every=5)
                rc = run_main_script()
                if rc == 2:
                    # Rejet permanent (ex: modération OpenAI) → mise de côté
                    try:
                        os.makedirs(REJECTED_DIR, exist_ok=True)
                        dest = os.path.join(REJECTED_DIR, 'Download.mp4')
                        if os.path.exists(MAIN_INPUT_PATH):
                            shutil.move(MAIN_INPUT_PATH, dest)
                        _state_file.unlink(missing_ok=True)
                        log.warning('Video rejetee definitivement (moderation) — deplacee dans moderation_rejected/')
                    except Exception as ex:
                        log.error(f'Impossible de deplacer la video rejetee: {ex}')
                elif rc != 0:
                    # Reprise échouée → vider l'état pour autoriser un fresh start
                    try:
                        _state_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                    # Remettre Download.mp4 dans download/ pour retry classique
                    try:
                        restore_path = os.path.join(BASE_DIR, 'download', 'Download.mp4')
                        if not os.path.exists(restore_path) and os.path.exists(MAIN_INPUT_PATH):
                            shutil.copy2(MAIN_INPUT_PATH, restore_path)
                            log.warning('Reprise echouee → video remise en file pour retry')
                    except Exception as ex:
                        log.error(f'Impossible de remettre la video en file: {ex}')
                log.info(f'Fin du cycle de reprise. Pause {GENERATE_INTERVAL} secondes')
                time.sleep(GENERATE_INTERVAL)
                continue

            # ── Flux normal : pick d'une nouvelle vidéo ───────────────────────
            src = pick_video_for_processing()
            if not src:
                log.info('Aucune vidéo dans ./download. Pause longue (60s).')
                time.sleep(60)
                continue
            log.info(f'Vidéo choisie pour traitement: {src}')
            ok = prepare_main_input(src)
            if ok:
                # Supprimer de download/ maintenant pour eviter double-pick,
                # mais seulement si ce n'est pas deja Download.mp4
                if os.path.abspath(src) != os.path.abspath(MAIN_INPUT_PATH):
                    try:
                        os.remove(src)
                    except Exception:
                        log.warning(f'Impossible de supprimer {src} de download/')
                wait_for_internet(poll_every=5)
                rc = run_main_script()
                if rc == 2:
                    # Rejet permanent (ex: modération OpenAI) → mise de côté
                    video_name = os.path.basename(src)
                    try:
                        os.makedirs(REJECTED_DIR, exist_ok=True)
                        dest = os.path.join(REJECTED_DIR, video_name)
                        if os.path.exists(MAIN_INPUT_PATH):
                            shutil.move(MAIN_INPUT_PATH, dest)
                        log.warning(f'Video rejetee definitivement (moderation) — deplacee dans moderation_rejected/{video_name}')
                    except Exception as ex:
                        log.error(f'Impossible de deplacer la video rejetee: {ex}')
                elif rc != 0:
                    video_name = os.path.basename(src)
                    _retry_counts[video_name] = _retry_counts.get(video_name, 0) + 1
                    if _retry_counts[video_name] >= MAX_RETRIES:
                        log.error(f'Video {video_name} abandonnee apres {MAX_RETRIES} echecs — archivee dans moderation_rejected/')
                        try:
                            os.makedirs(REJECTED_DIR, exist_ok=True)
                            dest = os.path.join(REJECTED_DIR, video_name)
                            if os.path.exists(MAIN_INPUT_PATH):
                                shutil.move(MAIN_INPUT_PATH, dest)
                        except Exception as ex:
                            log.error(f'Impossible d\'archiver la video: {ex}')
                        _retry_counts.pop(video_name, None)
                    else:
                        restore_path = os.path.join(BASE_DIR, 'download', video_name)
                        try:
                            if not os.path.exists(restore_path) and os.path.exists(MAIN_INPUT_PATH):
                                shutil.copy2(MAIN_INPUT_PATH, restore_path)
                                log.warning(f'Pipeline echoue (essai {_retry_counts[video_name]}/{MAX_RETRIES}) — video remise en file: {video_name}')
                        except Exception as ex:
                            log.error(f'Impossible de remettre la video en file: {ex}')
            log.info(f'Fin du cycle de génération. Pause {GENERATE_INTERVAL} secondes')
        except Exception:
            log.exception('Erreur inattendue dans generation_loop')
        time.sleep(GENERATE_INTERVAL)


def post_video(final_mp4, poll=True, extra_args=None, timeout=30*60):
    """Lance post_tiktok_inbox.py en streaming console + fichier log."""
    final_mp4 = str(Path(final_mp4))
    script = os.path.join(BASE_DIR, "pipeline", "post_tiktok_inbox.py")

    cmd = [
        PYTHON_EXE,
        "-X", "utf8",
        "-u",
        script,
        "--video", final_mp4,
    ]
    if poll:
        cmd.append("--poll")
    if extra_args:
        cmd.extend(list(extra_args))

    log.info("Lancement: %s", " ".join([repr(c) for c in cmd]))

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        p = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        t_out = threading.Thread(
            target=_pump_stream,
            args=(p.stdout, lambda s: log.info("post_tiktok: %s", s)),
            daemon=True
        )
        t_err = threading.Thread(
            target=_pump_stream,
            args=(p.stderr, lambda s: log.error("post_tiktok: %s", s)),
            daemon=True
        )
        t_out.start()
        t_err.start()

        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.error("Timeout (%ss) — kill post_tiktok_inbox.py", timeout)
            try:
                p.kill()
            finally:
                t_out.join(timeout=1)
                t_err.join(timeout=1)
            return False

        t_out.join(timeout=1)
        t_err.join(timeout=1)

        log.info("post_tiktok_inbox.py terminé avec code %s", rc)
        return rc == 0

    except Exception:
        log.exception("Erreur lors de l'exécution de post_tiktok_inbox.py")
        return False


def posting_loop():
    while True:
        try:
            log.info('Début du cycle de publication')
            vids = sorted(glob.glob(os.path.join(COMPOSITE_DIR, '*')), key=os.path.getmtime)
            vids = [v for v in vids if os.path.isfile(v) and v.lower().endswith(VIDEO_EXTS)]
            if not vids:
                log.info('Aucune vidéo à poster actuellement. Retente dans 10 min.')
                time.sleep(600)
                continue

            video_to_post = vids[0]
            log.info(f'Vidéo sélectionnée pour post: {video_to_post}')
            wait_for_internet(poll_every=5)
            success = post_video(video_to_post)
            if success:
                dest = os.path.join(POSTED_DIR, os.path.basename(video_to_post))
                try:
                    if os.path.exists(dest):
                        base, ext = os.path.splitext(os.path.basename(video_to_post))
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        dest = os.path.join(POSTED_DIR, f"{base}_{timestamp}{ext}")
                    shutil.move(video_to_post, dest)
                    log.info(f'Vidéo postée et déplacée vers {dest}')
                except Exception:
                    log.exception('Erreur lors du déplacement de la vidéo postée')
            else:
                log.warning('La tentative de post a échoué. Retente au prochain cycle.')
            log.info(f'Fin du cycle de publication. Pause {POST_INTERVAL} secondes')
        except Exception:
            log.exception('Erreur inattendue dans posting_loop')
        time.sleep(POST_INTERVAL)

def has_internet(host="1.1.1.1", port=53, timeout=3) -> bool:
    """
    Vérifie l'accès Internet en ouvrant un socket TCP vers un DNS public.
    - host=1.1.1.1 (Cloudflare) ou 8.8.8.8 (Google)
    - port=53 (DNS)
    """
    try:
        socket.setdefaulttimeout(timeout)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((host, port))
        return True
    except Exception:
        return False


def wait_for_internet(poll_every=5):
    """
    Bloque tant qu'il n'y a pas Internet.
    """
    while not has_internet():
        log.warning("Pas d'Internet détecté. Nouvelle tentative dans %ss...", poll_every)
        time.sleep(poll_every)
    log.info("Internet OK.")



# ==============================
# Main
# ==============================
if __name__ == '__main__':
    # ── Instance unique (named mutex Windows) ─────────────────────────────────
    _scheduler_mutex = None
    if sys.platform == "win32":
        try:
            import ctypes as _ct
            _ERROR_ALREADY_EXISTS = 183
            _scheduler_mutex = _ct.windll.kernel32.CreateMutexW(
                None, False, "Global\\MrMartinScheduler"
            )
            if _ct.windll.kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
                log.error("⛔  Un scheduler est déjà en cours d'exécution. Arrêt.")
                sys.exit(1)
        except Exception:
            pass

    # pipeline.log et upload_tiktok.log sont geres par main_v3.py (subprocess).
    # scraper.log est gere par scraper_tiktok.py (import, mode="w").
    # log.txt est gere par ce script (RotatingFileHandler, mode="w").
    # NE PAS tronquer manuellement : ca corrompt les FileHandlers deja ouverts.
    log.info(f"Python utilisé: {PYTHON_EXE}")
    ensure_dirs()

    # Scraping initial au demarrage (rattrapage si PC etait eteint)
    log.info('Scraping initial au demarrage...')
    scrape_if_needed()

    t0 = threading.Thread(target=scrape_loop, daemon=True)
    t1 = threading.Thread(target=generation_loop, daemon=True)
    t2 = threading.Thread(target=posting_loop, daemon=True)
    t0.start()
    t1.start()
    # t2.start()
    log.info('auto_scheduler demarré (scraping + generation). Appuyez sur Ctrl+C pour quitter.')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info('Interruption par utilisateur, arret du scheduler.')
