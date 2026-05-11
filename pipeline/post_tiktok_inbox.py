# post_tiktok_inbox.py
# Upload en brouillon (Inbox) OU Post direct via TikTok Content Posting API — FILE_UPLOAD
#
# Usage (brouillon par défaut) :
#   python post_tiktok_inbox.py --video <path> [--poll]
#
# Usage (post direct + caption) :
#   python post_tiktok_inbox.py --video <path> --direct --caption "Ma légende #hashtag" --privacy SELF_ONLY --disable-comment
#
# Option pour forcer la taille des chunks :
#   python post_tiktok_inbox.py --video <path> --chunk-mib 60 --poll
#
# Prérequis :
#   - .env avec TIKTOK_USER_ACCESS_TOKEN=xxxxxxxx

import os, math, json, argparse, requests, sys, subprocess
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

API_INIT_INBOX  = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
API_INIT_DIRECT = "https://open.tiktokapis.com/v2/post/publish/video/init/"
API_STATUS      = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
API_CREATOR_INFO = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"

MIB        = 1024 * 1024
MIN_CHUNK  = 5  * MIB     # 5 MiB
MAX_CHUNK  = 64 * MIB     # 64 MiB
MAX_LAST   = 128 * MIB    # 128 MiB (dernier chunk max)

def log(s):  print("[INFO]", s, flush=True)
def ok(s):   print("[OK]", s, flush=True)
def err(s):  print("[ERR]", s, flush=True)

class AccessTokenInvalid(Exception):
    """Le token d'accès est invalide ou manquant dans la requête."""
    pass

class ScopeNotAuthorized(Exception):
    """Le scope requis (video.publish) n'a pas été autorisé par l'utilisateur."""
    pass

def human_bytes(n: int) -> str:
    f = float(n)
    for u in ["B","KB","MB","GB","TB"]:
        if f < 1024 or u == "TB":
            return f"{f:.1f}{u}"
        f /= 1024

def ensure_token(override: str | None = None) -> str:
    """Retourne le token à utiliser. override prend la priorité sur l'env."""
    tok = (override or os.getenv("TIKTOK_USER_ACCESS_TOKEN") or "").strip()
    if not tok:
        raise SystemExit("[ERR] TIKTOK_USER_ACCESS_TOKEN manquant dans .env (lance d'abord l'auth/refresh).")
    return tok

def compute_plan_auto(total_size: int) -> tuple[int,int]:
    """
    Plan automatique conforme FILE_UPLOAD :
      - Si total_size ≤ 64 MiB → 1 seul chunk = total_size
      - Si 64 < total_size ≤ 128 MiB → 2 chunks (chunk_size ≈ half, borné 5..64 MiB)
      - Si total_size > 128 MiB → chunk_size = 64 MiB, total_parts = floor(total/64)
    NB: total_chunk_count = floor(total_size / chunk_size)
    Le dernier chunk peut dépasser chunk_size (≤ 128 MiB).
    """
    if total_size <= MAX_CHUNK:
        return total_size, 1

    if total_size <= 2 * MAX_CHUNK:
        # Forcer 2 chunks de ~ la moitié chacun (≤ 64 MiB)
        half_mib   = max(MIN_CHUNK, min(MAX_CHUNK, (total_size // 2) // MIB * MIB))
        chunk_size = half_mib
        total_parts = total_size // chunk_size  # floor
        if total_parts < 2:
            total_parts = 2
            chunk_size = max(MIN_CHUNK, min(MAX_CHUNK, (total_size // total_parts) // MIB * MIB))
        return chunk_size, total_parts

    # > 128 MiB → 64 MiB standard, floor parts
    chunk_size  = MAX_CHUNK
    total_parts = total_size // chunk_size  # floor (>= 2)
    return chunk_size, total_parts

def compute_plan_with_override(total_size: int, chunk_mib: int) -> tuple[int,int]:
    """
    Plan avec override utilisateur (chunk_mib forcé entre 5 et 64).
    Respecte total_chunk_count = floor(total/chunk_size).
    Si floor==1 et total>64 MiB, on ajuste pour 2 chunks.
    """
    if chunk_mib < 5 or chunk_mib > 64:
        raise SystemExit(f"[ERR] --chunk-mib doit être entre 5 et 64 (reçu {chunk_mib}).")
    chunk_size  = chunk_mib * MIB
    total_parts = total_size // chunk_size  # floor

    if total_parts == 0:
        # un seul chunk (≤64 MiB)
        return total_size, 1

    if total_parts == 1 and total_size > MAX_CHUNK:
        # 64 < total ≤ 128 MiB → forcer 2 chunks
        total_parts = 2
        chunk_size  = max(MIN_CHUNK, min(MAX_CHUNK, (total_size // total_parts) // MIB * MIB))

    return chunk_size, total_parts

def build_post_info(args, creator_info: dict | None = None) -> dict | None:
    """
    Construit le bloc post_info pour un POST DIRECT.
    Valide le privacy_level contre les options retournées par creator_info (obligatoire).
    Pour la conformité TikTok :
      - L'utilisateur DOIT avoir sélectionné explicitement privacy (pas de default)
      - disable_comment/duet/stitch = NOT allow_X (off par défaut)
      - Toujours désactivé si le créateur l'a désactivé côté app
    """
    if not args.direct:
        return None

    privacy = args.privacy
    if not privacy:
        raise SystemExit("[ERR] --privacy obligatoire en mode DIRECT (conformité TikTok : pas de valeur par défaut).")

    if creator_info:
        allowed = creator_info.get("privacy_level_options", [])
        if allowed and privacy not in allowed:
            raise SystemExit(f"[ERR] Privacy '{privacy}' non autorisée pour ce compte. Options: {allowed}")

    # Interaction : si --allow-X passé ET le créateur ne l'a pas désactivé → autorisé
    creator_comment_disabled = bool(creator_info and creator_info.get("comment_disabled"))
    creator_duet_disabled    = bool(creator_info and creator_info.get("duet_disabled"))
    creator_stitch_disabled  = bool(creator_info and creator_info.get("stitch_disabled"))

    disable_comment = creator_comment_disabled or (not args.allow_comment)
    disable_duet    = creator_duet_disabled    or (not args.allow_duet)
    disable_stitch  = creator_stitch_disabled  or (not args.allow_stitch)

    pi: dict = {
        "privacy_level": privacy,
        "disable_comment": disable_comment,
        "disable_duet": disable_duet,
        "disable_stitch": disable_stitch,
    }
    if args.caption:
        pi["title"] = args.caption[:2200]  # max 2200 UTF-16 runes

    # Disclosure commercial content
    if args.brand_organic:
        pi["brand_organic_toggle"] = True
    if args.brand_content:
        pi["brand_content_toggle"] = True
        # Règle TikTok : branded content ne peut pas être SELF_ONLY
        if privacy == "SELF_ONLY":
            raise SystemExit("[ERR] Branded Content ne peut pas être posté en SELF_ONLY (règle TikTok).")

    if args.cover_ms is not None:
        pi["video_cover_timestamp_ms"] = args.cover_ms
    return pi


def _check_video_duration(video_path: Path, creator_info: dict) -> None:
    """Vérifie que la durée de la vidéo ne dépasse pas max_video_post_duration_sec."""
    max_dur = creator_info.get("max_video_post_duration_sec", 0)
    if not max_dur:
        return
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        duration = float((result.stdout or "0").strip())
    except Exception as e:
        log(f"Impossible de mesurer la durée vidéo via ffprobe ({e}), check ignoré.")
        return
    if duration > max_dur:
        raise SystemExit(
            f"[ERR] Vidéo trop longue ({duration:.1f}s) pour ce créateur "
            f"(max autorisé : {max_dur}s)."
        )
    log(f"Durée vidéo : {duration:.1f}s (max créateur : {max_dur}s) ✓")


def _check_creator_can_post(creator_info: dict) -> None:
    """Stoppe si le créateur a atteint sa limite de posts ou ne peut pas poster."""
    # TikTok renvoie une clé status ou un message d'erreur dans creator_info
    if creator_info.get("status") == "user_quota_exceeded":
        raise SystemExit("[ERR] Le créateur a atteint sa limite de posts pour les 24h. Réessaie plus tard.")
    # Certains comptes retournent un flag explicite
    if creator_info.get("can_post") is False:
        raise SystemExit("[ERR] Ce créateur ne peut pas poster pour le moment.")

def _raise_if_token_invalid(resp: requests.Response):
    # TikTok renvoie HTTP 401/403 avec divers codes d'erreur
    if resp.status_code in (401, 403):
        try:
            j = resp.json()
        except Exception:
            j = {}
        code = ((j.get("error") or {}).get("code") or "").lower()
        if "scope_not_authorized" in code or "unaudited_client" in code:
            raise ScopeNotAuthorized(resp.text)
        if "access_token_invalid" in code or "access token" in (j.get("error", {}).get("message","").lower()):
            raise AccessTokenInvalid(resp.text)


def query_creator_info(token: str) -> dict:
    """
    Appelle /v2/post/publish/creator_info/query/ (obligatoire avant chaque post DIRECT).
    Retourne les infos du créateur : privacy_level_options, max_video_post_duration_sec, etc.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    r = requests.post(API_CREATOR_INFO, headers=headers, json={}, timeout=30)
    _raise_if_token_invalid(r)
    if r.status_code != 200:
        err(f"creator_info HTTP {r.status_code}: {r.text}")
        raise SystemExit(1)
    data = r.json().get("data", {})
    log(f"Creator: @{data.get('creator_username', '?')} — "
        f"privacy={data.get('privacy_level_options', [])}, "
        f"max_duration={data.get('max_video_post_duration_sec', '?')}s")
    return data

def init_file_upload(token: str,
                     video_size: int,
                     chunk_size: int,
                     total_parts: int,
                     direct: bool,
                     post_info: dict | None) -> tuple[str, str]:
    """
    Appel INIT. Envoie un plan cohérent :
      - chunk_size : 5..64 MiB (ou total_size si 1 chunk)
      - total_chunk_count : floor(video_size / chunk_size)
    """
    # Cas "1 chunk" : pour rester conforme, on déclare chunk_size = video_size
    if total_parts == 1:
        chunk_size = video_size

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    payload = {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_parts
        }
    }
    if direct and post_info:
        payload["post_info"] = post_info

    api = API_INIT_DIRECT if direct else API_INIT_INBOX
    r = requests.post(api, headers=headers, json=payload, timeout=30)

    # Détection dédiée du token invalide
    try:
        _raise_if_token_invalid(r)
    except AccessTokenInvalid:
        raise

    if r.status_code != 200:
        err(f"init({ 'DIRECT' if direct else 'INBOX' }) HTTP {r.status_code}: {r.text}")
        raise SystemExit(1)

    data = r.json().get("data") or {}
    publish_id = data.get("publish_id", "")
    upload_url = data.get("upload_url", "")
    if not publish_id or not upload_url:
        err(f"Réponse init incomplète: {r.text}")
        raise SystemExit(1)
    return publish_id, upload_url

def put_chunk(upload_url: str, file_path: Path, start: int, length: int, total: int, part_idx: int, total_parts: int):
    """
    Envoie un chunk [start, start+length-1].
    - Tous les chunks sauf le dernier : 5..64 MiB
    - Dernier chunk : peut dépasser chunk_size, max 128 MiB
    """
    end = start + length - 1
    if length <= 0:
        raise SystemExit(f"[ERR] Longueur de chunk invalide (idx={part_idx}, len={length}).")
    is_last = (part_idx == total_parts)
    if not is_last and (length < MIN_CHUNK or length > MAX_CHUNK):
        raise SystemExit(f"[ERR] Chunk #{part_idx} hors bornes (len={human_bytes(length)}).")
    if is_last and length > MAX_LAST:
        raise SystemExit(f"[ERR] Chunk final trop grand ({human_bytes(length)} > 128MB).")

    headers = {
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Content-Type": "video/mp4",
    }
    with file_path.open("rb") as f:
        f.seek(start)
        data = f.read(length)
    r = requests.put(upload_url, headers=headers, data=data, timeout=300)
    if r.status_code not in (200, 201, 206):
        err(f"PUT chunk #{part_idx} {start}-{end} HTTP {r.status_code}: {r.text[:400]}")
        raise SystemExit(1)

def poll_status(token: str, publish_id: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    payload = {"publish_id": publish_id}
    r = requests.post(API_STATUS, headers=headers, json=payload, timeout=30)
    # Optionnel: si le token a expiré ici, on pourrait aussi rafraîchir, mais
    # l’upload est déjà fait. On renvoie None et on laisse l’utilisateur relancer le poll.
    if r.status_code != 200:
        err(f"status HTTP {r.status_code}: {r.text}")
        return None
    return r.json()

def poll_status_until_done(token: str, publish_id: str, max_attempts: int = 24, delay: int = 5):
    """Interroge TikTok jusqu'à SUCCESS ou FAILURE (~2 min par défaut)."""
    import time
    for attempt in range(1, max_attempts + 1):
        st = poll_status(token, publish_id)
        if not st:
            log(f"Tentative {attempt}/{max_attempts}: réponse vide, on réessaie…")
            time.sleep(delay)
            continue

        status = st.get("data", {}).get("status", "")
        print(json.dumps(st, ensure_ascii=False, indent=2))

        if status in ("SUCCESS", "FAILURE", "SEND_TO_USER_INBOX"):
            ok(f"Statut final atteint: {status}")
            return status
        log(f"Tentative {attempt}/{max_attempts}: statut={status or 'N/A'}, nouvelle vérif dans {delay}s…")
        time.sleep(delay)

    err("Temps d'attente dépassé sans statut final.")
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True, help="Chemin MP4 à uploader/poster")
    ap.add_argument("--poll", action="store_true", help="Interroger le statut après upload")
    ap.add_argument("--token", type=str, default=None, help="Token d'accès TikTok (surpasse TIKTOK_USER_ACCESS_TOKEN)")

    # Options de publication directe
    ap.add_argument("--direct", action="store_true", help="Poster directement (sinon: upload en brouillon Inbox)")
    ap.add_argument("--caption", type=str, default=None, help="Légende/description du post (Direct Post uniquement)")
    ap.add_argument("--privacy", type=str, default=None,
                    choices=["PUBLIC_TO_EVERYONE","MUTUAL_FOLLOW_FRIENDS","FOLLOWER_OF_CREATOR","SELF_ONLY"],
                    help="Niveau de visibilité (Direct Post). Aucune valeur par défaut (conformité TikTok).")
    # Interactions : OFF par défaut, l'utilisateur DOIT cocher pour activer
    ap.add_argument("--allow-comment", dest="allow_comment", action="store_true",
                    help="Autoriser les commentaires (Direct Post). OFF par défaut.")
    ap.add_argument("--allow-duet", dest="allow_duet", action="store_true",
                    help="Autoriser les Duets (Direct Post). OFF par défaut.")
    ap.add_argument("--allow-stitch", dest="allow_stitch", action="store_true",
                    help="Autoriser les Stitch (Direct Post). OFF par défaut.")
    # Commercial content disclosure
    ap.add_argument("--brand-organic", dest="brand_organic", action="store_true",
                    help="Ton propre contenu promotionnel (Brand Organic).")
    ap.add_argument("--brand-content", dest="brand_content", action="store_true",
                    help="Contenu sponsorisé par une marque tierce (Branded Content / Paid Partnership).")
    ap.add_argument("--cover-ms", dest="cover_ms", type=int, default=None,
                    help="Timestamp (ms) pour choisir la vignette (Direct Post)")

    # Option pour forcer le chunk (MiB)
    ap.add_argument("--chunk-mib", dest="chunk_mib", type=int, default=None,
                    help="Taille d'un chunk en MiB (5..64). Si non spécifié, calcul auto conforme.")

    args = ap.parse_args()

    token = ensure_token(override=args.token)
    video_path = Path(args.video)
    if not video_path.exists():
        raise SystemExit(f"[ERR] Fichier introuvable: {video_path}")

    total_size = video_path.stat().st_size

    # Plan de chunks
    if args.chunk_mib is not None:
        chunk_size, total_parts = compute_plan_with_override(total_size, args.chunk_mib)
    else:
        chunk_size, total_parts = compute_plan_auto(total_size)

    # Sanity check : total_chunk_count doit être floor(total/chunk)
    # (Sauf cas 1 chunk où on enverra chunk_size = total_size à l'INIT)
    if total_parts > 1 and (total_size // chunk_size) != total_parts:
        # Réajuste proprement
        total_parts = max(2, total_size // chunk_size)

    log(f"Fichier: {video_path.name} ({human_bytes(total_size)}), chunk={human_bytes(chunk_size)}, parts={total_parts}")

    # 0) Query creator_info (obligatoire avant chaque post DIRECT, exigé par TikTok pour l'audit)
    creator_info = None
    if args.direct:
        try:
            creator_info = query_creator_info(token)
            _check_creator_can_post(creator_info)
            _check_video_duration(video_path, creator_info)
        except ScopeNotAuthorized:
            err("scope video.publish non autorisé — relance auth_tiktok_token_manager.py avec le nouveau scope")
            raise SystemExit(2)
        except AccessTokenInvalid:
            log("Token expiré lors de creator_info, refresh en cours...")
            try:
                subprocess.run([sys.executable, "auth_tiktok_refresh.py"], check=True)
            except subprocess.CalledProcessError as sube:
                raise SystemExit(f"[ERR] Échec refresh: {sube}") from sube
            load_dotenv(override=True)
            token = ensure_token()
            creator_info = query_creator_info(token)

    post_info = build_post_info(args, creator_info=creator_info)

    # 1) INIT (INBOX vs DIRECT) — avec auto refresh token si 401/access_token_invalid
    try:
        publish_id, upload_url = init_file_upload(
            token, total_size, chunk_size, total_parts,
            direct=args.direct,
            post_info=post_info
        )
    except ScopeNotAuthorized:
        err("scope video.publish non autorisé — relance auth_tiktok_token_manager.py avec le nouveau scope")
        raise SystemExit(2)
    except AccessTokenInvalid as e:
        err("Token invalide/expiré détecté à l'INIT. Lancement de auth_refreshtoken.py…")
        try:
            subprocess.run([sys.executable, "auth_tiktok_refresh.py"], check=True)
        except subprocess.CalledProcessError as sube:
            raise SystemExit(f"[ERR] Échec auth_refreshtoken.py: {sube}") from sube

        # Recharge le .env et récupère à nouveau le token
        load_dotenv(override=True)
        token = ensure_token()
        ok("Token rafraîchi. Nouvelle tentative INIT…")
        # Seconde tentative
        publish_id, upload_url = init_file_upload(
            token, total_size, chunk_size, total_parts,
            direct=args.direct,
            post_info=post_info
        )

    ok(f"Init OK ({'DIRECT' if args.direct else 'INBOX'}): publish_id={publish_id}")

    # 2) UPLOAD séquentiel (FILE_UPLOAD)
    start = 0
    for part_idx in range(1, total_parts + 1):
        remaining = total_size - start
        length = remaining if part_idx == total_parts else min(chunk_size, remaining)
        # Garde-fous
        if part_idx != total_parts and (length < MIN_CHUNK or length > MAX_CHUNK):
            raise SystemExit(f"[ERR] Chunk #{part_idx} hors bornes (len={human_bytes(length)}).")
        if part_idx == total_parts and length > MAX_LAST:
            raise SystemExit(f"[ERR] Chunk final trop grand ({human_bytes(length)} > 128MB).")

        log(f"Envoi chunk {part_idx}/{total_parts} (offset {start}, taille {human_bytes(length)})")
        put_chunk(upload_url, video_path, start, length, total_size, part_idx, total_parts)
        start += length

    ok("Upload terminé.")

    # 3) (Optionnel) Statut
    if args.poll:
        log("Interrogation du statut de publication…")
        poll_status_until_done(token, publish_id)

if __name__ == "__main__":
    main()
