# auth_tiktok_firefox.py
# Ouvre Firefox, attend l'auth TikTok (Desktop), récupère le token et l'enregistre.
import argparse, os, time, threading, hashlib, secrets, json, requests, webbrowser, subprocess, tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs, quote
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

ap = argparse.ArgumentParser(description="Connexion OAuth TikTok multi-compte")
ap.add_argument(
    "--shared-browser",
    action="store_true",
    help="Utilise la session navigateur habituelle au lieu d'un profil Firefox isolé.",
)
args = ap.parse_args()

# --- Config minimale ---
CLIENT_KEY    = (os.getenv("TIKTOK_CLIENT_KEY") or "").strip()
CLIENT_SECRET = (os.getenv("TIKTOK_CLIENT_SECRET") or "").strip()
REDIRECT_URI  = (os.getenv("TIKTOK_REDIRECT_URI") or "http://127.0.0.1:53682/callback").strip()
SCOPES        = (os.getenv("TIKTOK_SCOPES") or "user.info.basic,video.upload,video.publish").strip()

# Optionnel: forcer le chemin de Firefox (Windows)
FIREFOX_PATH  = (os.getenv("FIREFOX_PATH") or "").strip()  # ex: C:\Program Files\Mozilla Firefox\firefox.exe

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL     = "https://open.tiktokapis.com/v2/oauth/token/"
TOKENS_JSON   = BASE_DIR / "config" / "tiktok_tokens.json"
ENV_FILE      = BASE_DIR / ".env"

def log(s: str): print("[INFO]", s)
def err(s: str): print("[ERR]", s)

def assert_env():
    if not CLIENT_KEY or not CLIENT_SECRET:
        raise SystemExit("[ERR] TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET manquants dans .env")
    if not (REDIRECT_URI.startswith("http://") or REDIRECT_URI.startswith("https://")):
        raise SystemExit("[ERR] TIKTOK_REDIRECT_URI invalide. Exemple valide: http://127.0.0.1:53682/callback")

class _OpenedBrowser:
    def __init__(self, proc=None):
        self.proc = proc


def open_auth_url(auth_url: str):
    fresh_browser = not args.shared_browser and (os.getenv("TIKTOK_AUTH_FRESH_BROWSER", "1").strip() != "0")

    if fresh_browser and FIREFOX_PATH and Path(FIREFOX_PATH).exists():
        profile_dir = tempfile.mkdtemp(prefix="mr_martin_tiktok_auth_")
        log("Ouverture Firefox avec un profil isolé pour choisir le bon compte TikTok.")
        log("Connecte le compte voulu dans cette fenêtre, puis autorise l'application.")
        return _OpenedBrowser(subprocess.Popen([
            FIREFOX_PATH,
            "-no-remote",
            "-profile",
            profile_dir,
            auth_url,
        ]))

    # Essaie d'ouvrir explicitement Firefox si FIREFOX_PATH est fourni
    if FIREFOX_PATH and Path(FIREFOX_PATH).exists():
        try:
            webbrowser.register("firefox", None, webbrowser.BackgroundBrowser(FIREFOX_PATH))
            br = webbrowser.get("firefox")
            br.open(auth_url, new=1, autoraise=True)
            return _OpenedBrowser()
        except Exception:
            pass
    # Sinon, tente le navigateur par défaut
    try:
        br = webbrowser.get()  # windows-default / default
        br.open(auth_url, new=1, autoraise=True)
        return _OpenedBrowser()
    except Exception:
        return None

def gen_verifier(n=64):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(secrets.choice(alphabet) for _ in range(n))

def challenge_hex(verifier: str) -> str:
    # TikTok Desktop attend un SHA256 encodé HEX (pas base64url)
    return hashlib.sha256(verifier.encode("ascii")).hexdigest()

# --- Petit serveur HTTP local pour capter ?code=... ---
class CallbackHandler(BaseHTTPRequestHandler):
    code  = None
    state = None
    path_seen = None
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        CallbackHandler.code  = (qs.get("code")  or [""])[0]
        CallbackHandler.state = (qs.get("state") or [""])[0]
        CallbackHandler.path_seen = parsed.path
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK. Vous pouvez fermer cette fenetre.")
    def log_message(self, *args):  # silence les logs HTTP
        return

def wait_code_in_browser(auth_url: str, expected_state: str, timeout_sec=240) -> str:
    parsed = urlparse(REDIRECT_URI)
    port = parsed.port or 53682

    CallbackHandler.code = None
    CallbackHandler.state = None
    CallbackHandler.path_seen = None

    srv = HTTPServer(("127.0.0.1", port), CallbackHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()

    log(f"Serveur callback: http://127.0.0.1:{port}{parsed.path or '/callback'}")
    log("Ouverture du navigateur...")
    opened = open_auth_url(auth_url)
    if opened is None:
        err("Impossible d'initialiser un navigateur. Installe Firefox ou configure FIREFOX_PATH dans .env")
        raise SystemExit(1)
    log("Si rien ne s'ouvre, copie-colle l'URL ci-dessous dans Firefox:")
    print(auth_url)

    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if CallbackHandler.code:
            break
        time.sleep(0.2)

    try:
        srv.shutdown()
    except Exception:
        pass

    if not CallbackHandler.code:
        raise SystemExit("[ERR] Aucun code recu (timeout). Vérifie la Desktop Redirect URI dans le portail et le compte Sandbox.")

    # Vérifie l'état CSRF basique
    if CallbackHandler.state != expected_state:
        err(f"state inattendu. attendu={expected_state} recu={CallbackHandler.state}")
        raise SystemExit(1)

    return CallbackHandler.code

def update_env_file(var: str, val: str):
    # met à jour ou ajoute la variable dans .env
    lines = []
    if ENV_FILE.exists():
        with open(ENV_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    key_eq = var + "="
    found = False
    out_lines = []
    for line in lines:
        if line.strip().startswith(key_eq):
            out_lines.append(f"{var}={val}")
            found = True
        else:
            out_lines.append(line)
    if not found:
        out_lines.append(f"{var}={val}")
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")

def main():
    assert_env()

    # Construire l'URL d'auth simple (PKCE HEX)
    verifier = gen_verifier()
    chall    = challenge_hex(verifier)
    state    = "st" + secrets.token_hex(8)
    params   = {
        "client_key": CLIENT_KEY,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "code_challenge": chall,
        "code_challenge_method": "S256",
    }
    auth_url = AUTHORIZE_URL + "?" + urlencode(params, quote_via=quote)
    log(f"client_key={CLIENT_KEY[:8]}... redirect_uri={REDIRECT_URI}")

    # Ouvre Firefox et attend la redirection avec ?code=...
    code = wait_code_in_browser(auth_url, state)
    log(f"Code recu: {code[:12]}...")

    # Echange code -> tokens
    payload = {
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }
    r = requests.post(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type":"application/x-www-form-urlencoded"},
        timeout=30
    )
    if r.status_code != 200:
        err(f"Echec token HTTP {r.status_code}: {r.text[:500]}")
        raise SystemExit(1)

    tok = r.json()
    access_token  = (tok.get("access_token")  or "").strip()
    refresh_token = (tok.get("refresh_token") or "").strip()
    if not access_token:
        err("Reponse token sans access_token")
        print(tok)
        raise SystemExit(1)

    TOKENS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKENS_JSON, "w", encoding="utf-8") as f:
        json.dump(tok, f, ensure_ascii=False, indent=2)
    log(f"Tokens sauvegardes -> {TOKENS_JSON}")

    # Met à jour .env (rétro-compat)
    update_env_file("TIKTOK_USER_ACCESS_TOKEN", access_token)
    log("TIKTOK_USER_ACCESS_TOKEN mis à jour dans .env")
    if refresh_token:
        update_env_file("TIKTOK_USER_REFRESH_TOKEN", refresh_token)
        log("TIKTOK_USER_REFRESH_TOKEN mis à jour dans .env")

    # ── Enregistrement dans le gestionnaire de comptes multi-compte ───────────
    try:
        import time as _time
        from tiktok_account_manager import add_or_update_account, refresh_profile, set_active_account
        open_id = (tok.get("open_id") or "").strip()
        now = int(_time.time())
        expires_at = now + int(tok["expires_in"]) if tok.get("expires_in") else None
        refresh_expires_at = now + int(tok["refresh_expires_in"]) if tok.get("refresh_expires_in") else None
        acc_id = add_or_update_account(
            access_token=access_token,
            refresh_token=refresh_token,
            open_id=open_id,
            expires_at=expires_at,
            refresh_expires_at=refresh_expires_at,
        )
        set_active_account(acc_id)
        if refresh_profile(acc_id):
            log("✅ Profil TikTok récupéré (nom + avatar)")
        else:
            log("[WARN] Profil TikTok non récupéré. Vérifie que le scope user.info.basic est bien autorisé.")
        log(f"✅ Compte enregistré dans le gestionnaire : {acc_id} (actif)")
    except Exception as e:
        log(f"[WARN] Enregistrement compte manager échoué (non bloquant): {e}")

    print("\nOK. Nouveau compte enregistré et activé.")

if __name__ == "__main__":
    main()
