# tiktok_publish_helpers.py
# Helpers Python utilisés par le GUI pour peupler le dialog "Composer le post"
# (récupération creator_info, mesure durée vidéo).
# Aucune écriture, aucun side-effect : appels lecture seule.

import subprocess
from pathlib import Path
from typing import Optional

import requests

API_CREATOR_INFO = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"


class CreatorInfoError(Exception):
    pass


def query_creator_info(access_token: str, timeout: int = 30) -> dict:
    """
    Appelle /v2/post/publish/creator_info/query/ et retourne le bloc `data`.
    Lève CreatorInfoError en cas d'échec.
    """
    if not access_token:
        raise CreatorInfoError("Access token vide.")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    try:
        r = requests.post(API_CREATOR_INFO, headers=headers, json={}, timeout=timeout)
    except requests.RequestException as e:
        raise CreatorInfoError(f"Erreur réseau: {e}") from e

    if r.status_code != 200:
        raise CreatorInfoError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        payload = r.json()
    except Exception as e:
        raise CreatorInfoError(f"Réponse non-JSON: {e}") from e

    err = payload.get("error") or {}
    if err.get("code") and err.get("code") != "ok":
        raise CreatorInfoError(f"{err.get('code')}: {err.get('message','')}")
    return payload.get("data") or {}


def get_video_duration(path: Path | str) -> Optional[float]:
    """Retourne la durée d'une vidéo en secondes via ffprobe, ou None si indisponible."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, text=True, timeout=30,
        )
        return float((result.stdout or "0").strip())
    except Exception:
        return None


def get_video_dimensions(path: Path | str) -> Optional[tuple[int, int]]:
    """Retourne (width, height) via ffprobe, ou None."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", str(p)],
            capture_output=True, text=True, timeout=30,
        )
        wh = (result.stdout or "").strip().split("x")
        if len(wh) == 2:
            return int(wh[0]), int(wh[1])
    except Exception:
        return None
    return None


def extract_thumbnail(video_path: Path | str, out_path: Path | str, timestamp_s: float = 1.0) -> bool:
    """Génère une miniature JPEG via ffmpeg. Retourne True si OK."""
    v = Path(video_path)
    o = Path(out_path)
    if not v.exists():
        return False
    o.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(timestamp_s), "-i", str(v),
             "-frames:v", "1", "-q:v", "3", str(o)],
            capture_output=True, timeout=30,
        )
        return result.returncode == 0 and o.exists()
    except Exception:
        return False
