"""
tiktok_account_manager.py
Gestion de la rotation entre plusieurs comptes TikTok.

Variables .env requises:
  Compte 1 (existant) :
    TIKTOK_USER_ACCESS_TOKEN
    TIKTOK_USER_REFRESH_TOKEN

  Compte 2 (à ajouter) :
    TIKTOK_ACCOUNT2_ACCESS_TOKEN
    TIKTOK_ACCOUNT2_REFRESH_TOKEN

Fichiers de config:
  config/tiktok_tokens.json          — compte 1 (existant)
  config/tiktok_tokens_account2.json — compte 2 (nouveau)
  config/account_rotation.json       — état de la rotation (créé automatiquement)

Pour authentifier le compte 2 manuellement :
  Copier auth_tiktok_token_manager.py → l'exécuter avec --account 2
  (ou renseigner TIKTOK_ACCOUNT2_ACCESS_TOKEN et TIKTOK_ACCOUNT2_REFRESH_TOKEN dans .env)
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROTATION_FILE = Path("config/account_rotation.json")

# Définition de tous les comptes disponibles
ACCOUNTS: dict[str, dict] = {
    "1": {
        "env_access_token":  "TIKTOK_USER_ACCESS_TOKEN",
        "env_refresh_token": "TIKTOK_USER_REFRESH_TOKEN",
        "tokens_file":       Path("config/tiktok_tokens.json"),
        "label":             "Compte 1",
    },
    "2": {
        "env_access_token":  "TIKTOK_ACCOUNT2_ACCESS_TOKEN",
        "env_refresh_token": "TIKTOK_ACCOUNT2_REFRESH_TOKEN",
        "tokens_file":       Path("config/tiktok_tokens_account2.json"),
        "label":             "Compte 2",
    },
}


# ─────────────────────────────────────────────
# Persistance de l'état de rotation
# ─────────────────────────────────────────────

def _load_rotation() -> dict:
    try:
        if ROTATION_FILE.exists():
            return json.loads(ROTATION_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"last_used": "1", "counts": {"1": 0, "2": 0}}


def _save_rotation(state: dict) -> None:
    ROTATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROTATION_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─────────────────────────────────────────────
# API publique
# ─────────────────────────────────────────────

def get_available_accounts() -> list[str]:
    """Retourne les IDs des comptes ayant un access_token non vide dans .env."""
    return [
        acc_id
        for acc_id, info in ACCOUNTS.items()
        if (os.getenv(info["env_access_token"]) or "").strip()
    ]


def get_next_account() -> str:
    """
    Retourne l'ID du prochain compte à utiliser.
    Stratégie : compte le moins utilisé parmi ceux disponibles.
    Si un seul compte est disponible, retourne celui-là.
    """
    available = get_available_accounts()
    if not available:
        return "1"  # fallback même sans token configuré
    if len(available) == 1:
        return available[0]

    state = _load_rotation()
    counts = state.get("counts", {})
    return min(available, key=lambda acc_id: counts.get(acc_id, 0))


def mark_account_used(account_id: str) -> None:
    """Incrémente le compteur d'utilisation du compte."""
    state = _load_rotation()
    counts = state.setdefault("counts", {})
    counts[account_id] = counts.get(account_id, 0) + 1
    state["last_used"] = account_id
    _save_rotation(state)


def get_access_token(account_id: str) -> str:
    """Retourne l'access_token du compte (depuis .env)."""
    info = ACCOUNTS.get(account_id, ACCOUNTS["1"])
    return (os.getenv(info["env_access_token"]) or "").strip()


def get_tokens_file(account_id: str) -> Path:
    """Retourne le chemin du fichier de tokens du compte."""
    return ACCOUNTS.get(account_id, ACCOUNTS["1"])["tokens_file"]


def get_account_label(account_id: str) -> str:
    return ACCOUNTS.get(account_id, ACCOUNTS["1"])["label"]


def get_rotation_status() -> dict:
    """Retourne un résumé lisible de l'état de rotation (pour les logs)."""
    state = _load_rotation()
    available = get_available_accounts()
    return {
        "available": available,
        "counts": state.get("counts", {}),
        "last_used": state.get("last_used", "?"),
        "next": get_next_account(),
    }
