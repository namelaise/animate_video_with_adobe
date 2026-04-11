
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


# ==============================
# Configuration
# ==============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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

VIDEO_EXTS = ('.mp4', '.mov', '.mkv', '.avi')

# ==============================
# Logging (console + fichier)
# ==============================
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s')

# Handler console
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)


file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=5,             # garde log.txt.1 ... log.txt.5
    encoding="utf-8"
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)



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
    """Copie la vidéo sélectionnée vers MAIN_INPUT_PATH en la renommant Download.mp4"""
    try:
        if os.path.abspath(src_video) == os.path.abspath(MAIN_INPUT_PATH):
            logging.info('La vidéo sélectionnée est déjà Download.mp4')
            return True
        shutil.copy2(src_video, MAIN_INPUT_PATH)
        os.remove(src_video)
        logging.info(f'Copié {src_video} -> {MAIN_INPUT_PATH}')
        return True
    except Exception as e:
        logging.exception('Erreur lors de la copie vers Download.mp4')
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
            sys.executable,
            "-X", "utf8",
            "-u",
            os.path.join(BASE_DIR, 'main_v3.py')
        ]
        logging.info('Lancement de main.py avec %s', sys.executable)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

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

        t_out = threading.Thread(target=_pump_stream, args=(p.stdout, lambda s: logging.info("main.py: %s", s)))
        t_err = threading.Thread(target=_pump_stream, args=(p.stderr, lambda s: logging.error("main.py: %s", s)))
        t_out.daemon = True
        t_err.daemon = True
        t_out.start()
        t_err.start()

        rc = p.wait()
        t_out.join(timeout=1)
        t_err.join(timeout=1)

        logging.info('main.py terminé avec code %s', rc)
        return rc == 0

    except Exception:
        logging.exception('Erreur lors de l’exécution streaming de main.py')
        return False



def generation_loop():
    while True:
        try:
            logging.info('Début du cycle de génération')
            src = pick_video_for_processing()
            if not src:
                logging.info('Aucune vidéo trouvée dans ./download. Attente du prochain cycle.')
            else:
                logging.info(f'Vidéo choisie pour traitement: {src}')
                ok = prepare_main_input(src)
                if ok:
                    wait_for_internet(poll_every=5)
                    run_main_script()
            logging.info(f'Fin du cycle de génération. Pause {GENERATE_INTERVAL} secondes')
        except Exception:
            logging.exception('Erreur inattendue dans generation_loop')
        time.sleep(GENERATE_INTERVAL)


def post_video(final_mp4, poll=True, extra_args=None, timeout=30*60):
    """Lance post_tiktok_inbox.py en streaming console + fichier log."""
    final_mp4 = str(Path(final_mp4))
    script = os.path.join(BASE_DIR, "post_tiktok_inbox.py")

    cmd = [
        sys.executable,
        "-X", "utf8",
        "-u",
        script,
        "--video", final_mp4,
    ]
    if poll:
        cmd.append("--poll")
    if extra_args:
        cmd.extend(list(extra_args))

    logging.info("Lancement: %s", " ".join([repr(c) for c in cmd]))

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
            args=(p.stdout, lambda s: logging.info("post_tiktok: %s", s)),
            daemon=True
        )
        t_err = threading.Thread(
            target=_pump_stream,
            args=(p.stderr, lambda s: logging.error("post_tiktok: %s", s)),
            daemon=True
        )
        t_out.start()
        t_err.start()

        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logging.error("Timeout (%ss) — kill post_tiktok_inbox.py", timeout)
            try:
                p.kill()
            finally:
                t_out.join(timeout=1)
                t_err.join(timeout=1)
            return False

        t_out.join(timeout=1)
        t_err.join(timeout=1)

        logging.info("post_tiktok_inbox.py terminé avec code %s", rc)
        return rc == 0

    except Exception:
        logging.exception("Erreur lors de l’exécution de post_tiktok_inbox.py")
        return False


def posting_loop():
    while True:
        try:
            logging.info('Début du cycle de publication')
            vids = sorted(glob.glob(os.path.join(COMPOSITE_DIR, '*')), key=os.path.getmtime)
            vids = [v for v in vids if os.path.isfile(v) and v.lower().endswith(VIDEO_EXTS)]
            if not vids:
                logging.info('Aucune vidéo à poster actuellement. Retente dans 10 min.')
                time.sleep(600)
                continue

            video_to_post = vids[0]
            logging.info(f'Vidéo sélectionnée pour post: {video_to_post}')
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
                    logging.info(f'Vidéo postée et déplacée vers {dest}')
                except Exception:
                    logging.exception('Erreur lors du déplacement de la vidéo postée')
            else:
                logging.warning('La tentative de post a échoué. Retente au prochain cycle.')
            logging.info(f'Fin du cycle de publication. Pause {POST_INTERVAL} secondes')
        except Exception:
            logging.exception('Erreur inattendue dans posting_loop')
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
        logging.warning("Pas d'Internet détecté. Nouvelle tentative dans %ss...", poll_every)
        time.sleep(poll_every)
    logging.info("Internet OK.")



# ==============================
# Main
# ==============================
if __name__ == '__main__':
    ensure_dirs()
    t1 = threading.Thread(target=generation_loop, daemon=True)
    t2 = threading.Thread(target=posting_loop, daemon=True)
    t1.start()
    # t2.start()
    logging.info('auto_scheduler démarré. Appuyez sur Ctrl+C pour quitter.')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info('Interruption par utilisateur, arrêt du scheduler.')
