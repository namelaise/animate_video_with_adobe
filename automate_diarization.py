import whisper
from pyannote.audio import Pipeline
from pydub import AudioSegment
from tqdm import tqdm
import sys
import os

os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["SPEECHBRAIN_LOCAL_FILE"] = "1"



def transcribe_segments_with_diarization(audio_path, hf_token, output_dir="transcripts", whisper_model="medium"):
    """
    Transcrit un fichier audio avec diarisation (pyannote + Whisper)
    et enregistre les fichiers :
      - transcription_segments.txt (avec speakers)
      - transcription_full.txt (sans speakers)
    
    :param audio_path: chemin vers le fichier audio à traiter
    :param hf_token: token d'accès HuggingFace
    :param output_dir: dossier où sauvegarder les transcriptions
    :param whisper_model: modèle Whisper à utiliser ("small", "medium", etc.)
    :return: chemins des deux fichiers de sortie
    """
    print("[...] Chargement des modèles...")

    # Redirige tous les logs dans le néant
    # sys.stdout = open(os.devnull, 'w')
    # sys.stderr = open(os.devnull, 'w')

    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization", use_auth_token=hf_token)
    model = whisper.load_model(whisper_model)
    
    print("[✅] Modèles chargés.")

    print("[...] Diarisation de l'audio...")
    diarization = pipeline(audio_path)

    results = []
    os.makedirs("audio", exist_ok=True)

    # Remet les flux à la normale
    # sys.stdout = sys.__stdout__
    # sys.stderr = sys.__stderr__

    print("[...] Transcription segmentée par intervenant...")
   
    results = []

    # Crée la barre de progression
    segments_total = sum(1 for _ in diarization.itertracks(yield_label=True))
    for i, (turn, _, speaker) in tqdm(enumerate(diarization.itertracks(yield_label=True)), total=segments_total, desc="⏳ Traitement des segments", unit="segment"):
        start_ms = int(turn.start * 1000)
        end_ms = int(turn.end * 1000)

        audio = AudioSegment.from_file(audio_path)[start_ms:end_ms]
        seg_path = f"audio/segment_{i}.wav"
        # audio.export(seg_path, format="wav")

        result = model.transcribe(seg_path, language="fr")
        text = result["text"].strip()

        results.append({
            "start": round(turn.start, 2),
            "end": round(turn.end, 2),
            "speaker": speaker,
            "text": text
        })

    os.makedirs(output_dir, exist_ok=True)

    path_segments = os.path.join(output_dir, "transcription_segments.txt")
    path_full = os.path.join(output_dir, "transcription_full.txt")

    with open(path_segments, "w", encoding="utf-8") as f:
        for r in results:
            f.write(f"[{r['start']}s - {r['end']}s] {r['speaker']} : {r['text']}\n")

    with open(path_full, "w", encoding="utf-8") as f:
        for r in results:
            f.write(f"{r['text']}\n")

    print("[✅] Fichiers de transcription générés.")
    return path_segments, path_full