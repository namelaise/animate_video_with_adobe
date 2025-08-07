import asyncio
import sys
from playwright.async_api import async_playwright
import os
import random
import time
import shutil
from dotenv import load_dotenv

# --- Load .env
load_dotenv()

BASE_PROFILE_PATH = os.getenv("BASE_PROFILE_PATH")
CHROME_PATH = os.getenv("CHROME_PATH")
BASE_DIR = os.getenv("BASE_DIR")
URL_ADOBE = os.getenv("URL_ADOBE")
IMAGE_LEFT_PATH = os.getenv("IMAGE_LEFT_PATH")
IMAGE_RIGHT_PATH = os.getenv("IMAGE_RIGHT_PATH")

# --- Entrées du script
audio_path = sys.argv[1]
nom = sys.argv[2]
genre = sys.argv[3]
segment_id = sys.argv[4]
intervenant_index = sys.argv[5]
personnage_id = sys.argv[6]

TEMP_PROFILE_PATH = os.path.join(BASE_DIR, f"profiles/tmp_profile_{segment_id}_{intervenant_index}")
MAX_RETRIES = 3

async def main():
    shutil.copytree(BASE_PROFILE_PATH, TEMP_PROFILE_PATH, dirs_exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(TEMP_PROFILE_PATH, executable_path=CHROME_PATH, headless=False)
        page = await browser.new_page()

        try:
            await page.goto(URL_ADOBE)

            # 1. Cookies
            try:
                await page.click("button:has-text('Activer tout')", timeout=10000)
                print("✅ Cookies acceptés.")
            except:
                print("⚠️ Aucun bouton 'Activer tout' trouvé.")

            # 2. Sélection du personnage
            try:
                if nom == "Mr Martin":
                    personnage_selector = "img#Sticky\\ VQA\\.puppet-button"
                else:
                    safe_id = personnage_id.replace(" ", "\\ ").replace(".", "\\.")
                    personnage_selector = f"img#{safe_id}"

                await page.click(personnage_selector, timeout=10000)
                print(f"✅ Personnage sélectionné : {nom}")

                # 3. Réglage échelle à 33 %
                try:
                    slider = await page.query_selector('input[type="range"][aria-label*="Échelle du personnage"]')
                    await slider.evaluate("(el) => el.value = 0.33")
                    await slider.dispatch_event("input")
                    await slider.dispatch_event("change")
                    print("✅ Échelle réglée à 33%.")
                except:
                    print("❌ Impossible de régler l’échelle.")

                # 4. Déplacement du personnage (drag handle)
                await page.click('#puppet-ui-layer', timeout=10000)  # active sélection
                handle = await page.query_selector('#puppet-ui-layer')
                box = await handle.bounding_box()

                if box:
                    start_x = box["x"] + box["width"] / 2
                    start_y = box["y"] + box["height"] / 2
                    offset_x = 80 if nom == "Mr Martin" else -80

                    await page.mouse.move(start_x, start_y)
                    await page.mouse.down()
                    await page.mouse.move(start_x + offset_x, start_y, steps=10)
                    await page.mouse.up()
                    print(f"✅ Personnage déplacé horizontalement de {offset_x}px.")
                else:
                    print("❌ Bounding box introuvable.")
            except Exception as e:
                print(f"❌ Erreur sélection ou placement personnage : {e}")
                await browser.close()
                shutil.rmtree(TEMP_PROFILE_PATH, ignore_errors=True)
                return await main()

            # 5. Onglet Arrière-plan
            try:
                await page.click('sp-tab[label="Arrière-plan"]')
                print("✅ Onglet Arrière-plan ouvert.")
            except:
                print("❌ Erreur ouverture onglet Arrière-plan.")

            # 6. Ajout image de fond
            try:
                input_file = await page.query_selector('input[type="file"][accept*="image"]')
                image_path = IMAGE_RIGHT_PATH if nom == "Mr Martin" else IMAGE_LEFT_PATH
                await input_file.set_input_files(image_path)
                await page.wait_for_timeout(10000)
                print("✅ Image de fond chargée.")
            except:
                print("❌ Erreur chargement image.")

            # 7. Format 9:16
            try:
                await page.click('sp-tab[label="Taille"]')
                await page.click("span:has-text('9:16')", timeout=10000)
                print("✅ Format 9:16 sélectionné.")
            except Exception as e:
                print(f"❌ Erreur sélection format : {e}")

            # 8. Ajout de l’audio
            try:
                input_audio = await page.query_selector('input[type="file"][accept*="mp3"], input[type="file"][accept*="wav"]')
                await input_audio.set_input_files(audio_path)
                print("✅ Audio ajouté.")
            except:
                print("❌ Échec ajout audio.")

            await page.wait_for_timeout(10000)

            # 9. Vérification toast erreur
            try:
                await page.wait_for_selector('hz-toast[data-testid="hz-toast-negative"]', timeout=10000)
                print("❌ Erreur Adobe détectée, relance...")
                await browser.close()
                shutil.rmtree(TEMP_PROFILE_PATH, ignore_errors=True)
                return await main()
            except:
                print("✅ Aucun message d'erreur détecté.")

            # 10. Téléchargement
            try:
                print("⏳ Attente génération vidéo...")
                await page.wait_for_selector('#downloadExportOption:visible', timeout=480000)
                async with page.expect_download() as download_info:
                    await page.click('#downloadExportOption')
                download = await download_info.value

                filename = f"{nom} - {genre} - {segment_id}.mp4"
                output_path = os.path.join(BASE_DIR, filename)
                await download.save_as(output_path)

                print("✅ Vidéo téléchargée.")
            except Exception as e:
                print(f"❌ Erreur téléchargement : {e}")

        finally:
            await browser.close()
            shutil.rmtree(TEMP_PROFILE_PATH, ignore_errors=True)

# 🔁 Re-lance automatique si crash
for attempt in range(MAX_RETRIES):
    try:
        asyncio.run(main())
        break
    except Exception as e:
        print(f"❌ Tentative {attempt + 1} échouée : {e}")
        time.sleep(3)
