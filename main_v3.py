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

from automate_adobe_with_bg import Task, run_pool, _safe_filename
from assemble_guarded import assemble_from_tail_with_transcript
from segments_processing import (
    rewrite_transcript_with_intervenants_gpt,
    cut_audio_by_diarization,
)
from automate_diarization import transcribe_segments_with_diarization
from tiktok_account_manager import (
    get_active_account_id,
    get_active_account,
    get_access_token,
    get_account_label,
    get_rotation_status,
    mark_account_used,
    update_tokens,
)

load_dotenv()

# =========================================================
# LOGGING
# =========================================================

LOG_DIR = Path("./logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("pipeline")
log.setLevel(logging.INFO)
_fmt = logging.Formatter('[%(asctime)s] %(levelname)-8s [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
_fh = logging.FileHandler(LOG_DIR / "pipeline.log", mode="w", encoding="utf-8")
_fh.setFormatter(_fmt)
log.addHandler(_sh)
log.addHandler(_fh)
log.propagate = False

# Logger d'audit generation — fichier separe pour analyse des decalages
audit = logging.getLogger("generation_audit")
audit.setLevel(logging.DEBUG)
_audit_handler = logging.FileHandler(LOG_DIR / "generation_audit.log", encoding="utf-8", mode="w")
_audit_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
audit.addHandler(_audit_handler)
audit.propagate = False  # pas dans les logs standard

# stdout utf-8 (Windows)
# NE PAS remplacer sys.stdout par un nouveau TextIOWrapper si reconfigure() echoue :
# cela creerait deux wrappers sur le meme buffer binaire → corruptions + OSError sur print().
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


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

# ── Matching IPC (GUI ↔ pipeline) ─────────────────────────────────────────────
_base_path              = Path(BASE_DIR) if BASE_DIR else Path(__file__).parent
MATCHING_MODE_FILE      = _base_path / "matching_mode.json"
MATCHING_REQUEST_FILE   = _base_path / "matching_request.json"
MATCHING_RESPONSE_FILE  = _base_path / "matching_response.json"
MATCHING_TIMEOUT_S      = 65   # GUI timeout = 60s + 5s marge

# ── Pipeline state (reprise après crash) ──────────────────────────────────────
PIPELINE_STATE_FILE = str(_base_path / "pipeline_state.json")

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
os.makedirs("automation_logs", exist_ok=True)

# Supprime d'anciens segments audio éventuels
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


# =========================================================
# CLAUDE CLI HELPER
# =========================================================

def _call_claude_cli(prompt: str, timeout: int = 180) -> str:
    """
    Appelle Claude CLI (couvert par l'abonnement).
    Lève RuntimeError si le CLI n'est pas disponible ou retourne une erreur.
    """
    result = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "text"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI erreur (code {result.returncode}): {result.stderr.strip()[:300]}")
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("Claude CLI: réponse vide")
    return output


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
    Genere une description TikTok a partir de la transcription.
    Essaie Claude CLI en premier (abonnement), fallback sur OpenAI gpt-4o-mini.
    """
    try:
        segments = parse_segments_with_speakers(transcript_path)
        if not segments:
            log.warning("Aucun segment pour generer la description, fallback.")
            return "#mrmartin #canular #prank"

        dialogue_text = "\n".join(
            f"{s['label']}: {s['text']}" for s in segments
        )[:2000]

        system_instruction = (
            "Tu es un community manager TikTok specialise dans les videos de canulars telephoniques. "
            "A partir du dialogue suivant, genere une description TikTok COURTE : "
            "- Maximum 2 phrases qui resument la situation de facon drole et accrocheuse. "
            "- Ajoute 1 a 3 hashtags en rapport avec le theme de la video. "
            "- Termine TOUJOURS par #mrmartin #canular "
            "- Pas de guillemets autour de la reponse. "
            "- Pas d'emoji sauf si ca apporte vraiment quelque chose. "
            "- Reponds UNIQUEMENT avec la description, sans introduction."
        )

        description = None

        # ── Tentative 1 : Claude CLI ─────────────────────────────────────────
        try:
            prompt = f"{system_instruction}\n\nDialogue :\n{dialogue_text}"
            description = _call_claude_cli(prompt, timeout=60)
            log.info("Description TikTok generee via Claude CLI")
        except Exception as e:
            log.warning("Claude CLI indisponible pour description (%s) → fallback OpenAI", e)

        # ── Tentative 2 : OpenAI fallback ─────────────────────────────────────
        if not description:
            client = OpenAI()
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": dialogue_text},
                ],
                max_tokens=150,
                temperature=0.8,
            )
            description = (resp.choices[0].message.content or "").strip()
            log.info("Description TikTok generee via OpenAI gpt-4o-mini")

        if "#mrmartin" not in description.lower():
            description += " #mrmartin"
        if "#canular" not in description.lower():
            description += " #canular"
        log.info(f"Description TikTok finale: {description}")
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


def _detect_scope_not_authorized(blob: str) -> bool:
    blob_low = blob.lower()
    return "scope_not_authorized" in blob_low or "unaudited_client" in blob_low


def _detect_token_issue(blob: str) -> bool:
    blob_low = blob.lower()
    if "scope_not_authorized" in blob_low:
        return False  # pas un problème de token, c'est un problème de scope
    return (
        "access_token_invalid" in blob_low
        or "http 401" in blob_low
        or "unauthorized" in blob_low
    )


def _refresh_token_ok(python_exe: str, log_path: Path) -> bool:
    """Lance auth_tiktok_refresh.py et retourne True si le refresh a réussi."""
    rc, out, err = run_cmd_capture([python_exe, "auth_tiktok_refresh.py"], log_path)
    if rc != 0:
        blob = (out + "\n" + err).lower()
        if "refresh" in blob and ("expir" in blob or "invalid" in blob or "introuvable" in blob):
            log.error("⛔ Le refresh_token est expiré/invalide — une ré-authentification manuelle est nécessaire "
                      "(lance auth_tiktok_token_manager.py)")
        else:
            log.error(f"⛔ auth_tiktok_refresh.py a échoué (code={rc}). Voir {log_path}")
        return False
    log.info("🔄 Token TikTok rafraîchi avec succès")
    return True


def _parse_publish_id(stdout: str) -> str:
    """Extrait le publish_id du stdout de post_tiktok_inbox.py."""
    m = re.search(r'publish_id=(\S+)', stdout)
    return m.group(1) if m else ""


def save_upload_entry(publish_id: str, ok: bool, reason: str):
    """Sauvegarde une entrée dans upload_history.json pour le suivi des stats."""
    history_path = Path(BASE_DIR) / "upload_history.json"
    try:
        history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
    except Exception:
        history = []

    # Lire le template de prompt utilisé pour cette génération
    meta_path = Path(BASE_DIR) / "current_generation_meta.json"
    prompt_template = "(inconnu)"
    try:
        if meta_path.exists():
            prompt_template = json.loads(meta_path.read_text(encoding="utf-8")).get("prompt_template", "(inconnu)")
    except Exception:
        pass

    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "publish_id": publish_id,
        "prompt_template": prompt_template,
        "upload_ok": ok,
        "reason": reason,
        "video_id": None,         # rempli plus tard par fetch_stats.py
        "view_count": None,
        "like_count": None,
        "comment_count": None,
        "share_count": None,
    }
    history.append(entry)
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("📊 Entrée historique sauvée (template=%s, publish_id=%s)", prompt_template, publish_id or "N/A")


def _upload_with_account(
    final_mp4: str,
    account_id: str,
    python_exe: str,
    log_path: Path,
    caption: str | None,
) -> Tuple[bool, str]:
    """
    Tente un upload pour un compte spécifique.
    Retourne (success, reason).
    """
    token = get_access_token(account_id)
    use_direct = os.getenv("TIKTOK_DIRECT_POST", "0").strip() == "1"
    label = get_account_label(account_id)

    def _post(direct: bool = False, tok: str = token):
        cmd = [python_exe, "post_tiktok_inbox.py", "--video", final_mp4, "--poll",
               "--token", tok]
        if direct and caption:
            cmd.extend(["--direct", "--caption", caption])
        return run_cmd_capture(cmd, log_path)

    log.info(f"📤 Upload TikTok → {label} (compte {account_id})")

    rc, out, err = _post(direct=use_direct and bool(caption))
    if rc == 0:
        mode = "DIRECT" if (use_direct and caption) else "INBOX"
        log.info(f"✅ Upload TikTok OK ({mode}) — {label}")
        mark_account_used(account_id)
        save_upload_entry(_parse_publish_id(out), ok=True, reason="ok")
        return True, "ok"

    blob = out + "\n" + err

    # scope_not_authorized → fallback INBOX
    if _detect_scope_not_authorized(blob):
        log.warning(f"⚠️ scope video.publish non autorisé ({label}) → fallback INBOX")
        rc_fb, out_fb, _ = _post(direct=False)
        if rc_fb == 0:
            log.info(f"✅ Upload TikTok OK en mode INBOX (fallback) — {label}")
            mark_account_used(account_id)
            save_upload_entry(_parse_publish_id(out_fb), ok=True, reason="ok_inbox_fallback")
            return True, "ok_inbox_fallback"
        log.error(f"❌ Upload TikTok échoué même en INBOX ({label})")
        return False, "other_failed"

    if _contains_spam_risk(blob):
        log.error(f"🚫 Upload TikTok refusé spam risk ({label}): {TIKTOK_SPAM_RISK_KEY}")
        return False, "spam_risk"

    # Token invalide → refresh + retry
    if _detect_token_issue(blob):
        log.warning(f"🔐 Token invalide ({label}) → rafraîchissement…")
        refresh_cmd = [python_exe, "auth_tiktok_refresh.py", "--account", account_id]
        rc_r, _, _ = run_cmd_capture(refresh_cmd, log_path)
        if rc_r != 0:
            log.error(f"⛔ Refresh échoué pour {label}")
            return False, "token_failed"
        # Recharger le token depuis le gestionnaire de comptes
        from tiktok_account_manager import get_access_token as _gat
        fresh_token = _gat(account_id)
        rc2, out2, err2 = _post(direct=use_direct and bool(caption), tok=fresh_token)
        if rc2 == 0:
            log.info(f"✅ Upload TikTok OK après refresh — {label}")
            mark_account_used(account_id)
            save_upload_entry(_parse_publish_id(out2), ok=True, reason="ok")
            return True, "ok"
        blob2 = out2 + "\n" + err2
        if _contains_spam_risk(blob2):
            return False, "spam_risk"
        log.error(f"⛔ Token toujours invalide après refresh ({label})")
        return False, "token_failed"

    # Erreur inconnue → retry sans direct
    log.warning(f"❌ Upload TikTok erreur inconnue ({label}). Retry…")
    rc3, out3, err3 = _post(direct=False)
    if rc3 == 0:
        log.info(f"✅ Upload TikTok OK après retry ({label})")
        mark_account_used(account_id)
        save_upload_entry(_parse_publish_id(out3), ok=True, reason="ok")
        return True, "ok"
    if _contains_spam_risk(out3 + "\n" + err3):
        return False, "spam_risk"
    return False, "other_failed"


def upload_to_tiktok_with_retry(final_mp4: str, python_exe: str = sys.executable, caption: str = None) -> Tuple[bool, str]:
    """
    Upload la vidéo sur le compte actif (dernier compte sélectionné dans le GUI).
    """
    log_path = LOG_DIR / "upload_tiktok.log"
    preflight_mp4_checks(final_mp4)

    account_id = get_active_account_id()
    if not account_id:
        log.error("❌ Aucun compte TikTok configuré. Lance auth_tiktok_token_manager.py")
        return False, "no_account"

    status = get_rotation_status()
    log.info(f"📱 Compte TikTok actif : {status}")

    return _upload_with_account(final_mp4, account_id, python_exe, log_path, caption)


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
    if not os.path.exists(RAW_VIDEO_PATH):
        raise FileNotFoundError(f"Video source introuvable: {RAW_VIDEO_PATH}")
    cmd = f'ffmpeg -i "{RAW_VIDEO_PATH}" -q:a 0 -map a "{FULL_AUDIO_PATH}" -y'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.exists(FULL_AUDIO_PATH):
        raise RuntimeError(f"Extraction audio echouee (code {result.returncode}): {result.stderr[:200]}")
    log.info("Audio extrait : %s", FULL_AUDIO_PATH)
    return FULL_AUDIO_PATH


def split_full_audio(max_segment_duration_sec: int = 110) -> List[str]:
    audio = AudioSegment.from_file(FULL_AUDIO_PATH)
    segment_paths: List[str] = []
    total_parts = (len(audio) + max_segment_duration_sec * 1000 - 1) // (max_segment_duration_sec * 1000)

    log.info("Découpage audio en segments...")
    for i in tqdm(range(total_parts), desc="Découpage", unit="segment"):
        start_ms = i * max_segment_duration_sec * 1000
        end_ms = start_ms + max_segment_duration_sec * 1000
        segment = audio[start_ms:end_ms]
        filename = f"audio_{i + 1}.mp3"
        path_out = os.path.join(AUDIO_SEGMENTS_DIR, filename)
        segment.export(path_out, format="mp3")
        segment_paths.append(path_out)

    log.info("Audio découpé : %d segments", len(segment_paths))
    return segment_paths


# =========================================================
# IMAGE / BACKGROUND
# =========================================================

def build_background_prompt(script_text: str, templates_dir: str = "prompts/backgrounds") -> str:
    default_template = """
PROMPT IA — Décor de fond photo réaliste, strictement fidèle au script, sans personnage

Objectif :
Générer une image de décor de fond photoréaliste, crédible, moderne et détaillée, qui servira d'arrière-plan à une scène dans laquelle des personnages seront ajoutés plus tard.
L'image doit représenter uniquement l'environnement, sans aucun personnage visible.

Règle principale :
- Le décor doit être strictement déterminé par le script.
- Il faut analyser le script pour identifier le lieu réel, le contexte, l'ambiance, les objets pertinents et la disposition logique de la scène.
- Le décor doit correspondre fidèlement à ce que le script suggère ou décrit.
- Ne pas inventer de scène absurde, symbolique, surréaliste ou fantaisiste.
- Ne pas ajouter d'éléments incongrus non justifiés par le script.
- En cas d'ambiguïté, choisir l'interprétation la plus réaliste, naturelle et logique.

Type d'image attendu :
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
- Traces d'usage réalistes et naturelles
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
- L'espace doit sembler prêt à accueillir des personnages ensuite
- Prévoir une composition claire avec une zone visuellement exploitable pour ajouter des personnages au premier plan ou au centre
- Ajouter uniquement des objets cohérents avec le lieu et la situation
- Le décor peut contenir des traces de présence humaine indirectes
- Mais aucun humain ne doit apparaître

Texte dans l'image :
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

    # ── Extraction de contexte via GPT-4o ─────────────────────────────────
    # gpt-image-1 est un modèle image : il ne comprend pas bien 2000 mots de
    # transcription française. On demande d'abord à GPT-4o d'extraire les
    # éléments visuellement pertinents pour guider le modèle image.
    scene_context = cleaned  # fallback si GPT échoue
    try:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        _extraction = _client.chat.completions.create(
            model="gpt-4o",
            messages=[{
                "role": "system",
                "content": (
                    "Tu es un directeur artistique. On te donne la transcription d'un appel téléphonique "
                    "de canular (Mr Martin). Tu dois en extraire uniquement les informations visuelles "
                    "nécessaires pour générer un décor de fond (sans personnage). "
                    "Réponds en anglais, en 4 lignes maximum, format :\n"
                    "Location: [lieu précis, ex: french hospital reception desk, french tax office, car dealership showroom]\n"
                    "Topic: [sujet de l'appel en 5 mots max, ex: fake insurance claim, unpaid invoice]\n"
                    "Atmosphere: [ambiance en 3 mots, ex: tense and bureaucratic]\n"
                    "Key objects: [3-5 objets typiques du lieu, ex: reception counter, waiting chairs, administrative posters]\n"
                    "Ne mets rien d'autre."
                )
            }, {
                "role": "user",
                "content": f"Transcription (extrait) :\n{cleaned[:3000]}"
            }],
            max_tokens=120,
            temperature=0.3,
        )
        scene_context = _extraction.choices[0].message.content.strip()
        log.info("Contexte scene extrait par GPT-4o:\n%s", scene_context)
    except Exception as e:
        log.warning("Extraction contexte GPT-4o echouee, fallback sur transcription brute: %s", e)

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
        final_prompt = template_text.replace("{SCRIPT}", scene_context).replace("{{SCRIPT}}", scene_context)
    else:
        final_prompt = f"{template_text}\n\n{scene_context}"

    Path(IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    prompt_path = Path(IMAGES_DIR) / "prompt.txt"
    prompt_path.write_text(final_prompt, encoding="utf-8")

    # Persistance du template choisi pour l'historique d'upload
    meta_path = Path(BASE_DIR) / "current_generation_meta.json"
    try:
        meta_path.write_text(
            json.dumps({"prompt_template": chosen_name}, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception:
        pass

    log.info("Prompt image généré (template: %s) → %s", chosen_name, prompt_path)
    return final_prompt


class ModerationRejectedError(RuntimeError):
    """Levée quand OpenAI refuse le prompt pour violation de contenu."""


# ── Gemini Playwright ─────────────────────────────────────────────────────────

BASE_PROFILE_PATH = os.getenv(
    "BASE_PROFILE_PATH",
    r"C:\Users\n.amelaise\Desktop\martinV2\animate_video_with_adobe\playwright-profile"
)
CHROME_PATH_GEMINI = os.getenv(
    "CHROME_PATH",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)


async def _gemini_generate_image(prompt: str, output_path: str) -> bool:
    """
    Envoie `prompt` à Gemini.google.com via Playwright et sauvegarde l'image générée.
    Retourne True si succès, False sinon.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=BASE_PROFILE_PATH,
            executable_path=CHROME_PATH_GEMINI,
            headless=False,
            slow_mo=200,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
            ignore_default_args=["--enable-automation"],
        )

        page = await browser.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )

        try:
            await page.goto("https://gemini.google.com", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Fermer popup éventuel
            for sel in ["button:has-text('Continuer')", "button:has-text('Continue')",
                        "button:has-text('Accepter')", "button:has-text('Accept')",
                        "button:has-text('Got it')", "button:has-text('Compris')"]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click(timeout=3000)
                        await page.wait_for_timeout(800)
                except Exception:
                    pass

            # Trouver la zone de saisie
            input_sel = None
            for sel in ["[contenteditable='true']", "textarea", "rich-textarea", ".ql-editor"]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        input_sel = sel
                        break
                except Exception:
                    pass

            if not input_sel:
                log.warning("[Gemini] Zone de saisie non trouvée")
                return False

            el = page.locator(input_sel).first
            await el.click(timeout=5000)
            await page.wait_for_timeout(500)
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            await page.keyboard.type(prompt, delay=15)
            await page.wait_for_timeout(500)
            await page.keyboard.press("Enter")
            log.info("[Gemini] Prompt envoyé, attente de l'image...")

            # Attendre l'image (max 90s)
            img_url = None
            for i in range(18):
                await page.wait_for_timeout(5000)
                imgs = await page.evaluate("""
                    () => [...document.querySelectorAll('img')]
                        .filter(img => {
                            const src = img.src || '';
                            const w = img.naturalWidth;
                            const h = img.naturalHeight;
                            return w > 300 && h > 300
                                && !src.includes('avatar')
                                && !src.includes('favicon')
                                && !src.includes('logo')
                                && !src.includes('icon')
                                && (src.includes('blob:') || src.includes('googleusercontent')
                                    || src.includes('data:image'));
                        })
                        .map(img => ({src: img.src, w: img.naturalWidth, h: img.naturalHeight}))
                """)
                if imgs:
                    best = max(imgs, key=lambda x: x['w'] * x['h'])
                    img_url = best['src']
                    log.info(f"[Gemini] Image trouvée à t={i*5+5}s ({best['w']}x{best['h']})")
                    break
                if i % 3 == 0:
                    log.info(f"[Gemini] t={i*5+5}s — attente image...")

            if not img_url:
                log.warning("[Gemini] Timeout: pas d'image générée")
                return False

            # Tenter bouton download natif
            download_selectors = [
                "button[aria-label*='download' i]",
                "button[aria-label*='télécharger' i]",
                "button[aria-label*='Télécharger' i]",
                "[data-test-id='download-button']",
                "message-actions button[aria-label*='load' i]",
            ]
            for sel in download_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible(timeout=2000):
                        async with page.expect_download(timeout=15000) as dl_info:
                            await el.click(timeout=3000)
                        dl = await dl_info.value
                        await dl.save_as(output_path)
                        log.info(f"[Gemini] Image sauvegardée via bouton download: {output_path}")
                        return True
                except Exception:
                    continue

            # Fallback: fetch via JavaScript
            try:
                img_data = await page.evaluate(f"""
                    async () => {{
                        const response = await fetch('{img_url}');
                        const buffer = await response.arrayBuffer();
                        const bytes = new Uint8Array(buffer);
                        let binary = '';
                        for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
                        return btoa(binary);
                    }}
                """)
                import base64 as _b64
                img_bytes = _b64.b64decode(img_data)
                with open(output_path, "wb") as f:
                    f.write(img_bytes)
                log.info(f"[Gemini] Image sauvegardée via fetch JS: {output_path}")
                return True
            except Exception as e:
                log.warning(f"[Gemini] Fetch JS échoué: {e}")

            # Fallback: screenshot page
            await page.screenshot(path=output_path, full_page=False)
            log.info(f"[Gemini] Image sauvegardée via screenshot (fallback): {output_path}")
            return True

        except Exception as e:
            log.warning(f"[Gemini] Exception: {e}")
            return False
        finally:
            await page.close()
            await browser.close()


# Variations de cadrage/détail pour les images dynamiques
_BG_VARIATIONS = [
    "Slightly wider angle, showing more of the room and architectural details.",
    "Slightly closer framing, emphasizing the main work surface and key objects.",
    "Same location but with subtle shift in ambient lighting — warmer tone.",
    "Same location, alternative angle — from the side, revealing more depth.",
]

N_DYNAMIC_BACKGROUNDS = int(os.getenv("N_DYNAMIC_BACKGROUNDS", "3"))


async def _gemini_generate_multiple(prompts: list[str], output_paths: list[str]) -> list[bool]:
    """
    Ouvre UN seul navigateur et génère plusieurs images Gemini en séquence.
    Retourne une liste de bool (succès/échec par image).
    """
    from playwright.async_api import async_playwright
    results = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=BASE_PROFILE_PATH,
            executable_path=CHROME_PATH_GEMINI,
            headless=False,
            slow_mo=200,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
            ignore_default_args=["--enable-automation"],
        )

        for i, (prompt, output_path) in enumerate(zip(prompts, output_paths)):
            log.info(f"[Gemini] Image {i+1}/{len(prompts)}...")
            page = await browser.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            try:
                ok = await _gemini_page_generate(page, prompt, output_path)
                results.append(ok)
            except Exception as e:
                log.warning(f"[Gemini] Image {i+1} exception: {e}")
                results.append(False)
            finally:
                await page.close()
            # Petite pause entre les générations
            if i < len(prompts) - 1:
                await asyncio.sleep(3)

        await browser.close()

    return results


async def _gemini_page_generate(page, prompt: str, output_path: str) -> bool:
    """Génère UNE image Gemini sur une page déjà ouverte."""
    try:
        await page.goto("https://gemini.google.com", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        for sel in ["button:has-text('Continuer')", "button:has-text('Continue')",
                    "button:has-text('Accepter')", "button:has-text('Accept')"]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click(timeout=3000)
                    await page.wait_for_timeout(800)
            except Exception:
                pass

        input_sel = None
        for sel in ["[contenteditable='true']", "textarea", "rich-textarea", ".ql-editor"]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    input_sel = sel
                    break
            except Exception:
                pass

        if not input_sel:
            return False

        el = page.locator(input_sel).first
        await el.click(timeout=5000)
        await page.wait_for_timeout(500)
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Delete")
        await page.keyboard.type(prompt, delay=15)
        await page.wait_for_timeout(500)
        await page.keyboard.press("Enter")

        img_url = None
        for i in range(18):
            await page.wait_for_timeout(5000)
            imgs = await page.evaluate("""
                () => [...document.querySelectorAll('img')]
                    .filter(img => {
                        const src = img.src || '';
                        return img.naturalWidth > 300 && img.naturalHeight > 300
                            && !src.includes('avatar') && !src.includes('favicon')
                            && !src.includes('logo') && !src.includes('icon')
                            && (src.includes('blob:') || src.includes('googleusercontent')
                                || src.includes('data:image'));
                    })
                    .map(img => ({src: img.src, w: img.naturalWidth, h: img.naturalHeight}))
            """)
            if imgs:
                best = max(imgs, key=lambda x: x['w'] * x['h'])
                img_url = best['src']
                break

        if not img_url:
            return False

        # Tenter bouton download
        download_selectors = [
            "button[aria-label*='download' i]",
            "button[aria-label*='télécharger' i]",
            "[data-test-id='download-button']",
            "message-actions button[aria-label*='load' i]",
        ]
        for sel in download_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible(timeout=2000):
                    async with page.expect_download(timeout=15000) as dl_info:
                        await el.click(timeout=3000)
                    dl = await dl_info.value
                    await dl.save_as(output_path)
                    return True
            except Exception:
                continue

        # Fallback fetch JS
        try:
            img_data = await page.evaluate(f"""
                async () => {{
                    const r = await fetch('{img_url}');
                    const buf = await r.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    let b = '';
                    for (let i = 0; i < bytes.length; i++) b += String.fromCharCode(bytes[i]);
                    return btoa(b);
                }}
            """)
            import base64 as _b64
            with open(output_path, "wb") as f:
                f.write(_b64.b64decode(img_data))
            return True
        except Exception:
            pass

        await page.screenshot(path=output_path, full_page=False)
        return True

    except Exception as e:
        log.warning(f"[Gemini] Page exception: {e}")
        return False


def generate_dynamic_backgrounds_with_gemini(script_text: str, n: int = N_DYNAMIC_BACKGROUNDS) -> list[str]:
    """
    Génère N images de fond légèrement différentes via Gemini en un seul navigateur.
    Retourne la liste des chemins générés (au moins 1 requis).

    Les images sont nommées : full_background_0.png, full_background_1.png, ...
    L'image 0 est aussi copiée comme full_background.png (compat).
    """
    prompt_text = build_background_prompt(script_text)

    # Extraire le contexte visuel condensé (lignes Location/Topic/etc.)
    context_lines = "\n".join(
        line for line in prompt_text.split("\n")
        if any(kw in line for kw in ["Location:", "Topic:", "Atmosphere:", "Key objects:"])
    )[:400]

    base_prompt = (
        "Generate a photorealistic background image (absolutely no people, no characters, no humans) "
        "for a TikTok vertical video. Clean realistic environment for adding animated characters later. "
        "Wide landscape format, no text, no watermark.\n\n"
        + context_lines
    )

    # Construire N prompts avec variations
    prompts = [base_prompt]
    for i in range(1, n):
        variation = _BG_VARIATIONS[(i - 1) % len(_BG_VARIATIONS)]
        prompts.append(f"{base_prompt}\n\nVariation: {variation}")

    output_paths = [
        os.path.join(IMAGES_DIR, f"full_background_{i}.png") for i in range(n)
    ]

    log.info(f"[Gemini] Génération de {n} fonds dynamiques...")
    results = asyncio.run(_gemini_generate_multiple(prompts, output_paths))

    generated = [p for p, ok in zip(output_paths, results) if ok and os.path.exists(p)]
    if not generated:
        raise RuntimeError("Gemini: aucune image de fond générée")

    # Copier l'image 0 comme full_background.png (compat existante)
    import shutil as _shutil
    _shutil.copy2(generated[0], os.path.join(IMAGES_DIR, "full_background.png"))
    log.info(f"[Gemini] {len(generated)}/{n} fonds générés: {[os.path.basename(p) for p in generated]}")
    return generated


def generate_image_with_gemini(script_text: str) -> str:
    """
    Point d'entrée simple : génère les fonds dynamiques et retourne le chemin du premier.
    """
    generated = generate_dynamic_backgrounds_with_gemini(script_text)
    return generated[0]


def generate_image_with_openai(script_text: str) -> str:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    output_image_path = os.path.join(IMAGES_DIR, "full_background.png")

    prompt_text = build_background_prompt(script_text)

    try:
        log.info("Generation image OpenAI...")
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
        log.info("Image generee : %s", output_image_path)
        return output_image_path
    except Exception as e:
        err_str = str(e)
        if "moderation" in err_str.lower() or "safety" in err_str.lower() or "rejected" in err_str.lower():
            log.error("Image bloquee par moderation OpenAI — video mise de cote. Raison: %s", e)
            raise ModerationRejectedError(str(e)) from e
        log.error("Generation image echouee : %s", e)
        raise


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


def _split_one_background(bg_path: str, idx: int) -> Tuple[str, str]:
    """Split une image de fond en paire gauche/droite 9:16."""
    img = Image.open(bg_path).convert("RGB")
    img = center_crop_to_ratio(img, 9 / 8)
    img = img.resize((1152, 1024), Image.Resampling.LANCZOS)

    left_img  = img.crop((0, 0, 576, 1024)).resize((540, 960), Image.Resampling.LANCZOS)
    right_img = img.crop((576, 0, 1152, 1024)).resize((540, 960), Image.Resampling.LANCZOS)

    left_path  = os.path.join(LEFT_IMG_DIR,  f"left_{idx}.png")
    right_path = os.path.join(RIGHT_IMG_DIR, f"right_{idx}.png")

    left_img.save(left_path,  format="PNG", optimize=True)
    right_img.save(right_path, format="PNG", optimize=True)
    return left_path, right_path


def split_background_to_tiktok_pairs() -> Tuple[str, str]:
    """Compat : split l'image principale (index 0)."""
    bg_path = os.path.join(IMAGES_DIR, "full_background.png")
    left, right = _split_one_background(bg_path, 0)
    log.info("Images 9:16 générées (gauche/droite) avec crop propre.")
    return left, right


def split_all_dynamic_backgrounds() -> list[Tuple[str, str]]:
    """
    Split toutes les images full_background_N.png trouvées dans IMAGES_DIR.
    Retourne la liste des paires (left_N.png, right_N.png).
    """
    pairs: list[Tuple[str, str]] = []
    idx = 0
    while True:
        bg_path = os.path.join(IMAGES_DIR, f"full_background_{idx}.png")
        if not os.path.exists(bg_path):
            break
        try:
            left, right = _split_one_background(bg_path, idx)
            pairs.append((left, right))
            idx += 1
        except Exception as e:
            log.warning(f"Split background {idx} échoué: {e}")
            break
    if not pairs:
        # Fallback sur full_background.png classique
        left, right = split_background_to_tiktok_pairs()
        pairs.append((left, right))
    log.info(f"[Background] {len(pairs)} paires gauche/droite générées")
    return pairs


def assign_backgrounds_to_segments(
    segments: List[Dict],
    pairs: list[Tuple[str, str]],
) -> Dict[str, int]:
    """
    Assigne un index de fond à chaque segment.
    Règle : on change de fond à chaque changement de personnage.
    Retourne un dict {speaker_label: bg_index}.
    """
    if len(pairs) <= 1:
        return {}  # 1 seul fond → comportement d'origine

    # Collecter les locuteurs dans l'ordre d'apparition (dédupliqués)
    speakers_ordered: List[str] = []
    seen: set = set()
    for seg in segments:
        label = seg.get("label", "")
        if label not in seen:
            seen.add(label)
            speakers_ordered.append(label)

    # Attribuer un fond différent à chaque locuteur en round-robin
    mapping: Dict[str, int] = {}
    for i, speaker in enumerate(speakers_ordered):
        mapping[speaker] = i % len(pairs)

    log.info(f"[Background] Mapping fonds dynamiques: {mapping}")
    return mapping


# =========================================================
# AFFECTATION PUPPETS
# =========================================================

LABEL_TO_PUPPET_CACHE: Dict[str, str] = {}


def _ask_matching_or_random(speaker_label: str, genre: str, pool: List[str]) -> str:
    """
    Choisit un puppet selon le mode de matching configuré dans matching_mode.json.
    Si mode=manual, pause le pipeline et attend la réponse du GUI (timeout 65s).
    """
    mode = "auto"
    try:
        if MATCHING_MODE_FILE.exists():
            mode = json.loads(MATCHING_MODE_FILE.read_text(encoding="utf-8")).get("mode", "auto")
    except Exception:
        pass

    if mode != "manual" or not pool:
        return random.choice(pool) if pool else "Default VQA.puppet-button"

    log.info(f"🎭 Matching manuel requis pour «{speaker_label}» ({genre}) — GUI en attente…")

    # Nettoyer toute réponse précédente
    try:
        MATCHING_RESPONSE_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    try:
        MATCHING_REQUEST_FILE.write_text(
            json.dumps({
                "label": speaker_label,
                "genre": genre,
                "available_puppets": pool,
                "timestamp": datetime.now().isoformat(),
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        deadline = time.time() + MATCHING_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                if MATCHING_RESPONSE_FILE.exists():
                    resp = json.loads(MATCHING_RESPONSE_FILE.read_text(encoding="utf-8"))
                    puppet = resp.get("puppet", "")
                    if puppet in pool:
                        log.info(f"🎭 Choix GUI : «{speaker_label}» → «{puppet.replace(' VQA.puppet-button', '')}»")
                        return puppet
            except Exception:
                pass

        log.warning(f"🎭 Timeout matching pour «{speaker_label}» — choix automatique.")
        return random.choice(pool) if pool else "Default VQA.puppet-button"
    finally:
        MATCHING_REQUEST_FILE.unlink(missing_ok=True)
        MATCHING_RESPONSE_FILE.unlink(missing_ok=True)


def _get_label_genre(speaker_label: str) -> str:
    """Retourne le genre d'un label sans déclencher de matching (logique pure)."""
    n = speaker_label.strip().lower()
    if n == "mr martin" or n.startswith("homme"):
        return "homme"
    if n.startswith("femme"):
        return "femme"
    return "homme"


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

    # Réutiliser le cache pour la cohérence dans un même pipeline
    if speaker_label in LABEL_TO_PUPPET_CACHE:
        return genre, LABEL_TO_PUPPET_CACHE[speaker_label]

    puppet = _ask_matching_or_random(speaker_label, genre, pool)
    LABEL_TO_PUPPET_CACHE[speaker_label] = puppet
    return genre, puppet


def _speaker_index_hint(label: str) -> int:
    m = re.search(r'(?:homme|femme)\s+(\d+)', label, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def automate_generation_videos(
    max_threads: int = 4,
    segments_txt_path: str | None = None,
    audio_segments_dir: str | None = None,
    bg_pairs: list[Tuple[str, str]] | None = None,
):
    segments_txt_path = segments_txt_path or ALIGNED_SEGMENTS_PATH
    audio_segments_dir = audio_segments_dir or AUDIO_SEGMENTS_DIR

    if not os.path.exists(segments_txt_path):
        log.error("Fichier segments introuvable : %s", segments_txt_path)
        return
    if not os.path.isdir(audio_segments_dir):
        log.error("Dossier audio introuvable : %s", audio_segments_dir)
        return

    lines = parse_segments_with_speakers(segments_txt_path)
    if not lines:
        log.error("Aucune ligne valide dans le fichier segments.")
        return

    mp3_files = list_audio_segments_sorted(audio_segments_dir)
    if not mp3_files:
        log.error("Aucun segment audio .mp3 trouvé.")
        return

    # Mapping fonds dynamiques (vide si 1 seul fond)
    bg_assignment: Dict[str, int] = {}
    if bg_pairs and len(bg_pairs) > 1:
        bg_assignment = assign_backgrounds_to_segments(lines, bg_pairs)

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
        log.warning(
            "Nb lignes texte (%d) ≠ nb mp3 (%d). "
            "On continue avec les segments qui ont un mp3 correspondant.",
            len(lines), len(mp3_files)
        )
        audit.warning(f"DÉCALAGE texte/mp3: {len(lines)} lignes vs {len(mp3_files)} mp3")

    tasks: List[Task] = []
    skipped_segments = []
    for info in lines:
        seg_idx = info["index"]
        mp3_path = mp3_by_index.get(seg_idx)
        if mp3_path is None:
            log.warning("Segment %d (%s): pas de mp3 trouvé, skip.", seg_idx, info['label'])
            skipped_segments.append(info)
            audit.warning(
                f"  SKIP seg={seg_idx} label={info['label']} "
                f"timing={info['start_s']:.3f}-{info['end_s']:.3f}s "
                f"texte={info['text'][:60]}"
            )
            continue
        label = info["label"]
        # Check skip AVANT le popup de matching (genre est déterministe, ne dépend pas du puppet)
        _genre_check = _get_label_genre(label)
        _out_fname = _safe_filename(f"{label} - {_genre_check} - {seg_idx}.mp4")
        _out_path = os.path.join(VIDEO_SEGMENTS_DIR, _out_fname)
        if os.path.exists(_out_path) and os.path.getsize(_out_path) > 0:
            log.info("Segment %d (%s) deja genere, skip.", seg_idx, label)
            skipped_segments.append(info)
            continue
        # Demander le puppet seulement si le segment est à générer
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

        # Fond dynamique pour ce segment
        bg_idx = bg_assignment.get(label, 0) if bg_assignment else 0
        left_path  = bg_pairs[bg_idx][0] if (bg_pairs and bg_idx < len(bg_pairs)) else None
        right_path = bg_pairs[bg_idx][1] if (bg_pairs and bg_idx < len(bg_pairs)) else None
        if bg_assignment and label in bg_assignment:
            audit.info(f"    FOND dynamique: bg_{bg_idx} → left={os.path.basename(left_path or '')}")

        tasks.append(
            Task(
                audio_path=mp3_path,
                nom=label,
                genre=genre,
                segment_id=str(seg_idx),
                intervenant_index=str(_speaker_index_hint(label)),
                personnage_id=puppet,
                image_left_path=left_path,
                image_right_path=right_path,
            )
        )

    audit.info(f"Tasks à générer: {len(tasks)} | Skipped: {len(skipped_segments)}")

    if not tasks:
        log.error("Aucun job à traiter.")
        return

    _nb_total_adobe = len(tasks)
    log.info("Lancement de %d segments (max %d pages paralleles, 1 seul navigateur)", _nb_total_adobe, max_threads)

    def _adobe_progress(done: int, total: int):
        log.info("Adobe: %d/%d segments generes", done, total)

    asyncio.run(run_pool(tasks, concurrency=max_threads, on_progress=_adobe_progress))

    # Audit post-génération: vérifier les vidéos produites
    video_files = [f for f in os.listdir(VIDEO_SEGMENTS_DIR) if f.lower().endswith(".mp4")] if os.path.isdir(VIDEO_SEGMENTS_DIR) else []
    nb_ok = len(video_files)
    nb_total = len(tasks)
    audit.info(f"Vidéos produites: {nb_ok} (attendues: {nb_total})")
    if nb_ok != nb_total:
        audit.warning(f"DÉCALAGE VIDÉO: {nb_ok} vidéos vs {nb_total} tasks")
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

    if nb_ok == nb_total:
        log.info("[OK] Adobe — %d/%d segments generes", nb_ok, nb_total)
    else:
        log.warning("[PARTIEL] Adobe — %d/%d segments generes (%d manquants)", nb_ok, nb_total, nb_total - nb_ok)


# =========================================================
# VÉRIFICATION TEXTE
# =========================================================

def get_transcription_file_with_verification(transcription_path: str) -> str | None:
    if not os.path.exists(transcription_path):
        log.error("Le fichier %s est introuvable.", transcription_path)
        return None

    with open(transcription_path, "r", encoding="utf-8") as f:
        content = f.read()

    log.info("--- Aperçu de la transcription (début) ---\n%s\n--- Fin de l'aperçu ---", content[:1500])
    return content


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
        log.info("Vidéo archivée dans : %s", dst)

    log.info("Archivage terminé : %s", archive_dir)


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

    log.info("Nettoyage des fichiers temporaires terminé.")


# =========================================================
# PIPELINE STATE (reprise après crash)
# =========================================================

def _load_pipeline_state() -> dict:
    try:
        p = Path(PIPELINE_STATE_FILE)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _save_pipeline_state(state: dict):
    try:
        Path(PIPELINE_STATE_FILE).write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"Impossible de sauvegarder pipeline_state.json: {e}")

def _step_done(state: dict, step: str, *check_files) -> bool:
    """True si l'étape est marquée done ET tous les fichiers de sortie existent."""
    if step not in state.get("done", []):
        return False
    return all(Path(f).exists() for f in check_files)

def _mark_done(state: dict, step: str):
    state.setdefault("done", []).append(step)
    _save_pipeline_state(state)

def _cleanup_pipeline_state():
    """Supprime Download.mp4 et pipeline_state.json en fin de pipeline."""
    for p in (Path(RAW_VIDEO_PATH), Path(PIPELINE_STATE_FILE)):
        try:
            p.unlink(missing_ok=True)
        except Exception as e:
            log.warning(f"Impossible de supprimer {p.name}: {e}")
    log.info("🗑️  Download.mp4 et pipeline_state.json supprimés.")


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

    # ── Détection reprise pipeline (crash précédent) ───────────────────────────
    state = _load_pipeline_state()
    resuming = bool(state.get("done"))
    if resuming:
        log.info(f"♻️  Reprise pipeline détectée — étapes déjà faites: {state['done']}")
    else:
        state = {"started_at": datetime.now().isoformat(), "done": []}
        _save_pipeline_state(state)

    # ── 0) Nettoyage initial (ignoré si reprise) ───────────────────────────────
    if resuming:
        log.info("⏭️  Étape 0 (Nettoyage) — ignorée (reprise en cours)")
    else:
        with time_step("0) Nettoyage initial"):
            delete_outputs()
        _mark_done(state, "nettoyage")

    # ── 1) Extraction audio ────────────────────────────────────────────────────
    if _step_done(state, "audio", FULL_AUDIO_PATH):
        log.info("⏭️  Étape 1 (Extraction audio) — ignorée")
    else:
        with time_step("1) Extraction audio depuis RAW_VIDEO"):
            extract_audio_from_video()
        _mark_done(state, "audio")

    # ── 2) Transcription + diarisation ────────────────────────────────────────
    _diarization_json = os.path.join(TRANSCRIPTS_DIR, "diarization_segments.json")
    if _step_done(state, "transcription", RAW_SEGMENTS_PATH, _diarization_json):
        log.info("⏭️  Étape 2 (Transcription) — ignorée")
    else:
        with time_step("2) Transcription + diarisation"):
            wait_for_internet(label="Transcription + diarisation")
            transcribe_segments_with_diarization(
                audio_path=FULL_AUDIO_PATH,
                output_dir=TRANSCRIPTS_DIR
            )
        _mark_done(state, "transcription")
        try:
            _n_segs = len(parse_segments_with_speakers(RAW_SEGMENTS_PATH))
            log.info("[OK] Transcription — %d segments detectes", _n_segs)
        except Exception:
            pass

    # ── 3) Vérification + réécriture intervenants ─────────────────────────────
    if _step_done(state, "speakers", REWRITTEN_SEGMENTS_PATH):
        log.info("⏭️  Étape 3 (Speakers) — ignorée")
    else:
        with time_step("3) Vérification + réécriture intervenants"):
            segments_text_content = get_transcription_file_with_verification(RAW_SEGMENTS_PATH)
            wait_for_internet(label="OpenAI texte verification")
            rewrite_transcript_with_intervenants_gpt(
                contenu_segments_brut=segments_text_content,
                dossier_sortie=TRANSCRIPTS_DIR,
                nom_fichier_sortie="transcription_segments_intervenants.txt",
            )
        _mark_done(state, "speakers")

    # ── 4) Réalignement strict des timings ────────────────────────────────────
    if _step_done(state, "realignment", ALIGNED_SEGMENTS_PATH):
        log.info("⏭️  Étape 4 (Réalignement) — ignorée")
    else:
        with time_step("4) Réalignement strict des timings"):
            build_aligned_rewritten_segments(
                raw_segments_path=RAW_SEGMENTS_PATH,
                rewritten_segments_path=REWRITTEN_SEGMENTS_PATH,
                output_path=ALIGNED_SEGMENTS_PATH
            )
        _mark_done(state, "realignment")

    # Variables pipeline utilisées par les étapes suivantes (toujours recalculées)
    raw_lines      = parse_segments_with_speakers(RAW_SEGMENTS_PATH)
    rewritten_lines = parse_segments_with_speakers(REWRITTEN_SEGMENTS_PATH)
    aligned_lines  = parse_segments_with_speakers(ALIGNED_SEGMENTS_PATH)
    audit.info("=== ÉTAPE 4: RÉALIGNEMENT ===")
    audit.info(f"Segments bruts: {len(raw_lines)} | Réécrits: {len(rewritten_lines)} | Alignés: {len(aligned_lines)}")
    if len(raw_lines) != len(aligned_lines):
        audit.warning(f"DÉCALAGE: bruts ({len(raw_lines)}) != alignés ({len(aligned_lines)})")
    for seg in aligned_lines:
        audit.debug(f"  seg_aligned[{seg['index']}] {seg['start_s']:.3f}-{seg['end_s']:.3f}s label={seg['label']}")

    # ── 5) Découpage MP3 ──────────────────────────────────────────────────────
    _manifest_path = os.path.join(TRANSCRIPTS_DIR, "audio_segments_manifest.json")
    if _step_done(state, "audio_cut", _manifest_path):
        log.info("⏭️  Étape 5 (Découpage audio) — ignorée")
    else:
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
                preferer_copie_flux=False,
            )
            mp3_produced = sorted(
                [f for f in os.listdir(AUDIO_SEGMENTS_DIR) if f.lower().endswith(".mp3")]
            )
            audit.info(f"MP3 produits: {len(mp3_produced)} (attendus: {len(aligned_lines)})")
            if len(mp3_produced) != len(aligned_lines):
                audit.warning(f"DÉCALAGE AUDIO: {len(mp3_produced)} mp3 vs {len(aligned_lines)} segments alignés")
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
        _mark_done(state, "audio_cut")

    # ── 6) Manifest de contrôle audio ─────────────────────────────────────────
    if _step_done(state, "manifest", _manifest_path):
        log.info("⏭️  Étape 6 (Manifest) — ignorée")
    else:
        with time_step("6) Manifest de contrôle audio"):
            write_audio_segments_manifest(
                aligned_segments_path=ALIGNED_SEGMENTS_PATH,
                audio_dir=AUDIO_SEGMENTS_DIR,
                output_path=_manifest_path,
            )
        _mark_done(state, "manifest")

    # ── 7) Génération image de fond + split 9:16 ──────────────────────────────
    _left_img  = os.path.join(IMAGES_DIR, "left",  "left_0.png")
    _right_img = os.path.join(IMAGES_DIR, "right", "right_0.png")
    if _step_done(state, "background", _left_img, _right_img):
        log.info("⏭️  Étape 7 (Background) — ignorée")
    else:
        with time_step("7) Génération images de fond dynamiques + split 9:16"):
            full_txt = get_transcription_file_with_verification(RAW_SEGMENTS_PATH)
            wait_for_internet(label="image generation")
            # Essaie Gemini (gratuit) → génère N fonds dynamiques
            try:
                generate_dynamic_backgrounds_with_gemini(full_txt, n=N_DYNAMIC_BACKGROUNDS)
                log.info(f"✅ {N_DYNAMIC_BACKGROUNDS} fonds dynamiques générés via Gemini")
            except Exception as e:
                log.warning(f"⚠️ Gemini fonds dynamiques échoué ({e}) → fallback OpenAI (1 seul fond)")
                generate_image_with_openai(full_txt)
            split_all_dynamic_backgrounds()
        _mark_done(state, "background")

    # ── Charger les paires de fonds dynamiques (pour l'étape 8) ──────────────
    _bg_pairs = split_all_dynamic_backgrounds() if os.path.exists(
        os.path.join(IMAGES_DIR, "full_background.png")
    ) else None

    # ── 8) Génération vidéos segments (Adobe) ─────────────────────────────────
    # Si l'assemblage final est déjà là → on saute 8 et 9 directement
    if _step_done(state, "assembly", VIDEO_FINALE_PATH):
        log.info("⏭️  Étapes 8+9 (Adobe + Assemblage) — ignorées (video_final.mp4 présente)")
    else:
        _has_segments = os.path.isdir(VIDEO_SEGMENTS_DIR) and any(
            f.endswith(".mp4") for f in os.listdir(VIDEO_SEGMENTS_DIR)
        )
        if _step_done(state, "animation") and _has_segments:
            log.info("⏭️  Étape 8 (Adobe) — ignorée (segments vidéo présents)")
        else:
            with time_step("8) Génération vidéos segments (Adobe) avec fonds dynamiques"):
                wait_for_internet(label="Adobe generation")
                _adobe_concurrency = int(os.getenv("ADOBE_CONCURRENCY", "8"))
                log.info("Adobe concurrency: %d onglets | fonds: %d",
                         _adobe_concurrency, len(_bg_pairs) if _bg_pairs else 1)
                automate_generation_videos(
                    max_threads=_adobe_concurrency,
                    segments_txt_path=ALIGNED_SEGMENTS_PATH,
                    audio_segments_dir=AUDIO_SEGMENTS_DIR,
                    bg_pairs=_bg_pairs,
                )
            _mark_done(state, "animation")

        # ── 9) Assemblage final ────────────────────────────────────────────────
        with time_step("9) Assemblage final (sur transcript aligné)"):
            audit.info("=== ÉTAPE 9: ASSEMBLAGE FINAL ===")
            vid_files = sorted([f for f in os.listdir(VIDEO_SEGMENTS_DIR) if f.lower().endswith(".mp4")]) if os.path.isdir(VIDEO_SEGMENTS_DIR) else []
            aligned_for_assembly = parse_segments_with_speakers(ALIGNED_SEGMENTS_PATH)
            audit.info(f"Vidéos disponibles: {len(vid_files)} | Segments transcript: {len(aligned_for_assembly)}")

            video_indices = set()
            for vf in vid_files:
                m = re.search(r'(\d+)(?=\.mp4$)', vf)
                if m:
                    video_indices.add(int(m.group(1)))

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
                audit.info(f"Durée totale vidéos brutes: {total_video_dur:.3f}s | Durée attendue: {total_expected:.3f}s")

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
                    size_mb = os.path.getsize(VIDEO_FINALE_PATH) / (1024 * 1024)
                    audit.info(f"VIDÉO FINALE: duree={final_dur:.3f}s audio={audio_dur:.3f}s drift={drift_final:+.3f}s taille={os.path.getsize(VIDEO_FINALE_PATH)}o")
                    if abs(drift_final) > 0.5:
                        audit.warning(f"DRIFT FINAL IMPORTANT: vidéo {drift_final:+.3f}s par rapport à l'audio")
                    log.info("[OK] Assemblage — %.0fs, %.1f MB", final_dur, size_mb)
                except Exception as e:
                    audit.error(f"PROBE VIDÉO FINALE ERREUR: {e}")
                    log.info("[OK] Assemblage — video_final.mp4 generee")
            else:
                audit.error("VIDÉO FINALE NON PRODUITE")
                log.warning("[ECHEC] Assemblage — video_final.mp4 non produite")
        _mark_done(state, "assembly")

    # ── 10) Upload TikTok ──────────────────────────────────────────────────────
    with time_step("10) Generation description + Upload TikTok"):
        caption = generate_tiktok_description(ALIGNED_SEGMENTS_PATH)

        if not _refresh_token_ok(sys.executable, LOG_DIR / "upload_tiktok.log"):
            log.warning("⚠️ Refresh pré-upload échoué — tentative quand même")

        final_mp4 = VIDEO_FINALE_PATH
        wait_for_internet(label="Post TikTok")

        ok, reason = upload_to_tiktok_with_retry(final_mp4, python_exe=sys.executable, caption=caption)

        if ok:
            log.info("✅ Upload TikTok OK -> archivage standard")
            with time_step("11) Archivage (post OK)"):
                archive_outputs()
            _cleanup_pipeline_state()
            dt_all = (time.perf_counter() - t_all) / 60
            log.info(f"🎉 Pipeline COMPLET terminé en {dt_all:.3f} minutes")
            return True, "posted"

        log.warning(f"⚠️ Upload TikTok échoué (reason={reason}) -> stockage dans pending_posts puis reprise.")
        stash_unposted_videos(
            base_dir=BASE_DIR,
            raw_video_path=RAW_VIDEO_PATH,
            final_video_path=VIDEO_FINALE_PATH,
            reason=reason
        )
        with time_step(f"11) Nettoyage après échec TikTok ({reason})"):
            delete_outputs()
        _cleanup_pipeline_state()
        return False, f"{reason}_stashed"


def main():
    """
    Traite exactement UNE vidéo (Download.mp4) et sort.
    La gestion de la file d'attente est faite par auto_scheduler.py.
    """
    try:
        done, status = run_pipeline_once()
        if done and status == "posted":
            log.info("✅ Pipeline terminé avec succès.")
            sys.exit(0)
        if status and status.endswith("_stashed"):
            log.info(f"📦 Vidéo stashée ({status}) — pipeline terminé, le scheduler reprend la main.")
            sys.exit(0)
        log.warning(f"Pipeline terminé avec status inattendu: {status}")
        sys.exit(0)

    except ModerationRejectedError:
        # Code 2 = erreur permanente, le scheduler met la vidéo de côté sans retry
        sys.exit(2)

    except openai.RateLimitError as e:
        msg = str(e)
        if "insufficient_quota" in msg:
            log.error("Quota OpenAI insuffisant. Arret du pipeline (le scheduler relancera plus tard).")
            sys.exit(1)
        log.exception("RateLimitError non geree.")
        sys.exit(1)

    except Exception:
        log.exception("Erreur non geree dans le pipeline, arret.")
        sys.exit(1)


if __name__ == "__main__":
    main()