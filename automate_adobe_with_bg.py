# automate_adobe.py — version robuste
import asyncio
import sys
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
import os
import random
import time
import shutil
import re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- Config depuis .env
BASE_PROFILE_PATH = os.getenv("BASE_PROFILE_PATH")
CHROME_PATH = os.getenv("CHROME_PATH")
BASE_DIR = os.getenv("BASE_DIR")
URL_ADOBE = os.getenv("URL_ADOBE")
IMAGE_LEFT_PATH = os.getenv("IMAGE_LEFT_PATH")
IMAGE_RIGHT_PATH = os.getenv("IMAGE_RIGHT_PATH")
HEADLESS = os.getenv("HEADLESS", "true").strip().lower() in ("1", "true", "yes")
SLOWMO_MS = int(os.getenv("SLOWMO_MS", "0"))
TIMEOUT_MULT = float(os.getenv("TIMEOUT_MULT", "2.0"))  # pour rallonger facilement
VERBOSE_PAGE_LOGS = os.getenv("VERBOSE_PAGE_LOGS", "false").strip().lower() in ("1","true","yes")


# --- Entrées CLI (ne pas changer)
audio_path = sys.argv[1]
nom = sys.argv[2]                 # "Mr Martin" ou "homme 1" / "femme 2" ...
genre = sys.argv[3]               # "homme" / "femme"
segment_id = sys.argv[4]          # index segment
intervenant_index = sys.argv[5]   # hint
personnage_id = sys.argv[6]       # ex "Sticky VQA.puppet-button" ou autre

# --- Constantes robustesse
MAX_RETRIES_GLOBAL = 10
STEP_RETRIES = 10
DOWNLOAD_TIMEOUT_MS = int(600_000 * TIMEOUT_MULT)   
NAV_TIMEOUT_MS = int(60_000 * TIMEOUT_MULT)
ACTION_TIMEOUT_MS = int(15_000 * TIMEOUT_MULT)
UPLOAD_WAIT_MS = int(25_000 * TIMEOUT_MULT)  # 25s par défaut

# Dossier où télécharger les vidéos (par défaut: <BASE_DIR>/video_segments)
VIDEO_SEGMENTS_DIR = os.getenv("VIDEO_SEGMENTS_DIR", os.path.join(BASE_DIR or "", "video_segments"))

# Profils/temp
TEMP_PROFILE_PATH = os.path.join(BASE_DIR, f"profiles/tmp_profile_{segment_id}_{intervenant_index}")
LOGS_DIR = os.path.join(BASE_DIR, "automation_logs")
Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)

# NEW: créer le dossier des vidéos si besoin
Path(VIDEO_SEGMENTS_DIR).mkdir(parents=True, exist_ok=True)

def _sanitize_css_id(raw: str) -> str:
    # échappe espaces et points pour selecteur CSS #id
    return raw.replace(" ", "\\ ").replace(".", "\\.")

def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

async def _click_first(page, selectors, timeout=ACTION_TIMEOUT_MS):
    """
    Essaie plusieurs sélecteurs, clique le premier visible.
    """
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click(timeout=timeout)
            return True, sel
        except Exception:
            continue
    return False, None

async def _wait_any(page, selectors, state="visible", timeout=ACTION_TIMEOUT_MS):
    for sel in selectors:
        try:
            await page.locator(sel).first.wait_for(state=state, timeout=timeout)
            return True, sel
        except Exception:
            continue
    return False, None

async def _set_puppet_scale(page, value=0.33):
    # Essai JS direct
    for _ in range(STEP_RETRIES):
        try:
            slider = page.locator('input[type="range"][aria-label*="Échelle"]').first
            await slider.wait_for(state="attached", timeout=ACTION_TIMEOUT_MS)
            await slider.evaluate(f"(el) => {{ el.value = {value}; el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }}")
            await page.wait_for_timeout(400)
            # simple check: value bien appliquée ?
            current = await slider.evaluate("el => parseFloat(el.value)")
            if abs(current - value) < 0.02:
                return True
        except Exception:
            await page.wait_for_timeout(300)
    return False

async def _drag_center_horiz(page, layer_sel="#puppet-ui-layer", delta_x=80):
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
        await page.mouse.move(start_x + delta_x, start_y, steps=12)
        await page.mouse.up()
        return True
    except Exception:
        return False

async def _upload_file_to_input(page, input_selector, file_path):
    try:
        abs_path = os.path.abspath(file_path)
        loc = page.locator(input_selector).first
        await loc.wait_for(state="attached", timeout=ACTION_TIMEOUT_MS)
        await loc.set_input_files(abs_path)
        await page.wait_for_timeout(600)  # micro souffle
        return True
    except Exception:
        return False


async def _upload_audio_robust(page, audio_file_path: str) -> bool:
    """Try multiple strategies to upload an audio file (MP3/WAV) on Adobe Express."""
    abs_path = os.path.abspath(audio_file_path)
    if not (os.path.exists(abs_path) and os.path.getsize(abs_path) > 0):
        raise RuntimeError(f"Fichier audio introuvable ou vide: {abs_path}")

    # 1) Tentatives directes sur divers <input type="file">, même cachés
    input_candidates = [
        # accept explicite
        'input[type="file"][accept*="mp3"]',
        'input[type="file"][accept*="wav"]',
        # accept génériques
        'input[type="file"][accept*="audio"]',
        'input[type="file"][accept*="mpeg"]',
        'input[type="file"]',
    ]
    for sel in input_candidates:
        ok = await _upload_file_to_input(page, sel, abs_path)
        if ok:
            # petit wait pour laisser l’app avaler le fichier
            await page.wait_for_timeout(UPLOAD_WAIT_MS)
            if await _ensure_no_negative_toast(page):
                return True

    # 2) Fallback : file chooser natif (le site clique un bouton qui ouvre l’explorateur)
    #    On tente une liste de boutons plausibles.
    chooser_triggers = [
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
    for trigger in chooser_triggers:
        try:
            async with page.expect_file_chooser(timeout=ACTION_TIMEOUT_MS) as fc_info:
                await page.locator(trigger).first.click(timeout=ACTION_TIMEOUT_MS)
            file_chooser = await fc_info.value
            await file_chooser.set_files(abs_path)
            await page.wait_for_timeout(UPLOAD_WAIT_MS)
            if await _ensure_no_negative_toast(page):
                return True
        except Exception:
            continue

    # 3) Dernière chance : certains UIs n’attachent l’input qu’après ouverture d’un onglet "Audio"
    try:
        opened_audio_tab, _ = await _click_first(
            page,
            ['sp-tab[label="Audio"]', 'sp-tab:has-text("Audio")', "button:has-text('Audio')"],
            timeout=ACTION_TIMEOUT_MS
        )
        if opened_audio_tab:
            for sel in input_candidates:
                ok = await _upload_file_to_input(page, sel, abs_path)
                if ok:
                    await page.wait_for_timeout(UPLOAD_WAIT_MS)
                    if await _ensure_no_negative_toast(page):
                        return True
    except Exception:
        pass

    # Échec
    return False


async def _ensure_no_negative_toast(page):
    # si toast d'erreur Adobe, on renvoie False
    try:
        await page.locator('hz-toast[data-testid="hz-toast-negative"]').first.wait_for(state="visible", timeout=6_000)
        return False
    except Exception:
        return True

async def _screenshot(page, tag: str):
    path = os.path.join(LOGS_DIR, f"snap_{segment_id}_{intervenant_index}_{tag}.png")
    try:
        await page.screenshot(path=path, full_page=True)
        print(f"🖼️  Screenshot: {path}")
    except Exception:
        pass

async def _wait_ready_to_download(page):
    """
    Attend que le bouton de download soit visible ET cliquable.
    """
    btn = page.locator('#downloadExportOption').first
    await btn.wait_for(state="visible", timeout=DOWNLOAD_TIMEOUT_MS)
    # boucle jusqu'à non-disabled
    start = time.time()
    while True:
        try:
            disabled = await btn.evaluate("(el) => el.hasAttribute('disabled')")
            if not disabled:
                return True
        except Exception:
            pass
        if (time.time() - start) * 1000 > DOWNLOAD_TIMEOUT_MS:
            raise PlaywrightTimeout("Download button never enabled in time")
        await page.wait_for_timeout(800)

def _background_for_label(name_label: str) -> str:
    # Mr Martin à droite, autres à gauche (comme ton script)
    return IMAGE_RIGHT_PATH if name_label.strip().lower() == "mr martin" else IMAGE_LEFT_PATH

def _puppet_selector_for_label(name_label: str, puppet_id_raw: str):
    if name_label.strip().lower() == "mr martin":
        return "img#Sticky\\ VQA\\.puppet-button"
    return f"img#{_sanitize_css_id(puppet_id_raw)}"

async def run_once():
    # profil temporaire
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
            accept_downloads=True
        )
        page = await context.new_page()
        page.set_default_timeout(ACTION_TIMEOUT_MS)

       # --- logs utiles (désactivés par défaut)
        if VERBOSE_PAGE_LOGS:
            page.on("console", lambda msg: print(f"🗒  console[{msg.type}] {msg.text}"))
            page.on("pageerror", lambda err: print(f"💥 pageerror: {err}"))


        try:
            # 1) Aller sur la page
            await page.goto(URL_ADOBE, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_load_state("networkidle")

            # 2) Consentement cookies (plusieurs variantes)
            cookie_selectors = [
                "button:has-text('Activer tout')",
                "button:has-text('Tout accepter')",
                "button:has-text('Accepter tout')",
                "button:has-text('Accept all')",
                "button:has-text('J’accepte')",
                "button:has-text(\"J'accepte\")",
                "button[aria-label*='Accepter']",
            ]
            ok, used = await _click_first(page, cookie_selectors, timeout=6_000)
            print("✅ Cookies acceptés." if ok else "ℹ️ Aucun bouton cookies pressant.")

            # 3) Sélection du personnage
            puppet_sel = _puppet_selector_for_label(nom, personnage_id)
            # Fallbacks plausibles si l'id change
            puppet_alternatives = [
                puppet_sel,
                f"img[alt*='{personnage_id.split()[0]}']",
                f"[data-testid='{_sanitize_css_id(personnage_id)}']",
            ]
            ok, used = await _click_first(page, puppet_alternatives, timeout=ACTION_TIMEOUT_MS)
            if not ok:
                await _screenshot(page, "no_puppet")
                raise RuntimeError(f"Personnage introuvable: {personnage_id}")

            print(f"✅ Personnage sélectionné : {nom} ({personnage_id})")

            # 4) Réglage échelle 33%
            scaled = await _set_puppet_scale(page, 0.33)
            print("✅ Échelle réglée à 33%." if scaled else "⚠️ Impossible d’ajuster l’échelle (continuons).")

            # 5) Déplacement horizontal (Mr Martin à droite, autres à gauche)
            delta_x = 80 if nom.strip().lower() == "mr martin" else -80
            moved = await _drag_center_horiz(page, delta_x=delta_x)
            print(f"✅ Personnage déplacé de {delta_x}px." if moved else "⚠️ Déplacement non réalisé (continuons).")

            # 6) Onglet Arrière-plan -> charger image
            opened_bg_tab, _ = await _click_first(page, ['sp-tab[label="Arrière-plan"]', 'sp-tab:has-text("Arrière-plan")'], timeout=ACTION_TIMEOUT_MS)
            print("✅ Onglet Arrière-plan ouvert." if opened_bg_tab else "ℹ️ Onglet Arrière-plan déjà actif (ou introuvable).")

            image_path = _background_for_label(nom)
            img_ok = await _upload_file_to_input(page, 'input[type="file"][accept*="image"]', image_path)
            if not img_ok:
                await _screenshot(page, "bg_upload_fail")
                raise RuntimeError("Échec upload image de fond.")
            await page.wait_for_timeout(UPLOAD_WAIT_MS)
            print("✅ Image de fond chargée.")

            # 7) Format 9:16
            opened_size_tab, _ = await _click_first(page, ['sp-tab[label="Taille"]', 'sp-tab:has-text("Taille")'], timeout=ACTION_TIMEOUT_MS)
            if not opened_size_tab:
                print("ℹ️ Onglet Taille déjà actif (ou introuvable).")
            size_ok, _ = await _click_first(page, ["span:has-text('9:16')", "sp-menu-item:has-text('9:16')"], timeout=ACTION_TIMEOUT_MS)
            print("✅ Format 9:16 sélectionné." if size_ok else "⚠️ Impossible de sélectionner 9:16 (continuons).")

            # 8) Ajout de l’audio (robuste)
            audio_ok = await _upload_audio_robust(page, audio_path)
            if not audio_ok:
                await _screenshot(page, "audio_upload_fail")
                raise RuntimeError("Échec upload audio (toutes stratégies).")
            print("✅ Audio ajouté.")


            # 9) Vérifier absence d'erreur Adobe (toast)
            if not await _ensure_no_negative_toast(page):
                await _screenshot(page, "adobe_negative_toast")
                raise RuntimeError("Adobe a remonté une erreur (toast négatif).")

            # 10) Attendre prêt au téléchargement
            print("⏳ Génération vidéo… (attente bouton de téléchargement)")
            await _wait_ready_to_download(page)

            # 11) Télécharger
            filename = _safe_filename(f"{nom} - {genre} - {segment_id}.mp4")
            output_path = os.path.join(VIDEO_SEGMENTS_DIR, filename)

            async with page.expect_download() as download_info:
                await page.locator('#downloadExportOption').click()
            dl = await download_info.value
            await dl.save_as(output_path)

            # Validation disque
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                await _screenshot(page, "download_zero")
                raise RuntimeError("Téléchargement terminé mais fichier vide/introuvable.")

            print(f"✅ Vidéo téléchargée : {output_path}")

        finally:
            try:
                await context.close()
            except Exception:
                pass
            shutil.rmtree(TEMP_PROFILE_PATH, ignore_errors=True)

# ---- Boucle globale de résilience ----
for attempt in range(1, MAX_RETRIES_GLOBAL + 1):
    try:
        asyncio.run(run_once())
        break
    except Exception as e:
        print(f"❌ Tentative {attempt}/{MAX_RETRIES_GLOBAL} échouée : {e}")
        # petites pauses + screenshots côté page déjà faits si possible
        time.sleep(2 + attempt)
        if attempt == MAX_RETRIES_GLOBAL:
            # échec final -> on remonte un code erreur non nul
            sys.exit(1)
