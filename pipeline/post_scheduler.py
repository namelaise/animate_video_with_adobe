# pipeline/post_scheduler.py
# Gestion du planning de publications programmées.
# Persisté dans state/scheduled_posts.json

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

BASE_DIR      = Path(__file__).resolve().parent.parent
STATE_DIR     = BASE_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SCHEDULED_FILE = STATE_DIR / "scheduled_posts.json"


def _read() -> list[dict]:
    try:
        if SCHEDULED_FILE.exists():
            return json.loads(SCHEDULED_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _write(posts: list[dict]):
    fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(posts, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SCHEDULED_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def add(video_path: str, scheduled_at: str, title: str = "") -> dict:
    """
    Ajoute un post programmé.
    scheduled_at : ISO datetime string, ex. "2026-05-10T18:00:00"
    Retourne l'entrée créée.
    """
    posts = _read()
    entry = {
        "id":           f"sched_{int(datetime.now().timestamp())}",
        "video_path":   str(video_path),
        "scheduled_at": scheduled_at,
        "title":        title,
        "status":       "pending",
        "created_at":   datetime.now().isoformat(timespec="seconds"),
    }
    posts.append(entry)
    _write(posts)
    return entry


def remove(post_id: str) -> bool:
    posts = _read()
    before = len(posts)
    posts = [p for p in posts if p.get("id") != post_id]
    if len(posts) < before:
        _write(posts)
        return True
    return False


def get_all() -> list[dict]:
    return _read()


def get_due(now: datetime | None = None) -> list[dict]:
    """Retourne les posts dont scheduled_at ≤ now et status == pending."""
    if now is None:
        now = datetime.now()
    result = []
    for p in _read():
        if p.get("status") != "pending":
            continue
        try:
            if datetime.fromisoformat(p["scheduled_at"]) <= now:
                result.append(p)
        except Exception:
            pass
    return result


def mark_posted(post_id: str):
    posts = _read()
    for p in posts:
        if p.get("id") == post_id:
            p["status"]    = "posted"
            p["posted_at"] = datetime.now().isoformat(timespec="seconds")
            break
    _write(posts)


def mark_failed(post_id: str, reason: str = ""):
    posts = _read()
    for p in posts:
        if p.get("id") == post_id:
            p["status"]          = "failed"
            p["failure_reason"]  = reason
            p["failed_at"]       = datetime.now().isoformat(timespec="seconds")
            break
    _write(posts)


def cancel(post_id: str) -> bool:
    posts = _read()
    for p in posts:
        if p.get("id") == post_id and p.get("status") == "pending":
            p["status"] = "cancelled"
            _write(posts)
            return True
    return False
