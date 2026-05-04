import asyncio
import logging
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("login_gemini")

BASE_PROFILE = os.getenv("BASE_PROFILE_PATH",
    r"C:\Users\n.amelaise\Desktop\martinV2\animate_video_with_adobe\playwright-profile")
CHROME = os.getenv("CHROME_PATH",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe")

for lf in ["lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"]:
    try:
        Path(BASE_PROFILE, lf).unlink(missing_ok=True)
    except Exception:
        pass


async def main():
    log.info("Profil : %s", BASE_PROFILE)
    log.info("Navigateur : %s", CHROME)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            BASE_PROFILE,
            executable_path=CHROME,
            headless=False,
            args=["--no-first-run", "--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://gemini.google.com", wait_until="domcontentloaded", timeout=30000)
        log.info("Connecte-toi a Gemini avec ton compte Google, puis ferme le navigateur.")
        await ctx.wait_for_event("close", timeout=0)
    log.info("Session Gemini sauvegardee dans le profil Playwright.")


asyncio.run(main())
