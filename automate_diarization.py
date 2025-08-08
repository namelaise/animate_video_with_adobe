import whisper
from tqdm import tqdm
import sys
import os
import torch

os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["SPEECHBRAIN_LOCAL_FILE"] = "1"

def load_whisper_model(size="medium"):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"📦 Chargement du modèle Whisper '{size}' sur {device.upper()}...")
    return whisper.load_model(size, device=device)

def transcribe_segments_with_diarization(audio_path, hf_token=None, output_dir="transcripts", whisper_model="medium"):
    """
    Transcrit un fichier audio sans diarisation (Whisper seul)
    et enregistre les fichiers :
      - transcription_full.txt (texte uniquement)

    :param audio_path: chemin vers le fichier audio à traiter
    :param hf_token: ignoré ici
    :param output_dir: dossier où sauvegarder les transcriptions
    :param whisper_model: modèle Whisper à utiliser ("small", "medium", etc.)
    :return: chemin du fichier de sortie
    """
    print("[...] Chargement du modèle Whisper...")
    model = load_whisper_model(whisper_model)
    print("✅ Modèle Whisper chargé.")

    print("[...] Transcription de l'audio complet...")
    result = model.transcribe(audio_path, language="fr")
    full_text = result["text"].strip()

    os.makedirs(output_dir, exist_ok=True)
    path_full = os.path.join(output_dir, "transcription_full.txt")

    with open(path_full, "w", encoding="utf-8") as f:
        f.write(full_text)

    print("✅ Fichier de transcription généré : transcription_full.txt")
    return path_full