#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
automate_adobe_pool_embed.py — **Un seul navigateur**, jusqu'à N pages en parallèle (intégrable à main.py)

Ce module expose :
  - `Task` (dataclass) pour décrire un segment
  - `run_pool(tasks: list[Task], concurrency: int = 4)` qui ouvre 1 **Chromium persistant** et traite `tasks` en parallèle sur des **pages** réutilisées
  - des **helpers** rapides/robustes (attentes UI guidées, blocage du tracking, etc.)

Usage dans ton `main.py` :
  >>> from automate_adobe_pool_embed import Task, run_pool
  >>> tasks = [Task(audio_path, nom, genre, segment_id, intervenant_index, personnage_id), ...]
  >>> asyncio.run(run_pool(tasks, concurrency=4))

⚠️ Requiert Playwright installé/configuré et variables `.env` (mêmes clés que ta version).
"""

import asyncio
import os
import re
import subprocess
import sys
import time
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv

# Forcer UTF-8 sur stdout/stderr pour éviter OSError [Errno 22] sur Windows cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

# --- Config depuis .env
BASE_PROFILE_PATH = os.getenv("BASE_PROFILE_PATH")
CHROME_PATH = os.getenv("CHROME_PATH")
BASE_DIR = os.getenv("BASE_DIR", str(Path(__file__).parent.parent))
URL_ADOBE = os.getenv("URL_ADOBE")
IMAGE_LEFT_PATH = os.getenv("IMAGE_LEFT_PATH")
IMAGE_RIGHT_PATH = os.getenv("IMAGE_RIGHT_PATH")
HEADLESS = os.getenv("HEADLESS", "0").strip().lower() in ("1", "true", "yes")
SLOWMO_MS = int(os.getenv("SLOWMO_MS", "0"))
TIMEOUT_MULT = float(os.getenv("TIMEOUT_MULT", "2.5"))
VERBOSE_PAGE_LOGS = os.getenv("VERBOSE_PAGE_LOGS", "0").strip().lower() in ("1","true","yes")
BLOCK_TRACKING = os.getenv("BLOCK_TRACKING", "1").strip().lower() in ("1","true","yes")
BG_SECOND_PASS_NO_WAIT = os.getenv("BG_SECOND_PASS_NO_WAIT", "0").strip().lower() in ("1","true","yes")


# --- Constantes
STEP_RETRIES = 10
MAX_RETRIES_TASK = 10
DOWNLOAD_TIMEOUT_MS = int(300_000 * TIMEOUT_MULT)
NAV_TIMEOUT_MS = int(35_000 * TIMEOUT_MULT)
ACTION_TIMEOUT_MS = int(15_000 * TIMEOUT_MULT)
AUDIO_PROC_TIMEOUT_MS = int(90_000 * TIMEOUT_MULT)

VIDEO_SEGMENTS_DIR = os.getenv("VIDEO_SEGMENTS_DIR", os.path.join(BASE_DIR, "video_segments"))
TEMP_PROFILE_PATH = os.path.join(BASE_DIR, f"profiles/tmp_pool_profile")
LOGS_DIR = os.path.join(BASE_DIR, "automation_logs")
Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)
Path(VIDEO_SEGMENTS_DIR).mkdir(parents=True, exist_ok=True)

# --- Filtrage réseau pour accélérer
BLOCK_LIST = [
    re.compile(r"google-analytics|googletagmanager|doubleclick|hotjar|mixpanel|segment|amplitude", re.I),
    re.compile(r"/analytics/|/telemetry/|/collect", re.I),
    re.compile(r"/fonts\.(?:gstatic|googleapis)\.|/font\.woff2?", re.I),
]

# --- Modèle de tâche
@dataclass
class Task:
    audio_path: str
    nom: str
    genre: str
    segment_id: str
    intervenant_index: str
    personnage_id: str
    # Optionnel : surcharge des images de fond par segment (fonds dynamiques)
    image_left_path:  str = None
    image_right_path: str = None

# --- Utils génériques

def _sanitize_css_id(raw: str) -> str:
    return raw.replace(" ", "\\ ").replace(".", "\\.")

def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

async def _screenshot(page, tag: str, task: Task):
    path = os.path.join(LOGS_DIR, f"snap_{task.segment_id}_{task.intervenant_index}_{tag}.png")
    try:
        await asyncio.wait_for(page.screenshot(path=path, full_page=False), timeout=8)
        print(f"[Adobe] Screenshot: {path}")
    except Exception:
        pass
    
async def _dismiss_adobe_popup(page) -> bool:
    """Ferme la popup modale promo/inscription Adobe si elle est présente."""
    close_selectors = [
        "button[aria-label='Close']",
        "button[aria-label='Fermer']",
        "button[aria-label='close']",
        "[data-testid='close-button']",
        "sp-dialog button.spectrum-Dialog-closeButton",
        "div[role='dialog'] button[aria-label*='lose']",
        "div[role='dialog'] button[aria-label*='ermer']",
        # Bouton X générique dans un overlay/modal
        ".spectrum-Modal button.close",
        "button.spectrum-ClearButton",
        # Fallback : Escape pour fermer un dialog
    ]
    for sel in close_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible(timeout=1000):
                await el.click(timeout=3000)
                print(f"[Adobe] Popup fermee via {sel}")
                await page.wait_for_timeout(500)
                return True
        except Exception:
            continue
    # NE PAS utiliser Escape en fallback — Escape ferme le modal "Animer des personnages"
    # entier et redirige vers l'accueil, ce qui détruit la session de travail.
    return False


async def click_no_wait(page, selector: str) -> bool:
    """
    Clique immédiatement si l'élément existe (sans wait), sinon ne fait rien.
    Retourne True si un click a été tenté, False sinon.
    """
    try:
        return await page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                el.click();
                return true;
            }""",
            selector,
        )
    except Exception:
        return False
    
async def click_no_wait_fire_and_forget(page, selector: str):
    try:
        # on schedule et on n'attend pas le résultat
        asyncio.create_task(click_no_wait(page, selector))
    except Exception:
        pass

async def wait_for_any(page, selectors, state="visible", timeout=ACTION_TIMEOUT_MS):
    start = time.time()
    for _ in range(STEP_RETRIES):
        for sel in selectors:
            try:
                await page.locator(sel).first.wait_for(state=state, timeout=int(timeout/STEP_RETRIES))
                return sel
            except Exception:
                continue
    raise PlaywrightTimeout(f"Aucun sélecteur trouvé ({state}) dans {timeout} ms: {selectors}")

async def click_first(page, selectors, timeout=ACTION_TIMEOUT_MS):
    sel = await wait_for_any(page, selectors, state="visible", timeout=timeout)
    await page.locator(sel).first.click(timeout=timeout)
    return sel

async def ensure_no_negative_toast(page) -> bool:
    try:
        await page.locator('hz-toast[data-testid="hz-toast-negative"]').first.wait_for(state="visible", timeout=800)
        return False
    except Exception:
        return True

async def wait_until_idle_ui(page, timeout_ms=20_000):
    start = time.time()
    while True:
        try:
            busy = await page.evaluate("() => !!document.querySelector('[aria-busy=\"true\"], .is-loading, [data-loading=\"true\"]')")
            if not busy:
                return True
        except Exception:
            pass
        if (time.time()-start)*1000 > timeout_ms:
            raise PlaywrightTimeout("UI pas idle à temps")
        await page.wait_for_timeout(120)

async def set_puppet_scale(page, value=0.33):
    for _ in range(STEP_RETRIES):
        try:
            slider = page.locator('input[type="range"][aria-label*="Échelle"], input[type="range"][aria-label*="Scale"]').first
            await slider.wait_for(state="attached", timeout=ACTION_TIMEOUT_MS)
            await slider.evaluate(f"(el) => {{ el.value = {value}; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }}")
            await page.wait_for_timeout(80)
            current = await slider.evaluate("el => parseFloat(el.value)")
            if abs(current - value) < 0.02:
                return True
        except Exception:
            await page.wait_for_timeout(80)
    return False

async def drag_center_horiz(page, layer_sel="#puppet-ui-layer", delta_x=80):
    try:
        handle = page.locator(layer_sel).first
        await handle.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
        box = await handle.bounding_box()
        if not box:
            return False
        start_x = box["x"] + box["width"]/2
        start_y = box["y"] + box["height"]/2
        await page.mouse.move(start_x, start_y)
        await page.mouse.down()
        await page.mouse.move(start_x + delta_x, start_y, steps=8)
        await page.mouse.up()
        return True
    except Exception:
        return False

async def upload_file_to_input(page, input_selector, file_path):
    try:
        abs_path = os.path.abspath(file_path)
        loc = page.locator(input_selector).first
        await loc.wait_for(state="attached", timeout=ACTION_TIMEOUT_MS)
        await loc.set_input_files(abs_path)
        return True
    except Exception:
        return False

async def upload_audio_robust(page, audio_file_path: str) -> bool:
    abs_path = os.path.abspath(audio_file_path)
    if not (os.path.exists(abs_path) and os.path.getsize(abs_path) > 0):
        raise RuntimeError(f"Audio introuvable ou vide: {abs_path}")

    # Étape 1 : cliquer sur "parcourez vos fichiers" pour ouvrir le sélecteur de fichier natif.
    # Adobe Express n'écoute PAS les events DOM sur input[type=file] — il faut passer par le
    # file chooser natif déclenché par le lien visible "parcourez vos fichiers".
    browse_selectors = [
        "text=parcourez vos fichiers",
        "a:has-text('parcourez vos fichiers')",
        "span:has-text('parcourez vos fichiers')",
        "text=browse your files",
        "a:has-text('browse your files')",
    ]
    for sel in browse_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() == 0:
                continue
            print(f"[Adobe] audio: found '{sel}', expect_file_chooser...")
            async with page.expect_file_chooser(timeout=30_000) as fc_info:
                await el.click(timeout=10_000)
            fc = await fc_info.value
            await fc.set_files(abs_path)
            print(f"[Adobe] audio upload via file chooser OK")
            try:
                await wait_until_idle_ui(page, timeout_ms=AUDIO_PROC_TIMEOUT_MS)
            except Exception:
                pass
            return await ensure_no_negative_toast(page)
        except Exception as exc:
            print(f"[Adobe] audio file chooser echec ({sel!r}): {exc}")
            continue

    # Étape 2 : fallback set_input_files (moins fiable, Adobe peut ignorer)
    input_candidates = [
        'input[type="file"][accept*="mp3"]',
        'input[type="file"][accept*="wav"]',
        'input[type="file"][accept*="audio"]',
        'input[type="file"][accept*="mpeg"]',
        'input[type="file"]',
    ]
    for sel in input_candidates:
        if await upload_file_to_input(page, sel, abs_path):
            print(f"[Adobe] audio fallback set_input_files: {sel!r}")
            try:
                await wait_until_idle_ui(page, timeout_ms=AUDIO_PROC_TIMEOUT_MS)
            except Exception:
                pass
            return await ensure_no_negative_toast(page)

    return False

class AdobeRenderError(RuntimeError):
    """Erreur de rendu Adobe — déclenche un retry complet de la tâche."""
    pass

async def _check_oups_error(page) -> bool:
    """Retourne True si Adobe affiche le dialog 'Oups! Une erreur s'est produite'.
    Utilise JavaScript avec traversal des shadow roots pour fiabilité maximale."""
    try:
        found = await page.evaluate("""() => {
            function hasText(node, text) {
                if (!node) return false;
                if (node.nodeType === 3) {
                    const v = node.nodeValue || '';
                    if (v.includes(text)) return true;
                }
                const sr = node.shadowRoot;
                if (sr) {
                    for (const c of sr.childNodes) { if (hasText(c, text)) return true; }
                }
                for (const c of (node.childNodes || [])) {
                    if (hasText(c, text)) return true;
                }
                return false;
            }
            return hasText(document.body, 'Oups') || hasText(document.body, "erreur s'est produite");
        }""")
        return bool(found)
    except Exception:
        pass
    return False

async def wait_ready_to_download(page):
    """Attend que le bouton de téléchargement soit activé (non disabled)."""
    btn = page.locator('#downloadExportOption').first
    await btn.wait_for(state="visible", timeout=DOWNLOAD_TIMEOUT_MS)
    start = time.time()
    while True:
        try:
            disabled = await btn.evaluate("(el) => el.hasAttribute('disabled')")
            if not disabled:
                return True
        except Exception:
            pass
        # Détecter la popup d'erreur Adobe ("Une erreur s'est produite lors de la création")
        if await _check_oups_error(page):
            await asyncio.wait_for(page.screenshot(path=os.path.join(LOGS_DIR, f"snap_oups_{id(page)}.png"), full_page=False), timeout=8)
            raise AdobeRenderError("Adobe: erreur de création animation (popup Oups)")
        if (time.time() - start) * 1000 > DOWNLOAD_TIMEOUT_MS:
            try:
                snap = os.path.join(LOGS_DIR, f"snap_wait_dl_timeout_{id(page)}.png")
                await asyncio.wait_for(page.screenshot(path=snap, full_page=False), timeout=8)
            except Exception:
                pass
            raise PlaywrightTimeout("Bouton téléchargement jamais activé")
        await asyncio.sleep(0.5)

# --- Mapping

def _video_has_background(video_path: str) -> bool:
    """Vérifie que le premier frame de la vidéo n'est pas un fond uni (blanc/gris/noir).
    Un fond uni avec très peu de variance indique que l'image de fond n'a pas été appliquée."""
    try:
        tmp_frame = os.path.join(LOGS_DIR, f"_check_frame_{os.getpid()}.png")
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-y", "-i", video_path, "-frames:v", "1", "-q:v", "2", tmp_frame],
            timeout=15, check=True,
        )
        if not os.path.exists(tmp_frame) or os.path.getsize(tmp_frame) < 500:
            return False
        # Utilise ffprobe pour obtenir la luminosité moyenne et l'entropie du frame
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-f", "lavfi",
             "-i", f"movie={tmp_frame},signalstats", "-show_entries",
             "frame_tags=lavfi.signalstats.HUEAVG,lavfi.signalstats.SATAVG",
             "-of", "csv=p=0"],
            capture_output=True, text=True, timeout=10,
        )
        # Méthode alternative plus simple : vérifier la taille du PNG compressé
        # Un frame avec une vraie image de fond sera plus lourd qu'un fond uni
        frame_size = os.path.getsize(tmp_frame)
        os.remove(tmp_frame)
        # Un fond uni PNG compressé fait typiquement < 5KB, une vraie image > 15KB
        if frame_size < 5_000:
            print(f"⚠️  Frame suspecte: {frame_size} octets (trop petit, probable fond manquant)")
            return False
        return True
    except Exception as e:
        print(f"⚠️  Vérification frame échouée (non bloquant): {e}")
        return True  # En cas d'erreur de vérification, on ne bloque pas


def background_for_task(task: "Task") -> str:
    """
    Retourne le chemin de l'image de fond pour cette tâche.
    Priorité :
      1. task.image_right_path / task.image_left_path (fonds dynamiques par segment)
      2. IMAGE_RIGHT_PATH / IMAGE_LEFT_PATH (env, comportement classique)
    """
    is_martin = task.nom.strip().lower() == "mr martin"
    if is_martin:
        path = task.image_right_path or IMAGE_RIGHT_PATH
    else:
        path = task.image_left_path or IMAGE_LEFT_PATH
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        raise RuntimeError(f"Image de fond introuvable: {abs_path}")
    if os.path.getsize(abs_path) < 1024:
        raise RuntimeError(f"Image de fond trop petite / corrompue ({os.path.getsize(abs_path)} octets): {abs_path}")
    return path


def background_for_label(name_label: str) -> str:
    """Compat legacy — utilisé si pas de Task disponible."""
    path = IMAGE_RIGHT_PATH if name_label.strip().lower() == "mr martin" else IMAGE_LEFT_PATH
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        raise RuntimeError(f"Image de fond introuvable: {abs_path}")
    if os.path.getsize(abs_path) < 1024:
        raise RuntimeError(f"Image de fond trop petite / corrompue ({os.path.getsize(abs_path)} octets): {abs_path}")
    return path

# def puppet_selector_for_label(name_label: str, puppet_id_raw: str):
#     if name_label.strip().lower() == "mr martin":
#         return "img#Sticky\\ VQA\\.puppet-button"
#     return f"img#{_sanitize_css_id(puppet_id_raw)}"

def puppet_selectors_for_label(name_label: str, puppet_id_raw: str):
    # Sticky forcé pour Mr Martin
    if name_label.strip().lower() == "mr martin":
        puppet_id_raw = "Sticky VQA.puppet-button"

    # IMPORTANT: on évite #id (escaping) => on utilise [id="..."]
    return [
        f'button.qa-thumbnail-button:has(img[id="{puppet_id_raw}"])',
        f'img[id="{puppet_id_raw}"]',
        f'button.qa-thumbnail-button:has(img[aria-label="Sticky"])' if "Sticky" in puppet_id_raw else "",
        f'button.qa-thumbnail-button:has(img[alt="Sticky"])' if "Sticky" in puppet_id_raw else "",
        f'img[aria-label="Sticky"]' if "Sticky" in puppet_id_raw else "",
        f'img[alt="Sticky"]' if "Sticky" in puppet_id_raw else "",
    ]


# --- Une tâche sur une page dédiée
async def _run_task_on_page(context, task: Task):
    page = await context.new_page()
    page.set_default_timeout(ACTION_TIMEOUT_MS)

    # Auto-dismiss tout dialog natif (alert/confirm/prompt) pour éviter le freeze
    async def _handle_dialog(dialog):
        print(f"[Adobe] {task.segment_id} dialog natif: type={dialog.type} msg={dialog.message[:80]!r}")
        try:
            await dialog.accept()
        except Exception:
            pass
    page.on("dialog", _handle_dialog)

    # Log erreurs API Adobe pour diagnostics render
    async def _log_api_error(response):
        url = response.url
        if response.status >= 400 and ('adobe' in url.lower() or 'cc-api' in url.lower() or 'animate' in url.lower()):
            print(f"[Adobe] {task.segment_id} API HTTP {response.status}: {url[:120]}", flush=True)
            try:
                body = await asyncio.wait_for(response.text(), timeout=3)
                print(f"[Adobe] {task.segment_id} API body: {body[:300]}", flush=True)
            except Exception:
                pass
    page.on("response", _log_api_error)

    if VERBOSE_PAGE_LOGS:
        page.on("console", lambda msg: print(f"[{task.segment_id}] [console] {msg.type}: {msg.text}"))
        page.on("pageerror", lambda err: print(f"[{task.segment_id}] [pageerror]: {err}"))

    if BLOCK_TRACKING:
        await page.route("**/*", lambda route, request: route.abort() if any(r.search(request.url) for r in BLOCK_LIST) else route.continue_())

    try:
        t_task = time.time()
        await page.goto(URL_ADOBE, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        # Attendre que le splash Adobe disparaisse et que l'UI soit visible
        try:
            await page.wait_for_selector(
                "button.qa-thumbnail-button, [data-testid='sign-in-button'], [data-testid='login-button'], a:has-text('Se connecter'), a:has-text('Sign in')",
                state="attached", timeout=60_000
            )
        except Exception:
            pass
        await wait_until_idle_ui(page, timeout_ms=10_000)
        # await click_first(page, '[data-testid="sidebar-button-@hz/project-x:home"]', timeout=NAV_TIMEOUT_MS)

        # Cookies (best-effort)
        try:
            await click_first(page, [
                "button:has-text(‘Activer tout’)",
                "button:has-text(‘Tout accepter’)",
                "button:has-text(‘Accepter tout’)",
                "button:has-text(‘Accept all’)",
                "button:has-text(‘J’accepte’)",
                "button:has-text(\"J’accepte\")",
                "button[aria-label*=’Accepter’]",
            ], timeout=2_500)
        except Exception:
            pass

        # Ne fermer la popup que si les puppets ne sont PAS encore visibles.
        # _dismiss_adobe_popup utilise Escape en fallback, ce qui ferme le modal
        # "Animer des personnages" entier et redirige vers l'accueil.
        puppets_already_loaded = await page.locator("button.qa-thumbnail-button").count() > 0
        if not puppets_already_loaded:
            await _dismiss_adobe_popup(page)

        # # Sélection puppet
        # puppet_sel = puppet_selector_for_label(task.nom, task.personnage_id)
        # alternatives = [
        #     puppet_sel,
        #     f"img[alt*='{task.personnage_id.split()[0]}']",
        #     f"[data-testid='{_sanitize_css_id(task.personnage_id)}']",
        # ]
        # await click_first(page, alternatives, timeout=ACTION_TIMEOUT_MS)
        
        # Sélection puppet (robuste)
        selectors = [s for s in puppet_selectors_for_label(task.nom, task.personnage_id) if s]
        print(f"[Adobe] {task.segment_id} puppet_selectors={selectors[0]!r} count={await page.locator(selectors[0]).count() if selectors else 0}")

        # On attend qu’au moins une vignette soit chargée (sinon "visible" ne viendra jamais)
        try:
            await page.locator("button.qa-thumbnail-button img.qa-thumbnail-image").first.wait_for(
                state="attached", timeout=ACTION_TIMEOUT_MS
            )
        except Exception:
            pass

        print(f"[Adobe] {task.segment_id} calling wait_for_any...")
        # Compter toutes les vignettes disponibles pour le diagnostic
        all_thumbs = await page.locator("button.qa-thumbnail-button").count()
        print(f"[Adobe] {task.segment_id} vignettes totales chargées: {all_thumbs}")
        if all_thumbs == 0:
            await _screenshot(page, "no_puppets", task)
        sel = await wait_for_any(page, selectors, state="attached", timeout=ACTION_TIMEOUT_MS)
        print(f"[Adobe] {task.segment_id} puppet found: {sel!r}")

        loc = page.locator(sel).first
        try:
            await loc.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
        except Exception:
            pass

        # Le parent "button" clique mieux que l'img
        try:
            btn = loc.locator("xpath=ancestor::button[1]")
            if await btn.count():
                await btn.first.click(timeout=ACTION_TIMEOUT_MS)
            else:
                await loc.click(timeout=ACTION_TIMEOUT_MS)
        except Exception:
            # dernier recours: click forcé si Adobe "cache" visuellement mais c'est cliquable
            try:
                await loc.click(timeout=ACTION_TIMEOUT_MS, force=True)
            except Exception:
                await _screenshot(page, "puppet_click_fail", task)
                raise

        print(f"[Adobe] {task.segment_id} puppet clique OK")

        # Mise à l'échelle + placement
        print(f"[Adobe] {task.segment_id} set_puppet_scale...")
        await set_puppet_scale(page, 0.33)
        print(f"[Adobe] {task.segment_id} set_puppet_scale OK")
        delta_x = 80 if task.nom.strip().lower() == "mr martin" else -80
        print(f"[Adobe] {task.segment_id} drag_center_horiz delta_x={delta_x}...")
        await drag_center_horiz(page, delta_x=delta_x)
        print(f"[Adobe] {task.segment_id} drag_center_horiz OK")

        # Arrière-plan — retry robuste pour s'assurer que l'onglet est bien ouvert
        print(f"[Adobe] {task.segment_id} ouverture onglet Arriere-plan...")
        bg_tab_opened = False
        for _bg_attempt in range(3):
            try:
                await click_first(page, ['sp-tab[label="Arrière-plan"]', 'sp-tab:has-text("Arrière-plan")'], timeout=ACTION_TIMEOUT_MS)
                bg_tab_opened = True
                break
            except Exception:
                print(f"[Adobe] {task.segment_id} onglet Arriere-plan essai {_bg_attempt+1}/3 echoue, retry...")
                await page.wait_for_timeout(1000)
        if not bg_tab_opened:
            await _screenshot(page, "bg_tab_fail", task)
            raise RuntimeError("Impossible d'ouvrir l'onglet Arrière-plan après 3 essais")
        print(f"[Adobe] {task.segment_id} onglet Arriere-plan ouvert OK")

        image_path = background_for_task(task)
        print(f"[Adobe] {task.segment_id} upload fond: {image_path}...")

        # Upload du fond avec retry (le file input peut mettre du temps à apparaître)
        bg_uploaded = False
        for _up_attempt in range(3):
            if await upload_file_to_input(page, 'input[type="file"][accept*="image"]', image_path):
                bg_uploaded = True
                break
            print(f"[Adobe] {task.segment_id} upload fond essai {_up_attempt+1}/3 echoue, retry...")
            await page.wait_for_timeout(1500)
        if not bg_uploaded:
            await _screenshot(page, "bg_upload_fail", task)
            raise RuntimeError("Upload background échoué après 3 essais")
        print(f"[Adobe] {task.segment_id} upload fond OK, wait idle 25s...")

        await wait_until_idle_ui(page, timeout_ms=25_000)
        print(f"[Adobe] {task.segment_id} idle OK apres fond")

        # 9:16 (best-effort)
        try:
            await click_first(page, ['sp-tab[label="Taille"]', 'sp-tab:has-text("Taille")'], timeout=ACTION_TIMEOUT_MS)
            await click_first(page, ["span:has-text('9:16')", "sp-menu-item:has-text('9:16')"], timeout=ACTION_TIMEOUT_MS)
            print(f"[Adobe] {task.segment_id} ratio 9:16 OK")
        except Exception:
            print(f"[Adobe] {task.segment_id} ratio 9:16 skip (best-effort)")

        # Audio
        print(f"[Adobe] {task.segment_id} upload audio: {task.audio_path}...")
        if not await upload_audio_robust(page, task.audio_path):
            await _screenshot(page, "audio_upload_fail", task)
            raise RuntimeError("Upload audio échoué")
        print(f"[Adobe] {task.segment_id} upload audio OK", flush=True)
        print(f"[Adobe] {task.segment_id} DBG1 avant screenshot", flush=True)
        try:
            snap = os.path.join(LOGS_DIR, f"snap_after_audio_{task.segment_id}.png")
            await asyncio.wait_for(page.screenshot(path=snap, full_page=False), timeout=8)
            print(f"[Adobe] {task.segment_id} DBG2 screenshot OK: {snap}", flush=True)
        except asyncio.TimeoutError:
            print(f"[Adobe] {task.segment_id} DBG2 screenshot TIMEOUT", flush=True)
        except BaseException as _se:
            print(f"[Adobe] {task.segment_id} DBG2 screenshot EXC {type(_se).__name__}: {_se}", flush=True)
        print(f"[Adobe] {task.segment_id} DBG3 apres screenshot block", flush=True)
        if not await ensure_no_negative_toast(page):
            await _screenshot(page, "adobe_negative_toast", task)
            raise RuntimeError("Toast d'erreur Adobe")

        print(f"[Adobe] {task.segment_id} wait_ready_to_download...")
        try:
            await wait_ready_to_download(page)
        except Exception:
            await _screenshot(page, "download_btn_timeout", task)
            raise
        print(f"[Adobe] {task.segment_id} pret a telecharger")

        # Download
        filename = _safe_filename(f"{task.nom} - {task.genre} - {task.segment_id}.mp4")
        output_path = os.path.join(VIDEO_SEGMENTS_DIR, filename)
        try:
            print(f"[Adobe] {task.segment_id} click downloadExportOption...")
            async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                await page.locator('#downloadExportOption').click(timeout=ACTION_TIMEOUT_MS)
                print(f"[Adobe] {task.segment_id} click OK, attente download event...")
                # Si Adobe ouvre un dialog de confirmation, le valider
                try:
                    confirm_sel = await wait_for_any(
                        page,
                        [
                            "button:has-text('Télécharger')",
                            "button:has-text('Download')",
                            "sp-button:has-text('Télécharger')",
                            "sp-button:has-text('Download')",
                        ],
                        state="visible",
                        timeout=5_000,
                    )
                    print(f"[Adobe] {task.segment_id} dialog confirm found: {confirm_sel!r}, click...")
                    await page.locator(confirm_sel).first.click(timeout=ACTION_TIMEOUT_MS)
                    print(f"[Adobe] {task.segment_id} dialog confirm clique")
                except Exception:
                    print(f"[Adobe] {task.segment_id} pas de dialog confirm (OK)")
            dl = await dl_info.value
            print(f"[Adobe] {task.segment_id} download event recu, save_as...")
            await dl.save_as(output_path)
            print(f"[Adobe] {task.segment_id} save_as OK")
        except Exception:
            await _screenshot(page, "download_event_timeout", task)
            raise

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            await _screenshot(page, "download_zero", task)
            raise RuntimeError("Fichier export vide/introuvable")

        # Vérification du premier frame — détecter l'absence de fond
        if not _video_has_background(output_path):
            await _screenshot(page, "bg_missing_in_video", task)
            os.remove(output_path)
            raise RuntimeError(f"Vidéo générée SANS fond d'image — segment supprimé pour retry: {output_path}")

        print(f"[OK] Termine {task.segment_id} -> {output_path} ({time.time()-t_task:.2f}s)")
    finally:
        try:
            await page.close()
        except Exception:
            pass

# --- Helpers profil
def _prepare_profile(dest: str):
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    Path(os.path.dirname(dest)).mkdir(parents=True, exist_ok=True)
    shutil.copytree(BASE_PROFILE_PATH, dest, dirs_exist_ok=True)
    for lf_name in ["lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"]:
        lf = Path(dest) / lf_name
        try:
            lf.unlink(missing_ok=True)
        except Exception:
            pass

_BROWSER_LAUNCH_ARGS = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=InterestCohort,Translate,BackForwardCache",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
]

async def _launch_context(p, profile_path: str):
    return await p.chromium.launch_persistent_context(
        profile_path,
        executable_path=CHROME_PATH,
        headless=HEADLESS,
        slow_mo=SLOWMO_MS,
        accept_downloads=True,
        args=_BROWSER_LAUNCH_ARGS,
    )


# --- Orchestrateur principal (1 navigateur, N pages)
async def run_pool(tasks: List[Task], concurrency: int = 4, on_progress=None):
    # Nettoyer les profils numérotés orphelins d'un crash précédent
    _profiles_dir = Path(os.path.dirname(TEMP_PROFILE_PATH))
    _base_name = os.path.basename(TEMP_PROFILE_PATH)
    for _orphan in _profiles_dir.glob(f"{_base_name}_*"):
        shutil.rmtree(_orphan, ignore_errors=True)

    _prepare_profile(TEMP_PROFILE_PATH)

    async with async_playwright() as p:
        # ctx_holder permet de remplacer le contexte à chaud en cas de crash
        ctx_holder = {
            "ctx": await _launch_context(p, TEMP_PROFILE_PATH),
            "idx": 0,
        }
        ctx_lock = asyncio.Lock()

        async def _ensure_live_context(crashed_idx: int):
            """Recrée le navigateur si le contexte courant est celui qui a crashé.
            Protégé par un lock : si plusieurs tâches détectent le même crash,
            une seule effectue la relance, les autres attendent et repartent."""
            async with ctx_lock:
                if ctx_holder["idx"] != crashed_idx:
                    # Déjà recréé par une autre tâche concurrente
                    return
                print(f"[Adobe] ⚠️  Browser crash détecté (ctx #{crashed_idx}) — relance en cours...")
                try:
                    await ctx_holder["ctx"].close()
                except Exception:
                    pass
                new_idx = crashed_idx + 1
                new_profile = f"{TEMP_PROFILE_PATH}_{new_idx}"
                _prepare_profile(new_profile)
                ctx_holder["ctx"] = await _launch_context(p, new_profile)
                ctx_holder["idx"] = new_idx
                print(f"[Adobe] ✅ Browser relancé (ctx #{new_idx})")
                await asyncio.sleep(3)

        sem = asyncio.Semaphore(max(1, concurrency))
        _done = {"count": 0}

        async def guarded(task: Task):
            for attempt in range(1, MAX_RETRIES_TASK + 1):
                async with sem:
                    current_idx = ctx_holder["idx"]
                    try:
                        await _run_task_on_page(ctx_holder["ctx"], task)
                        _done["count"] += 1
                        if on_progress:
                            on_progress(_done["count"], len(tasks))
                        return
                    except AdobeRenderError as e:
                        print(f"[FAIL] {task.segment_id} essai {attempt}/{MAX_RETRIES_TASK} render error: {e}")
                        await asyncio.sleep(min(10*attempt, 60))
                    except Exception as e:
                        err_str = str(e)
                        # Crash navigateur : BrowserContext fermé inopinément
                        if ("Target page, context or browser has been closed" in err_str
                                or ("BrowserContext" in err_str and "closed" in err_str.lower())):
                            print(f"[FAIL] {task.segment_id} essai {attempt}/{MAX_RETRIES_TASK} : browser crash → relance")
                            await _ensure_live_context(current_idx)
                        elif "ERR_FAILED" in err_str or "ERR_CONNECTION" in err_str:
                            print(f"[FAIL] {task.segment_id} essai {attempt}/{MAX_RETRIES_TASK} : {e}")
                            await asyncio.sleep(min(20*attempt, 120))
                        else:
                            print(f"[FAIL] {task.segment_id} essai {attempt}/{MAX_RETRIES_TASK} : {e}")
                            await asyncio.sleep(min(2*attempt, 6))
            _done["count"] += 1
            if on_progress:
                on_progress(_done["count"], len(tasks))
            print(f"⛔ Abandon {task.segment_id} après {MAX_RETRIES_TASK} échecs")

        try:
            await asyncio.gather(*(guarded(t) for t in tasks))
        finally:
            try:
                await ctx_holder["ctx"].close()
            except Exception:
                pass
            # Nettoyage de tous les profils temporaires créés
            shutil.rmtree(TEMP_PROFILE_PATH, ignore_errors=True)
            for i in range(1, ctx_holder["idx"] + 1):
                shutil.rmtree(f"{TEMP_PROFILE_PATH}_{i}", ignore_errors=True)
