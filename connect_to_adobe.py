import asyncio
from playwright.async_api import async_playwright

PROFILE_PATH = r"C:\Users\n.amelaise\Desktop\mr martin\playwright-profile"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(PROFILE_PATH,executable_path=CHROME_PATH,  headless=False)
        page = await browser.new_page()
        await page.goto("https://express.adobe.com/")
        print("🟢 Connecte-toi à Adobe, puis ferme la fenêtre quand c’est bon.")
        await page.wait_for_timeout(300_000)  # 5 minutes max pour te connecter

asyncio.run(main())
