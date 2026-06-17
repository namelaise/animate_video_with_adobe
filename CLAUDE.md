# CLAUDE.md — Mr Martin Video Automation

## Architecture globale

```
auto_scheduler.py  (processus principal, 2 threads daemon)
├── Thread scrape_loop()      → pipeline/scraper_tiktok.py (importé via sys.path)
└── Thread generation_loop()  → main_v3.py (subprocess)
                                  └── pipeline/post_tiktok_inbox.py (subprocess)

gui.py  (processus séparé, lancé par daily_launcher_gui.bat)
└── Lance auto_scheduler.py en subprocess via "Démarrer"
```

## Structure des fichiers

```
/                           ← entry points uniquement
├── gui.py
├── auto_scheduler.py
├── main_v3.py
├── daily_launcher_gui.bat
├── daily_launcher.bat
├── requirements.txt
├── CLAUDE.md
│
├── pipeline/               ← étapes du pipeline (importées par main_v3 via sys.path)
│   ├── scraper_tiktok.py
│   ├── automate_adobe_with_bg.py
│   ├── automate_diarization.py
│   ├── segments_processing.py
│   ├── assemble_guarded.py
│   ├── post_tiktok_inbox.py        # voie API officielle (inbox/direct)
│   └── post_tiktok_playwright.py   # bypass Playwright (en attendant audit Direct Post)
│
├── tiktok/                 ← API & auth TikTok (importé via sys.path)
│   ├── tiktok_account_manager.py
│   ├── auth_tiktok_refresh.py
│   ├── auth_tiktok_token_manager.py
│   └── fetch_stats.py
│
├── assets/                 ← icônes app (app_icon.ico / .png)
│
├── config/                 ← configuration persistante (versionné sauf tokens)
│   ├── scraper_config.json     comptes TikTok, quota, durée min/max
│   ├── gui_config.json         préférences UI
│   ├── tiktok_tokens*.json     ignoré git (OAuth tokens)
│   └── tiktok_accounts.json    ignoré git (données comptes)
│
├── state/                  ← état runtime (créé automatiquement, majoritairement ignoré git)
│   ├── upload_history.json     trackée — historique des uploads pour les stats
│   ├── pipeline_state.json     reprise après crash
│   ├── matching_mode.json      mode auto/manuel (IPC GUI↔pipeline)
│   ├── matching_response.json  réponse matching puppet (IPC)
│   ├── scraper_history.json    IDs vidéos déjà téléchargées (dédup)
│   └── scraper_daily.json      compteur quotidien scraper
│
├── tools/                  ← helpers manuels (connexion navigateur)
│   ├── login_adobe.py
│   ├── login_gemini.py
│   └── login_tiktok.py      # sauvegarde la session TikTok par compte
│
└── docs/                   ← landing page GitHub Pages (index/privacy/tos.html)
```

## Flux de données : scraping → pipeline → upload

```
1. SCRAPING (pipeline/scraper_tiktok.py)
   TikTok → yt-dlp (cookies Firefox) → download/*.mp4
   Config  : config/scraper_config.json (comptes, quota, durée)
   État    : state/scraper_history.json, state/scraper_daily.json

2. PIPELINE (main_v3.py, appelé par auto_scheduler)
   Download.mp4
   ├── ffmpeg → audio/audio_full.mp3
   ├── automate_diarization.py → transcripts/*.txt/.json (AssemblyAI)
   ├── segments_processing.py  → transcription_segments_intervenants.txt (GPT-4o / Claude CLI)
   ├── automate_adobe_with_bg.py → video_segments/*.mp4 (Adobe Express / Playwright)
   ├── assemble_guarded.py     → video_finale/video_final.mp4 (ffmpeg)
   └── post_tiktok_inbox.py    → TikTok API
   État    : state/pipeline_state.json (reprise crash), state/current_generation_meta.json

3. ERREUR UPLOAD → pending_posts/Video_<timestamp>/
   ├── video_final.mp4
   └── meta.json {"reason": "spam_risk|token_failed|other_failed", "timestamp": "..."}
```

## sys.path — imports entre sous-dossiers

`auto_scheduler.py` et `main_v3.py` ajoutent `pipeline/` et `tiktok/` au sys.path au démarrage :

```python
sys.path.insert(0, str(Path(__file__).parent / "pipeline"))
sys.path.insert(0, str(Path(__file__).parent / "tiktok"))
```

Les scripts dans ces dossiers ont `BASE_DIR = Path(__file__).resolve().parent.parent` pour pointer vers la racine du projet.

## Système de logs

| Fichier log       | Écrit par              | Mode | Onglet GUI |
|-------------------|------------------------|------|------------|
| logs/scraper.log  | pipeline/scraper_tiktok.py | w  | Scraper    |
| logs/pipeline.log | main_v3.py             | w    | Pipeline   |
| logs/upload_tiktok.log | main_v3.py        | a    | Upload     |
| logs/log.txt      | auto_scheduler.py      | w    | Tout       |

**Règle** : mode `w` = log vidé à chaque nouvelle instance du script qui l'écrit.

**NE PAS** ajouter de truncation manuelle via `open(..., "w").close()` — cela corrompt les FileHandlers déjà ouverts sur Windows.

## Format de log attendu par le GUI

```
[2026-04-16 12:07:45] INFO     [pipeline] Message...
[2026-04-16 12:07:45] ERROR    [scraper] Erreur...
```
Regex : `^\[(\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}))\]\s+(INFO|WARNING|ERROR|DEBUG)\s+\[(\w+)\]\s+(.*)$`

Les lignes hors format sont affichées en gris sans coloration.

## Retry vidéo (auto_scheduler)

Quand `main_v3.py` échoue :
1. La vidéo source a déjà été supprimée de `download/` avant le run
2. Le scheduler copie `Download.mp4` → `download/{video_name}` pour retry automatique
3. Limite : **MAX_RETRIES = 3** — après 3 échecs, la vidéo est archivée dans `archive/`
4. Pour forcer un retry, remettre manuellement la vidéo dans `download/`

## Cookies TikTok (yt-dlp)

Ordre de priorité dans `_run_ytdlp_with_cookie_fallback()` :
1. `cookies.txt` à la racine (fichier exporté manuellement — le plus fiable)
2. Firefox
3. Edge
4. Chrome (bloqué depuis Chrome 127 — App-Bound Encryption)
5. Sans cookies (fallback)

Depuis Chrome 127, yt-dlp ne peut plus lire les cookies Chrome. **Utiliser Firefox ou cookies.txt.**

## Variables d'environnement (.env)

Voir `.env.example` à la racine pour la liste complète. Variables clés :

- `OPENAI_API_KEY` — GPT-4o pour transcription/description
- `ASSEMBLYAI_API_KEY` — Diarisation audio (AssemblyAI)
- `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` — OAuth TikTok
- `BASE_PROFILE_PATH` — profil Chrome de base pour Gemini/Adobe
- `URL_ADOBE` — URL de l'outil Adobe Express utilisé
- `BASE_DIR` — optionnel, racine du projet (défaut : dossier du script)

## Posting loop

`posting_loop()` dans auto_scheduler.py est **désactivée**.
L'upload se fait directement depuis `main_v3.py` à la fin du pipeline.
Le GUI permet le retry manuel des vidéos en `pending_posts/`.

## Mode publication TikTok : Playwright (bypass) vs API

Le routage se fait dans `_upload_with_account()` (main_v3.py) via `TIKTOK_USE_PLAYWRIGHT` :

- `TIKTOK_USE_PLAYWRIGHT=0` (défaut) → `pipeline/post_tiktok_inbox.py` (Content Posting API officielle, mode INBOX/brouillon par défaut ; DIRECT si `direct_post_enabled and auto_publish`)
- `TIKTOK_USE_PLAYWRIGHT=1` → `pipeline/post_tiktok_playwright.py` (bypass conservé en secours)
  - Réutilise la session navigateur sauvée dans `playwright-profiles/tiktok_<account_id>/`
  - Setup : `python tools/login_tiktok.py --account-id <id>` (par compte)
  - Pas de quota API, pas de scope `video.publish` requis

Codes retour de `post_tiktok_playwright.py` :
- `0` succès (stdout `publish_id=playwright_<timestamp>`)
- `2` pas de session — relancer `login_tiktok.py`
- `3` spam_risk détecté (mapping vers `pending_posts/`)
- `1` autre échec

## Points de vigilance

- `main_v3.py` est lancé en subprocess avec `-X utf8 -u` pour stdout en temps réel
- stdout → `log.info("main.py: ...")` → log.txt (scheduler)
- stderr → `log.error("main.py: ...")` → log.txt (scheduler)
- `pipeline.log` est écrit directement par main_v3.py (son propre FileHandler)
- Donc pipeline.log et log.txt contiennent les mêmes infos pipeline (deux sources)
- Le profil Gemini temporaire copie uniquement les fichiers essentiels (cookies/session) — pas le profil entier, pour éviter les crashs de version Chrome

## Commandes utiles

```bash
# Tester le scraper manuellement
python pipeline/scraper_tiktok.py --limit 1

# Tester yt-dlp avec Firefox
python -m yt_dlp --flat-playlist -j --playlist-end 3 --cookies-from-browser firefox https://www.tiktok.com/@martinspam001

# Lancer uniquement le pipeline sur une vidéo
cp ma_video.mp4 Download.mp4
python main_v3.py

# Lancer le GUI
python gui.py
# ou
daily_launcher_gui.bat

# Rafraîchir les stats TikTok manuellement
python tiktok/fetch_stats.py --update-only

# Connexion manuelle Adobe / Gemini / TikTok
python tools/login_adobe.py
python tools/login_gemini.py
python tools/login_tiktok.py --account-id acc_1   # un profil par compte TikTok

# Tester le post Playwright manuellement
python pipeline/post_tiktok_playwright.py --video Download.mp4 --caption "test" --privacy PUBLIC_TO_EVERYONE --allow-comment
```
