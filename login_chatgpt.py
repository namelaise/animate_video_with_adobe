# manual_chatgpt_setup.py - Approche 100% manuelle
import asyncio
import os
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv()

PROFILE_PATH = os.getenv("BASE_PROFILE_PATH")
CHROME_PATH = os.getenv("CHROME_PATH")

async def main():
    print("🎯 Méthode manuelle - 100% sûre")
    print("\n📋 ÉTAPES À SUIVRE :")
    print("1. Je vais ouvrir Chrome avec votre profil")
    print("2. Allez MANUELLEMENT sur chatgpt.com")
    print("3. Connectez-vous normalement")
    print("4. Testez une génération d'image")
    print("5. Fermez Chrome quand terminé")
    
    input("\n➡️  Appuyez sur ENTRÉE pour ouvrir Chrome...")
    
    async with async_playwright() as p:
        # Juste ouvrir Chrome, sans aller nulle part
        browser = await p.chromium.launch_persistent_context(
            PROFILE_PATH,
            executable_path=CHROME_PATH,
            headless=False,
            # Aucune page automatique
        )
        
        print("✅ Chrome ouvert avec votre profil")
        print("🌐 Allez maintenant sur https://chatgpt.com")
        print("⏳ Prenez votre temps pour vous connecter...")
        
        # Attendre très longtemps
        try:
            await asyncio.sleep(1800)  # 30 minutes
        except KeyboardInterrupt:
            print("\n🔄 Interruption détectée")
        
        print("💾 Sauvegarde du profil...")
        await browser.close()
    
    print("✅ Terminé! Votre session ChatGPT est sauvée.")

if __name__ == "__main__":
    asyncio.run(main())