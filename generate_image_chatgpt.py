# generate_image_chatgpt.py — version robuste
import asyncio
import sys
import os
import time
import shutil
import re
import random
import base64
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv

load_dotenv()

# --- Config depuis .env
BASE_PROFILE_PATH = os.getenv("BASE_PROFILE_PATH")
CHROME_PATH = os.getenv("CHROME_PATH") 
BASE_DIR = os.getenv("BASE_DIR")
CHATGPT_URL = os.getenv("CHATGPT_URL", "https://chatgpt.com")
IMAGES_DIR = os.getenv("IMAGES_DIR", "images")
LEFT_IMG_DIR = os.getenv("LEFT_IMG_DIR", "images/left")
RIGHT_IMG_DIR = os.getenv("RIGHT_IMG_DIR", "images/right")
HEADLESS = os.getenv("HEADLESS", "true").strip().lower() in ("1", "true", "yes")
SLOWMO_MS = int(os.getenv("SLOWMO_MS", "0"))
TIMEOUT_MULT = float(os.getenv("TIMEOUT_MULT", "2.0"))
VERBOSE_PAGE_LOGS = os.getenv("VERBOSE_PAGE_LOGS", "false").strip().lower() in ("1","true","yes")

# --- Arguments CLI
script_text = sys.argv[1] if len(sys.argv) > 1 else ""

# --- Constantes robustesse
MAX_RETRIES_GLOBAL = 3
STEP_RETRIES = 3
NAV_TIMEOUT_MS = int(60_000 * TIMEOUT_MULT)
ACTION_TIMEOUT_MS = int(15_000 * TIMEOUT_MULT)
IMAGE_WAIT_MS = int(60_000 * TIMEOUT_MULT)  # 60s pour génération image

# Profils/temp
TEMP_PROFILE_PATH = os.path.join(BASE_DIR, "profiles/tmp_profile_chatgpt")
LOGS_DIR = os.path.join(BASE_DIR, "automation_logs")
Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)

# Créer les dossiers d'images
for img_dir in [IMAGES_DIR, LEFT_IMG_DIR, RIGHT_IMG_DIR]:
    Path(img_dir).mkdir(parents=True, exist_ok=True)

def build_background_prompt(script_text: str, templates_dir: str = "prompts/backgrounds") -> str:
    """
    Construit le prompt d'image en choisissant aléatoirement un template .txt dans `templates_dir`.
    """
    # Prompt par défaut (fallback)
    default_template = """
    Crée une image photo très réaliste et moderne, au format carré 1080x960, inspirée d'un script de conversation.

    Style visuel :
    - Ambiance réaliste et très moderne : éclairage naturel ou artificiel cohérent
    - Cadrage grand angle avec profondeur et espace
    - Décor riche et immersif (textures, reflets, ombres)
    - Couleurs vives et contrastées (non cartoon)

    Contexte :
    - Lieu logique par rapport au script (marché, parking, plage, bureau...)
    - Vaste et détaillé avec éléments de décor
    - Uniquement des traces d'activité humaine (pas de personnages visibles)

    📝 Extrait du script :
    {SCRIPT}
    """.strip()

    # Nettoyage du script
    cleaned = " ".join((script_text or "").split())
    
    # Récupération aléatoire d'un template .txt
    template_text = None
    chosen_name = "(default)"
    try:
        tdir = Path(templates_dir)
        txt_files = [p for p in tdir.glob("*.txt") if p.is_file()]
        if txt_files:
            chosen = random.choice(txt_files)
            template_text = chosen.read_text(encoding="utf-8").strip()
            chosen_name = chosen.name
    except Exception:
        pass

    if not template_text:
        template_text = default_template

    # Injection du script
    if "{SCRIPT}" in template_text or "{{SCRIPT}}" in template_text:
        final_prompt = (
            template_text
            .replace("{SCRIPT}", cleaned)
            .replace("{{SCRIPT}}", cleaned)
        )
    else:
        final_prompt = f"{template_text}\n\n{cleaned}"

    # Sauvegarde pour debug
    prompt_path = Path(IMAGES_DIR) / "prompt.txt"
    prompt_path.write_text(final_prompt, encoding="utf-8")

    print(f"✅ Prompt image généré (template: {chosen_name}) → {prompt_path}")
    return final_prompt

def _safe_filename(name: str) -> str:
    """Nettoie un nom de fichier"""
    name = re.sub(r'[\\/*?:"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

async def _click_first(page, selectors, timeout=ACTION_TIMEOUT_MS):
    """Essaie plusieurs sélecteurs, clique le premier visible."""
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
    """Attend qu'un des sélecteurs soit dans l'état donné"""
    for sel in selectors:
        try:
            await page.locator(sel).first.wait_for(state=state, timeout=timeout)
            return True, sel
        except Exception:
            continue
    return False, None

async def _screenshot(page, tag: str):
    """Prend une capture d'écran pour debug"""
    path = os.path.join(LOGS_DIR, f"snap_chatgpt_{tag}.png")
    try:
        await page.screenshot(path=path, full_page=True)
        print(f"🖼️  Screenshot: {path}")
    except Exception:
        pass

async def _type_prompt(page, prompt_text: str) -> bool:
    """Tape le prompt dans la zone de texte ChatGPT"""
    # Sélecteurs possibles pour la zone de texte
    text_selectors = [
        'textarea[placeholder*="Message"]',
        'textarea[data-id="root"]',
        'div[contenteditable="true"]',
        '#prompt-textarea',
        '[data-testid="message-input"]',
        'textarea',
    ]
    
    for sel in text_selectors:
        try:
            textarea = page.locator(sel).first
            await textarea.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
            await textarea.click()
            await textarea.fill(prompt_text)
            await page.wait_for_timeout(500)
            return True
        except Exception:
            continue
    return False

async def _send_message(page) -> bool:
    """Envoie le message via le bouton d'envoi"""
    send_selectors = [
        'button[data-testid="send-button"]',
        'button[aria-label*="Send"]',
        'button[aria-label*="Envoyer"]',
        'button:has(svg)',  # souvent le bouton send a juste une icône
        'button[type="submit"]',
    ]
    
    return (await _click_first(page, send_selectors))[0]

async def _wait_for_image_generation(page) -> bool:
    """Attend que la génération d'image soit terminée"""
    # On attend que les indicateurs de chargement disparaissent
    loading_selectors = [
        '[data-testid="loading"]',
        '.loading',
        '[aria-label*="Loading"]',
        'div:has-text("Generating")',
        'div:has-text("Génération")',
    ]
    
    # Attendre que le chargement commence (optionnel)
    await page.wait_for_timeout(2000)
    
    # Puis attendre qu'il se termine
    for sel in loading_selectors:
        try:
            await page.locator(sel).wait_for(state="hidden", timeout=IMAGE_WAIT_MS)
        except Exception:
            pass
    
    # Attendre qu'une image apparaisse
    image_selectors = [
        'img[alt*="Generated"]',
        'img[src*="dalle"]',
        'img[src*="generated"]',
        '.message img',
        'div[data-testid*="image"] img',
    ]
    
    return (await _wait_any(page, image_selectors, timeout=IMAGE_WAIT_MS))[0]

async def _download_generated_images(page) -> list:
    """Télécharge toutes les images générées"""
    downloaded_paths = []
    
    # Sélecteurs pour trouver les images générées
    image_selectors = [
        'img[src*="dalle"]',
        'img[src*="generated"]',
        'img[alt*="Generated"]',
        '.message img',
        'div[data-testid*="image"] img',
    ]
    
    for sel in image_selectors:
        try:
            images = await page.locator(sel).all()
            for i, img in enumerate(images):
                try:
                    # Récupérer l'URL de l'image
                    img_url = await img.get_attribute('src')
                    if not img_url or img_url.startswith('data:'):
                        continue
                    
                    # Télécharger l'image
                    filename = f"chatgpt_generated_{i}_{int(time.time())}.png"
                    img_path = os.path.join(IMAGES_DIR, filename)
                    
                    # Utiliser Playwright pour télécharger
                    response = await page.request.get(img_url)
                    if response.ok:
                        with open(img_path, 'wb') as f:
                            f.write(await response.body())
                        downloaded_paths.append(img_path)
                        print(f"✅ Image téléchargée: {img_path}")
                
                except Exception as e:
                    print(f"⚠️ Erreur téléchargement image {i}: {e}")
                    
        except Exception:
            continue
    
    return downloaded_paths

def split_background_to_tiktok_pairs(image_path: str) -> tuple:
    """
    Découpe l'image en deux plages 9:16 (gauche/droite).
    Nécessite PIL/Pillow
    """
    try:
        from PIL import Image
        
        img = Image.open(image_path)
        
        # Redimensionner si nécessaire
        expected_w, expected_h = 1080, 960
        if img.size != (expected_w, expected_h):
            print(f"⚠️ Redimension de l'image en {expected_w}x{expected_h}")
            img = img.resize((expected_w, expected_h))
        
        # Découper en deux
        left_img = img.crop((0, 0, 540, 960))
        right_img = img.crop((540, 0, 1080, 960))
        
        left_path = os.path.join(LEFT_IMG_DIR, "left_0.png")
        right_path = os.path.join(RIGHT_IMG_DIR, "right_0.png")
        
        left_img.save(left_path)
        right_img.save(right_path)
        
        print("✅ Deux images 9:16 générées (gauche/droite).")
        return left_path, right_path
        
    except ImportError:
        print("⚠️ PIL/Pillow non installé, impossible de découper l'image")
        return None, None
    except Exception as e:
        print(f"❌ Erreur lors du découpage: {e}")
        return None, None

async def run_once():
    """Fonction principale d'exécution"""
    # Profil temporaire
    if os.path.exists(TEMP_PROFILE_PATH):
        shutil.rmtree(TEMP_PROFILE_PATH, ignore_errors=True)
    Path(os.path.dirname(TEMP_PROFILE_PATH)).mkdir(parents=True, exist_ok=True)
    shutil.copytree(BASE_PROFILE_PATH, TEMP_PROFILE_PATH, dirs_exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            TEMP_PROFILE_PATH,
            executable_path=CHROME_PATH,
            headless=False,
            slow_mo=SLOWMO_MS,
        )
        page = await context.new_page()
        page.set_default_timeout(ACTION_TIMEOUT_MS)

        # Logs de debug
        if VERBOSE_PAGE_LOGS:
            page.on("console", lambda msg: print(f"🗒  console[{msg.type}] {msg.text}"))
            page.on("pageerror", lambda err: print(f"💥 pageerror: {err}"))

        try:
            # 1) Aller sur ChatGPT
            print("🌐 Navigation vers ChatGPT...")
            await page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_load_state("networkidle")

            # 2) Gestion des popups/cookies
            cookie_selectors = [
                "button:has-text('Accept')",
                "button:has-text('Accepter')",
                "button:has-text('OK')",
                "button[aria-label*='Accept']",
                ".cookie button",
            ]
            await _click_first(page, cookie_selectors, timeout=5000)

            # 3) Vérifier si on est connecté / gérer la connexion
            # Si on voit un bouton "Log in", il faut se connecter
            login_needed, _ = await _wait_any(page, [
                "button:has-text('Log in')",
                "a:has-text('Log in')",
                "button:has-text('Se connecter')",
            ], timeout=5000)
            
            if login_needed:
                print("⚠️ Connexion requise - assurez-vous d'être connecté dans votre profil Chrome")
                await _screenshot(page, "login_required")
                # Vous pouvez ajouter ici la logique de connexion automatique si nécessaire

            # 4) Construire et envoyer le prompt
            prompt_text = build_background_prompt(script_text)
            
            print("✏️ Saisie du prompt...")
            typed = await _type_prompt(page, prompt_text)
            if not typed:
                await _screenshot(page, "type_failed")
                raise RuntimeError("Impossible de taper le prompt dans ChatGPT")

            # 5) Envoyer le message
            print("📤 Envoi du message...")
            sent = await _send_message(page)
            if not sent:
                await _screenshot(page, "send_failed")
                raise RuntimeError("Impossible d'envoyer le message")

            # 6) Attendre la génération de l'image
            print("⏳ Attente de la génération d'image...")
            image_generated = await _wait_for_image_generation(page)
            if not image_generated:
                await _screenshot(page, "no_image_generated")
                raise RuntimeError("Aucune image générée ou timeout")

            # 7) Télécharger les images
            print("💾 Téléchargement des images...")
            downloaded_paths = await _download_generated_images(page)
            
            if not downloaded_paths:
                await _screenshot(page, "no_download")
                raise RuntimeError("Aucune image téléchargée")

            print(f"✅ {len(downloaded_paths)} image(s) téléchargée(s)")

            # 8) Découper la première image en deux parties 9:16
            if downloaded_paths:
                main_image = downloaded_paths[0]
                # Copier vers full_background.png pour compatibilité
                full_bg_path = os.path.join(IMAGES_DIR, "full_background.png")
                shutil.copy2(main_image, full_bg_path)
                
                left_path, right_path = split_background_to_tiktok_pairs(full_bg_path)
                if left_path and right_path:
                    print(f"✅ Images découpées: {left_path}, {right_path}")

            return downloaded_paths

        finally:
            try:
                await context.close()
            except Exception:
                pass
            shutil.rmtree(TEMP_PROFILE_PATH, ignore_errors=True)

# ---- Boucle globale de résilience ----
def main():
    for attempt in range(1, MAX_RETRIES_GLOBAL + 1):
        try:
            result = asyncio.run(run_once())
            print("🎉 Génération d'images terminée avec succès!")
            return result
        except Exception as e:
            print(f"❌ Tentative {attempt}/{MAX_RETRIES_GLOBAL} échouée : {e}")
            time.sleep(2 + attempt)
            if attempt == MAX_RETRIES_GLOBAL:
                print("💥 Échec final après toutes les tentatives")
                sys.exit(1)

if __name__ == "__main__":
    main()