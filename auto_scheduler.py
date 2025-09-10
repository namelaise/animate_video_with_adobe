import os
import time
import threading
import shutil
import glob
import subprocess
import logging
from datetime import datetime
import sys

# Configuration
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
GENERATE_INTERVAL = 2 * 60 * 60  # 2 heures
POST_INTERVAL = 2 * 60 * 60  # 2 heures

VIDEO_EXTS = ('.mp4', '.mov', '.mkv', '.avi')

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')


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
    # choisit la première (chronologique)
    return vids[0]


def prepare_main_input(src_video):
    """Copie la vidéo sélectionnée vers MAIN_INPUT_PATH en la renommant Download.mp4"""
    try:
        if os.path.abspath(src_video) == os.path.abspath(MAIN_INPUT_PATH):
            logging.info('La vidéo sélectionnée est déjà Download.mp4')
            return True
        shutil.copy2(src_video, MAIN_INPUT_PATH)
        logging.info(f'Copié {src_video} -> {MAIN_INPUT_PATH}')
        return True
    except Exception as e:
        logging.exception('Erreur lors de la copie vers Download.mp4')
        return False


def run_main_script():
    """Appelle main.py et attend la fin. Ne plante pas si main.py lève une erreur."""
    try:
        cmd = ['python', os.path.join(BASE_DIR, 'main.py')]
        logging.info('Lancement de main.py')
        cmd = [sys.executable, os.path.join(BASE_DIR, 'main.py')]
        logging.info('Lancement de main.py avec %s', sys.executable)
        # Forcer l'encodage UTF-8 dans le processus enfant (évite UnicodeEncodeError sous Windows)
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = env.get('PYTHONIOENCODING', 'utf-8')
        env['PYTHONUTF8'] = env.get('PYTHONUTF8', '1')
        res = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True, timeout=60*60, env=env)
        # remonter les logs pour diagnostic
        if res.stdout:
            logging.info('main.py stdout:\n%s', res.stdout)
        if res.stderr:
            logging.error('main.py stderr:\n%s', res.stderr)
        # tenter de supprimer la vidéo source (copiée précédemment) pour ne pas la retraiter
        try:
            src = pick_video_for_processing()
            if src and os.path.exists(src) and os.path.abspath(src) != os.path.abspath(MAIN_INPUT_PATH):
                os.remove(src)
            logging.info(f'Supprimé la vidéo source {src}')
        except Exception:
            logging.exception('Impossible de supprimer la vidéo source')
        logging.info(f'main.py terminé avec code {res.returncode}')
        if res.stdout:
            logging.debug(res.stdout)
        if res.stderr:
            logging.debug(res.stderr)
        return res.returncode == 0
    except subprocess.TimeoutExpired:
        logging.exception('main.py a expiré')
        return False
    except Exception:
        logging.exception('Erreur lors de l execution de main.py')
        return False


def collect_composite_videos():
    """Rassemble les fichiers vidéos générés et les place dans COMPOSITE_DIR."""
    candidates = []
    # chercher dans dossiers habituels
    search_dirs = [
        os.path.join(BASE_DIR, 'video_finale'),
        os.path.join(BASE_DIR, 'output'),
        os.path.join(BASE_DIR, 'video_segments'),
        BASE_DIR,
    ]
    for d in search_dirs:
        if os.path.isdir(d):
            for ext in VIDEO_EXTS:
                candidates.extend(glob.glob(os.path.join(d, f'*{ext}')))
    # filtrer Download.mp4
    candidates = [c for c in candidates if os.path.abspath(c) != os.path.abspath(MAIN_INPUT_PATH)]

    moved = []
    for src in sorted(set(candidates), key=lambda p: os.path.getmtime(p)):
        try:
            dest = os.path.join(COMPOSITE_DIR, os.path.basename(src))
            # éviter d'écraser
            if os.path.exists(dest):
                base, ext = os.path.splitext(os.path.basename(src))
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                dest = os.path.join(COMPOSITE_DIR, f"{base}_{timestamp}{ext}")
            shutil.move(src, dest)
            logging.info(f'Move {src} -> {dest}')
            moved.append(dest)
        except Exception:
            logging.exception(f'Impossible de déplacer {src} vers {COMPOSITE_DIR}')
    return moved


def generation_loop():
    while True:
        try:
            logging.info('Début du cycle de génération')
            src = pick_video_for_processing()
            if not src:
                logging.info('Aucune vidéo trouvée dans les dossiers de download. J attenderai le prochain cycle.')
            else:
                logging.info(f'Vidéo choisie pour traitement: {src}')
                ok = prepare_main_input(src)
                if ok:
                    run_main_script()
                    moved = collect_composite_videos()
                    if moved:
                        logging.info(f'Fichiers composites déplacés: {moved}')
                    else:
                        logging.info('Aucun fichier composite trouvé après le traitement')
            logging.info(f'Fin du cycle de génération. Pause {GENERATE_INTERVAL} secondes')
        except Exception:
            logging.exception('Erreur inattendue dans generation_loop')
        time.sleep(GENERATE_INTERVAL)


def post_video(video_path):
    """Tente de publier la vidéo en appelant post_tiktok_inbox.py avec le chemin en argument."""
    try:
        cmd = ['python', os.path.join(BASE_DIR, 'post_tiktok_inbox.py'), video_path]
        logging.info(f'Lancement du post pour {video_path}')
        res = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True, timeout=30*60)
        logging.info(f'post_tiktok_inbox.py terminé avec code {res.returncode}')
        if res.stdout:
            logging.debug(res.stdout)
        if res.stderr:
            logging.debug(res.stderr)
        return res.returncode == 0
    except subprocess.TimeoutExpired:
        logging.exception('post_tiktok_inbox.py a expiré')
        return False
    except Exception:
        logging.exception('Erreur lors de l appel a post_tiktok_inbox.py')
        return False


def posting_loop():
    while True:
        try:
            logging.info('Début du cycle de publication')
            vids = sorted(glob.glob(os.path.join(COMPOSITE_DIR, '*')), key=os.path.getmtime)
            vids = [v for v in vids if os.path.isfile(v) and v.lower().endswith(VIDEO_EXTS)]
            if not vids:
                logging.info('Aucune vidéo à poster actuellement. je retenterai dans 10 minutes.')
                time.sleep(600)
                continue

            video_to_post = vids[0]
            logging.info(f'Vidéo sélectionnée pour post: {video_to_post}')
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
                logging.warning('La tentative de post a échoué. Reessaiera au prochain cycle.')
            logging.info(f'Fin du cycle de publication. Pause {POST_INTERVAL} secondes')
        except Exception:
            logging.exception('Erreur inattendue dans posting_loop')
        time.sleep(POST_INTERVAL)


if __name__ == '__main__':
    ensure_dirs()
    t1 = threading.Thread(target=generation_loop, daemon=True)
    t2 = threading.Thread(target=posting_loop, daemon=True)
    t1.start()
    t2.start()
    logging.info('auto_scheduler démarré. Appuyez sur Ctrl+C pour quitter.')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info('Interruption par l utilisateur, arrêt du scheduler.')
