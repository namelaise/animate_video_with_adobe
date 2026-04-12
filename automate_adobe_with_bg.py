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
import time
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv

load_dotenv()

# --- Config depuis .env
BASE_PROFILE_PATH = os.getenv("BASE_PROFILE_PATH")
CHROME_PATH = os.getenv("CHROME_PATH")
BASE_DIR = os.getenv("BASE_DIR", ".")
URL_ADOBE = os.getenv("URL_ADOBE")
IMAGE_LEFT_PATH = os.getenv("IMAGE_LEFT_PATH")
IMAGE_RIGHT_PATH = os.getenv("IMAGE_RIGHT_PATH")
HEADLESS = os.getenv("HEADLESS", "1").strip().lower() in ("1", "true", "yes")
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
        await page.screenshot(path=path, full_page=True)
        print(f"🖼️  Screenshot: {path}")
    except Exception:
        pass
    
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

    input_candidates = [
        'input[type="file"][accept*="mp3"]',
        'input[type="file"][accept*="wav"]',
        'input[type="file"][accept*="audio"]',
        'input[type="file"][accept*="mpeg"]',
        'input[type="file"]',
    ]
    for sel in input_candidates:
        if await upload_file_to_input(page, sel, abs_path):
            try:
                await wait_until_idle_ui(page, timeout_ms=AUDIO_PROC_TIMEOUT_MS)
            except Exception:
                pass
            return await ensure_no_negative_toast(page)

    triggers = [
        "button:has-text('Ajouter une voix')",
        "button:has-text('Ajouter un audio')",
        "button:has-text('Ajouter une piste audio')",
        "button:has-text('Importer')",
        "button:has-text('Upload')",
        "sp-button:has-text('Audio')",
        "sp-action-button:has-text('Audio')",
        "[data-testid*='audio'][role='button']",
        "button[aria-label*='audio']",
    ]
    for trigger in triggers:
        try:
            async with page.expect_file_chooser(timeout=ACTION_TIMEOUT_MS) as fc_info:
                await page.locator(trigger).first.click(timeout=ACTION_TIMEOUT_MS)
            fc = await fc_info.value
            await fc.set_files(abs_path)
            await wait_until_idle_ui(page, timeout_ms=AUDIO_PROC_TIMEOUT_MS)
            return await ensure_no_negative_toast(page)
        except Exception:
            continue

    try:
        await click_first(page, ['sp-tab[label="Audio"]', 'sp-tab:has-text("Audio")', "button:has-text('Audio')"], timeout=ACTION_TIMEOUT_MS)
        for sel in input_candidates:
            if await upload_file_to_input(page, sel, abs_path):
                await wait_until_idle_ui(page, timeout_ms=AUDIO_PROC_TIMEOUT_MS)
                return await ensure_no_negative_toast(page)
    except Exception:
        pass

    return False

async def wait_ready_to_download(page):
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
        if (time.time() - start) * 1000 > DOWNLOAD_TIMEOUT_MS:
            raise PlaywrightTimeout("Bouton téléchargement jamais activé")
        await asyncio.sleep(0.2)

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


def background_for_label(name_label: str) -> str:
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

    if VERBOSE_PAGE_LOGS:
        page.on("console", lambda msg: print(f"[{task.segment_id}] 🗒 {msg.type}: {msg.text}"))
        page.on("pageerror", lambda err: print(f"[{task.segment_id}] 💥 pageerror: {err}"))

    if BLOCK_TRACKING:
        await page.route("**/*", lambda route, request: route.abort() if any(r.search(request.url) for r in BLOCK_LIST) else route.continue_())

    try:
        t_task = time.time()
        await page.goto(URL_ADOBE, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        # await click_first(page, '[data-testid="sidebar-button-@hz/project-x:home"]', timeout=NAV_TIMEOUT_MS)
        await wait_until_idle_ui(page, timeout_ms=10_000)

        # Cookies (best-effort)
        try:
            await click_first(page, [
                "button:has-text('Activer tout')",
                "button:has-text('Tout accepter')",
                "button:has-text('Accepter tout')",
                "button:has-text('Accept all')",
                "button:has-text('J’accepte')",
                "button:has-text(\"J'accepte\")",
                "button[aria-label*='Accepter']",
            ], timeout=2_500)
        except Exception:
            pass

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

        # On attend qu’au moins une vignette soit chargée (sinon "visible" ne viendra jamais)
        try:
            await page.locator("button.qa-thumbnail-button img.qa-thumbnail-image").first.wait_for(
                state="attached", timeout=ACTION_TIMEOUT_MS
            )
        except Exception:
            pass

        sel = await wait_for_any(page, selectors, state="attached", timeout=ACTION_TIMEOUT_MS)

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
            # dernier recours: click forcé si Adobe “cache” visuellement mais c'est cliquable
            try:
                await loc.click(timeout=ACTION_TIMEOUT_MS, force=True)
            except Exception:
                await _screenshot(page, "puppet_click_fail", task)
                raise


        # Mise à l'échelle + placement
        await set_puppet_scale(page, 0.33)
        delta_x = 80 if task.nom.strip().lower() == "mr martin" else -80
        await drag_center_horiz(page, delta_x=delta_x)

        # Arrière-plan — retry robuste pour s'assurer que l'onglet est bien ouvert
        bg_tab_opened = False
        for _bg_attempt in range(3):
            try:
                await click_first(page, ['sp-tab[label="Arrière-plan"]', 'sp-tab:has-text("Arrière-plan")'], timeout=ACTION_TIMEOUT_MS)
                bg_tab_opened = True
                break
            except Exception:
                print(f"⚠️  {task.segment_id} onglet Arrière-plan essai {_bg_attempt+1}/3 échoué, retry…")
                await page.wait_for_timeout(1000)
        if not bg_tab_opened:
            await _screenshot(page, "bg_tab_fail", task)
            raise RuntimeError("Impossible d'ouvrir l'onglet Arrière-plan après 3 essais")

        image_path = background_for_label(task.nom)

        # Upload du fond avec retry (le file input peut mettre du temps à apparaître)
        bg_uploaded = False
        for _up_attempt in range(3):
            if await upload_file_to_input(page, 'input[type="file"][accept*="image"]', image_path):
                bg_uploaded = True
                break
            print(f"⚠️  {task.segment_id} upload fond essai {_up_attempt+1}/3 échoué, retry…")
            await page.wait_for_timeout(1500)
        if not bg_uploaded:
            await _screenshot(page, "bg_upload_fail", task)
            raise RuntimeError("Upload background échoué après 3 essais")

        await wait_until_idle_ui(page, timeout_ms=25_000)

        # 9:16 (best-effort)
        try:
            await click_first(page, ['sp-tab[label="Taille"]', 'sp-tab:has-text("Taille")'], timeout=ACTION_TIMEOUT_MS)
            await click_first(page, ["span:has-text('9:16')", "sp-menu-item:has-text('9:16')"], timeout=ACTION_TIMEOUT_MS)
        except Exception:
            pass

        # Audio
        if not await upload_audio_robust(page, task.audio_path):
            await _screenshot(page, "audio_upload_fail", task)
            raise RuntimeError("Upload audio échoué")
        if not await ensure_no_negative_toast(page):
            await _screenshot(page, "adobe_negative_toast", task)
            raise RuntimeError("Toast d'erreur Adobe")

        await wait_ready_to_download(page)

        # Download
        filename = _safe_filename(f"{task.nom} - {task.genre} - {task.segment_id}.mp4")
        output_path = os.path.join(VIDEO_SEGMENTS_DIR, filename)
        async with page.expect_download() as dl_info:
            await page.locator('#downloadExportOption').click()
        dl = await dl_info.value
        await dl.save_as(output_path)

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            await _screenshot(page, "download_zero", task)
            raise RuntimeError("Fichier export vide/introuvable")

        # Vérification du premier frame — détecter l'absence de fond
        if not _video_has_background(output_path):
            await _screenshot(page, "bg_missing_in_video", task)
            os.remove(output_path)
            raise RuntimeError(f"Vidéo générée SANS fond d'image — segment supprimé pour retry: {output_path}")

        print(f"✅ Terminé {task.segment_id} → {output_path} (⏱ {time.time()-t_task:.2f}s)")
    finally:
        try:
            await page.close()
        except Exception:
            pass

# --- Orchestrateur principal (1 navigateur, N pages)
async def run_pool(tasks: List[Task], concurrency: int = 4):
    if os.path.exists(TEMP_PROFILE_PATH):
        shutil.rmtree(TEMP_PROFILE_PATH, ignore_errors=True)
    Path(os.path.dirname(TEMP_PROFILE_PATH)).mkdir(parents=True, exist_ok=True)
    shutil.copytree(BASE_PROFILE_PATH, TEMP_PROFILE_PATH, dirs_exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            TEMP_PROFILE_PATH,
            executable_path=CHROME_PATH,
            headless=HEADLESS,
            slow_mo=SLOWMO_MS,
            accept_downloads=True,
            args=[
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-features=InterestCohort,Translate,BackForwardCache",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
            ],
        )
        sem = asyncio.Semaphore(max(1, concurrency))

        async def guarded(task: Task):
            for attempt in range(1, MAX_RETRIES_TASK + 1):
                async with sem:
                    try:
                        await _run_task_on_page(context, task)
                        return
                    except Exception as e:
                        print(f"❌ {task.segment_id} essai {attempt}/{MAX_RETRIES_TASK} : {e}")
                        await asyncio.sleep(min(2*attempt, 6))
            print(f"⛔ Abandon {task.segment_id} après {MAX_RETRIES_TASK} échecs")

        try:
            await asyncio.gather(*(guarded(t) for t in tasks))
        finally:
            try:
                await context.close()
            except Exception:
                pass
            shutil.rmtree(TEMP_PROFILE_PATH, ignore_errors=True)
