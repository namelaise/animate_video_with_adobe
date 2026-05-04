"""
Ouvre le Chromium de Playwright (via l'API Python) sur le profil playwright-profile
et attend que tu te connectes a Adobe Express.
Ferme le navigateur quand tu es connecte. Le script se terminera automatiquement.

IMPORTANT: utilise le Chromium de Playwright pour que les cookies soient
lisibles par l'automation (pas de chiffrement App-Bound de Chrome).
"""
import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

BASE_PROFILE = os.getenv("BASE_PROFILE_PATH", r"C:\Users\n.amelaise\Desktop\martinV2\animate_video_with_adobe\playwright-profile")
CHROME_PATH = os.getenv("CHROME_PATH", r"C:\Users\n.amelaise\AppData\Local\ms-playwright\chromium-1181\chrome-win\chrome.exe")
URL_ADOBE = os.getenv("URL_ADOBE", "https://new.express.adobe.com/tools/animate-from-audio")

# Supprimer les lockfiles
for lf_name in ["lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"]:
    lf = Path(BASE_PROFILE) / lf_name
    try:
        lf.unlink(missing_ok=True)
    except FileNotFoundError:
        pass


async def main():
    print(f"Ouverture Chromium Playwright sur: {BASE_PROFILE}", flush=True)
    print("Connecte-toi a Adobe Express, puis FERME le navigateur.", flush=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            BASE_PROFILE,
            executable_path=CHROME_PATH,
            headless=False,
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        # Ouvrir Adobe Express
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(URL_ADOBE, wait_until="domcontentloaded", timeout=30_000)

        print("Navigateur ouvert. Connecte-toi, puis FERME toutes les fenetres.", flush=True)
        print("(Le script detecte la fermeture automatiquement)", flush=True)

        # Attendre que le contexte se ferme (l'utilisateur ferme le navigateur)
        await context.wait_for_event("close", timeout=0)

    print("Session Adobe sauvegardee dans playwright-profile.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
