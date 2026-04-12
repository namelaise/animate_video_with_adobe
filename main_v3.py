# === automate_pipeline.py (version recalée pour limiter les décalages audio/personnage) ===
import os
import re
import json
import time
import random
import shutil
import base64
import subprocess
import sys
import asyncio
import io
import logging
import shlex
import socket

from typing import List, Dict, Tuple, Optional
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

from dotenv import load_dotenv
from pydub import AudioSegment
from PIL import Image
from tqdm import tqdm
from openai import OpenAI
import openai

from automate_adobe_with_bg import Task, run_pool
from assemble_guarded import assemble_from_tail_with_transcript
from segments_processing import (
    rewrite_transcript_with_intervenants_gpt,
    cut_audio_by_diarization,
)
from automate_diarization import transcribe_segments_with_diarization

load_dotenv()

# =========================================================
# LOGGING
# =========================================================

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

# Logger d'audit generation — fichier separe pour analyse des decalages
audit = logging.getLogger("generation_audit")
audit.setLevel(logging.DEBUG)
_audit_handler = logging.FileHandler(LOG_DIR / "generation_audit.log", encoding="utf-8", mode="w")
_audit_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
audit.addHandler(_audit_handler)
audit.propagate = False  # pas dans les logs standard

# stdout utf-8 (Windows)
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


@contextmanager
def time_step(name: str):
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
        log.info(f"✅ Fin étape: {name} (⏱ {dt:.3f}s)\n")


# =========================================================
# CONFIG GLOBALE
# =========================================================

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
RAW_VIDEO_PATH = os.getenv(
    "RAW_VIDEO_PATH",
    os.path.join(BASE_DIR, "Download.mp4") if BASE_DIR else "Download.mp4"
)

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

RAW_SEGMENTS_PATH = os.path.join(TRANSCRIPTS_DIR, "transcription_segments.txt")
REWRITTEN_SEGMENTS_PATH = os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants.txt")
ALIGNED_SEGMENTS_PATH = os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants_aligned.txt")

# Réglages anti-décalage
SEGMENT_PADDING_MS = int(os.getenv("SEGMENT_PADDING_MS", "0"))
MIN_SEGMENT_MS = int(os.getenv("MIN_SEGMENT_MS", "250"))

for _folder in [
    RAW_AUDIO_DIR,
    AUDIO_SEGMENTS_DIR,
    TRANSCRIPTS_DIR,
    IMAGES_DIR,
    LEFT_IMG_DIR,
    RIGHT_IMG_DIR,
    VIDEO_FINALE_DIR,
]:
    os.makedirs(_folder, exist_ok=True)

if os.path.exists("automation_logs"):
    shutil.rmtree("automation_logs", ignore_errors=True)

# Supprime d’anciens segments audio éventuels
for _file in os.listdir(AUDIO_SEGMENTS_DIR):
    if _file.startswith("audio_") and _file.endswith(".mp3"):
        os.remove(os.path.join(AUDIO_SEGMENTS_DIR, _file))


# =========================================================
# UTILITAIRES SYSTÈME / RÉSEAU
# =========================================================

def has_internet(host="1.1.1.1", port=53, timeout=3) -> bool:
    try:
        socket.setdefaulttimeout(timeout)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((host, port))
        return True
    except Exception:
        return False


def wait_for_internet(poll_every=5, label=""):
    first = True
    while not has_internet():
        if first:
            log.warning(f"🌐 Internet KO — mise en pause ({label})")
            first = False
        time.sleep(poll_every)
    if not first:
        log.info(f"🌐 Internet revenu — reprise ({label})")


def run_cmd_capture(args: List[str], log_path: Path, env: dict | None = None, cwd: str | None = None):
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


def ffprobe_duration_seconds(path: str) -> float:
    if not os.path.exists(path):
        return 0.0
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ]
    try:
        out = subprocess.check_output(cmd, text=True).strip()
        return float(out)
    except Exception:
        return 0.0


def preflight_mp4_checks(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Fichier vidéo introuvable: {path}")
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb < 1:
        log.warning(f"⚠️ Vidéo très légère ({size_mb:.2f} MB) — risque de rejet.")
    if size_mb > 5000:
        log.error(f"🚫 Vidéo trop lourde ({size_mb:.0f} MB) — au-delà des limites usuelles.")
        raise RuntimeError("Fichier vidéo trop volumineux")


# =========================================================
# TIKTOK
# =========================================================

def generate_tiktok_description(transcript_path: str) -> str:
    """
    Genere une description TikTok a partir de la transcription :
    2 phrases max + hashtags contextuels + #mrmartin #canular
    """
    try:
        segments = parse_segments_with_speakers(transcript_path)
        if not segments:
            log.warning("Aucun segment pour generer la description, fallback.")
            return "#mrmartin #canular #prank"

        # Construire un resume du dialogue (max 2000 chars pour le prompt)
        dialogue_text = "\n".join(
            f"{s['label']}: {s['text']}" for s in segments
        )[:2000]

        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Tu es un community manager TikTok specialise dans les videos de canulars telephoniques. "
                        "A partir du dialogue suivant, genere une description TikTok COURTE : "
                        "- Maximum 2 phrases qui resument la situation de facon drole et accrocheuse. "
                        "- Ajoute 1 a 3 hashtags en rapport avec le theme de la video. "
                        "- Termine TOUJOURS par #mrmartin #canular "
                        "- Pas de guillemets autour de la reponse. "
                        "- Pas d'emoji sauf si ca apporte vraiment quelque chose."
                    ),
                },
                {"role": "user", "content": dialogue_text},
            ],
            max_tokens=150,
            temperature=0.8,
        )
        description = resp.choices[0].message.content.strip()
        # S'assurer que les hashtags obligatoires sont presents
        if "#mrmartin" not in description.lower():
            description += " #mrmartin"
        if "#canular" not in description.lower():
            description += " #canular"
        log.info(f"Description TikTok generee: {description}")
        return description
    except Exception as e:
        log.error(f"Erreur generation description: {e}")
        return "#mrmartin #canular #prank"


TIKTOK_SPAM_RISK_KEY = "spam_risk_too_many_pending_share"


def _contains_spam_risk(blob: str) -> bool:
    return TIKTOK_SPAM_RISK_KEY in (blob or "").lower()


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
    pending_root = Path(base_dir) / "pending_posts"
    dest_dir = next_indexed_dir(pending_root, prefix="Video_")

    meta = {
        "reason": reason,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "raw_video_src": raw_video_path,
        "final_video_src": final_video_path,
    }
    (dest_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if raw_video_path and os.path.exists(raw_video_path):
        dst_raw = dest_dir / Path(raw_video_path).name
        try:
            shutil.move(raw_video_path, dst_raw)
            log.info(f"📦 Download déplacé vers pending: {dst_raw}")
        except Exception as e:
            log.error(f"❌ Impossible de déplacer Download.mp4 vers pending: {e}")

    if final_video_path and os.path.exists(final_video_path):
        dst_final = dest_dir / Path(final_video_path).name
        try:
            shutil.move(final_video_path, dst_final)
            log.info(f"📦 video_final déplacé vers pending: {dst_final}")
        except Exception as e:
            log.error(f"❌ Impossible de déplacer video_final.mp4 vers pending: {e}")

    log.info(f"✅ Vidéos non postées stockées dans: {dest_dir}")
    return dest_dir


def upload_to_tiktok_with_retry(final_mp4: str, python_exe: str = sys.executable, caption: str = None) -> Tuple[bool, str]:
    log_path = LOG_DIR / "upload_tiktok.log"
    preflight_mp4_checks(final_mp4)

    def _post():
        cmd = [python_exe, "post_tiktok_inbox.py", "--video", final_mp4, "--poll"]
        if caption:
            cmd.extend(["--direct", "--caption", caption])
        return run_cmd_capture(cmd, log_path)

    rc, out, err = _post()
    if rc == 0:
        log.info("📤 Upload TikTok OK (1er essai)")
        return True, "ok"

    blob = (out + "\n" + err)
    blob_low = blob.lower()

    if _contains_spam_risk(blob):
        log.error(f"🚫 Upload TikTok refusé (spam risk): {TIKTOK_SPAM_RISK_KEY}")
        return False, "spam_risk"

    token_issue = (
        "access_token_invalid" in blob_low
        or "http 401" in blob_low
        or "unauthorized" in blob_low
    )

    if token_issue:
        log.warning("🔐 Token TikTok possiblement invalide → rafraîchissement…")
        _ = run_cmd_capture([python_exe, "auth_tiktok_refresh.py"], log_path)
        rc2, out2, err2 = _post()
        if rc2 == 0:
            log.info("📤 Upload TikTok OK après refresh token")
            return True, "ok"
        blob2 = (out2 + "\n" + err2)
        if _contains_spam_risk(blob2):
            log.error(f"🚫 Upload TikTok refusé après retry (spam risk): {TIKTOK_SPAM_RISK_KEY}")
            return False, "spam_risk"
        log.error("❌ Upload TikTok encore en échec après refresh. Voir logs/upload_tiktok.log")
        return False, "token_failed"

    log.error("❌ Upload TikTok échoué (pas un problème de token). Voir logs/upload_tiktok.log")
    return False, "other_failed"


# =========================================================
# UTILITAIRES TEXTE / TIMINGS
# =========================================================

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
                "raw_line": line.rstrip("\n"),
            })
    return parsed


def format_segment_line(start_s: float, end_s: float, label: str, text: str) -> str:
    return f"[{start_s:.3f} → {end_s:.3f}] {label}: {text}".rstrip()


def validate_same_segment_count(raw_segments_path: str, rewritten_segments_path: str) -> None:
    raw = parse_segments_with_speakers(raw_segments_path)
    rew = parse_segments_with_speakers(rewritten_segments_path)

    if not raw:
        raise RuntimeError(f"Aucun segment valide dans {raw_segments_path}")
    if not rew:
        raise RuntimeError(f"Aucun segment valide dans {rewritten_segments_path}")

    if len(raw) != len(rew):
        raise RuntimeError(
            f"Le nombre de segments réécrits diffère du brut: raw={len(raw)} / rewritten={len(rew)}. "
            "On ne continue pas pour éviter un désalignement."
        )


def build_aligned_rewritten_segments(
    raw_segments_path: str,
    rewritten_segments_path: str,
    output_path: str
) -> str:
    """
    Source de vérité pour les timings = raw_segments_path.
    Source de vérité pour les labels/textes = rewritten_segments_path.
    On réécrit un fichier final avec:
      - start/end du brut
      - label/texte du réécrit
    """
    validate_same_segment_count(raw_segments_path, rewritten_segments_path)

    raw = parse_segments_with_speakers(raw_segments_path)
    rew = parse_segments_with_speakers(rewritten_segments_path)

    lines_out: List[str] = []
    for r, w in zip(raw, rew):
        lines_out.append(format_segment_line(
            start_s=r["start_s"],
            end_s=r["end_s"],
            label=w["label"],
            text=w["text"],
        ))

    Path(output_path).write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    log.info(f"🧭 Fichier segments aligné créé: {output_path}")
    return output_path


def _extract_last_int_from_name(name: str) -> int:
    nums = re.findall(r'(\d+)', name)
    return int(nums[-1]) if nums else -1


def list_audio_segments_sorted(audio_dir: str) -> List[str]:
    files = [
        os.path.join(audio_dir, f)
        for f in os.listdir(audio_dir)
        if f.lower().endswith(".mp3")
    ]
    files.sort(key=lambda p: (_extract_last_int_from_name(os.path.basename(p)), os.path.basename(p)))
    return files


def write_audio_segments_manifest(
    aligned_segments_path: str,
    audio_dir: str,
    output_path: str
) -> str:
    """
    Manifest de contrôle pour diagnostiquer les dérives :
      - timings théoriques du transcript aligné
      - durée réelle de chaque mp3
    """
    segments = parse_segments_with_speakers(aligned_segments_path)
    mp3_files = list_audio_segments_sorted(audio_dir)

    items: List[dict] = []
    for i, seg in enumerate(segments):
        audio_path = mp3_files[i] if i < len(mp3_files) else None
        expected_duration = max(0.0, seg["end_s"] - seg["start_s"])
        real_duration = ffprobe_duration_seconds(audio_path) if audio_path else 0.0
        items.append({
            "index": seg["index"],
            "label": seg["label"],
            "text": seg["text"],
            "start_s": seg["start_s"],
            "end_s": seg["end_s"],
            "expected_duration_s": round(expected_duration, 3),
            "audio_path": audio_path,
            "real_audio_duration_s": round(real_duration, 3),
            "duration_delta_s": round(real_duration - expected_duration, 3),
        })

    Path(output_path).write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    log.info(f"🧾 Manifest audio écrit: {output_path}")
    return output_path


# =========================================================
# AUDIO
# =========================================================

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


# =========================================================
# IMAGE / BACKGROUND
# =========================================================

def build_background_prompt(script_text: str, templates_dir: str = "prompts/backgrounds") -> str:
    default_template = """
PROMPT IA — Décor de fond photo réaliste, strictement fidèle au script, sans personnage

Objectif :
Générer une image de décor de fond photoréaliste, crédible, moderne et détaillée, qui servira d’arrière-plan à une scène dans laquelle des personnages seront ajoutés plus tard.
L’image doit représenter uniquement l’environnement, sans aucun personnage visible.

Règle principale :
- Le décor doit être strictement déterminé par le script.
- Il faut analyser le script pour identifier le lieu réel, le contexte, l’ambiance, les objets pertinents et la disposition logique de la scène.
- Le décor doit correspondre fidèlement à ce que le script suggère ou décrit.
- Ne pas inventer de scène absurde, symbolique, surréaliste ou fantaisiste.
- Ne pas ajouter d’éléments incongrus non justifiés par le script.
- En cas d’ambiguïté, choisir l’interprétation la plus réaliste, naturelle et logique.

Type d’image attendu :
- Photo réaliste
- Décor uniquement
- Aucun humain
- Aucune silhouette
- Aucun personnage
- Aucun visage
- Aucun corps partiel
- Aucun reflet humain dans les vitres ou miroirs
- Image pensée comme un background propre et crédible dans lequel des personnages seront ajoutés ensuite

Style visuel :
- Rendu photographique réaliste haut de gamme
- Ambiance immersive et crédible
- Scène visuellement riche mais naturelle
- Composition lisible et équilibrée
- Aucune stylisation cartoon, illustration, peinture ou surréalisme

Qualité & rendu photo :
- Niveau de détail élevé
- Textures réalistes
- Traces d’usage réalistes et naturelles
- Éclairage cohérent avec le lieu et le moment suggéré par le script
- Lumière réaliste, agréable, bien exposée
- Ombres naturelles
- Reflets plausibles
- HDR naturel
- Couleurs réalistes, équilibrées
- Haute netteté
- Perspective crédible
- Objectif photo réaliste type 24–35 mm
- Géométrie propre et réaliste

Décor & composition :
- Construire un lieu réaliste, logique et fidèle au script
- L’espace doit sembler prêt à accueillir des personnages ensuite
- Prévoir une composition claire avec une zone visuellement exploitable pour ajouter des personnages au premier plan ou au centre
- Ajouter uniquement des objets cohérents avec le lieu et la situation
- Le décor peut contenir des traces de présence humaine indirectes
- Mais aucun humain ne doit apparaître

Texte dans l’image :
- Ne mettre du texte visible que si cela est naturellement justifié par le lieu
- Si texte présent : il doit être court, lisible, réaliste et intégré naturellement

Interdictions :
- Aucun personnage
- Aucune silhouette
- Aucun reflet humain
- Aucun décor absurde
- Aucun objet fantaisiste
- Aucun style cartoon / BD / anime
- Aucun rendu CGI artificiel
- Aucun artefact IA
- Aucun texte illisible
- Aucun ajout gratuit sans lien avec le script

Script à analyser pour déterminer le décor de fond :
{SCRIPT}

NEGATIVE PROMPT:
people, person, human, man, woman, child, crowd, silhouette, face, body, reflection of a person,
absurd scene, surreal, fantasy, nonsense objects, cartoon, illustration, anime, stylized, painterly,
3d render look, CGI look, unrealistic environment, fake lighting, warped perspective, lowres, blurry,
noisy, oversaturated, oversharpened, plastic textures, deformed geometry, unreadable text, random letters,
duplicated objects, impossible reflections
    """.strip()

    cleaned = " ".join((script_text or "").split())
    banned = globals().get("BANNED_WORDS") or []
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

    try:
        print("⏳ Génération de l'image via OpenAI...")
        resp = client.images.generate(
            model="gpt-image-1",
            prompt=prompt_text,
            size="1536x1024",
            quality="high",
            output_format="png",
        )
        image_b64 = resp.data[0].b64_json
        with open(output_image_path, "wb") as f:
            f.write(base64.b64decode(image_b64))

        print(f"✅ Image générée : {output_image_path}")
        return output_image_path
    except Exception as e:
        print(f"❌ Génération image échouée : {e}")
        return None


def center_crop_to_ratio(img: Image.Image, target_ratio: float) -> Image.Image:
    w, h = img.size
    current_ratio = w / h

    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        return img.crop((0, top, w, top + new_h))


def split_background_to_tiktok_pairs() -> Tuple[str, str]:
    bg_path = os.path.join(IMAGES_DIR, "full_background.png")
    img = Image.open(bg_path).convert("RGB")

    img = center_crop_to_ratio(img, 9 / 8)
    img = img.resize((1152, 1024), Image.Resampling.LANCZOS)

    left_img = img.crop((0, 0, 576, 1024))
    right_img = img.crop((576, 0, 1152, 1024))

    left_img = left_img.resize((540, 960), Image.Resampling.LANCZOS)
    right_img = right_img.resize((540, 960), Image.Resampling.LANCZOS)

    left_path = os.path.join(LEFT_IMG_DIR, "left_0.png")
    right_path = os.path.join(RIGHT_IMG_DIR, "right_0.png")

    left_img.save(left_path, format="PNG", optimize=True)
    right_img.save(right_path, format="PNG", optimize=True)

    print("✅ Deux images 9:16 générées (gauche/droite) avec crop propre.")
    return left_path, right_path


# =========================================================
# AFFECTATION PUPPETS
# =========================================================

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
    segments_txt_path = segments_txt_path or ALIGNED_SEGMENTS_PATH
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

    audit.info("=== ÉTAPE 8: GÉNÉRATION VIDÉOS ADOBE ===")
    audit.info(f"Lignes texte: {len(lines)} | Fichiers mp3: {len(mp3_files)}")

    # Construire un dict index_segment -> mp3_path pour matcher par numéro
    mp3_by_index: dict[int, str] = {}
    for mp3_path in mp3_files:
        fname = os.path.basename(mp3_path)
        nums = re.findall(r'(\d+)', fname)
        if nums:
            mp3_by_index[int(nums[0])] = mp3_path

    if len(lines) != len(mp3_files):
        print(
            f"⚠️ Nb lignes texte ({len(lines)}) ≠ nb mp3 ({len(mp3_files)}). "
            "On continue avec les segments qui ont un mp3 correspondant."
        )
        audit.warning(f"DÉCALAGE texte/mp3: {len(lines)} lignes vs {len(mp3_files)} mp3")

    tasks: List[Task] = []
    skipped_segments = []
    for info in lines:
        seg_idx = info["index"]
        mp3_path = mp3_by_index.get(seg_idx)
        if mp3_path is None:
            print(f"⚠️ Segment {seg_idx} ({info['label']}): pas de mp3 trouvé, skip.")
            skipped_segments.append(info)
            audit.warning(
                f"  SKIP seg={seg_idx} label={info['label']} "
                f"timing={info['start_s']:.3f}-{info['end_s']:.3f}s "
                f"texte={info['text'][:60]}"
            )
            continue
        label = info["label"]
        genre, puppet = choose_puppet_for_label(label)
        try:
            seg_audio = AudioSegment.from_file(mp3_path)
            mp3_dur_ms = len(seg_audio)
        except Exception:
            mp3_dur_ms = -1
        expected_dur_ms = (info["end_s"] - info["start_s"]) * 1000
        drift_ms = mp3_dur_ms - expected_dur_ms if mp3_dur_ms > 0 else 0
        audit.info(
            f"  TASK seg={seg_idx} label={label} puppet={puppet} "
            f"timing={info['start_s']:.3f}-{info['end_s']:.3f}s "
            f"mp3_dur={mp3_dur_ms}ms expected={expected_dur_ms:.0f}ms drift={drift_ms:+.0f}ms "
            f"audio={os.path.basename(mp3_path)}"
        )
        if abs(drift_ms) > 500:
            audit.warning(f"    DRIFT IMPORTANT: {drift_ms:+.0f}ms sur seg={seg_idx}")
        tasks.append(
            Task(
                audio_path=mp3_path,
                nom=label,
                genre=genre,
                segment_id=str(seg_idx),
                intervenant_index=str(_speaker_index_hint(label)),
                personnage_id=puppet,
            )
        )

    audit.info(f"Tasks à générer: {len(tasks)} | Skipped: {len(skipped_segments)}")

    if not tasks:
        print("❌ Aucun job à traiter.")
        return

    print(f"⏳ Lancement de {len(tasks)} segments (max {max_threads} pages parallèles, 1 seul navigateur)")
    asyncio.run(run_pool(tasks, concurrency=max_threads))

    # Audit post-génération: vérifier les vidéos produites
    video_files = [f for f in os.listdir(VIDEO_SEGMENTS_DIR) if f.lower().endswith(".mp4")] if os.path.isdir(VIDEO_SEGMENTS_DIR) else []
    audit.info(f"Vidéos produites: {len(video_files)} (attendues: {len(tasks)})")
    if len(video_files) != len(tasks):
        audit.warning(f"DÉCALAGE VIDÉO: {len(video_files)} vidéos vs {len(tasks)} tasks")
        task_ids = {t.segment_id for t in tasks}
        video_ids = set()
        for vf in video_files:
            nums = re.findall(r'(\d+)', vf)
            if nums:
                video_ids.add(nums[-1])
        missing = task_ids - video_ids
        if missing:
            audit.warning(f"  Vidéos manquantes pour segments: {sorted(missing)}")
    for vf in sorted(video_files):
        vpath = os.path.join(VIDEO_SEGMENTS_DIR, vf)
        audit.debug(f"  {vf} taille={os.path.getsize(vpath)}o")

    print("✅ Toutes les vidéos animées ont été générées.")


# =========================================================
# VÉRIFICATION TEXTE
# =========================================================

def get_transcription_file_with_verification(transcription_path: str) -> str | None:
    if not os.path.exists(transcription_path):
        print(f"❌ Le fichier {transcription_path} est introuvable.")
        return None

    with open(transcription_path, "r", encoding="utf-8") as f:
        content = f.read()

    print("\n--- Aperçu de la transcription (début) ---")
    print(content[:1500])
    print("--- Fin de l’aperçu ---\n")

    print("✅ Le contenu vous semble-t-il correct ?")
    print("Appuie sur [y] pour continuer, [n] pour arrêter, ou attendre 30 secondes pour continuer automatiquement.")

    start_time = time.time()
    timeout_s = 30

    while True:
        if time.time() - start_time > timeout_s:
            print("⏳ Temps écoulé. Suite du traitement...")
            return content

        if os.name == 'nt':
            import msvcrt
            if msvcrt.kbhit():
                key = msvcrt.getwch().lower()
                if key == 'y':
                    print("➡️ Poursuite du traitement...")
                    return content
                if key == 'n':
                    print("❌ Traitement interrompu par l’utilisateur.")
                    sys.exit()
        else:
            import select
            if select.select([sys.stdin], [], [], 1)[0]:
                key = sys.stdin.readline().strip().lower()
                if key == 'y':
                    print("➡️ Poursuite du traitement...")
                    return content
                if key == 'n':
                    print("❌ Traitement interrompu par l’utilisateur.")
                    sys.exit()
        time.sleep(1)


# =========================================================
# ARCHIVAGE / DELETE
# =========================================================

def archive_outputs():
    archive_root = os.path.join(BASE_DIR, "archive")
    os.makedirs(archive_root, exist_ok=True)

    existing = [
        d for d in os.listdir(archive_root)
        if os.path.isdir(os.path.join(archive_root, d)) and re.match(r"^Video_\d+$", d)
    ]
    next_idx = 1 + max((int(d.split("_")[1]) for d in existing), default=0)

    archive_dir = os.path.join(archive_root, f"Video_{next_idx}")
    os.makedirs(archive_dir, exist_ok=False)

    src = os.path.join(BASE_DIR, "video_finale", "video_final.mp4")
    if os.path.exists(src):
        dst = os.path.join(archive_dir, "video_final.mp4")
        shutil.move(src, dst)
        print(f"✅ Vidéo archivée dans : {dst}")

    print(f"✅ Tous les fichiers ont été archivés dans : {archive_dir}")


def delete_outputs():
    files_to_delete = [
        os.path.join(RAW_AUDIO_DIR, "audio_full.mp3"),
        os.path.join(IMAGES_DIR, "prompt.txt"),
        os.path.join(IMAGES_DIR, "full_background.png"),
        os.path.join(LEFT_IMG_DIR, "left_0.png"),
        os.path.join(RIGHT_IMG_DIR, "right_0.png"),
        os.path.join("output", "intervenants.json"),
        os.path.join("output", "prompt_nettoye.txt"),
        os.path.join(TRANSCRIPTS_DIR, "transcription_full.txt"),
        os.path.join(TRANSCRIPTS_DIR, "transcription_segments.txt"),
        os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants.txt"),
        os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants_aligned.txt"),
        os.path.join(TRANSCRIPTS_DIR, "diarization_segments.json"),
        os.path.join(TRANSCRIPTS_DIR, "speakers.json"),
        os.path.join(TRANSCRIPTS_DIR, "audio_segments_manifest.json"),
        os.path.join(BASE_DIR, "video_finale", "video_final.mp4"),
    ]

    for p in files_to_delete:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    if os.path.exists(AUDIO_SEGMENTS_DIR):
        shutil.rmtree(AUDIO_SEGMENTS_DIR, ignore_errors=True)
    if os.path.exists(VIDEO_SEGMENTS_DIR):
        shutil.rmtree(VIDEO_SEGMENTS_DIR, ignore_errors=True)
    if os.path.exists("generated_backgrounds"):
        shutil.rmtree("generated_backgrounds", ignore_errors=True)

    os.makedirs(AUDIO_SEGMENTS_DIR, exist_ok=True)
    os.makedirs(VIDEO_SEGMENTS_DIR, exist_ok=True)

    print("✅ Tous les fichiers ont été supprimés")


# =========================================================
# PIPELINE MAIN
# =========================================================

def run_pipeline_once() -> Tuple[bool, str]:
    """
    Retourne (done, status)
      - done=True, status="posted" si upload OK + archive OK
      - done=False, status="spam_risk_stashed" si spam risk -> stash + continue
    """
    log.info("📦 Traitement initial démarré…")
    t_all = time.perf_counter()

    with time_step("0) Nettoyage initial"):
        delete_outputs()

    with time_step("1) Extraction audio depuis RAW_VIDEO"):
        extract_audio_from_video()

    with time_step("2) Transcription + diarisation"):
        wait_for_internet(label="Transcription + diarisation")
        transcribe_segments_with_diarization(
            audio_path=FULL_AUDIO_PATH,
            output_dir=TRANSCRIPTS_DIR
        )

    with time_step("3) Vérification + réécriture intervenants"):
        segments_text_content = get_transcription_file_with_verification(RAW_SEGMENTS_PATH)
        wait_for_internet(label="OpenAI texte verification")
        rewrite_transcript_with_intervenants_gpt(
            contenu_segments_brut=segments_text_content,
            dossier_sortie=TRANSCRIPTS_DIR,
            nom_fichier_sortie="transcription_segments_intervenants.txt",
        )

    with time_step("4) Réalignement strict des timings"):
        build_aligned_rewritten_segments(
            raw_segments_path=RAW_SEGMENTS_PATH,
            rewritten_segments_path=REWRITTEN_SEGMENTS_PATH,
            output_path=ALIGNED_SEGMENTS_PATH
        )
        # Audit: comparer les fichiers brut / réécrit / aligné
        raw_lines = parse_segments_with_speakers(RAW_SEGMENTS_PATH)
        rewritten_lines = parse_segments_with_speakers(REWRITTEN_SEGMENTS_PATH)
        aligned_lines = parse_segments_with_speakers(ALIGNED_SEGMENTS_PATH)
        audit.info("=== ÉTAPE 4: RÉALIGNEMENT ===")
        audit.info(f"Segments bruts: {len(raw_lines)} | Réécrits: {len(rewritten_lines)} | Alignés: {len(aligned_lines)}")
        if len(raw_lines) != len(aligned_lines):
            audit.warning(f"DÉCALAGE: bruts ({len(raw_lines)}) != alignés ({len(aligned_lines)})")
        for i, seg in enumerate(aligned_lines):
            audit.debug(f"  seg_aligned[{seg['index']}] {seg['start_s']:.3f}-{seg['end_s']:.3f}s label={seg['label']}")

    with time_step("5) Découpage MP3 sur timings bruts alignés"):
        audio_full_duration_ms = len(AudioSegment.from_file(FULL_AUDIO_PATH))
        audit.info("=== ÉTAPE 5: DÉCOUPAGE AUDIO ===")
        audit.info(f"Audio source: {FULL_AUDIO_PATH} duree={audio_full_duration_ms}ms")
        audit.info(f"Padding={SEGMENT_PADDING_MS}ms | Min segment={MIN_SEGMENT_MS}ms")

        cut_audio_by_diarization(
            chemin_fichier_audio_mp3=FULL_AUDIO_PATH,
            chemin_fichier_segments_txt=ALIGNED_SEGMENTS_PATH,
            dossier_sortie_segments_audio=AUDIO_SEGMENTS_DIR,
            padding_millisecondes=SEGMENT_PADDING_MS,
            duree_minimale_conservee_millisecondes=MIN_SEGMENT_MS,
            preferer_copie_flux=False,   # précision > rapidité
        )

        # Audit: lister les mp3 produits avec leurs durées
        mp3_produced = sorted(
            [f for f in os.listdir(AUDIO_SEGMENTS_DIR) if f.lower().endswith(".mp3")],
            key=lambda f: f
        )
        audit.info(f"MP3 produits: {len(mp3_produced)} (attendus: {len(aligned_lines)})")
        if len(mp3_produced) != len(aligned_lines):
            audit.warning(f"DÉCALAGE AUDIO: {len(mp3_produced)} mp3 vs {len(aligned_lines)} segments alignés")
            # Identifier les segments manquants
            mp3_indices = set()
            for fname in mp3_produced:
                nums = re.findall(r'(\d+)', fname)
                if nums:
                    mp3_indices.add(int(nums[0]))
            for seg in aligned_lines:
                if seg["index"] not in mp3_indices:
                    dur_ms = (seg["end_s"] - seg["start_s"]) * 1000
                    audit.warning(
                        f"  SEGMENT MANQUANT idx={seg['index']} "
                        f"duree={dur_ms:.0f}ms (min={MIN_SEGMENT_MS}ms) "
                        f"label={seg['label']} texte={seg['text'][:60]}"
                    )
        for fname in mp3_produced:
            fpath = os.path.join(AUDIO_SEGMENTS_DIR, fname)
            try:
                seg_audio = AudioSegment.from_file(fpath)
                audit.debug(f"  {fname} duree={len(seg_audio)}ms taille={os.path.getsize(fpath)}o")
            except Exception as e:
                audit.error(f"  {fname} ILLISIBLE: {e}")

    with time_step("6) Manifest de contrôle audio"):
        write_audio_segments_manifest(
            aligned_segments_path=ALIGNED_SEGMENTS_PATH,
            audio_dir=AUDIO_SEGMENTS_DIR,
            output_path=os.path.join(TRANSCRIPTS_DIR, "audio_segments_manifest.json")
        )

    with time_step("7) Génération image de fond + split 9:16"):
        full_txt = get_transcription_file_with_verification(RAW_SEGMENTS_PATH)
        wait_for_internet(label="OpenAI image generation")
        generate_image_with_openai(full_txt)
        split_background_to_tiktok_pairs()

    with time_step("8) Génération vidéos segments (Adobe)"):
        wait_for_internet(label="Adobe generation")
        automate_generation_videos(
            max_threads=4,
            segments_txt_path=ALIGNED_SEGMENTS_PATH,
            audio_segments_dir=AUDIO_SEGMENTS_DIR,
        )

    with time_step("9) Assemblage final (sur transcript aligné)"):
        # Audit pré-assemblage
        audit.info("=== ÉTAPE 9: ASSEMBLAGE FINAL ===")
        vid_files = sorted([f for f in os.listdir(VIDEO_SEGMENTS_DIR) if f.lower().endswith(".mp4")]) if os.path.isdir(VIDEO_SEGMENTS_DIR) else []
        aligned_for_assembly = parse_segments_with_speakers(ALIGNED_SEGMENTS_PATH)
        audit.info(f"Vidéos disponibles: {len(vid_files)} | Segments transcript: {len(aligned_for_assembly)}")

        # Extraire les index des vidéos produites pour filtrer le transcript
        video_indices = set()
        for vf in vid_files:
            m = re.search(r'(\d+)(?=\.mp4$)', vf)
            if m:
                video_indices.add(int(m.group(1)))

        # Créer un transcript filtré ne contenant que les lignes avec une vidéo
        filtered_transcript_path = os.path.join(TRANSCRIPTS_DIR, "transcription_segments_for_assembly.txt")
        kept = 0
        skipped_assembly = 0
        with open(ALIGNED_SEGMENTS_PATH, "r", encoding="utf-8") as fin, \
             open(filtered_transcript_path, "w", encoding="utf-8") as fout:
            for line_num, line in enumerate(fin, start=1):
                if line_num in video_indices:
                    fout.write(line)
                    kept += 1
                else:
                    skipped_assembly += 1
                    audit.info(f"  Ligne {line_num} exclue de l'assemblage (pas de vidéo)")

        audit.info(f"Transcript filtré: {kept} lignes gardées, {skipped_assembly} exclues → {filtered_transcript_path}")

        if len(vid_files) != kept:
            audit.warning(f"DÉCALAGE RÉSIDUEL: {len(vid_files)} vidéos vs {kept} lignes filtrées")

        # Durées des vidéos vs timings attendus
        total_video_dur = 0.0
        for vf in vid_files:
            vpath = os.path.join(VIDEO_SEGMENTS_DIR, vf)
            try:
                probe_cmd = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", vpath],
                    capture_output=True, text=True
                )
                vdur = float(probe_cmd.stdout.strip()) if probe_cmd.stdout.strip() else 0
                total_video_dur += vdur
                audit.debug(f"  {vf} duree_video={vdur:.3f}s taille={os.path.getsize(vpath)}o")
            except Exception as e:
                audit.error(f"  {vf} PROBE ERREUR: {e}")

        filtered_lines = parse_segments_with_speakers(filtered_transcript_path)
        if filtered_lines:
            total_expected = filtered_lines[-1]["end_s"]
            audit.info(f"Durée totale vidéos brutes: {total_video_dur:.3f}s | Durée attendue (transcript filtré): {total_expected:.3f}s")

        assemble_from_tail_with_transcript(
            video_segments_dir=VIDEO_SEGMENTS_DIR,
            transcript_path=filtered_transcript_path,
            output_path=VIDEO_FINALE_PATH,
            crf=18,
            preset="veryfast",
            audio_bitrate="192k",
            min_keep_sec=0.10,
            force_fps=60,
        )

        # Audit post-assemblage
        if os.path.exists(VIDEO_FINALE_PATH):
            try:
                probe_cmd = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", VIDEO_FINALE_PATH],
                    capture_output=True, text=True
                )
                final_dur = float(probe_cmd.stdout.strip()) if probe_cmd.stdout.strip() else 0
                audio_dur = len(AudioSegment.from_file(FULL_AUDIO_PATH)) / 1000.0
                drift_final = final_dur - audio_dur
                audit.info(f"VIDÉO FINALE: duree={final_dur:.3f}s audio={audio_dur:.3f}s drift={drift_final:+.3f}s taille={os.path.getsize(VIDEO_FINALE_PATH)}o")
                if abs(drift_final) > 0.5:
                    audit.warning(f"DRIFT FINAL IMPORTANT: vidéo {drift_final:+.3f}s par rapport à l'audio")
            except Exception as e:
                audit.error(f"PROBE VIDÉO FINALE ERREUR: {e}")
        else:
            audit.error("VIDÉO FINALE NON PRODUITE")

    with time_step("10) Generation description + Upload TikTok"):
        # Generer la description TikTok a partir de la transcription
        caption = generate_tiktok_description(ALIGNED_SEGMENTS_PATH)

        run_cmd_capture([sys.executable, "auth_tiktok_refresh.py"], LOG_DIR / "upload_tiktok.log")

        final_mp4 = VIDEO_FINALE_PATH
        wait_for_internet(label="Post TikTok")

        ok, reason = upload_to_tiktok_with_retry(final_mp4, python_exe=sys.executable, caption=caption)

        if ok:
            log.info("✅ Upload TikTok OK -> archivage standard")
            with time_step("11) Archivage (post OK)"):
                archive_outputs()
            dt_all = (time.perf_counter() - t_all) / 60
            log.info(f"🎉 Pipeline COMPLET terminé en {dt_all:.3f} minutes")
            print("\n✅ Traitement terminé.")
            return True, "posted"

        if reason == "spam_risk":
            log.warning("⚠️ Spam risk détecté -> stockage des vidéos non postées (pending_posts) puis reprise.")
            stash_unposted_videos(
                base_dir=BASE_DIR,
                raw_video_path=RAW_VIDEO_PATH,
                final_video_path=VIDEO_FINALE_PATH,
                reason=TIKTOK_SPAM_RISK_KEY
            )
            with time_step("11) Nettoyage après spam risk (sans arrêter)"):
                delete_outputs()
            return False, "spam_risk_stashed"

        raise RuntimeError(f"Upload TikTok a échoué (reason={reason}) — consulte logs/upload_tiktok.log")


def main():
    while True:
        try:
            done, status = run_pipeline_once()
            if done and status == "posted":
                break

            if status == "spam_risk_stashed":
                continue

        except openai.RateLimitError as e:
            msg = str(e)
            if "insufficient_quota" in msg:
                log.error("⛔ Quota OpenAI insuffisant (insufficient_quota). Pause 15 minutes puis relance du pipeline…")
                time.sleep(15 * 60)
                continue
            raise

        except Exception:
            log.exception("💥 Erreur non gérée dans le pipeline, arrêt.")
            raise


if __name__ == "__main__":
    main()