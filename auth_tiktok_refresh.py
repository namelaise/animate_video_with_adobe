# === auth_tiktok_refresh.py ===
# Rafraîchit l'access_token TikTok (Open API v2) à partir d'un refresh_token.
# - Lit config/tiktok_tokens.json (ou l'ENV) pour trouver le refresh_token
# - Appelle /v2/oauth/token (grant_type=refresh_token)
# - Sauvegarde la réponse dans config/tiktok_tokens.json
# - Met à jour .env avec TIKTOK_USER_ACCESS_TOKEN (+ rafraîchit TIKTOK_USER_REFRESH_TOKEN si fourni)

import os
import json
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ------------------------------
# Configuration
# ------------------------------
CLIENT_KEY: str = (os.getenv("TIKTOK_CLIENT_KEY") or "").strip()
CLIENT_SECRET: str = (os.getenv("TIKTOK_CLIENT_SECRET") or "").strip()

TOKEN_URL: str = "https://open.tiktokapis.com/v2/oauth/token/"

TOKENS_JSON: Path = Path("config/tiktok_tokens.json")   # même fichier que l'init
ENV_FILE: Path = Path(".env")

# Clés .env à maintenir
ENV_ACCESS_TOKEN_KEY = "TIKTOK_USER_ACCESS_TOKEN"
ENV_REFRESH_TOKEN_KEY = "TIKTOK_USER_REFRESH_TOKEN"     # pratique si tu veux aussi le stocker en .env


# ------------------------------
# Utils
# ------------------------------
def log(msg: str) -> None:
    print("[INFO]", msg)

def err(msg: str) -> None:
    print("[ERR]", msg)

def assert_env() -> None:
    if not CLIENT_KEY or not CLIENT_SECRET:
        raise SystemExit("[ERR] TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET manquants dans .env")

def read_tokens_file() -> dict:
    if not TOKENS_JSON.exists():
        err(f"{TOKENS_JSON} introuvable. Lance d'abord l'auth initiale (auth_tiktok_firefox.py).")
        return {}
    try:
        return json.loads(TOKENS_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        err(f"Lecture JSON échouée ({TOKENS_JSON}): {e}")
        return {}

def write_tokens_file(tokens: dict) -> None:
    TOKENS_JSON.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_JSON.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Tokens sauvegardés -> {TOKENS_JSON}")

def update_env_file(var: str, val: str) -> None:
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
    log(f"{var} mis à jour dans .env")

def choose_refresh_token(tokens_from_file: dict) -> str:
    """
    Ordre de priorité pour trouver le refresh_token :
      1) config/tiktok_tokens.json["refresh_token"]
      2) .env -> TIKTOK_USER_REFRESH_TOKEN
      3) .env -> TIKTOK_REFRESH_TOKEN (alias possible)
    """
    rt = (tokens_from_file.get("refresh_token") or "").strip()
    if rt:
        return rt
    rt = (os.getenv(ENV_REFRESH_TOKEN_KEY) or "").strip()
    if rt:
        return rt
    rt = (os.getenv("TIKTOK_REFRESH_TOKEN") or "").strip()
    return rt

def enrich_with_expiry_timestamps(token_response: dict) -> dict:
    """
    Ajoute des champs 'expires_at' et 'refresh_expires_at' (timestamps unix approx.) si 'expires_in'
    ou 'refresh_expires_in' sont présents.
    """
    now = int(time.time())
    out = dict(token_response)  # shallow copy
    try:
        if "expires_in" in out and isinstance(out["expires_in"], (int, float)):
            out["expires_at"] = now + int(out["expires_in"])
        if "refresh_expires_in" in out and isinstance(out["refresh_expires_in"], (int, float)):
            out["refresh_expires_at"] = now + int(out["refresh_expires_in"])
    except Exception:
        pass
    return out


# ------------------------------
# Refresh flow
# ------------------------------
def refresh_access_token() -> None:
    assert_env()

    tokens_before = read_tokens_file()
    refresh_token = choose_refresh_token(tokens_before)

    if not refresh_token:
        raise SystemExit("[ERR] Aucun refresh_token trouvé. Relance l'auth initiale (auth_tiktok_firefox.py).")

    log("Demande de refresh…")
    payload = {
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
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

    token_resp = resp.json()
    access_token = (token_resp.get("access_token") or "").strip()
    new_refresh_token = (token_resp.get("refresh_token") or "").strip()

    if not access_token:
        err("Réponse de refresh sans access_token :")
        print(json.dumps(token_resp, indent=2, ensure_ascii=False))
        raise SystemExit(1)

    # Si TikTok renvoie un nouveau refresh_token, on l'utilise ; sinon on conserve l'ancien
    if not new_refresh_token:
        new_refresh_token = refresh_token

    # Ajoute des timestamps utiles
    token_resp = enrich_with_expiry_timestamps(token_resp)

    # Persistance fichier
    write_tokens_file(token_resp)

    # Met à jour .env
    update_env_file(ENV_ACCESS_TOKEN_KEY, access_token)
    update_env_file(ENV_REFRESH_TOKEN_KEY, new_refresh_token)

    # Feedback
    exp_in = token_resp.get("expires_in")
    exp_at = token_resp.get("expires_at")
    if exp_in and exp_at:
        log(f"✅ Nouveau access_token OK (expire dans ~{int(exp_in)}s, vers {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(exp_at)))}).")
    else:
        log("✅ Nouveau access_token OK.")

    print("\nTu peux relancer ton script de post. Si tu as un 401 plus tard, relance ce fichier pour rafraîchir à nouveau.")


# ------------------------------
# Main
# ------------------------------
if __name__ == "__main__":
    refresh_access_token()
