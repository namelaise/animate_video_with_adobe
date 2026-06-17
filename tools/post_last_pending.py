"""
post_last_pending.py — Génère une description et poste la dernière vidéo
dans pending_posts/ via Playwright (bypass API TikTok).

Logique :
  1. Trouve le dossier pending_posts/Video_N le plus récent (ou indiqué via --dir)
  2. Si caption.txt existe → on l'utilise
     sinon : ffmpeg extrait l'audio → OpenAI Whisper → GPT pour la description
            → sauvegarde dans caption.txt
  3. Appelle pipeline/post_tiktok_playwright.py sur video_final.mp4

Usage :
  python tools/post_last_pending.py
  python tools/post_last_pending.py --dir pending_posts/Video_16
  python tools/post_last_pending.py --no-regen      # n'écrase pas caption.txt existant
  python tools/post_last_pending.py --privacy SELF_ONLY --dry-run
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

PENDING_ROOT = BASE_DIR / "pending_posts"
PLAYWRIGHT_SCRIPT = BASE_DIR / "pipeline" / "post_tiktok_playwright.py"


SYSTEM_PROMPT = (
    "Tu es un community manager TikTok spécialisé dans les vidéos de canulars téléphoniques. "
    "À partir de la transcription du dialogue, génère une description TikTok COURTE : "
    "- Maximum 2 phrases qui résument la situation de façon drôle et accrocheuse. "
    "- Ajoute 1 à 3 hashtags en rapport avec le thème de la vidéo. "
    "- Termine TOUJOURS par #mrmartin #canular "
    "- Pas de guillemets autour de la réponse. "
    "- Pas d'emoji sauf si ça apporte vraiment quelque chose. "
    "- Réponds UNIQUEMENT avec la description, sans introduction."
)


def find_latest_pending(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = BASE_DIR / p
        if not p.exists():
            raise SystemExit(f"[ERR] Dossier introuvable : {p}")
        return p
    if not PENDING_ROOT.exists():
        raise SystemExit(f"[ERR] {PENDING_ROOT} n'existe pas.")
    dirs = [d for d in PENDING_ROOT.iterdir()
            if d.is_dir() and re.match(r"^Video_\d+$", d.name)]
    if not dirs:
        raise SystemExit(f"[ERR] Aucune vidéo dans {PENDING_ROOT}")
    dirs.sort(key=lambda d: int(d.name.split("_")[1]), reverse=True)
    return dirs[0]


def extract_audio(video: Path) -> Path:
    out = Path(tempfile.gettempdir()) / f"{video.stem}_caption.mp3"
    cmd = ["ffmpeg", "-y", "-i", str(video),
           "-vn", "-acodec", "libmp3lame", "-ab", "64k", "-ar", "16000",
           str(out)]
    print(f"[INFO] Extraction audio → {out.name}", flush=True)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not out.exists():
        raise SystemExit(f"[ERR] ffmpeg a échoué :\n{res.stderr[-500:]}")
    return out


def transcribe_with_whisper(audio: Path) -> str:
    client = OpenAI()
    print(f"[INFO] Transcription Whisper (taille audio {audio.stat().st_size // 1024} KB)…", flush=True)
    with audio.open("rb") as f:
        resp = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="text",
            language="fr",
        )
    text = (resp if isinstance(resp, str) else str(resp)).strip()
    print(f"[INFO] Transcription : {len(text)} caractères", flush=True)
    return text


def generate_description(transcript: str) -> str:
    client = OpenAI()
    snippet = transcript[:2500]
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": snippet},
        ],
        max_tokens=150,
        temperature=0.8,
    )
    desc = (resp.choices[0].message.content or "").strip().strip('"').strip("'")
    if "#mrmartin" not in desc.lower():
        desc += " #mrmartin"
    if "#canular" not in desc.lower():
        desc += " #canular"
    return desc


def build_caption(video_dir: Path, regen: bool) -> str:
    caption_file = video_dir / "caption.txt"
    if caption_file.exists() and not regen:
        cap = caption_file.read_text(encoding="utf-8").strip()
        if cap:
            print(f"[INFO] caption.txt existant utilisé : {cap[:80]}…", flush=True)
            return cap

    source_video = video_dir / "Download.mp4"
    if not source_video.exists():
        source_video = video_dir / "video_final.mp4"
    if not source_video.exists():
        raise SystemExit(f"[ERR] Aucune vidéo source dans {video_dir}")

    audio = extract_audio(source_video)
    try:
        transcript = transcribe_with_whisper(audio)
    finally:
        try:
            audio.unlink(missing_ok=True)
        except Exception:
            pass

    desc = generate_description(transcript)
    caption_file.write_text(desc, encoding="utf-8")
    print(f"[OK] Description générée → {caption_file}", flush=True)
    print(f"[OK] {desc}", flush=True)
    return desc


def run_playwright_post(video: Path, caption: str, privacy: str,
                        allow_comment: bool, allow_duet: bool, allow_stitch: bool,
                        account_id: str | None, headless: bool) -> int:
    cmd = [
        sys.executable, str(PLAYWRIGHT_SCRIPT),
        "--video", str(video),
        "--caption", caption,
        "--privacy", privacy,
    ]
    if account_id:
        cmd += ["--account-id", account_id]
    if allow_comment:
        cmd.append("--allow-comment")
    if allow_duet:
        cmd.append("--allow-duet")
    if allow_stitch:
        cmd.append("--allow-stitch")
    if headless:
        cmd.append("--headless")

    print(f"[INFO] Lancement Playwright : {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd)
    return proc.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None,
                    help="Dossier pending_posts/Video_N spécifique (défaut : le plus récent)")
    ap.add_argument("--no-regen", action="store_true",
                    help="Réutilise caption.txt s'il existe (défaut). "
                         "Sans ce flag, on régénère si vide ou absent uniquement.")
    ap.add_argument("--force-regen", action="store_true",
                    help="Force la régénération même si caption.txt existe.")
    ap.add_argument("--privacy", default=os.getenv("TIKTOK_AUTO_PUBLISH_PRIVACY", "PUBLIC_TO_EVERYONE"),
                    choices=["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"])
    ap.add_argument("--account-id", dest="account_id", default=None)
    ap.add_argument("--allow-comment", dest="allow_comment", action="store_true", default=
                    os.getenv("TIKTOK_AUTO_PUBLISH_ALLOW_COMMENT", "1").strip() == "1")
    ap.add_argument("--allow-duet", dest="allow_duet", action="store_true", default=
                    os.getenv("TIKTOK_AUTO_PUBLISH_ALLOW_DUET", "0").strip() == "1")
    ap.add_argument("--allow-stitch", dest="allow_stitch", action="store_true", default=
                    os.getenv("TIKTOK_AUTO_PUBLISH_ALLOW_STITCH", "0").strip() == "1")
    ap.add_argument("--headless", action="store_true", default=
                    os.getenv("TIKTOK_PLAYWRIGHT_HEADLESS", "0").strip() == "1")
    ap.add_argument("--dry-run", action="store_true",
                    help="Génère seulement la description sans poster.")
    args = ap.parse_args()

    video_dir = find_latest_pending(args.dir)
    print(f"[INFO] Dossier sélectionné : {video_dir.name}", flush=True)

    final_video = video_dir / "video_final.mp4"
    if not final_video.exists():
        raise SystemExit(f"[ERR] video_final.mp4 manquant dans {video_dir}")

    caption = build_caption(video_dir, regen=args.force_regen)

    if args.dry_run:
        print("[OK] --dry-run : caption générée, post ignoré.", flush=True)
        return

    rc = run_playwright_post(
        final_video, caption, args.privacy,
        args.allow_comment, args.allow_duet, args.allow_stitch,
        args.account_id, args.headless,
    )
    if rc == 0:
        print("[OK] Post publié.", flush=True)
    elif rc == 2:
        print("[ERR] Session TikTok absente — lance tools/login_tiktok.py.", flush=True)
    elif rc == 3:
        print("[ERR] spam_risk détecté.", flush=True)
    else:
        print(f"[ERR] Post échoué (rc={rc}).", flush=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
