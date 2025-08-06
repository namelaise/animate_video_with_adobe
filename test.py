import whisper
from pyannote.audio import Pipeline
from datetime import timedelta
import json
import os

# 📂 Chemin de ton fichier audio
AUDIO_FILE = r"C:\Users\n.amelaise\Desktop\mr martin\script\audio_segments\audio_1.mp3"

# 🔐 Ton token Hugging Face (read only)
HF_TOKEN = "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"

# 📘 1. Transcription avec Whisper
print("🔊 Transcription en cours...")
model = whisper.load_model("medium")
result = model.transcribe(AUDIO_FILE, language="fr")
transcription = result["text"]
print(f"✅ Transcription : {transcription[:60]}...")

# 📗 2. Diarization avec pyannote.audio
print("🧠 Diarization avec pyannote.audio...")
pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=HF_TOKEN)
diarization = pipeline(AUDIO_FILE)

# 📙 3. Fusion des segments
segments = []
for turn, _, speaker in diarization.itertracks(yield_label=True):
    segment = {
        "start": str(timedelta(seconds=turn.start)),
        "end": str(timedelta(seconds=turn.end)),
        "speaker": speaker
    }
    segments.append(segment)
    print(f"[{segment['start']} → {segment['end']}] : {speaker}")

# 📁 4. Enregistrement JSON
output = {
    "transcription": transcription,
    "segments": segments
}

with open("output_diarization.json", "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

print("📦 Résultat enregistré dans output_diarization.json")
