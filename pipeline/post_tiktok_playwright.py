"""
post_tiktok_playwright.py — Bypass temporaire de l'API Content Posting TikTok.

Poste une vidéo sur TikTok via Playwright en réutilisant la session sauvegardée
par tools/login_tiktok.py. Utilisé tant que l'app TikTok n'est pas approuvée
pour le Direct Post.

CLI (compatible avec post_tiktok_inbox.py pour faciliter le routage main_v3) :
    python pipeline/post_tiktok_playwright.py --video <mp4>
        [--caption "Légende #tag"]
        [--privacy PUBLIC_TO_EVERYONE|MUTUAL_FOLLOW_FRIENDS|SELF_ONLY]
        [--allow-comment] [--allow-duet] [--allow-stitch]
        [--account-id acc_1]
        [--headless]

Codes retour :
    0  → succès (publish_id=playwright_<ts> émis sur stdout)
    2  → non connecté (lance tools/login_tiktok.py)
    3  → spam_risk détecté
    1  → autre échec
"""

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Page

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "tiktok"))

load_dotenv(BASE_DIR / ".env")

CHROME_PATH = os.getenv(
    "CHROME_PATH",
    r"C:\Users\n.amelaise\AppData\Local\ms-playwright\chromium-1181\chrome-win\chrome.exe",
)
SNAP_DIR = BASE_DIR / "automation_logs"
SNAP_DIR.mkdir(parents=True, exist_ok=True)

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=upload&lang=en"

PRIVACY_LABELS = {
    "PUBLIC_TO_EVERYONE": ("Tout le monde", "Everyone", "Public"),
    "MUTUAL_FOLLOW_FRIENDS": ("Amis", "Friends"),
    "SELF_ONLY": ("Vous uniquement", "Moi uniquement", "Only you", "Only me", "Private"),
}

POST_BUTTON_TEXTS = ("Publier", "Post", "Poster")
PRIVACY_TRIGGER_RE = re.compile(r"qui peut (voir|regarder)|who can (watch|view)", re.I)
CANCEL_BUTTON_TEXTS = ("Annuler", "Cancel", "Pas maintenant", "Not now", "Plus tard", "Later")

SPAM_PATTERNS = [
    re.compile(r"may violate.*community", re.I),
    re.compile(r"violates? our community", re.I),
    re.compile(r"unable to post", re.I),
    re.compile(r"spam", re.I),
]


def log(msg: str) -> None:
    print(f"[INFO] {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"[OK] {msg}", flush=True)


def err(msg: str) -> None:
    print(f"[ERR] {msg}", flush=True)


def _resolve_account_id(arg_id: str | None) -> str:
    if arg_id:
        return arg_id
    try:
        from tiktok_account_manager import get_active_account_id  # type: ignore
        a = get_active_account_id()
        if a:
            return a
    except Exception:
        pass
    return "default"


def _profile_path(account_id: str) -> Path:
    root = Path(os.getenv("TIKTOK_PROFILE_DIR", str(BASE_DIR / "playwright-profiles")))
    return root / f"tiktok_{account_id}"


async def _kill_joyride(page: Page) -> bool:
    """Supprime l'overlay react-joyride (tour onboarding TikTok Studio) qui
    bloque les clics. Tente d'abord 'Skip'/'Ignorer', puis retire le portail DOM."""
    skip_selectors = [
        "button[data-action='skip']",
        "button[aria-label*='Skip']",
        "button[aria-label*='Ignorer']",
        "button[aria-label*='Passer']",
        "button.react-joyride__tooltip__button--skip",
        "[data-test-id='button-skip']",
        "[data-test-id='button-close']",
    ]
    for sel in skip_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=2000)
                log("react-joyride : bouton Skip cliqué")
                await page.wait_for_timeout(400)
                return True
        except Exception:
            continue
    # Fallback : retirer le portail directement
    try:
        removed = await page.evaluate(
            "() => { const p = document.getElementById('react-joyride-portal');"
            " if (p) { p.remove(); return true; }"
            " const ov = document.querySelector('.react-joyride__overlay');"
            " if (ov) { ov.remove(); return true; }"
            " return false; }"
        )
        if removed:
            log("react-joyride : portail DOM supprimé")
            return True
    except Exception:
        pass
    return False


async def _dismiss_modals(page: Page) -> int:
    """Ferme les modals TikTok Studio (vérifications, tutoriels, tips).
    Ordre : 0) overlay react-joyride,
            1) bouton 'Annuler/Cancel' explicite dans un dialog,
            2) X (aria-label close, ou bouton avec SVG en haut à droite),
            3) Escape.
    Retourne 1 si quelque chose a été fermé, sinon 0."""
    # 0) Onboarding react-joyride (overlay grisé qui bloque tous les clics)
    if await _kill_joyride(page):
        return 1

    # 1) Bouton Annuler / Cancel dans un dialog ouvert
    for txt in CANCEL_BUTTON_TEXTS:
        try:
            btn = page.locator(
                f"div.TUXModal button:has-text('{txt}'), div[role='dialog'] button:has-text('{txt}')"
            ).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=2000)
                log(f"Modal fermé via bouton '{txt}'")
                await page.wait_for_timeout(500)
                return 1
        except Exception:
            continue

    # 2) X fermeture (aria-label localisé OU bouton SVG dans le header du modal)
    close_selectors = [
        "div.TUXModal button[aria-label='Close']",
        "div.TUXModal button[aria-label='Fermer']",
        "div[class*='TUXModal'] button[aria-label*='lose']",
        "div[class*='TUXModal'] button[aria-label*='ermer']",
        "div[role='dialog'] button[aria-label*='lose']",
        "div[role='dialog'] button[aria-label*='ermer']",
        "button[data-e2e='modal-close-button']",
    ]
    for sel in close_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=2000)
                log("Modal fermé via X")
                await page.wait_for_timeout(500)
                return 1
        except Exception:
            continue

    # 3) Escape en dernier recours
    overlay = page.locator("div.TUXModal-overlay[data-transition-status='open']").first
    try:
        if await overlay.count() > 0 and await overlay.is_visible():
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
            log("Modal fermé via Escape (fallback)")
            return 1
    except Exception:
        pass
    return 0


async def _wait_no_overlay(page: Page, timeout_s: int = 12, max_attempts: int = 6) -> None:
    """Attend qu'il n'y ait plus d'overlay TUXModal ouvert. Limite les tentatives
    pour ne pas boucler indéfiniment si la fermeture échoue."""
    import time as _t
    deadline = _t.time() + timeout_s
    attempts = 0
    while _t.time() < deadline and attempts < max_attempts:
        tux = page.locator("div.TUXModal-overlay[data-transition-status='open']").first
        joyride = page.locator("div.react-joyride__overlay, #react-joyride-portal").first
        try:
            tux_open = await tux.count() > 0 and await tux.is_visible()
        except Exception:
            tux_open = False
        try:
            joy_open = await joyride.count() > 0
        except Exception:
            joy_open = False
        if not tux_open and not joy_open:
            return
        closed = await _dismiss_modals(page)
        if not closed:
            await page.wait_for_timeout(600)
        attempts += 1
    log("Overlay toujours présent après tentatives — on continue.")


async def _snap(page: Page, tag: str) -> None:
    try:
        path = SNAP_DIR / f"tiktok_post_{tag}_{int(time.time())}.png"
        await page.screenshot(path=str(path), full_page=False)
        log(f"screenshot → {path.name}")
    except Exception:
        pass


async def _is_logged_in(page: Page) -> bool:
    """Détecte la présence d'un input file ou d'un bouton 'Select video' sur la page d'upload."""
    selectors = [
        "input[type='file']",
        "button:has-text('Select video')",
        "div:has-text('Select video')",
        "[data-e2e='upload-btn']",
    ]
    for sel in selectors:
        try:
            if await page.locator(sel).first.count() > 0:
                return True
        except Exception:
            continue
    return False


async def _detect_spam(page: Page) -> bool:
    try:
        body = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        return False
    return any(p.search(body) for p in SPAM_PATTERNS)


async def _set_video(page: Page, video_path: Path) -> None:
    log("Sélection du fichier vidéo…")
    # L'input file peut être caché dans une iframe TikTok Studio
    input_locator = None
    for ctx in [page, *page.frames]:
        try:
            loc = ctx.locator("input[type='file']")
            if await loc.count() > 0:
                input_locator = loc.first
                break
        except Exception:
            continue
    if input_locator is None:
        raise RuntimeError("Impossible de trouver l'input[type=file] de TikTok")
    await input_locator.set_input_files(str(video_path))
    ok("Vidéo envoyée au navigateur, attente du traitement…")


async def _wait_upload_processed(page: Page, timeout_s: int = 600) -> None:
    """Attend que TikTok ait fini de traiter la vidéo (apparition de l'éditeur de caption)."""
    deadline = time.time() + timeout_s
    caption_selectors = [
        "div[contenteditable='true']",
        "[data-text='true']",
        "div.public-DraftEditor-content",
    ]
    while time.time() < deadline:
        await _dismiss_modals(page)
        for sel in caption_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    ok("Éditeur de caption détecté — vidéo traitée.")
                    return
            except Exception:
                pass
        if await _detect_spam(page):
            raise SpamRisk("Spam risk détecté pendant le traitement")
        await page.wait_for_timeout(2000)
    raise PWTimeout("Timeout en attendant la fin du traitement vidéo")


async def _set_caption(page: Page, caption: str) -> None:
    if not caption:
        return
    log("Saisie de la caption…")
    await _wait_no_overlay(page)
    editor = None
    for sel in ("div[contenteditable='true']", "div.public-DraftEditor-content"):
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                editor = loc
                break
        except Exception:
            continue
    if editor is None:
        log("Éditeur de caption introuvable — caption ignorée.")
        return

    # Tentative de click ; en cas d'overlay, on focus via JS
    try:
        await editor.click(timeout=5000)
    except Exception:
        await _dismiss_modals(page)
        try:
            await editor.click(timeout=5000)
        except Exception:
            log("Click intercepté — fallback focus JS.")
            try:
                await editor.evaluate("(el) => el.focus()")
            except Exception:
                pass

    # Sélectionner tout puis remplacer
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")
    # Tape le texte caractère par caractère pour laisser TikTok parser les hashtags
    await editor.type(caption, delay=15)
    ok("Caption saisie.")


async def _set_privacy(page: Page, privacy: str | None) -> None:
    if not privacy:
        return
    labels = PRIVACY_LABELS.get(privacy.upper(), ())
    if not labels:
        return
    log(f"Configuration de la visibilité : {privacy}")

    # Vérifie si la valeur souhaitée est déjà sélectionnée (input/combobox affichée)
    try:
        for label in labels:
            already = page.locator(
                f"input[value='{label}'], div[role='combobox']:has-text('{label}')"
            ).first
            if await already.count() > 0 and await already.is_visible():
                ok(f"Visibilité déjà réglée sur '{label}' — pas de changement.")
                return
    except Exception:
        pass

    try:
        trigger = page.get_by_text(PRIVACY_TRIGGER_RE).first
        if await trigger.count() == 0:
            log("Dropdown visibilité introuvable — étape ignorée.")
            return
        await trigger.scroll_into_view_if_needed()
        # Cliquer sur le combobox voisin (le texte est un label au-dessus)
        combobox = trigger.locator(
            "xpath=ancestor::*[1]/following-sibling::*[1]//*[@role='combobox' or self::input]"
        ).first
        if await combobox.count() > 0:
            await combobox.click(timeout=3000)
        else:
            await trigger.click(timeout=3000)
    except Exception as e:
        log(f"Impossible d'ouvrir le sélecteur de visibilité ({e}) — étape ignorée.")
        return

    for label in labels:
        try:
            opt = page.get_by_role("option", name=re.compile(rf"^{re.escape(label)}$", re.I)).first
            if await opt.count() == 0:
                opt = page.get_by_text(re.compile(rf"^{re.escape(label)}$", re.I)).first
            if await opt.count() > 0:
                await opt.click()
                ok(f"Visibilité réglée : {label}")
                return
        except Exception:
            continue
    log(f"Aucune option correspondant à {privacy} trouvée.")


async def _toggle_interaction(page: Page, label_regex: str, want_on: bool) -> None:
    """Trouve un checkbox/switch par label et l'aligne sur want_on."""
    try:
        label = page.get_by_text(re.compile(label_regex, re.I)).first
        if await label.count() == 0:
            return
        container = label.locator("xpath=ancestor::*[self::div or self::label][1]")
        cb = container.locator("input[type='checkbox']").first
        if await cb.count() == 0:
            cb = container.locator("[role='switch']").first
        if await cb.count() == 0:
            return
        try:
            is_checked = await cb.is_checked()
        except Exception:
            aria = await cb.get_attribute("aria-checked")
            is_checked = (aria == "true")
        if is_checked != want_on:
            await cb.click()
            log(f"Toggle '{label_regex}' → {want_on}")
    except Exception:
        pass


async def _click_post(page: Page) -> None:
    log("Clic sur Post…")
    await _wait_no_overlay(page)
    candidates = [
        "button[data-e2e='post_video_button']",
        *[f"button:has-text('{t}'):not(:has-text('brouillon'))" for t in POST_BUTTON_TEXTS],
        *[f"div[role='button']:has-text('{t}'):not(:has-text('brouillon'))" for t in POST_BUTTON_TEXTS],
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            if await btn.count() == 0:
                continue
            await btn.scroll_into_view_if_needed()
            try:
                await btn.wait_for(state="visible", timeout=5000)
            except Exception:
                pass
            await btn.click(timeout=8000)
            return
        except Exception:
            continue
    raise RuntimeError("Bouton Post introuvable")


async def _wait_post_done(page: Page, timeout_s: int = 180) -> None:
    """Attend confirmation : redirection vers /tiktokstudio/content, toast 'posted', ou bouton 'Upload another'."""
    success_patterns = [
        re.compile(r"your video is being uploaded", re.I),
        re.compile(r"video uploaded", re.I),
        re.compile(r"posted", re.I),
        re.compile(r"upload another", re.I),
    ]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        url = page.url
        if "/tiktokstudio/content" in url or "/upload" not in url and "tiktokstudio" in url:
            ok(f"Redirection détectée : {url}")
            return
        if await _detect_spam(page):
            raise SpamRisk("Spam risk détecté après clic Post")
        try:
            body = await page.locator("body").inner_text(timeout=2000)
            if any(p.search(body) for p in success_patterns):
                ok("Confirmation de publication détectée.")
                return
        except Exception:
            pass
        await page.wait_for_timeout(2000)
    raise PWTimeout("Timeout en attendant la confirmation de publication")


class SpamRisk(Exception):
    pass


async def run(args) -> int:
    video_path = Path(args.video).resolve()
    if not video_path.exists():
        err(f"Fichier introuvable : {video_path}")
        return 1

    account_id = _resolve_account_id(args.account_id)
    profile = _profile_path(account_id)
    if not profile.exists():
        err(f"Profil Playwright TikTok absent pour {account_id} : {profile}")
        err("Lance d'abord : python tools/login_tiktok.py --account-id " + account_id)
        return 2

    for lf in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile / lf).unlink(missing_ok=True)
        except Exception:
            pass

    log(f"Compte : {account_id} | profil : {profile.name}")
    log(f"Vidéo  : {video_path.name} ({video_path.stat().st_size // 1024} KB)")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(profile),
            executable_path=CHROME_PATH,
            headless=args.headless,
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass

            if "/login" in page.url or not await _is_logged_in(page):
                await _snap(page, "not_logged_in")
                err(f"Pas de session active pour {account_id}. Lance tools/login_tiktok.py.")
                return 2

            await _set_video(page, video_path)
            try:
                await _wait_upload_processed(page)
            except SpamRisk:
                await _snap(page, "spam_processing")
                err("spam_risk")
                return 3

            await _set_caption(page, args.caption or "")

            await _set_privacy(page, args.privacy)

            await _toggle_interaction(page, r"comment", want_on=args.allow_comment)
            await _toggle_interaction(page, r"duet", want_on=args.allow_duet)
            await _toggle_interaction(page, r"stitch", want_on=args.allow_stitch)

            await _snap(page, "before_post")
            await _click_post(page)

            try:
                await _wait_post_done(page)
            except SpamRisk:
                await _snap(page, "spam_after_post")
                err("spam_risk")
                return 3
            except PWTimeout:
                # Si pas d'erreur visible, on considère que c'est probablement parti
                if await _detect_spam(page):
                    err("spam_risk")
                    return 3
                await _snap(page, "post_timeout")
                err("Timeout sans confirmation explicite — vérifier manuellement.")
                return 1

            publish_id = f"playwright_{int(time.time())}"
            ok(f"Publication OK — publish_id={publish_id}")
            await _snap(page, "post_done")
            return 0
        except SpamRisk:
            err("spam_risk")
            return 3
        except Exception as e:
            err(f"Échec Playwright : {e}")
            await _snap(page, "exception")
            return 1
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--caption", default=None)
    ap.add_argument("--privacy", default=None,
                    choices=["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"])
    ap.add_argument("--allow-comment", dest="allow_comment", action="store_true")
    ap.add_argument("--allow-duet", dest="allow_duet", action="store_true")
    ap.add_argument("--allow-stitch", dest="allow_stitch", action="store_true")
    ap.add_argument("--account-id", dest="account_id", default=None)
    ap.add_argument("--headless", action="store_true",
                    help="Mode headless (défaut : avec UI, plus fiable).")
    # Args ignorés pour compat avec post_tiktok_inbox.py (--poll, --token, --direct, etc.)
    ap.add_argument("--poll", action="store_true", help="(ignoré en mode Playwright)")
    ap.add_argument("--direct", action="store_true", help="(ignoré : Playwright poste toujours direct)")
    ap.add_argument("--token", default=None, help="(ignoré en mode Playwright)")
    ap.add_argument("--chunk-mib", dest="chunk_mib", type=int, default=None, help="(ignoré)")
    ap.add_argument("--brand-organic", dest="brand_organic", action="store_true", help="(ignoré)")
    ap.add_argument("--brand-content", dest="brand_content", action="store_true", help="(ignoré)")
    ap.add_argument("--cover-ms", dest="cover_ms", type=int, default=None, help="(ignoré)")
    return ap.parse_args()


def main():
    args = parse_args()
    rc = asyncio.run(run(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
