
import os
import subprocess
import uuid
from pydub import AudioSegment
import whisper
from PIL import Image, ImageDraw
import os
import base64
from PIL import Image
from io import BytesIO
from openai import OpenAI
import json
from tqdm import tqdm
import concurrent.futures
import random
import re
import shutil
from dotenv import load_dotenv

from automate_diarization import transcribe_segments_with_diarization

load_dotenv()

# === Config ===

personnages_disponibles = {
    "homme": [
        "Fergus VQA.puppet-button",
        "Cecil VQA.puppet-button",
        "David VQA.puppet-button",
        "Elliot VQA.puppet-button",
        "Jonty VQA.puppet-button",
        "Jonty with prosthetic VQA.puppet-button",
        "Atlas VQA.puppet-button"
    ],
    "femme": [
        "Edith VQA.puppet-button",
        "Agnes VQA.puppet-button",
        "Chivy VQA.puppet-button",
        "Gaby VQA.puppet-button",
        "Zibby VQA.puppet-button",
        "Yara VQA.puppet-button",
        "Yara with prosthetic VQA.puppet-button"
    ]
}


# 📂 Chemin de ton fichier audio
AUDIO_PATH = r"C:\Users\n.amelaise\Desktop\mr martin\script\audio_segments\audio_1.mp3"

# 🔐 Ton token Hugging Face (read only)
HUGGINGFACE_TOKEN = "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"

banned_words = ["alcool", "alcoolisé", "chasseur", "chasse", "fusil", "tuer", "insulte", "dispute"]

# --- CONFIGURATION DES DOSSIERS ---
BASE_DIR = r'C:\Users\n.amelaise\Desktop\mr martin\script'
MR_MARTIN_FOLDER = r'C:\Users\n.amelaise\Desktop\mr martin'
RAW_VIDEO = os.path.join(BASE_DIR, '', 'Download.mp4')
RAW_AUDIO_DIR = os.path.join(BASE_DIR, 'audio')
AUDIO_SEGMENTS_DIR = os.path.join(BASE_DIR, 'audio_segments')
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, 'transcripts')
IMAGES_DIR = os.path.join(BASE_DIR, 'images')
LEFT_IMG_DIR = os.path.join(IMAGES_DIR, 'left')
RIGHT_IMG_DIR = os.path.join(IMAGES_DIR, 'right')

# Crée tous les dossiers s'ils n'existent pas
for folder in [RAW_AUDIO_DIR, AUDIO_SEGMENTS_DIR, TRANSCRIPTS_DIR, IMAGES_DIR, LEFT_IMG_DIR, RIGHT_IMG_DIR]:
    os.makedirs(folder, exist_ok=True)


# --- ETAPE 1 : EXTRAIRE L'AUDIO ---
def extract_audio():
    audio_path = os.path.join(RAW_AUDIO_DIR, 'audio_full.mp3')
    cmd = f'ffmpeg -i "{RAW_VIDEO}" -q:a 0 -map a "{audio_path}" -y'
    subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[✅] Audio extrait : {audio_path}")
    return audio_path


# --- ETAPE 2 : DECOUPER L'AUDIO (<2 min) ---
# Supprime les anciens fichiers
for file in os.listdir(AUDIO_SEGMENTS_DIR):
    if file.startswith("audio_") and file.endswith(".mp3"):
        os.remove(os.path.join(AUDIO_SEGMENTS_DIR, file))



def split_audio(audio_path, max_duration_sec=110):
    audio = AudioSegment.from_file(audio_path)
    segments = []

    total_parts = (len(audio) + max_duration_sec * 1000 - 1) // (max_duration_sec * 1000)

    print("[...] Découpage audio en segments...")
    for i in tqdm(range(total_parts), desc="Découpage", unit="segment"):
        start = i * max_duration_sec * 1000
        end = start + max_duration_sec * 1000
        segment = audio[start:end]
        
        filename = f"audio_{i + 1}.mp3"
        seg_path = os.path.join(AUDIO_SEGMENTS_DIR, filename)

        segment.export(seg_path, format="mp3")
        segments.append(seg_path)

    print(f"[✅] Audio découpé : {len(segments)} segments")
    return segments




# --- ETAPE 3 : TRANSCRIRE CHAQUE SEGMENT ---
def transcribe_segments(segments):
    print("[...] Chargement du modèle Whisper 'medium'...")
    model = whisper.load_model("medium")
    print("[✅] Modèle chargé.")

    text_with_timestamps = ""
    text_without_timestamps = ""

    print("[...] Transcription en cours des segments audio...")
    for seg in tqdm(segments, desc="Transcription", unit="segment"):
        result = model.transcribe(seg, language="fr")
        for phrase in result['segments']:
            start = round(phrase['start'], 2)
            end = round(phrase['end'], 2)
            text = phrase['text'].strip()
            text_with_timestamps += f"[{start} → {end}] {text}\n"
            text_without_timestamps += f"{text}\n"

        text_with_timestamps += "\n"
        text_without_timestamps += "\n"

    # Chemins des fichiers
    path_segments = os.path.join(TRANSCRIPTS_DIR, "transcription_segments.txt")
    path_full = os.path.join(TRANSCRIPTS_DIR, "transcription_full.txt")

    # Sauvegarde
    with open(path_segments, 'w', encoding='utf-8') as f:
        f.write(text_with_timestamps)

    with open(path_full, 'w', encoding='utf-8') as f:
        f.write(text_without_timestamps)

    print("[✅] Transcription enregistrée avec et sans timestamps.")
    return text_with_timestamps, text_without_timestamps



def analyze_speakers_with_gpt(full_text):
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    prompt = f"""
        🔧🎯 Prompt IA – Détection des intervenants et genres (version robuste, sans erreur)
        Lis attentivement le script de conversation ci-dessous et retourne exclusivement un tableau JSON listant tous les intervenants (y compris Mr Martin).

        ✅ Objectif :
        Pour chaque intervenant, fournis un objet JSON contenant :

        "nom" : un nom générique comme "intervenant 1", "intervenant 2", etc. (sauf Mr Martin)
        "genre" : "homme", "femme" ou "inconnu" selon les règles ci-dessous.

        🧠 Règles strictes de détection :
        🔹 1. Mr Martin :
        Toujours présent et toujours de genre "homme".

        🔹 2. Détection d’un nouvel intervenant :
        Considère qu’une nouvelle voix apparaît lorsque :
        - Il y a un changement de ton ou de style de langage évident.
        - Une phrase type est présente, comme : "Je vous passe quelqu’un", "Oui bonjour", "Oh ?", etc.

        🔹 3. Détection du genre d’un intervenant :
        ✅ S’il y a une correction directe de la part de l’intervenant :
        → Utilise ce genre.

        🔄 Sinon, regarde comment Mr Martin s’adresse à l’intervenant :
        → "Bonjour monsieur" → genre réel = "femme" (inversion volontaire).
        → "Bonjour madame" → genre réel = "homme" (inversion volontaire).

        ❓ Si aucune info de genre → "inconnu".

        🛑 Tu dois renommer tous les speakers détectés :
        - Le speaker qui fait les canulars (Mr Martin) s’appelle toujours **"Mr Martin"**.
        - Les autres speakers s’appellent **"intervenant 1"**, **"intervenant 2"**, etc., dans l’ordre de leur apparition (hors Mr Martin).

        ⚠️ Ne réponds **qu'avec du JSON valide** et rien d’autre. Aucun texte explicatif avant ou après.

        Format attendu :
        [
        {{"nom": "Mr Martin", "genre": "homme"}},
        {{"nom": "intervenant 1", "genre": "femme"}},
        ...
        ]

        Script :
        \"\"\"
        {full_text}
        \"\"\"
    """

    print("[...] Analyse des intervenants via GPT (nouvelle API)...")
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "user", "content": prompt}
        ],
        temperature=0.3
    )

    reply = response.choices[0].message.content
    try:
        speakers_data = json.loads(reply)
        print(f"[✅] Intervenants détectés : {len(speakers_data)}")

        # 💾 Sauvegarde dans output/intervenants.json
        os.makedirs("output", exist_ok=True)
        with open("output/intervenants.json", "w", encoding="utf-8") as f:
            json.dump(speakers_data, f, indent=2, ensure_ascii=False)
            print("💾 Fichier 'output/intervenants.json' enregistré.")

        return speakers_data
    except json.JSONDecodeError:
        print("[❌] Réponse non JSON :")
        print(reply)
        return []




# --- ETAPE 5 : GENERER PROMPT POUR IMAGE DE FOND ---
def generate_prompt(full_text):
    prompt_intro = """
    🎬 🎯 Prompt IA – Scène immersive absurde basée sur un script audio
    Crée une image photoréalistterte haute résolution, au format carré 9:8, inspirée d’un script de conversation absurde fourni en entrée.

    🎨 Style global :
    Ambiance réaliste moderne : architecture récente, éléments de décor contemporains (LEDs, enseignes lumineuses, mobilier récent, véhicules modernes).
    Éclairage naturel ou fluorescent, comme un matin clair ou un lieu public bien éclairé.
    Composition immersive, détails nets, textures visibles, profondeur de champ douce.

    🌀 Contraste absurde :
    Ajoute des éléments absurdes du script : objets déplacés, incohérences, panneaux mal traduits, mobilier mal placé, etc.

    🧩 Contexte :
    Lieu extérieur ou semi-intérieur logique selon le script. Aucun personnage visible, mais des traces d’activité humaine.

    📝 Extrait du script :
    """

    # ✂️ Couper et nettoyer le texte du script
    cleaned_text = full_text.replace("\n", " ").replace("  ", " ").strip()
    # Supprimer quelques termes à risque
    for word in banned_words:
        cleaned_text = cleaned_text.replace(word, "")

    script_excerpt = cleaned_text[:2500] + " [...]" if len(cleaned_text) > 2500 else cleaned_text

    final_prompt = prompt_intro.strip() + "\n\n" + script_excerpt

    # Sauvegarde pour debug si besoin
    prompt_path = os.path.join(IMAGES_DIR, "prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(final_prompt)

    print("[✅] Prompt généré avec script réduit.")
    return final_prompt



def generate_image_with_openai(prompt_text, output_path):
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    print("[...] Génération de l'image via DALL·E en cours...")

    # 🔒 Liste de mots à risque
    banned_words = [
        "femme", "femmes", "homme", "hommes", "droits", "violence",
        "égalité", "militer", "syndicat", "CGT", "sexuel", "genre",
        "banderole", "t-shirt", "contre", "pour", "discrimination",
        "manifestation", "militant", "militante"
    ]

    # 🧼 Nettoyage du prompt
    cleaned_text = prompt_text
    for word in banned_words:
        cleaned_text = cleaned_text.replace(word, " ")

    # 💾 Sauvegarde du prompt filtré pour debug
    os.makedirs("output", exist_ok=True)
    with open("output/prompt_nettoye.txt", "w", encoding="utf-8") as f:
        f.write(cleaned_text)

    # 🧠 Appel DALL·E
    try:
        response = client.images.generate(
            model="gpt-image-1",
            prompt=prompt_text,
        )

        image_base64 = response.data[0].b64_json
        image_bytes = base64.b64decode(image_base64)

        # Save the image to a file
        with open(output_path, "wb") as f:
            f.write(image_bytes)

        print(f"[✅] Image générée avec GPT enregistrée : {output_path}")
        return output_path

    except Exception as e:
        print(f"[❌] Génération image échouée : {e}")
        return None

# --- ETAPE 6 : CREER IMAGE PLACEHOLDER + DECOUPAGE 9:16 ---
def split_horizontal_image_to_tiktok_verticals():
    img_path = os.path.join(IMAGES_DIR, 'full_background.png')
    img = Image.open(img_path)

    expected_width, expected_height = 1080, 960
    if img.size != (expected_width, expected_height):
        print(f"[WARN] Redimension de l’image en {expected_width}x{expected_height} pour TikTok.")
        img = img.resize((expected_width, expected_height))

    # Découpe verticale
    left = img.crop((0, 0, 540, 960))         # Gauche
    right = img.crop((540, 0, 1080, 960))     # Droite

    left_path = os.path.join(LEFT_IMG_DIR, 'left_0.png')
    right_path = os.path.join(RIGHT_IMG_DIR, 'right_0.png')

    left.save(left_path)
    right.save(right_path)

    print("[✅] Deux images 9:16 générées pour TikTok.")
    return left_path, right_path

# 🔁 Mapping global des intervenants déjà associés à un personnage
mapping_personnages = {}

def load_intervenants_and_segments():
    intervenants_path = os.path.join("output", "intervenants.json")
    audio_segments_path = os.path.join("audio_segments")

    if not os.path.exists(intervenants_path):
        print("[❌] Le fichier intervenants.json est introuvable.")
        return []

    with open(intervenants_path, "r", encoding="utf-8") as f:
        intervenants = json.load(f)

    segments = [f for f in os.listdir(audio_segments_path) if f.endswith(".mp3")]
    segments.sort()

    combinaison = []
    
    # Copie de la liste des persos restants par genre
    personnages_restants = {
        "homme": personnages_disponibles["homme"].copy(),
        "femme": personnages_disponibles["femme"].copy()
    }

    for i, intervenant in enumerate(intervenants):
        nom = intervenant["nom"]
        genre = intervenant["genre"]

        if nom == "Mr Martin":
            personnage_adobe = "Sticky VQA.puppet-button"
        else:
            if nom not in mapping_personnages:
                if personnages_restants[genre]:
                    personnage_adobe = random.choice(personnages_restants[genre])
                    mapping_personnages[nom] = personnage_adobe
                    personnages_restants[genre].remove(personnage_adobe)
                else:
                    print(f"[⚠️] Plus de personnages disponibles pour le genre : {genre}")
                    personnage_adobe = "Default VQA.puppet-button"
                    mapping_personnages[nom] = personnage_adobe
            else:
                personnage_adobe = mapping_personnages[nom]

        intervenants[i]["personnage_adobe"] = personnage_adobe

    for segment in segments:
        for i, intervenant in enumerate(intervenants):
            combinaison.append({
                "segment_file": os.path.join(audio_segments_path, segment),
                "intervenant_nom": intervenant["nom"],
                "intervenant_genre": intervenant["genre"],
                "segment_index": segment.replace(".mp3", ""),
                "intervenant_index": i,
                "personnage_adobe": intervenant["personnage_adobe"]
            })

    # print(json.dumps(combinaison, indent=2, ensure_ascii=False))
    return combinaison


def run_automate_adobe(job):
    try:
        subprocess.run([
            "python", "automate_adobe.py",
            job["segment_file"],
            job["intervenant_nom"],
            job["intervenant_genre"],
            job["segment_index"],
            str(job["intervenant_index"]),
            job["personnage_adobe"]
        ], check=True)
        print(f"✅ Fini : {job['intervenant_nom']} - {job['segment_file']}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Erreur pendant l'exécution de automate_adobe.py pour {job['intervenant_nom']} : {e}")


def automate_generation_videos(max_threads=4):
    jobs = load_intervenants_and_segments()

    if not jobs:
        print("[❌] Aucun job à traiter.")
        return

    print(f"[...] Lancement en parallèle de {len(jobs)} jobs (max {max_threads} simultanés)")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_threads) as executor:
        executor.map(run_automate_adobe, jobs)

    print("✅ Toutes les vidéos animées ont été générées.")


def archive_outputs():
    parent_dir = os.path.dirname(BASE_DIR)
    count = len([d for d in os.listdir(parent_dir) if os.path.isdir(os.path.join(parent_dir, d)) and d.startswith("Video_")]) + 1
    archive_dir = os.path.join(parent_dir, f"Video_{count}")
    
    print(f"📁 Création du dossier d’archive : Video_{count}")
    os.makedirs(archive_dir, exist_ok=True)

    files_to_move = [
        RAW_VIDEO,
        os.path.join(RAW_AUDIO_DIR, "audio_full.mp3"),
        os.path.join(AUDIO_SEGMENTS_DIR, "audio_1.mp3"),
        os.path.join(AUDIO_SEGMENTS_DIR, "audio_2.mp3"),
        os.path.join(IMAGES_DIR, "prompt.txt"),
        os.path.join(IMAGES_DIR, "full_background.png"),
        os.path.join(LEFT_IMG_DIR, "left_0.png"),
        os.path.join(RIGHT_IMG_DIR, "right_0.png")
    ]

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


# --- PIPELINE PRINCIPAL ---
def main():
    print("📦 Traitement initial démarré...")
    audio_path = extract_audio()
    split_audio(audio_path)

    transcribe_segments_with_diarization( audio_path=AUDIO_PATH, hf_token=HUGGINGFACE_TOKEN )

    transcription_path = os.path.join(TRANSCRIPTS_DIR, "transcription_segments.txt")
    if not os.path.exists(transcription_path):
        print("[❌] Le fichier transcription_segments.txt est introuvable.")
        return []

    with open(transcription_path, "r", encoding="utf-8") as f:
        text_without_timestamps = f.read()

    analyze_speakers_with_gpt(text_without_timestamps)
    prompt = generate_prompt(text_without_timestamps)
    image_path = os.path.join(IMAGES_DIR, "full_background.png")
    # generate_image_with_openai(prompt, image_path)
    split_horizontal_image_to_tiktok_verticals()

    # Appel à automate_diarization et sauvegarde dans diarization/
    diarization_output_dir = os.path.join(BASE_DIR, "diarization")
    os.makedirs(diarization_output_dir, exist_ok=True)

    os.system("python automate_diarization.py")

    automate_generation_videos()
    archive_outputs()

    print("\n✅ Traitement terminé.")

if __name__ == "__main__":
    main()
