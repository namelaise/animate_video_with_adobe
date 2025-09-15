# === automate_pipeline.py (version épurée, noms explicites) ===
import os
import re
import json
import time
import random
import shutil
import base64
import subprocess, sys
import concurrent.futures
from typing import List, Dict, Tuple
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from pydub import AudioSegment
from PIL import Image
from tqdm import tqdm
from openai import OpenAI

from assemble_guarded import assemble_from_tail_with_transcript
from segments_processing import (
    rewrite_transcript_with_intervenants_gpt,
    cut_audio_by_diarization,
)
from automate_diarization import transcribe_segments_with_diarization

load_dotenv()

# Nettoyage éventuel de logs
if os.path.exists("automation_logs"):
    shutil.rmtree("automation_logs", ignore_errors=True)
    
# --- tout en haut de main.py ---
import os, sys, io
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # Pour Python <3.7 ou environnements particuliers
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# =========================
#      CONFIG GLOBALE
# =========================

# Personnages Adobe Character Animator disponibles (par genre)
PUPPETS_BY_GENDER: Dict[str, List[str]] = {
    "homme": [
        "Fergus VQA.puppet-button",
        "Cecil VQA.puppet-button",
        "David VQA.puppet-button",
        "Elliot VQA.puppet-button",
        "Jonty VQA.puppet-button",
        "Jonty with prosthetic VQA.puppet-button",
        "Atlas VQA.puppet-button",
    ],
    "femme": [
        "Edith VQA.puppet-button",
        "Agnes VQA.puppet-button",
        "Chivy VQA.puppet-button",
        "Gaby VQA.puppet-button",
        "Zibby VQA.puppet-button",
        "Yara VQA.puppet-button",
        "Yara with prosthetic VQA.puppet-button",
    ],
}

# Mots sensibles à filtrer du prompt d'image (pré-nettoyage très léger)
BANNED_WORDS: List[str] = [
    "alcool", "alcoolisé", "chasseur", "chasse", "fusil", "tuer", "insulte", "dispute",
]

# Dossiers de travail (attendus dans l'env)
BASE_DIR = os.getenv("BASE_DIR")
RAW_VIDEO_PATH = os.getenv("RAW_VIDEO_PATH", os.path.join(BASE_DIR, "Download.mp4") if BASE_DIR else "Download.mp4")

RAW_AUDIO_DIR = os.path.join(BASE_DIR, "audio")
AUDIO_SEGMENTS_DIR = os.path.join(BASE_DIR, "audio_segments")
VIDEO_SEGMENTS_DIR = os.path.join(BASE_DIR, "video_segments")
VIDEO_FINALE_DIR = os.path.join(BASE_DIR, "video_finale")
VIDEO_FINALE_PATH = os.path.join(VIDEO_FINALE_DIR, "video_final.mp4")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")
IMAGES_DIR = os.path.join(BASE_DIR, "images")
LEFT_IMG_DIR = os.path.join(IMAGES_DIR, "left")
RIGHT_IMG_DIR = os.path.join(IMAGES_DIR, "right")

FULL_AUDIO_PATH = os.path.join(RAW_AUDIO_DIR, "audio_full.mp3")

# Crée l'arborescence nécessaire
for _folder in [RAW_AUDIO_DIR, AUDIO_SEGMENTS_DIR, TRANSCRIPTS_DIR, IMAGES_DIR, LEFT_IMG_DIR, RIGHT_IMG_DIR]:
    os.makedirs(_folder, exist_ok=True)

# Supprime d’anciens segments audio éventuels (re-run propre)
for _file in os.listdir(AUDIO_SEGMENTS_DIR):
    if _file.startswith("audio_") and _file.endswith(".mp3"):
        os.remove(os.path.join(AUDIO_SEGMENTS_DIR, _file))

# =========================
#     UTILITAIRES TEXTE
# =========================

SPEAKER_LINE_RE = re.compile(
    r'^\s*\[(?P<start>[\d\.]+)\s*[→\-]\s*(?P<end>[\d\.]+)\]\s*(?P<label>[^:]+)\s*:\s*(?P<text>.*)$',
    re.UNICODE
)

def parse_segments_with_speakers(segments_txt_path: str) -> List[Dict]:
    """
    Parse un fichier du type:
      [0.01 → 0.69] Mr Martin : Allo ?
      [0.69 → 2.27] femme 1 : ...
    Retourne une liste ordonnée de dicts: index, start_s, end_s, label, text.
    """
    parsed: List[Dict] = []
    with open(segments_txt_path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f, start=1):
            match = SPEAKER_LINE_RE.match(line.rstrip("\n"))
            if not match:
                continue
            parsed.append({
                "index": idx,
                "start_s": float(match.group("start")),
                "end_s": float(match.group("end")),
                "label": match.group("label").strip(),
                "text": match.group("text"),
            })
    return parsed

def _extract_last_int_from_name(name: str) -> int:
    """Renvoie le dernier entier trouvé dans un nom de fichier, sinon -1 (pour tri)."""
    nums = re.findall(r'(\d+)', name)
    return int(nums[-1]) if nums else -1

def list_audio_segments_sorted(audio_dir: str) -> List[str]:
    """Liste les .mp3 triés par entier trouvé dans le nom (puis lexicographiquement)."""
    files = [os.path.join(audio_dir, f) for f in os.listdir(audio_dir) if f.lower().endswith(".mp3")]
    files.sort(key=lambda p: (_extract_last_int_from_name(os.path.basename(p)), os.path.basename(p)))
    return files

# =========================
#   ETAPES AUDIO & VISU
# =========================

def extract_audio_from_video() -> str:
    """Extrait la piste audio du RAW_VIDEO_PATH vers FULL_AUDIO_PATH (mp3)."""
    cmd = f'ffmpeg -i "{RAW_VIDEO_PATH}" -q:a 0 -map a "{FULL_AUDIO_PATH}" -y'
    subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"✅ Audio extrait : {FULL_AUDIO_PATH}")
    return FULL_AUDIO_PATH

def split_full_audio(max_segment_duration_sec: int = 110) -> List[str]:
    """
    Découpe l'audio complet en segments mp3 d'environ `max_segment_duration_sec`.
    Retourne la liste des chemins créés.
    """
    audio = AudioSegment.from_file(FULL_AUDIO_PATH)
    segment_paths: List[str] = []

    total_parts = (len(audio) + max_segment_duration_sec * 1000 - 1) // (max_segment_duration_sec * 1000)

    print("⏳ Découpage audio en segments...")
    for i in tqdm(range(total_parts), desc="Découpage", unit="segment"):
        start_ms = i * max_segment_duration_sec * 1000
        end_ms = start_ms + max_segment_duration_sec * 1000
        segment = audio[start_ms:end_ms]

        filename = f"audio_{i + 1}.mp3"
        path_out = os.path.join(AUDIO_SEGMENTS_DIR, filename)

        segment.export(path_out, format="mp3")
        segment_paths.append(path_out)

    print(f"✅ Audio découpé : {len(segment_paths)} segments")
    return segment_paths

# build_background_prompt.py
import os
import random
from pathlib import Path

def build_background_prompt(script_text: str, templates_dir: str = "prompts/backgrounds") -> str:
    """
    Construit le prompt d'image en choisissant aléatoirement un template .txt dans `templates_dir`.
    - Si le template contient {SCRIPT} ou {{SCRIPT}}, on remplace par le script nettoyé.
    - Sinon, on concatène le script nettoyé après le template.
    - Fallback sur un prompt par défaut si aucun fichier n'est trouvé.
    - Sauvegarde le prompt final dans IMAGES_DIR/prompt.txt (IMAGES_DIR doit exister dans ton projet).
    """
    # ----- Prompt par défaut (fallback) -----
    default_template = """
    Prompt IA – Scène immersive absurde et ultra-détaillée basée sur un script audio
    Crée une image photo très réaliste et moderne, voir futurisque ou un peu luxueux, au format carré 9:8, inspirée d’un script de conversation absurde fourni en entrée.

    Style visuel :
    Ambiance réaliste et très moderne : éclairage naturel ou artificiel cohérent avec un lieu public ou semi-public et en cohérence avec le lieu du script
    (ex: forêt, champ, magasin, etc) avec des couleurs vives.
    Cadrage grand angle (profondeur / espace). Décor riche et immersif (textures, reflets, ombres), couleurs vives et contrastées (non cartoon).

    Contraste absurde à intégrer :
    Objets incongrus inspirés du script mais intégrés de façon réaliste (échelle étrange, objets flottants/mal placés).
    Panneaux/affiches avec citations exactes du script en français (pub, pancarte bricolée, graffiti).
    Plusieurs petites scènes absurdes en avant-plan et arrière-plan.

    Contexte :
    Lieu logique par rapport au script (marché, parking, plage, bureau…), vaste et détaillé.
    Éléments de décor racontant une histoire secondaire absurde (stand inutile, pile d’objets improbables, comptoir à pancarte absurde).
    Uniquement des traces d’activité humaine (pas de personnages visibles).

    📝 Extrait du script :
    {SCRIPT}
    """.strip()

    # ----- Nettoyage du script -----
    cleaned = " ".join((script_text or "").split())  # compresse les espaces/retours
    banned = globals().get("BANNED_WORDS") or globals().get("banned_words") or []
    for w in banned:
        cleaned = cleaned.replace(w, "")

    # ----- Récupération aléatoire d'un template .txt -----
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
        # on tombera sur le fallback ci-dessous
        pass

    if not template_text:
        template_text = default_template

    # ----- Injection du script -----
    if "{SCRIPT}" in template_text or "{{SCRIPT}}" in template_text:
        final_prompt = (
            template_text
            .replace("{SCRIPT}", cleaned)
            .replace("{{SCRIPT}}", cleaned)
        )
    else:
        final_prompt = f"{template_text}\n\n{cleaned}"

    # ----- Sauvegarde pour debug -----
    IMAGES_DIR = globals().get("IMAGES_DIR") or "images"
    Path(IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    prompt_path = Path(IMAGES_DIR) / "prompt.txt"
    prompt_path.write_text(final_prompt, encoding="utf-8")

    print(f"✅ Prompt image généré (template: {chosen_name}) → {prompt_path}")
    return final_prompt

def generate_image_with_openai(script_text: str) -> str | None:
    """
    Génère une image via OpenAI Images API (gpt-image-1) à partir du script.
    Retourne le chemin de l'image enregistrée, ou None en cas d'échec.
    """
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    output_image_path = os.path.join(IMAGES_DIR, "full_background.png")

    prompt_text = build_background_prompt(script_text)

    # Nettoyage supplémentaire (pour log uniquement)
    local_banned = ["violence", "égalité", "militer", "syndicat", "CGT", "sexuel", "genre", "t-shirt", "discrimination", r"\b(président|premier[e]? ministre|député[e]?|ministre|gouvernement|politique|élysée|assemblée)\b",
        r"\b(Macron|Sarkozy|Le Pen|Mélenchon|Borne|Attal|Bardella|Trump|Biden)\b",
        r"\b(public figure|celebrity|célébrité)\b",]
    cleaned_for_log = prompt_text
    for w in local_banned:
        cleaned_for_log = cleaned_for_log.replace(w, " ")
    os.makedirs("output", exist_ok=True)
    with open("output/prompt_nettoye.txt", "w", encoding="utf-8") as f:
        f.write(cleaned_for_log)

    try:
        print("⏳ Génération de l'image via OpenAI...")
        resp = client.images.generate(
            model="gpt-image-1",
            prompt=prompt_text,
        )
        image_b64 = resp.data[0].b64_json
        with open(output_image_path, "wb") as f:
            f.write(base64.b64decode(image_b64))

        print(f"✅ Image générée : {output_image_path}")
        return output_image_path
    except Exception as e:
        print(f"❌  Génération image échouée : {e}")
        return None

def split_background_to_tiktok_pairs() -> Tuple[str, str]:
    """
    Découpe l'image 'full_background.png' (1080x960 attendu) en deux plages 9:16 (gauche/droite).
    """
    bg_path = os.path.join(IMAGES_DIR, "full_background.png")
    img = Image.open(bg_path)

    expected_w, expected_h = 1080, 960
    if img.size != (expected_w, expected_h):
        print(f"⚠️ Redimension de l’image en {expected_w}x{expected_h} pour TikTok.")
        img = img.resize((expected_w, expected_h))

    left_img = img.crop((0, 0, 540, 960))
    right_img = img.crop((540, 0, 1080, 960))

    left_path = os.path.join(LEFT_IMG_DIR, "left_0.png")
    right_path = os.path.join(RIGHT_IMG_DIR, "right_0.png")

    left_img.save(left_path)
    right_img.save(right_path)

    print("✅ Deux images 9:16 générées (gauche/droite).")
    return left_path, right_path

# =========================
#   AFFECTATION PUPPETS
# =========================

LABEL_TO_PUPPET_CACHE: Dict[str, str] = {}

def choose_puppet_for_label(speaker_label: str) -> Tuple[str, str]:
    """
    Assigne (genre, puppet) à un label:
      - "Mr Martin" => ('homme', 'Sticky VQA.puppet-button')
      - 'homme N'   => ('homme', puppet aléatoire mais stable par label)
      - 'femme N'   => ('femme', puppet aléatoire mais stable par label)
    """
    label_norm = speaker_label.strip().lower()
    if label_norm == "mr martin":
        return "homme", "Sticky VQA.puppet-button"

    if label_norm.startswith("homme"):
        genre = "homme"
        pool = PUPPETS_BY_GENDER.get("homme", [])
    elif label_norm.startswith("femme"):
        genre = "femme"
        pool = PUPPETS_BY_GENDER.get("femme", [])
    else:
        genre = "homme"
        pool = PUPPETS_BY_GENDER.get("homme", []) + PUPPETS_BY_GENDER.get("femme", [])

    if speaker_label in LABEL_TO_PUPPET_CACHE:
        return genre, LABEL_TO_PUPPET_CACHE[speaker_label]

    puppet = random.choice(pool) if pool else "Default VQA.puppet-button"
    LABEL_TO_PUPPET_CACHE[speaker_label] = puppet
    return genre, puppet

def _speaker_index_hint(label: str) -> int:
    """Extrait N à partir de 'homme N' / 'femme N' (sinon 0)."""
    m = re.search(r'(?:homme|femme)\s+(\d+)', label, re.IGNORECASE)
    return int(m.group(1)) if m else 0

def run_automate_adobe(job: Dict):
    """
    Lance automate_adobe.py pour un segment donné.
    job = {
      "segment_file": str,
      "speaker_label": str,
      "speaker_genre": str,
      "segment_index": int,
      "speaker_index_hint": int,
      "personnage_adobe": str,
    }
    """
    try:
        subprocess.run([
            sys.executable, "automate_adobe.py",
            job["segment_file"],
            job["speaker_label"],
            job["speaker_genre"],
            str(job["segment_index"]),
            str(job.get("speaker_index_hint", 0)),
            job["personnage_adobe"]
        ], check=True)

        print(f"✅ Fini : {job['speaker_label']} - {os.path.basename(job['segment_file'])}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Erreur automate_adobe.py pour {job['speaker_label']} : {e}")

def automate_generation_videos(
    max_threads: int = 4,
    segments_txt_path: str | None = None,
    audio_segments_dir: str | None = None,
):
    """
    Génère une vidéo par segment :
      - lit le fichier texte réécrit (Mr Martin / homme N / femme N)
      - associe chaque ligne à un mp3 découpé
      - choisit le puppet
      - lance automate_adobe.py en parallèle
    """
    segments_txt_path = segments_txt_path or os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants.txt")
    audio_segments_dir = audio_segments_dir or AUDIO_SEGMENTS_DIR

    if not os.path.exists(segments_txt_path):
        print(f"❌ Fichier segments introuvable : {segments_txt_path}")
        return
    if not os.path.isdir(audio_segments_dir):
        print(f"❌ Dossier audio introuvable : {audio_segments_dir}")
        return

    lines = parse_segments_with_speakers(segments_txt_path)
    if not lines:
        print("❌ Aucune ligne valide dans le fichier segments.")
        return

    mp3_files = list_audio_segments_sorted(audio_segments_dir)
    if not mp3_files:
        print("❌ Aucun segment audio .mp3 trouvé.")
        return

    pair_count = min(len(lines), len(mp3_files))
    if len(lines) != len(mp3_files):
        print(f"⚠️ Nb lignes texte ({len(lines)}) ≠ nb mp3 ({len(mp3_files)}) — on traitera {pair_count} paires.")

    jobs: List[Dict] = []
    for i in range(pair_count):
        info = lines[i]
        mp3_path = mp3_files[i]
        label = info["label"]
        genre, puppet = choose_puppet_for_label(label)
        print("segment_file", mp3_path)

        jobs.append({
            "segment_file": mp3_path,
            "speaker_label": label,
            "speaker_genre": genre,
            "segment_index": info["index"],
            "speaker_index_hint": _speaker_index_hint(label),
            "personnage_adobe": puppet,
        })

    if not jobs:
        print("❌ Aucun job à traiter.")
        return

    print(f"⏳ Lancement de {len(jobs)} jobs (max {max_threads})")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_threads) as executor:
        executor.map(run_automate_adobe, jobs)

    print("✅ Toutes les vidéos animées ont été générées.")

# =========================
#   VERIFICATION TEXTE
# =========================

def get_transcription_file_with_verification(transcription_path: str) -> str | None:
    """
    Affiche un aperçu du fichier de transcription et demande validation utilisateur.
    [y] pour continuer, [n] pour arrêter, auto-continue après 5 minutes.
    """
    if not os.path.exists(transcription_path):
        print("❌ Le fichier transcription_full.txt est introuvable.")
        return None

    with open(transcription_path, "r", encoding="utf-8") as f:
        content = f.read()

    print("\n--- Aperçu de la transcription (début) ---")
    print(content[:1500])
    print("--- Fin de l’aperçu ---\n")

    print("✅ Le contenu vous semble-t-il correct ?")
    print("Appuie sur [y] pour continuer, [n] pour arrêter, ou attendre 5 minutes pour continuer automatiquement.")

    start_time = time.time()
    timeout_s = 30  # 5 min

    while True:
        if time.time() - start_time > timeout_s:
            print("⏳ Temps écoulé. Suite du traitement...")
            return content

        if os.name == 'nt':  # Windows
            import msvcrt
            if msvcrt.kbhit():
                key = msvcrt.getwch().lower()
                if key == 'y':
                    print("➡️  Poursuite du traitement...")
                    return content
                if key == 'n':
                    print("❌ Traitement interrompu par l’utilisateur.")
                    exit()
        else:
            import sys, select
            if select.select([sys.stdin], [], [], 1)[0]:
                key = sys.stdin.readline().strip().lower()
                if key == 'y':
                    print("➡️  Poursuite du traitement...")
                    return content
                if key == 'n':
                    print("❌ Traitement interrompu par l’utilisateur.")
                    exit()
        time.sleep(1)
        
# =========================
#   ARCHIVAGE
# =========================        
def archive_outputs():
    parent_dir = os.path.dirname(BASE_DIR)
    count = len([d for d in os.listdir(parent_dir) if os.path.isdir(os.path.join(parent_dir, d)) and d.startswith("Video_")]) + 1
    archive_dir = os.path.join(parent_dir, f"Video_{count}")
    
    print(f"📁 Création du dossier d’archive : Video_{count}")
    os.makedirs(archive_dir, exist_ok=True)

    files_to_move = [
        # RAW_VIDEO,
        os.path.join(RAW_AUDIO_DIR, "audio_full.mp3"),
        os.path.join(AUDIO_SEGMENTS_DIR, "audio_1.mp3"),
        os.path.join(AUDIO_SEGMENTS_DIR, "audio_2.mp3"),
        os.path.join(IMAGES_DIR, "prompt.txt"),
        os.path.join(IMAGES_DIR, "full_background.png"),
        os.path.join(LEFT_IMG_DIR, "left_0.png"),
        os.path.join(RIGHT_IMG_DIR, "right_0.png"),
        os.path.join("output", "intervenants.json"), 
        os.path.join(TRANSCRIPTS_DIR, "transcription_full.txt"), 
        os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants.txt"), 
        os.path.join(TRANSCRIPTS_DIR, "transcription_segments.txt"), 
        os.path.join(TRANSCRIPTS_DIR, "diarization_segments.json"), 
        os.path.join(TRANSCRIPTS_DIR, "speakers.json"), 
        os.path.join(BASE_DIR, "video_finale", "video_final.mp4"),
        os.path.join(BASE_DIR, "video_finale", "video_composite.mp4")
    ]

    if os.path.exists("audio_segments"):
        shutil.rmtree("audio_segments", ignore_errors=True)
    
    if os.path.exists("video_segments"):
        shutil.rmtree("video_segments", ignore_errors=True)
    
    

    for file in files_to_move:
        if os.path.exists(file):
            shutil.move(file, archive_dir)
            print(f"📦 Fichier déplacé : {os.path.basename(file)}")

    # Déplacement des .mp4 dans BASE_DIR
    for file in os.listdir(BASE_DIR):
        if file.endswith(".mp4"):
            shutil.move(os.path.join(BASE_DIR, file), archive_dir)
            print(f"📦 Vidéo archivée : {file}")

    print(f"✅ Tous les fichiers ont été archivés dans : {archive_dir}")
    
    
# =========================
#   DELETE
# =========================        
def delete_outputs():
    files_to_delete = [
        # RAW_VIDEO,
        os.path.join(RAW_AUDIO_DIR, "audio_full.mp3"),
        os.path.join(AUDIO_SEGMENTS_DIR, "audio_1.mp3"),
        os.path.join(AUDIO_SEGMENTS_DIR, "audio_2.mp3"),
        os.path.join(IMAGES_DIR, "prompt.txt"),
        os.path.join(IMAGES_DIR, "full_background.png"),
        os.path.join(LEFT_IMG_DIR, "left_0.png"),
        os.path.join(RIGHT_IMG_DIR, "right_0.png"),
        os.path.join("output", "intervenants.json"), 
        os.path.join(TRANSCRIPTS_DIR, "transcription_full.txt"), 
        os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants.txt"), 
        os.path.join(TRANSCRIPTS_DIR, "transcription_segments.txt"), 
        os.path.join(TRANSCRIPTS_DIR, "diarization_segments.json"), 
        os.path.join(TRANSCRIPTS_DIR, "speakers.json"), 
        os.path.join(BASE_DIR, "video_finale", "video_final.mp4"),
    ]

    if os.path.exists("audio_segments"):
        shutil.rmtree("audio_segments", ignore_errors=True)
    
    if os.path.exists("video_segments"):
        shutil.rmtree("video_segments", ignore_errors=True)
    

    for file in files_to_delete:
        if os.path.exists(file):
            os.remove(file)
            print(f"📦 Fichier supprimé : {os.path.basename(file)}")

    # Déplacement des .mp4 dans BASE_DIR
    for file in os.listdir(BASE_DIR):
        if file.endswith(".mp4"):
            os.remove(os.path.join(BASE_DIR, file))
            print(f"📦 Vidéo archivée : {file}")

    print(f"✅ Tous les fichiers ont été supprimés ")



# =========================
#      PIPELINE MAIN
# =========================

def main():
    print("📦 Traitement initial démarré...")

    # 1) Extraire audio du RAW_VIDEO
    extract_audio_from_video()

    # 2) Transcription + diarisation
    transcribe_segments_with_diarization(audio_path=FULL_AUDIO_PATH, output_dir=TRANSCRIPTS_DIR)

    # 3) Vérification et réécriture avec intervenants
    raw_segments_txt = os.path.join(TRANSCRIPTS_DIR, "transcription_segments.txt")
    segments_text_content = get_transcription_file_with_verification(raw_segments_txt)

    rewritten_segments_path = rewrite_transcript_with_intervenants_gpt(
        contenu_segments_brut=segments_text_content,
        dossier_sortie=TRANSCRIPTS_DIR,
        nom_fichier_sortie="transcription_segments_intervenants.txt",
    )

    # 4) Découpage MP3 selon segments réécrits
    cut_audio_by_diarization(
        chemin_fichier_audio_mp3=FULL_AUDIO_PATH,
        chemin_fichier_segments_txt=rewritten_segments_path,
        dossier_sortie_segments_audio=AUDIO_SEGMENTS_DIR,
        padding_millisecondes=80,
        duree_minimale_conservee_millisecondes=250,
        preferer_copie_flux=True,
    )

    # 5) Image de fond + split 9:16 (⚠️ ici on passe le texte brut des segments pour garder le comportement existant)
    # full_txt_path = os.path.join(TRANSCRIPTS_DIR, "transcription_segments.txt")
    # full_txt = get_transcription_file_with_verification(full_txt_path)
    # generate_image_with_openai(full_txt)
    # split_background_to_tiktok_pairs()

    # 6) Génération des vidéos segments (Adobe)
    automate_generation_videos(
        max_threads=4,
        segments_txt_path=os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants.txt"),
        audio_segments_dir=AUDIO_SEGMENTS_DIR,
    )

    # 7) Assemblage final à partir des queues (durées = transcript)
    transcript_segments_path = os.path.join(TRANSCRIPTS_DIR, "transcription_segments_intervenants.txt")

    assemble_from_tail_with_transcript(
        video_segments_dir=VIDEO_SEGMENTS_DIR,
        transcript_path=transcript_segments_path,
        output_path=VIDEO_FINALE_PATH,
        crf=18,
        preset="veryfast",
        audio_bitrate="192k",
        min_keep_sec=0.10,
        force_fps=60,  # garder le comportement existant
    )  
    
    # 8) Key & Overlay : suppression fond vert + superposition sur un background (choisi dans ./video_background)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    composite_out = os.path.join(BASE_DIR, "video_finale",  f"video_{timestamp}.mp4")
    bg_dir = os.path.join(BASE_DIR, "video_background") 
                                    
    print("🎬 Key & Overlay (fond vert → background)…")
    subprocess.run([
        sys.executable, "key_and_overlay.py",
        "--actor", VIDEO_FINALE_PATH,
        "--bg-dir", bg_dir,
        "--out", composite_out,
        "--key-color", "#00B140",
        "--similarity", "0.18",
        "--blend", "0.06",
        "--final-pix-fmt", "yuv420p",   # compatibilité maximale (TikTok / lecteurs Windows)
        # "--hdr-to-sdr",                 # convertit BT.2020/HLG -> BT.709 si nécessaire
        "--verbose-ffmpeg",
    ], check=True)
    
    
    subprocess.run([sys.executable, "auth_tiktok_refresh.py"], check=True)

    # 2) upload en Inbox
    final_mp4 = os.path.join(BASE_DIR, "video_finale", "video_composite.mp4")
    subprocess.run([
        sys.executable, "post_tiktok_inbox.py",
        "--video", final_mp4,
        "--poll",                 # optionnel, pour suivre le statut
    ], check=True)
    
    # archive_outputs()  
    delete_outputs()  

    print("\n✅ Traitement terminé.")

if __name__ == "__main__":
    main()
