# === auth_tiktok_refresh.py ===
# Rafraîchit l'access_token TikTok pour UN compte spécifique.
#
# Usage :
#   python auth_tiktok_refresh.py               (compte actif)
#   python auth_tiktok_refresh.py --account acc_2
#
# Le compte est identifié dans config/tiktok_accounts.json

import argparse
import os
import json
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ── CLI args ──────────────────────────────────────────────────────────────────
_ap = argparse.ArgumentParser(add_help=False)
_ap.add_argument("--account", type=str, default=None,
                 help="ID du compte à rafraîchir (ex: acc_1). Défaut: compte actif.")
_args, _ = _ap.parse_known_args()

# Import account manager (disponible dans le même dossier)
from tiktok_account_manager import (
    get_active_account_id,
    get_refresh_token,
    update_tokens,
    list_accounts,
)

ACCOUNT_ID: str = _args.account or get_active_account_id() or "acc_1"

# Paramètres TikTok
CLIENT_KEY:    str = (os.getenv("TIKTOK_CLIENT_KEY") or "").strip()
CLIENT_SECRET: str = (os.getenv("TIKTOK_CLIENT_SECRET") or "").strip()
TOKEN_URL:     str = "https://open.tiktokapis.com/v2/oauth/token/"
ENV_FILE:      Path = Path(".env")


# ─────────────────────────────────────────────
# Utils
# ─────────────────────────────────────────────

def log(msg: str) -> None:
    print("[INFO]", msg)

def err(msg: str) -> None:
    print("[ERR]", msg)

def assert_env() -> None:
    if not CLIENT_KEY or not CLIENT_SECRET:
        raise SystemExit("[ERR] TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET manquants dans .env")

def _update_dotenv(var: str, val: str) -> None:
    """Met à jour ou ajoute une variable dans .env (rétro-compat compte actif)."""
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
    prefix = var + "="
    updated = False
    out = []
    for line in lines:
        if line.strip().startswith(prefix):
            out.append(f"{var}={val}")
            updated = True
        else:
            out.append(line)
    if not updated:
        out.append(f"{var}={val}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")

def _enrich_expiry(token_response: dict) -> dict:
    now = int(time.time())
    out = dict(token_response)
    try:
        if "expires_in" in out and isinstance(out["expires_in"], (int, float)):
            out["expires_at"] = now + int(out["expires_in"])
        if "refresh_expires_in" in out and isinstance(out["refresh_expires_in"], (int, float)):
            out["refresh_expires_at"] = now + int(out["refresh_expires_in"])
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────
# Refresh
# ─────────────────────────────────────────────

def refresh_access_token(account_id: str) -> None:
    assert_env()

    refresh_token = get_refresh_token(account_id)
    if not refresh_token:
        raise SystemExit(
            f"[ERR] Aucun refresh_token pour le compte {account_id}. "
            "Lance auth_tiktok_token_manager.py pour re-authentifier."
        )

    log(f"Refresh du compte {account_id}…")
    payload = {
        "client_key":    CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
    }

    try:
        resp = requests.post(
            TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    except requests.RequestException as e:
        raise SystemExit(f"[ERR] Requête HTTP échouée: {e}")

    if resp.status_code != 200:
        raise SystemExit(f"[ERR] Refresh HTTP {resp.status_code}: {resp.text[:600]}")

    token_resp = _enrich_expiry(resp.json())
    new_access  = (token_resp.get("access_token") or "").strip()
    new_refresh = (token_resp.get("refresh_token") or refresh_token).strip()

    if not new_access:
        err("Réponse sans access_token :")
        print(json.dumps(token_resp, indent=2, ensure_ascii=False))
        raise SystemExit(1)

    # Sauvegarder dans le gestionnaire de comptes
    update_tokens(account_id, new_access, new_refresh)
    log(f"✅ Tokens mis à jour pour {account_id}")

    # Rétro-compat .env si c'est le compte actif
    active_id = get_active_account_id()
    if account_id == active_id:
        _update_dotenv("TIKTOK_USER_ACCESS_TOKEN", new_access)
        _update_dotenv("TIKTOK_USER_REFRESH_TOKEN", new_refresh)
        log(f"✅ .env mis à jour (compte actif {account_id})")

    exp_in = token_resp.get("expires_in")
    exp_at = token_resp.get("expires_at")
    if exp_in and exp_at:
        log(f"Expire dans ~{int(exp_in)}s "
            f"(vers {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(exp_at)))})")


if __name__ == "__main__":
    log(f"Compte cible : {ACCOUNT_ID}")
    refresh_access_token(ACCOUNT_ID)
