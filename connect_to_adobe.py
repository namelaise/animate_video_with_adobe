import asyncio
import os
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# Charger .env
load_dotenv()

# Récupère BASE_DIR du .env
BASE_DIR = os.getenv("BASE_DIR")
if not BASE_DIR:
    raise RuntimeError("❌ BASE_DIR manquant dans le fichier .env")

PROFILE_PATH = os.path.join(BASE_DIR, "playwright-profile")
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_PATH,
            executable_path=CHROME_PATH,
            headless=False
        )
        page = await browser.new_page()
        await page.goto("https://express.adobe.com/")
        print("🟢 Connecte-toi à Adobe, puis ferme la fenêtre quand c’est bon.")
        await page.wait_for_timeout(300_000)  # 5 minutes max

asyncio.run(main())
