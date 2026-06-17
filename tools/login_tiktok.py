"""
login_tiktok.py — Ouvre Chromium Playwright pour se connecter à TikTok et sauvegarder
la session dans un profil dédié au compte (cookies, localStorage).

Usage :
    python tools/login_tiktok.py                       # profil par défaut (compte actif)
    python tools/login_tiktok.py --account-id acc_2    # profil pour acc_2

Le profil est stocké dans : playwright-profiles/tiktok_<account_id>/
Connecte-toi à TikTok, puis FERME le navigateur — la session est sauvegardée.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "tiktok"))

load_dotenv(BASE_DIR / ".env")

CHROME_PATH = os.getenv(
    "CHROME_PATH",
    r"C:\Users\n.amelaise\AppData\Local\ms-playwright\chromium-1181\chrome-win\chrome.exe",
)


def _resolve_account_id(arg_id: str | None) -> str:
    if arg_id:
        return arg_id
    try:
        from tiktok_account_manager import get_active_account_id  # type: ignore
        acc = get_active_account_id()
        if acc:
            return acc
    except Exception:
        pass
    return "default"


def _profile_path(account_id: str) -> Path:
    root = Path(os.getenv("TIKTOK_PROFILE_DIR", str(BASE_DIR / "playwright-profiles")))
    return root / f"tiktok_{account_id}"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-id", default=None, help="ID du compte TikTok (défaut : compte actif)")
    args = ap.parse_args()

    account_id = _resolve_account_id(args.account_id)
    profile = _profile_path(account_id)
    profile.mkdir(parents=True, exist_ok=True)

    for lf in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (profile / lf).unlink(missing_ok=True)
        except Exception:
            pass

    print(f"[INFO] Compte : {account_id}", flush=True)
    print(f"[INFO] Profil : {profile}", flush=True)
    print(f"[INFO] Connecte-toi à TikTok, puis FERME le navigateur.", flush=True)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(profile),
            executable_path=CHROME_PATH,
            headless=False,
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded", timeout=60_000)
        await ctx.wait_for_event("close", timeout=0)

    print(f"[OK] Session TikTok sauvegardée dans {profile}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
