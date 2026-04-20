"""
tiktok_account_manager.py
Gestion des comptes TikTok (illimité).

Stockage : config/tiktok_accounts.json
Avatars   : config/avatars/<account_id>.jpg

Format du fichier :
{
  "active_account_id": "acc_1",
  "accounts": {
    "acc_1": {
      "id": "acc_1",
      "open_id": "...",
      "username": "@martinspam001",
      "display_name": "Mr Martin",
      "avatar_url": "https://...",
      "avatar_local": "config/avatars/acc_1.jpg",
      "access_token": "act.xxx",
      "refresh_token": "rft.xxx",
      "tokens_file": "config/tiktok_tokens_acc_1.json",
      "upload_count": 0,
      "added_at": "2026-04-20T10:00:00"
    }
  }
}

Migration automatique : si le fichier n'existe pas mais que .env contient
TIKTOK_USER_ACCESS_TOKEN, le compte existant est importé comme "acc_1".
"""

import json
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ACCOUNTS_FILE = Path("config/tiktok_accounts.json")
AVATARS_DIR   = Path("config/avatars")

TIKTOK_USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/?fields=open_id,avatar_url,display_name,username"


# ─────────────────────────────────────────────
# Persistance
# ─────────────────────────────────────────────

def _load_store() -> dict:
    if ACCOUNTS_FILE.exists():
        try:
            return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"active_account_id": None, "accounts": {}}


def _save_store(store: dict) -> None:
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─────────────────────────────────────────────
# Migration automatique depuis .env (compte 1)
# ─────────────────────────────────────────────

def _migrate_from_env_if_needed() -> None:
    """Si aucun compte n'existe et que .env a un token, crée acc_1 automatiquement."""
    store = _load_store()
    if store["accounts"]:
        return  # déjà des comptes

    token = (os.getenv("TIKTOK_USER_ACCESS_TOKEN") or "").strip()
    refresh = (os.getenv("TIKTOK_USER_REFRESH_TOKEN") or "").strip()
    if not token:
        return  # rien à migrer

    acc = {
        "id":           "acc_1",
        "open_id":      "",
        "username":     "(compte 1)",
        "display_name": "Compte 1",
        "avatar_url":   "",
        "avatar_local": "",
        "access_token": token,
        "refresh_token": refresh,
        "tokens_file":  "config/tiktok_tokens.json",
        "upload_count": 0,
        "added_at":     datetime.now().isoformat(timespec="seconds"),
    }

    # Essayer d'enrichir avec le profil TikTok
    _fetch_and_update_profile(acc)

    store["accounts"]["acc_1"] = acc
    store["active_account_id"] = "acc_1"
    _save_store(store)


# ─────────────────────────────────────────────
# Profil TikTok
# ─────────────────────────────────────────────

def _fetch_and_update_profile(acc: dict) -> None:
    """Appelle l'API TikTok pour récupérer username, display_name et avatar."""
    token = acc.get("access_token", "")
    if not token:
        return
    try:
        import urllib.request as _req
        req = _req.Request(
            TIKTOK_USER_INFO_URL,
            headers={"Authorization": f"Bearer {token}"},
        )
        with _req.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        user = data.get("data", {}).get("user", {})
        if user.get("username"):
            acc["username"] = "@" + user["username"].lstrip("@")
        if user.get("display_name"):
            acc["display_name"] = user["display_name"]
        if user.get("open_id"):
            acc["open_id"] = user["open_id"]
        if user.get("avatar_url"):
            acc["avatar_url"] = user["avatar_url"]
            # Télécharger l'avatar
            local = download_avatar(user["avatar_url"], acc["id"])
            if local:
                acc["avatar_local"] = local
    except Exception:
        pass  # profil enrichi en best-effort


def download_avatar(avatar_url: str, account_id: str) -> str:
    """Télécharge l'avatar et retourne le chemin local."""
    if not avatar_url:
        return ""
    try:
        AVATARS_DIR.mkdir(parents=True, exist_ok=True)
        dest = AVATARS_DIR / f"{account_id}.jpg"
        import urllib.request as _req
        _req.urlretrieve(avatar_url, str(dest))
        return str(dest)
    except Exception:
        return ""


def refresh_profile(account_id: str) -> None:
    """Refetch le profil TikTok pour un compte (après refresh token par ex.)."""
    store = _load_store()
    acc = store["accounts"].get(account_id)
    if not acc:
        return
    _fetch_and_update_profile(acc)
    _save_store(store)


# ─────────────────────────────────────────────
# API publique
# ─────────────────────────────────────────────

def list_accounts() -> list[dict]:
    """Retourne la liste des comptes dans l'ordre d'ajout."""
    _migrate_from_env_if_needed()
    store = _load_store()
    return list(store["accounts"].values())


def get_active_account_id() -> str | None:
    _migrate_from_env_if_needed()
    store = _load_store()
    active = store.get("active_account_id")
    # Vérifier que le compte actif existe encore
    if active and active in store["accounts"]:
        return active
    # Fallback : premier compte disponible
    if store["accounts"]:
        first = next(iter(store["accounts"]))
        store["active_account_id"] = first
        _save_store(store)
        return first
    return None


def get_active_account() -> dict | None:
    acc_id = get_active_account_id()
    if not acc_id:
        return None
    store = _load_store()
    return store["accounts"].get(acc_id)


def set_active_account(account_id: str) -> None:
    store = _load_store()
    if account_id not in store["accounts"]:
        raise KeyError(f"Compte {account_id} introuvable")
    store["active_account_id"] = account_id
    _save_store(store)


def add_or_update_account(
    access_token: str,
    refresh_token: str = "",
    open_id: str = "",
    username: str = "",
    display_name: str = "",
    avatar_url: str = "",
    account_id: str | None = None,
) -> str:
    """
    Ajoute ou met à jour un compte.
    Retourne l'account_id utilisé.
    Si open_id est fourni, cherche si un compte avec ce open_id existe déjà.
    """
    store = _load_store()
    accounts = store["accounts"]

    # Chercher un compte existant par open_id
    existing_id = None
    if open_id:
        for aid, acc in accounts.items():
            if acc.get("open_id") == open_id:
                existing_id = aid
                break

    if existing_id:
        acc_id = existing_id
    elif account_id and account_id in accounts:
        acc_id = account_id
    else:
        # Générer un nouvel ID
        existing_nums = [
            int(k.replace("acc_", "")) for k in accounts if k.startswith("acc_")
            if k.replace("acc_", "").isdigit()
        ]
        next_num = max(existing_nums, default=0) + 1
        acc_id = f"acc_{next_num}"

    # Déterminer le fichier de tokens
    tokens_file = f"config/tiktok_tokens_{acc_id}.json"
    if acc_id == "acc_1":
        tokens_file = "config/tiktok_tokens.json"  # compat ancien

    # Écrire le fichier de tokens
    Path(tokens_file).parent.mkdir(parents=True, exist_ok=True)
    token_data = {
        "access_token":  access_token,
        "refresh_token": refresh_token,
    }
    Path(tokens_file).write_text(
        json.dumps(token_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Construire/mettre à jour l'entrée
    acc = accounts.get(acc_id, {})
    acc["id"]            = acc_id
    acc["open_id"]       = open_id or acc.get("open_id", "")
    acc["username"]      = username or acc.get("username", f"(compte {acc_id})")
    acc["display_name"]  = display_name or acc.get("display_name", acc["username"])
    acc["avatar_url"]    = avatar_url or acc.get("avatar_url", "")
    acc["avatar_local"]  = acc.get("avatar_local", "")
    acc["access_token"]  = access_token
    acc["refresh_token"] = refresh_token
    acc["tokens_file"]   = tokens_file
    acc["upload_count"]  = acc.get("upload_count", 0)
    acc["added_at"]      = acc.get("added_at", datetime.now().isoformat(timespec="seconds"))

    # Fetch profil TikTok si pas encore de username
    if not username and not acc.get("username", "").startswith("@"):
        _fetch_and_update_profile(acc)

    # Télécharger avatar si URL fournie et pas encore en local
    if acc["avatar_url"] and not acc["avatar_local"]:
        local = download_avatar(acc["avatar_url"], acc_id)
        if local:
            acc["avatar_local"] = local

    accounts[acc_id] = acc

    # Activer si c'est le seul compte
    if len(accounts) == 1:
        store["active_account_id"] = acc_id

    _save_store(store)
    return acc_id


def remove_account(account_id: str) -> None:
    store = _load_store()
    if account_id not in store["accounts"]:
        return
    del store["accounts"][account_id]
    # Si c'était le compte actif, basculer sur un autre
    if store.get("active_account_id") == account_id:
        store["active_account_id"] = next(iter(store["accounts"]), None)
    _save_store(store)
    # Supprimer l'avatar local
    avatar = AVATARS_DIR / f"{account_id}.jpg"
    try:
        avatar.unlink(missing_ok=True)
    except Exception:
        pass


def get_access_token(account_id: str | None = None) -> str:
    """Retourne l'access_token du compte (ou du compte actif si None)."""
    _migrate_from_env_if_needed()
    store = _load_store()
    if account_id is None:
        account_id = store.get("active_account_id")
    if not account_id:
        # Dernier fallback : variable d'env
        return (os.getenv("TIKTOK_USER_ACCESS_TOKEN") or "").strip()
    acc = store["accounts"].get(account_id, {})
    return acc.get("access_token", "").strip()


def get_refresh_token(account_id: str | None = None) -> str:
    _migrate_from_env_if_needed()
    store = _load_store()
    if account_id is None:
        account_id = store.get("active_account_id")
    if not account_id:
        return (os.getenv("TIKTOK_USER_REFRESH_TOKEN") or "").strip()
    acc = store["accounts"].get(account_id, {})
    return acc.get("refresh_token", "").strip()


def update_tokens(account_id: str, access_token: str, refresh_token: str) -> None:
    """Met à jour les tokens d'un compte après refresh."""
    store = _load_store()
    acc = store["accounts"].get(account_id)
    if not acc:
        return
    acc["access_token"]  = access_token
    acc["refresh_token"] = refresh_token
    # Mettre à jour le fichier de tokens
    tokens_file = Path(acc.get("tokens_file", f"config/tiktok_tokens_{account_id}.json"))
    try:
        existing = {}
        if tokens_file.exists():
            existing = json.loads(tokens_file.read_text(encoding="utf-8"))
        existing["access_token"]  = access_token
        existing["refresh_token"] = refresh_token
        tokens_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    _save_store(store)


def mark_account_used(account_id: str) -> None:
    """Incrémente le compteur d'uploads."""
    store = _load_store()
    acc = store["accounts"].get(account_id)
    if acc:
        acc["upload_count"] = acc.get("upload_count", 0) + 1
        _save_store(store)


def get_account_label(account_id: str) -> str:
    store = _load_store()
    acc = store["accounts"].get(account_id, {})
    return acc.get("username") or acc.get("display_name") or account_id


def get_rotation_status() -> dict:
    """Résumé de l'état pour les logs."""
    active_id = get_active_account_id()
    accounts  = list_accounts()
    return {
        "active":   active_id,
        "accounts": [f"{a['id']}:{a.get('username', '?')}" for a in accounts],
    }
