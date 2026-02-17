# === automate_pipeline.py (version modifiée: gestion spam_risk_too_many_pending_share) ===
import os
import re
import json
import time
import random
import shutil
import base64
import subprocess, sys
import concurrent.futures
from typing import List, Dict, Tuple, Optional
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from pydub import AudioSegment
from PIL import Image
from tqdm import tqdm
from openai import OpenAI
import openai
import asyncio
from automate_adobe_with_bg import Task, run_pool

from assemble_guarded import assemble_from_tail_with_transcript
from segments_processing import (
    rewrite_transcript_with_intervenants_gpt,
    cut_audio_by_diarization,
)
from automate_diarization import transcribe_segments_with_diarization

load_dotenv()

# --- À mettre en haut du fichier (imports + setup logging) ---
import logging
from contextlib import contextmanager
import shlex
import socket

LOG_DIR = Path("./logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

def has_internet(host="1.1.1.1", port=53, timeout=3) -> bool:
    try:
        socket.setdefaulttimeout(timeout)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((host, port))
        return True
    except Exception:
        return False

def wait_for_internet(poll_every=5, label=""):
    """
    Bloque l'exécution tant qu'il n'y a pas Internet.
    """
    first = True
    while not has_internet():
        if first:
            log.warning(f"🌐 Internet KO — mise en pause ({label})")
            first = False
        time.sleep(poll_every)
    if not first:
        log.info(f"🌐 Internet revenu — reprise ({label})")

def run_cmd_capture(args: list[str], log_path: Path, env: dict | None = None, cwd: str | None = None):
    """
    Exécute une commande, capture stdout/stderr, write dans log_path et retourne (rc, out, err).
    """
    started = datetime.now().isoformat(timespec="seconds")
    cmd_str = shlex.join(args)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n=== {started} RUN: {cmd_str} ===\n")
    proc = subprocess.run(args, capture_output=True, text=True, env=env, cwd=cwd)
    out = proc.stdout or ""
    err = proc.stderr or ""
    with open(log_path, "a", encoding="utf-8") as f:
        if out.strip():
            f.write("--- STDOUT ---\n")
            f.write(out + ("\n" if not out.endswith("\n") else ""))
        if err.strip():
            f.write("--- STDERR ---\n")
            f.write(err + ("\n" if not err.endswith("\n") else ""))
        f.write(f"=== RETURN CODE: {proc.returncode} ===\n")
    return proc.returncode, out, err

def preflight_mp4_checks(path: str) -> None:
    """Vérifs rapides pour éviter des 4xx TikTok silencieux."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Fichier vidéo introuvable: {path}")
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb < 1:
        log.warning(f"⚠️ Vidéo très légère ({size_mb:.2f} MB) — risque de rejet.")
    if size_mb > 5000:
        log.error(f"🚫 Vidéo trop lourde ({size_mb:.0f} MB) — au-delà des limites usuelles.")
        raise RuntimeError("Fichier vidéo trop volumineux")

# --------------------------------------------------------------------
# NOUVEAU: gestion des échecs TikTok (spam risk) + stash des vidéos
# --------------------------------------------------------------------
TIKTOK_SPAM_RISK_KEY = "spam_risk_too_many_pending_share"

def _contains_spam_risk(blob: str) -> bool:
    b = (blob or "").lower()
    return (TIKTOK_SPAM_RISK_KEY in b)

def ensure_dir(p: str | Path):
    Path(p).mkdir(parents=True, exist_ok=True)

def next_indexed_dir(root: str | Path, prefix: str = "Video_") -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    existing = [
        d for d in root.iterdir()
        if d.is_dir() and re.match(rf"^{re.escape(prefix)}\d+$", d.name)
    ]
    next_idx = 1 + max((int(d.name.split("_")[1]) for d in existing), default=0)
    out = root / f"{prefix}{next_idx}"
    out.mkdir(parents=True, exist_ok=False)
    return out

def stash_unposted_videos(
    base_dir: str,
    raw_video_path: str,
    final_video_path: str,
    reason: str = "tiktok_failed"
) -> Path:
    """
    Crée BASE_DIR/pending_posts/Video_<n>/ puis y déplace:
      - Download.mp4 (RAW_VIDEO_PATH)
      - video_final.mp4 (VIDEO_FINALE_PATH)
    """
    pending_root = Path(base_dir) / "pending_posts"
    dest_dir = next_indexed_dir(pending_root, prefix="Video_")

    meta = {
        "reason": reason,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "raw_video_src": raw_video_path,
        "final_video_src": final_video_path,
    }
    (dest_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # Déplace Download.mp4
    if raw_video_path and os.path.exists(raw_video_path):
        dst_raw = dest_dir / Path(raw_video_path).name
        try:
            shutil.move(raw_video_path, dst_raw)
            log.info(f"📦 Download déplacé vers pending: {dst_raw}")
        except Exception as e:
            log.error(f"❌ Impossible de déplacer Download.mp4 vers pending: {e}")

    # Déplace video_final.mp4
    if final_video_path and os.path.exists(final_video_path):
        dst_final = dest_dir / Path(final_video_path).name
        try:
            shutil.move(final_video_path, dst_final)
            log.info(f"📦 video_final déplacé vers pending: {dst_final}")
        except Exception as e:
            log.error(f"❌ Impossible de déplacer video_final.mp4 vers pending: {e}")

    log.info(f"✅ Vidéos non postées stockées dans: {dest_dir}")
    return dest_dir

def upload_to_tiktok_with_retry(final_mp4: str, python_exe: str = sys.executable) -> Tuple[bool, str]:
    """
    Tente upload Inbox → en cas d'‘access_token_invalid’/401: refresh token puis 1 retry.
    Retourne (ok, reason).
      - ok=True  => reason="ok"
      - ok=False => reason="spam_risk" ou "token_failed" ou "other_failed"
    Log détaillé: logs/upload_tiktok.log
    """
    log_path = LOG_DIR / "upload_tiktok.log"
    preflight_mp4_checks(final_mp4)

    def _post():
        return run_cmd_capture(
            [python_exe, "post_tiktok_inbox.py", "--video", final_mp4, "--poll"],
            log_path
        )

    # 1) premier essai
    rc, out, err = _post()
    if rc == 0:
        log.info("📤 Upload TikTok OK (1er essai)")
        return True, "ok"

    blob = (out + "\n" + err)
    blob_low = blob.lower()

    # NOUVEAU: spam risk -> on ne retry pas, on stash côté et on continue la prod
    if _contains_spam_risk(blob):
        log.error(f"🚫 Upload TikTok refusé (spam risk): {TIKTOK_SPAM_RISK_KEY}")
        return False, "spam_risk"

    token_issue = ("access_token_invalid" in blob_low) or ("http 401" in blob_low) or ("unauthorized" in blob_low)

    # 2) si token invalide: refresh puis retry
    if token_issue:
        log.warning("🔐 Token TikTok possiblement invalide → rafraîchissement…")
        _ = run_cmd_capture([python_exe, "auth_tiktok_refresh.py"], log_path)
        rc2, out2, err2 = _post()
        if rc2 == 0:
            log.info("📤 Upload TikTok OK après refresh token")
            return True, "ok"
        else:
            # re-check spam risk au 2e essai
            blob2 = (out2 + "\n" + err2)
            if _contains_spam_risk(blob2):
                log.error(f"🚫 Upload TikTok refusé après retry (spam risk): {TIKTOK_SPAM_RISK_KEY}")
                return False, "spam_risk"
            log.error("❌ Upload TikTok encore en échec après refresh. Voir logs/upload_tiktok.log")
            return False, "token_failed"

    # 3) autre erreur
    log.error("❌ Upload TikTok échoué (pas un problème de token). Voir logs/upload_tiktok.log")
    return False, "other_failed"

# Logging console + fichier
LOG_DIR = Path("./logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "pipeline.log", encoding="utf-8")
    ]
)
log = logging.getLogger("pipeline")

@contextmanager
def time_step(name: str):
    """Context manager pour chronométrer une étape et logger durée + statut."""
    log.info(f"🚀 Début étape: {name}")
    t0 = time.perf_counter()
    try:
        yield
    except Exception as e:
        dt = time.perf_counter() - t0
        log.exception(f"💥 Échec étape: {name} (⏱ {dt:.3f}s) — {e}")
        raise
    else:
        dt = time.perf_counter() - t0
        log.info(f"✅ Fin étape: {name} (⏱ {dt:.3f}s)\n\n")

# Nettoyage éventuel de logs
if os.path.exists("automation_logs"):
    shutil.rmtree("automation_logs", ignore_errors=True)

# stdout utf-8 (Windows)
import io
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# =========================
#      CONFIG GLOBALE
# =========================

PUPPETS_BY_GENDER: Dict[str, List[str]] = {
    "homme": [
        "Fergus VQA.puppet-button",
        "Cecil VQA.puppet-button",
        "David VQA.puppet-button",
        "Elliot VQA.puppet-button",
        "Jonty VQA.puppet-button",
        "Jonty with prosthetic VQA.puppet-button",
        "Atlas VQA.puppet-button",
    ],
    "femme": [
        "Edith VQA.puppet-button",
        "Agnes VQA.puppet-button",
        "Chivy VQA.puppet-button",
        "Gaby VQA.puppet-button",
        "Zibby VQA.puppet-button",
        "Yara VQA.puppet-button",
        "Yara with prosthetic VQA.puppet-button",
    ],
}

BANNED_WORDS: List[str] = [
    "alcool", "alcoolisé", "chasseur", "chasse", "fusil", "tuer", "insulte", "dispute",
]

BASE_DIR = os.getenv("BASE_DIR")
RAW_VIDEO_PATH = os.getenv("RAW_VIDEO_PATH", os.path.join(BASE_DIR, "Download.mp4") if BASE_DIR else "Download.mp4")

RAW_AUDIO_DIR = os.path.join(BASE_DIR, "audio")
AUDIO_SEGMENTS_DIR = os.path.join(BASE_DIR, "audio_segments")
VIDEO_SEGMENTS_DIR = os.path.join(BASE_DIR, "video_segments")
VIDEO_FINALE_DIR = os.path.join(BASE_DIR, "video_finale")
VIDEO_FINALE_PATH = os.path.join(VIDEO_FINALE_DIR, "video_final.mp4")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")
IMAGES_DIR = os.path.join(BASE_DIR, "images")
LEFT_IMG_DIR = os.path.join(IMAGES_DIR, "left")
RIGHT_IMG_DIR = os.path.join(IMAGES_DIR, "right")

FULL_AUDIO_PATH = os.path.join(RAW_AUDIO_DIR, "audio_full.mp3")

for _folder in [RAW_AUDIO_DIR, AUDIO_SEGMENTS_DIR, TRANSCRIPTS_DIR, IMAGES_DIR, LEFT_IMG_DIR, RIGHT_IMG_DIR, VIDEO_FINALE_DIR]:
    os.makedirs(_folder, exist_ok=True)

# Supprime d’anciens segments audio éventuels (re-run propre)
for _file in os.listdir(AUDIO_SEGMENTS_DIR):
    if _file.startswith("audio_") and _file.endswith(".mp3"):
        os.remove(os.path.join(AUDIO_SEGMENTS_DIR, _file))

# =========================
#     UTILITAIRES TEXTE
# =========================

SPEAKER_LINE_RE = re.compile(
    r'^\s*\[(?P<start>[\d\.]+)\s*[→\-]\s*(?P<end>[\d\.]+)\]\s*(?P<label>[^:]+)\s*:\s*(?P<text>.*)$',
    re.UNICODE
)

def parse_segments_with_speakers(segments_txt_path: str) -> List[Dict]:
    parsed: List[Dict] = []
    with open(segments_txt_path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f, start=1):
            match = SPEAKER_LINE_RE.match(line.rstrip("\n"))
            if not match:
                continue
            parsed.append({
                "index": idx,
                "start_s": float(match.group("start")),
                "end_s": float(match.group("end")),
                "label": match.group("label").strip(),
                "text": match.group("text"),
            })
    return parsed

def _extract_last_int_from_name(name: str) -> int:
    nums = re.findall(r'(\d+)', name)
    return int(nums[-1]) if nums else -1

def list_audio_segments_sorted(audio_dir: str) -> List[str]:
    files = [os.path.join(audio_dir, f) for f in os.listdir(audio_dir) if f.lower().endswith(".mp3")]
    files.sort(key=lambda p: (_extract_last_int_from_name(os.path.basename(p)), os.path.basename(p)))
    return files

# =========================
#   ETAPES AUDIO & VISU
# =========================

def extract_audio_from_video() -> str:
    cmd = f'ffmpeg -i "{RAW_VIDEO_PATH}" -q:a 0 -map a "{FULL_AUDIO_PATH}" -y'
    subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"✅ Audio extrait : {FULL_AUDIO_PATH}")
    return FULL_AUDIO_PATH

def split_full_audio(max_segment_duration_sec: int = 110) -> List[str]:
    audio = AudioSegment.from_file(FULL_AUDIO_PATH)
    segment_paths: List[str] = []
    total_parts = (len(audio) + max_segment_duration_sec * 1000 - 1) // (max_segment_duration_sec * 1000)

    print("⏳ Découpage audio en segments...")
    for i in tqdm(range(total_parts), desc="Découpage", unit="segment"):
        start_ms = i * max_segment_duration_sec * 1000
        end_ms = start_ms + max_segment_duration_sec * 1000
        segment = audio[start_ms:end_ms]
        filename = f"audio_{i + 1}.mp3"
        path_out = os.path.join(AUDIO_SEGMENTS_DIR, filename)
        segment.export(path_out, format="mp3")
        segment_paths.append(path_out)

    print(f"✅ Audio découpé : {len(segment_paths)} segments")
    return segment_paths

# build_background_prompt.py
def build_background_prompt(script_text: str, templates_dir: str = "prompts/backgrounds") -> str:
    default_template = """
    Prompt IA – Scène immersive absurde et ultra-détaillée basée sur un script audio
    Crée une image photo très réaliste et moderne, voir futurisque ou un peu luxueux, au format carré 9:8, inspirée d’un script de conversation absurde fourni en entrée.

    Style visuel :
    Ambiance réaliste et très moderne : éclairage naturel ou artificiel cohérent avec un lieu public ou semi-public et en cohérence avec le lieu du script
    (ex: forêt, champ, magasin, etc) avec des couleurs vives.
    Cadrage grand angle (profondeur / espace). Décor riche et immersif (textures, reflets, ombres), couleurs vives et contrastées (non cartoon).

    Contraste absurde à intégrer :
    Objets incongrus inspirés du script mais intégrés de façon réaliste (échelle étrange, objets flottants/mal placés).
    Panneaux/affiches avec citations exactes du script en français (pub, pancarte bricolée, graffiti).
    Plusieurs petites scènes absurdes en avant-plan et arrière-plan.

    Contexte :
    Lieu logique par rapport au script (marché, parking, plage, bureau…), vaste et détaillé.
    Éléments de décor racontant une histoire secondaire absurde (stand inutile, pile d’objets improbables, comptoir à pancarte absurde).
    Uniquement des traces d’activité humaine (pas de personnages visibles).

    📝 Extrait du script :
    {SCRIPT}
    """.strip()

    cleaned = " ".join((script_text or "").split())
    banned = globals().get("BANNED_WORDS") or globals().get("banned_words") or []
    for w in banned:
        cleaned = cleaned.replace(w, "")

    template_text = None
    chosen_name = "(default)"
    try:
        tdir = Path(templates_dir)
        txt_files = [p for p in tdir.glob("*.txt") if p.is_file()]
        if txt_files:
            chosen = random.choice(txt_files)
            template_text = chosen.read_text(encoding="utf-8").strip()
            chosen_name = chosen.name
    except Exception:
        pass

    if not template_text:
        template_text = default_template

    if "{SCRIPT}" in template_text or "{{SCRIPT}}" in template_text:
        final_prompt = template_text.replace("{SCRIPT}", cleaned).replace("{{SCRIPT}}", cleaned)
    else:
        final_prompt = f"{template_text}\n\n{cleaned}"

    Path(IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    prompt_path = Path(IMAGES_DIR) / "prompt.txt"
    prompt_path.write_text(final_prompt, encoding="utf-8")

    print(f"✅ Prompt image généré (template: {chosen_name}) → {prompt_path}")
    return final_prompt

def generate_image_with_openai(script_text: str) -> str | None:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    output_image_path = os.path.join(IMAGES_DIR, "full_background.png")

    prompt_text = build_background_prompt(script_text)

    local_banned = [
        "violence", "égalité", "militer", "syndicat", "CGT", "sexuel", "genre",
        "t-shirt", "discrimination",
        r"\b(président|premier[e]? ministre|député[e]?|ministre|gouvernement|politique|élysée|assemblée)\b",
        r"\b(Macron|Sarkozy|Le Pen|Mélenchon|Borne|Attal|Bardella|Trump|Biden)\b",
        r"\b(public figure|celebrity|célébrité)\b",
    ]
    cleaned_for_log = prompt_text
    for w in local_banned:
        cleaned_for_log = cleaned_for_log.replace(w, " ")
    os.makedirs("output", exist_ok=True)
    with open("output/prompt_nettoye.txt", "w", encoding="utf-8") as f:
        f.write(cleaned_for_log)

    try:
        print("⏳ Génération de l'image via OpenAI...")
        resp = client.images.generate(
            model="gpt-image-1",
            prompt=prompt_text,
        )
        image_b64 = resp.data[0].b64_json
        with open(output_image_path, "wb") as f:
            f.write(base64.b64decode(image_b64))

        print(f"✅ Image générée : {output_image_path}")
        return output_image_path
    except Exception as e:
        print(f"❌  Génération image échouée : {e}")
        return None

def split_background_to_tiktok_pairs() -> Tuple[str, str]:
    bg_path = os.path.join(IMAGES_DIR, "full_background.png")
    img = Image.open(bg_path)

    expected_w, expected_h = 1080, 960
    if img.size != (expected_w, expected_h):
        print(f"⚠️ Redimension de l’image en {expected_w}x{expected_h} pour TikTok.")
        img = img.resize((expected_w, expected_h))

    left_img = img.crop((0, 0, 540, 960))
    right_img = img.crop((540, 0, 1080, 960))

    left_path = os.path.join(LEFT_IMG_DIR, "left_0.png")
    right_path = os.path.join(RIGHT_IMG_DIR, "right_0.png")

    left_img.save(left_path)
    right_img.save(right_path)

    print("✅ Deux images 9:16 générées (gauche/droite).")
    return left_path, right_path

# =========================
#   AFFECTATION PUPPETS
# =========================

LABEL_TO_PUPPET_CACHE: Dict[str, str] = {}

def choose_puppet_for_label(speaker_label: str) -> Tuple[str, str]:
    label_norm = speaker_label.strip().lower()
    if label_norm == "mr martin":
        return "homme", "Sticky VQA.puppet-button"

    if label_norm.startswith("homme"):
        genre = "homme"
        pool = PUPPETS_BY_GENDER.get("homme", [])
    elif label_norm.startswith("femme"):
        genre = "femme"
        pool = PUPPETS_BY_GENDER.get("femme", [])
    else:
        genre = "homme"
        pool = PUPPETS_BY_GENDER.get("homme", []) + PUPPETS_BY_GENDER.get("femme", [])

    if speaker_label in LABEL_TO_PUPPET_CACHE:
        return genre, LABEL_TO_PUPPET_CACHE[speaker_label]

    puppet = random.choice(pool) if pool else "Default VQA.puppet-button"
    LABEL_TO_PUPPET_CACHE[speaker_label] = puppet
    return genre, puppet

def _speaker_index_hint(label: str) -> int:
    m = re.search(r'(?:homme|femme)\s+(\d+)', label, re.IGNORECASE)
    return int(m.group(1)) if m else 0

def automate_generation_videos(
    max_threads: int = 4,
    segments_txt_path: str | None = None,
    audio_segments_dir: str | None = None,
):
    segments_txt_path = segments_txt_path or os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants.txt")
    audio_segments_dir = audio_segments_dir or AUDIO_SEGMENTS_DIR

    if not os.path.exists(segments_txt_path):
        print(f"❌ Fichier segments introuvable : {segments_txt_path}")
        return
    if not os.path.isdir(audio_segments_dir):
        print(f"❌ Dossier audio introuvable : {audio_segments_dir}")
        return

    lines = parse_segments_with_speakers(segments_txt_path)
    if not lines:
        print("❌ Aucune ligne valide dans le fichier segments.")
        return

    mp3_files = list_audio_segments_sorted(audio_segments_dir)
    if not mp3_files:
        print("❌ Aucun segment audio .mp3 trouvé.")
        return

    pair_count = min(len(lines), len(mp3_files))
    if len(lines) != len(mp3_files):
        print(f"⚠️ Nb lignes texte ({len(lines)}) ≠ nb mp3 ({len(mp3_files)}) — on traitera {pair_count} paires.")

    tasks: list[Task] = []
    for i in range(pair_count):
        info = lines[i]
        mp3_path = mp3_files[i]
        label = info["label"]
        genre, puppet = choose_puppet_for_label(label)
        tasks.append(
            Task(
                audio_path=mp3_path,
                nom=label,
                genre=genre,
                segment_id=str(info["index"]),
                intervenant_index=str(_speaker_index_hint(label)),
                personnage_id=puppet,
            )
        )

    if not tasks:
        print("❌ Aucun job à traiter.")
        return

    print(f"⏳ Lancement de {len(tasks)} segments (max {max_threads} pages parallèles, 1 seul navigateur)")
    asyncio.run(run_pool(tasks, concurrency=max_threads))
    print("✅ Toutes les vidéos animées ont été générées.")

# =========================
#   VERIFICATION TEXTE
# =========================

def get_transcription_file_with_verification(transcription_path: str) -> str | None:
    if not os.path.exists(transcription_path):
        print("❌ Le fichier transcription_full.txt est introuvable.")
        return None

    with open(transcription_path, "r", encoding="utf-8") as f:
        content = f.read()

    print("\n--- Aperçu de la transcription (début) ---")
    print(content[:1500])
    print("--- Fin de l’aperçu ---\n")

    print("✅ Le contenu vous semble-t-il correct ?")
    print("Appuie sur [y] pour continuer, [n] pour arrêter, ou attendre 5 minutes pour continuer automatiquement.")

    start_time = time.time()
    timeout_s = 30  # dans ton code c’est 30s

    while True:
        if time.time() - start_time > timeout_s:
            print("⏳ Temps écoulé. Suite du traitement...")
            return content

        if os.name == 'nt':
            import msvcrt
            if msvcrt.kbhit():
                key = msvcrt.getwch().lower()
                if key == 'y':
                    print("➡️  Poursuite du traitement...")
                    return content
                if key == 'n':
                    print("❌ Traitement interrompu par l’utilisateur.")
                    exit()
        else:
            import select
            if select.select([sys.stdin], [], [], 1)[0]:
                key = sys.stdin.readline().strip().lower()
                if key == 'y':
                    print("➡️  Poursuite du traitement...")
                    return content
                if key == 'n':
                    print("❌ Traitement interrompu par l’utilisateur.")
                    exit()
        time.sleep(1)

# =========================
#   ARCHIVAGE (post OK)
# =========================
def archive_outputs():
    """
    Ton archive actuelle (je la laisse telle quelle).
    Attention: ton code d'origine supprimait les .mp4 dans BASE_DIR; ici on ne touche pas.
    On archive seulement video_final.mp4 si présent.
    """
    archive_root = os.path.join(BASE_DIR, "archive")
    os.makedirs(archive_root, exist_ok=True)

    existing = [
        d for d in os.listdir(archive_root)
        if os.path.isdir(os.path.join(archive_root, d)) and re.match(r"^Video_\d+$", d)
    ]
    next_idx = 1 + max((int(d.split("_")[1]) for d in existing), default=0)

    archive_dir = os.path.join(archive_root, f"Video_{next_idx}")
    os.makedirs(archive_dir, exist_ok=False)

    # Archive video_final.mp4
    src = os.path.join(BASE_DIR, "video_finale", "video_final.mp4")
    if os.path.exists(src):
        dst = os.path.join(archive_dir, "video_final.mp4")
        shutil.move(src, dst)
        print(f"✅ Vidéo archivée dans : {dst}")

    print(f"✅ Tous les fichiers ont été archivés dans : {archive_dir}")

# =========================
#   DELETE (inchangé)
# =========================
def delete_outputs():
    files_to_delete = [
        os.path.join(RAW_AUDIO_DIR, "audio_full.mp3"),
        os.path.join(AUDIO_SEGMENTS_DIR, "audio_1.mp3"),
        os.path.join(AUDIO_SEGMENTS_DIR, "audio_2.mp3"),
        os.path.join(IMAGES_DIR, "prompt.txt"),
        os.path.join(IMAGES_DIR, "full_background.png"),
        os.path.join(LEFT_IMG_DIR, "left_0.png"),
        os.path.join(RIGHT_IMG_DIR, "right_0.png"),
        os.path.join("output", "intervenants.json"),
        os.path.join(TRANSCRIPTS_DIR, "transcription_full.txt"),
        os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants.txt"),
        os.path.join(TRANSCRIPTS_DIR, "transcription_segments.txt"),
        os.path.join(TRANSCRIPTS_DIR, "diarization_segments.json"),
        os.path.join(TRANSCRIPTS_DIR, "speakers.json"),
        os.path.join(BASE_DIR, "video_finale", "video_final.mp4"),
    ]

    for p in files_to_delete:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    if os.path.exists("audio_segments"):
        shutil.rmtree("audio_segments", ignore_errors=True)

    if os.path.exists("video_segments"):
        shutil.rmtree("video_segments", ignore_errors=True)

    if os.path.exists("generated_backgrounds"):
        shutil.rmtree("generated_backgrounds", ignore_errors=True)

    print(f"✅ Tous les fichiers ont été supprimés ")

# =========================
#      PIPELINE MAIN
# =========================

def run_pipeline_once() -> Tuple[bool, str]:
    """
    Retourne (done, status)
      - done=True, status="posted" si upload OK + archive OK
      - done=False, status="spam_risk_stashed" si spam risk -> stash + continue (boucle main continue)
    """
    log.info("📦 Traitement initial démarré…")
    t_all = time.perf_counter()

    # 0) Nettoyage initial
    with time_step("0) Nettoyage initial"):
        delete_outputs()

    # 1) Extraire audio du RAW_VIDEO
    with time_step("1) Extraction audio depuis RAW_VIDEO"):
        extract_audio_from_video()

    # 2) Transcription + diarisation
    with time_step("2) Transcription + diarisation"):
        wait_for_internet(label="Transcription + diarisation")
        transcribe_segments_with_diarization(
            audio_path=FULL_AUDIO_PATH,
            output_dir=TRANSCRIPTS_DIR
        )

    # 3) Vérification et réécriture avec intervenants
    with time_step("3) Vérification + réécriture intervenants"):
        raw_segments_txt = os.path.join(TRANSCRIPTS_DIR, "transcription_segments.txt")
        segments_text_content = get_transcription_file_with_verification(raw_segments_txt)
        wait_for_internet(label="OpenAI texte verification")
        rewritten_segments_path = rewrite_transcript_with_intervenants_gpt(
            contenu_segments_brut=segments_text_content,
            dossier_sortie=TRANSCRIPTS_DIR,
            nom_fichier_sortie="transcription_segments_intervenants.txt",
        )

    # 4) Découpage MP3 selon segments réécrits
    with time_step("4) Découpage MP3 par segments réécrits"):
        cut_audio_by_diarization(
            chemin_fichier_audio_mp3=FULL_AUDIO_PATH,
            chemin_fichier_segments_txt=rewritten_segments_path,
            dossier_sortie_segments_audio=AUDIO_SEGMENTS_DIR,
            padding_millisecondes=80,
            duree_minimale_conservee_millisecondes=250,
            preferer_copie_flux=True,
        )

    # 5) Image de fond + split 9:16
    with time_step("5) Génération image de fond + split 9:16"):
        full_txt_path = os.path.join(TRANSCRIPTS_DIR, "transcription_segments.txt")
        full_txt = get_transcription_file_with_verification(full_txt_path)
        wait_for_internet(label="OpenAI image generation")
        generate_image_with_openai(full_txt)
        split_background_to_tiktok_pairs()

    # 6) Génération des vidéos segments (Adobe)
    with time_step("6) Génération vidéos segments (Adobe)"):
        wait_for_internet(label="Adobe generation")
        automate_generation_videos(
            max_threads=4,
            segments_txt_path=os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants.txt"),
            audio_segments_dir=AUDIO_SEGMENTS_DIR,
        )

    # 7) Assemblage final
    with time_step("7) Assemblage final (durées = transcript)"):
        transcript_segments_path = os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants.txt")
        assemble_from_tail_with_transcript(
            video_segments_dir=VIDEO_SEGMENTS_DIR,
            transcript_path=transcript_segments_path,
            output_path=VIDEO_FINALE_PATH,
            crf=18,
            preset="veryfast",
            audio_bitrate="192k",
            min_keep_sec=0.10,
            force_fps=60,
        )

    # 8) Upload TikTok + gestion spam risk
    with time_step("8) Upload TikTok"):
        # Refresh token (préventif)
        run_cmd_capture([sys.executable, "auth_tiktok_refresh.py"], LOG_DIR / "upload_tiktok.log")

        final_mp4 = VIDEO_FINALE_PATH
        wait_for_internet(label="Post TikTok")

        ok, reason = upload_to_tiktok_with_retry(final_mp4, python_exe=sys.executable)

        if ok:
            log.info("✅ Upload TikTok OK -> archivage standard")
            with time_step("9) Archivage (post OK)"):
                archive_outputs()
            dt_all = (time.perf_counter() - t_all) / 60
            log.info(f"🎉 Pipeline COMPLET terminé en {dt_all:.3f} minutes")
            print("\n✅ Traitement terminé.")
            return True, "posted"

        # NOUVEAU: spam risk => stash Download.mp4 + video_final.mp4 et continuer la prod
        if reason == "spam_risk":
            log.warning("⚠️ Spam risk détecté -> stockage des vidéos non postées (pending_posts) puis reprise.")
            stash_unposted_videos(
                base_dir=BASE_DIR,
                raw_video_path=RAW_VIDEO_PATH,
                final_video_path=VIDEO_FINALE_PATH,
                reason=TIKTOK_SPAM_RISK_KEY
            )
            # On nettoie le reste des outputs pour repartir proprement
            with time_step("9) Nettoyage après spam risk (sans arrêter)"):
                delete_outputs()
            return False, "spam_risk_stashed"

        # Autres erreurs: on conserve ton comportement "hard fail"
        raise RuntimeError(f"Upload TikTok a échoué (reason={reason}) — consulte logs/upload_tiktok.log")

def main():
    """
    Boucle principale :
      - lance le pipeline complet
      - si spam risk TikTok -> stash et on recommence
      - si quota OpenAI insuffisant -> attend 15 minutes puis relance
      - sinon -> on stop sur erreur
    """
    while True:
        try:
            done, status = run_pipeline_once()
            if done and status == "posted":
                break  # terminé normalement

            # Si spam_risk_stashed => on continue immédiatement (création suivante)
            if status == "spam_risk_stashed":
                continue

        except openai.RateLimitError as e:
            msg = str(e)
            if "insufficient_quota" in msg:
                log.error("⛔ Quota OpenAI insuffisant (insufficient_quota). Pause 15 minutes puis relance du pipeline…")
                time.sleep(15 * 60)
                continue
            else:
                raise

        except Exception:
            log.exception("💥 Erreur non gérée dans le pipeline, arrêt.")
            raise

if __name__ == "__main__":
    main()
