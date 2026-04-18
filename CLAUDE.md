# CLAUDE.md — Mr Martin Video Automation

## Architecture globale

```
auto_scheduler.py  (processus principal, 2 threads daemon)
├── Thread scrape_loop()      → scraper_tiktok.py (importé directement)
└── Thread generation_loop()  → main_v3.py (subprocess)
                                  └── post_tiktok_inbox.py (subprocess)

gui.py  (processus séparé, lancé par daily_launcher_gui.bat)
└── Lance auto_scheduler.py en subprocess via "Démarrer"
```

## Flux de données : scraping → pipeline → upload

```
1. SCRAPING (scraper_tiktok.py)
   TikTok → yt-dlp (Firefox cookies) → download/*.mp4
   États persistés : scraper_history.json, scraper_daily.json

2. PIPELINE (main_v3.py, appelé par auto_scheduler)
   Download.mp4
   ├── ffmpeg → audio/audio_full.mp3
   ├── automate_diarization.py → transcripts/*.txt/.json (AssemblyAI)
   ├── segments_processing.py → transcription_segments_intervenants.txt (GPT-4o)
   ├── automate_adobe_with_bg.py → video_segments/*.mp4 (Adobe Express / Playwright)
   ├── assemble_guarded.py → video_finale/video_final.mp4 (ffmpeg)
   └── post_tiktok_inbox.py → TikTok API

3. ERREUR UPLOAD → pending_posts/Video_<timestamp>/
   ├── video_final.mp4
   └── meta.json {"reason": "spam_risk|token_failed|other_failed", "timestamp": "..."}
```

## Système de logs

| Fichier log      | Écrit par          | Mode  | Onglet GUI  |
|------------------|--------------------|-------|-------------|
| scraper.log      | scraper_tiktok.py  | w     | Scraper     |
| pipeline.log     | main_v3.py         | w     | Pipeline    |
| upload_tiktok.log| main_v3.py         | a     | Upload      |
| log.txt          | auto_scheduler.py  | w     | Tout        |

**Règle** : mode `w` = log vidé à chaque nouvelle instance du script qui l'écrit.
- `scraper.log` → vidé quand le scheduler démarre (import scraper_tiktok)
- `pipeline.log` → vidé à chaque run de main_v3.py (subprocess)
- `log.txt` → vidé à chaque restart du scheduler

**NE PAS** ajouter de truncation manuelle via `open(..., "w").close()` — cela corrompt les FileHandlers déjà ouverts sur Windows.

## Format de log attendu par le GUI

```
[2026-04-16 12:07:45] INFO     [pipeline] Message...
[2026-04-16 12:07:45] ERROR    [scraper] Erreur...
```
Regex : `^\[(\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}))\]\s+(INFO|WARNING|ERROR|DEBUG)\s+\[(\w+)\]\s+(.*)$`

Les lignes ne correspondant pas au format sont affichées en gris sans coloration.

## Retry vidéo (auto_scheduler)

Quand `main_v3.py` échoue :
1. La vidéo source a déjà été supprimée de `download/` avant le run
2. Le scheduler copie `Download.mp4` → `download/{video_name}` pour retry automatique
3. La vidéo sera reprise au prochain cycle (10s)
4. **Limite** : pas de compteur de retry — une vidéo en erreur permanente boucle indéfiniment. Supprimer manuellement de `download/` pour sortir de la boucle.

## Cookies TikTok (yt-dlp)

Ordre de priorité dans `_run_ytdlp_with_cookie_fallback()` :
1. `cookies.txt` à la racine (fichier exporté manuellement — le plus fiable)
2. Firefox (fonctionne avec Chrome 127+)
3. Edge
4. Chrome (bloqué depuis Chrome 127 — App-Bound Encryption)
5. Sans cookies (fallback)

Depuis Chrome 127, yt-dlp ne peut plus lire les cookies Chrome. **Utiliser Firefox ou cookies.txt.**

## Variables d'environnement (.env)

- `OPENAI_API_KEY` — GPT-4o pour transcription/description
- `ASSEMBLYAI_API_KEY` — Diarisation audio
- `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` — OAuth TikTok
- `BASE_DIR` — optionnel, racine du projet

## Fichiers d'état

- `scraper_history.json` — IDs des vidéos déjà téléchargées (dédup)
- `scraper_daily.json` — Compteur quotidien `{"date": "...", "count": N}`
- `config/tiktok_tokens.json` — Tokens OAuth TikTok
- `.ytdlp_last_update` — Date du dernier `pip upgrade yt-dlp` (évite 60s au lancement)

## Posting loop

`posting_loop()` dans auto_scheduler.py est **désactivée** (`t2.start()` commenté ligne ~399).
L'upload se fait directement depuis `main_v3.py` à la fin du pipeline.
Le GUI permet aussi le retry manuel des vidéos en `pending_posts/`.

## Points de vigilance

- `main_v3.py` est lancé en subprocess avec `-X utf8 -u` pour stdout en temps réel
- stdout → `log.info("main.py: ...")` → log.txt (scheduler)
- stderr → `log.error("main.py: ...")` → log.txt (scheduler)
- `pipeline.log` est écrit directement par main_v3.py (son propre FileHandler)
- Donc pipeline.log et log.txt contiennent les mêmes infos pipeline (deux sources)

## Commandes utiles

```bash
# Tester le scraper manuellement
python scraper_tiktok.py --limit 1

# Tester yt-dlp avec Firefox
python -m yt_dlp --flat-playlist -j --playlist-end 3 --cookies-from-browser firefox https://www.tiktok.com/@martinspam001

# Lancer uniquement le pipeline sur une vidéo
cp ma_video.mp4 Download.mp4
python main_v3.py

# Lancer le GUI
python gui.py
# ou
daily_launcher_gui.bat
```
