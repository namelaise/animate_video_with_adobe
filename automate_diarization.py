# automate_diarization.py
# Prérequis: pip install assemblyai
# AAI_API_KEY doit être défini dans l'environnement

import os
import json
import subprocess
import shutil
import assemblyai as aai
from typing import List, Dict, Any

# ---------- Utils ----------

def ffprobe_duration_seconds(path: str) -> float:
    """Retourne la durée (s) via ffprobe, 0.0 si indisponible."""
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            path
        ], stderr=subprocess.STDOUT).decode("utf-8", "ignore").strip()
        return float(out)
    except Exception:
        return 0.0

def hms(sec: float) -> str:
    ms = int(round(sec * 1000))
    h = ms // 3600000; ms %= 3600000
    m = ms // 60000;   ms %= 60000
    s = ms / 1000.0
    return f"{h:02d}:{m:02d}:{s:06.3f}"

def compute_gaps(segments: List[Dict[str, Any]], end_of_audio: float, gap_min_s: float = 0.10):
    """Retourne la liste des (gap_start, gap_end, duration) entre segments successifs + fin (si manquante)."""
    gaps = []
    for i in range(len(segments) - 1):
        a_end = segments[i]["end"]
        b_start = segments[i+1]["start"]
        if b_start > a_end + gap_min_s - 1e-9:
            gaps.append((a_end, b_start, b_start - a_end))
    # gap final si la diarisation ne va pas jusqu'à la fin de l'audio
    if end_of_audio > 0 and segments:
        last_end = segments[-1]["end"]
        if end_of_audio > last_end + gap_min_s:
            gaps.append((last_end, end_of_audio, end_of_audio - last_end))
    return gaps

# ---------- Core ----------

def transcribe_segments_with_diarization(
    audio_path: str,
    output_dir: str = "transcripts",
    language: str = "fr",
    speakers_expected: int | None = None,   # si tu sais qu'il y a 2 interlocuteurs au téléphone, essaie 2
    format_text: bool = True,
    add_blank_tail: bool = True,            # ajoute un segment BLANC si la diarisation s'arrête avant la fin audio
    min_gap_for_blank: float = 0.1,        # seuil min pour considérer un "blanc"
):
    """
    Transcrit un fichier audio via AssemblyAI, avec diarisation activée.
    Écrit:
      - transcription_full.txt                  (texte complet)
      - diarization_segments.json               (liste des utterances: start/end/speaker/text)
      - transcription_segments.txt              (version lisible)
      - transcription_segments_with_blanks.txt  (idem + lignes [BLANC X.XXs])
      - speakers.json                           (stats simples)
      - blanks.json                             (liste des blancs détectés)
    Retourne: (path_json, path_spk, path_txt, path_full, segments)

    Remarque: pas d’analyse de genre/Mr Martin ici — tu le fais après.
    """
    api_key = os.getenv("AAI_API_KEY")
    if not api_key:
        raise RuntimeError("AAI_API_KEY non défini.")

    aai.settings.api_key = api_key
    os.makedirs(output_dir, exist_ok=True)

    # --- Durée audio réelle (pour vérifier la fin couverte) ---
    audio_len = ffprobe_duration_seconds(audio_path)
    if audio_len > 0:
        print(f"ℹ️ Durée audio (ffprobe): {hms(audio_len)} ({audio_len:.3f}s)")
    else:
        print("ℹ️ Durée audio non disponible (ffprobe introuvable ou échec).")

    # --- Config AAI (diarisation ON) ---
    # Notes:
    # - speech_model=aai.SpeechModel.best est déjà le plus précis.
    # - speakers_expected: si tu sais qu'il y a ~2 interlocuteurs, fixe 2.
    #   Sinon laisse None (AAI estimera).
    # - format_text=True: ponctuation + normalisation légère.
    cfg = aai.TranscriptionConfig(
        speech_model=aai.SpeechModel.best,
        language_code=language,
        speaker_labels=True,
        speakers_expected=speakers_expected,
        format_text=format_text,
        # Conseils additionnels : si tu as un stéréo "caller gauche / callee droite",
        # active le split par canaux (si ton audio le permet) :
        # dual_channel=True,
        # word_boost=["cognac","tombola","festival"],  # optionnel: boost lexical si certains mots sont clés
        # boost_param=aai.BoostParam.high,             # si word_boost utilisé
    )

    # --- Transcription synchrone ---
    tr = aai.Transcriber().transcribe(audio_path, config=cfg)
    if tr.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"Transcription failed: {tr.error}")

    full_text = (tr.text or "").strip()
    print(f"ℹ️ AAI: status={tr.status}, texte={len(full_text)} chars")

    # --- Utterances -> segments ---
    segments = []
    if tr.utterances:
        for utt in tr.utterances:
            segments.append({
                "start": (utt.start or 0) / 1000.0,
                "end":   (utt.end   or 0) / 1000.0,
                "speaker": f"SPK{utt.speaker}" if utt.speaker is not None else "SPK?",
                "text": (utt.text or "").strip(),
            })
    else:
        # Fallback minimal si pas d'utterances (rare)
        segments.append({
            "start": 0.0,
            "end": 0.0,
            "speaker": "SPK?",
            "text": full_text,
        })

    # --- Vérif couverture fin audio & ajout BLANC de queue si nécessaire ---
    if add_blank_tail and audio_len > 0 and segments:
        last_end = segments[-1]["end"]
        tail_gap = audio_len - last_end
        if tail_gap > min_gap_for_blank:
            print(f"⚠️ Diarisation s'arrête avant la fin: last_end={hms(last_end)} ; fin audio={hms(audio_len)} ; gap={tail_gap:.3f}s")
            segments.append({
                "start": last_end,
                "end": audio_len,
                "speaker": "BLANC",
                "text": "",  # pas de texte pour un blanc
            })

    # --- Écritures fichiers principaux ---
    path_full = os.path.join(output_dir, "transcription_full.txt")
    path_json = os.path.join(output_dir, "diarization_segments.json")
    path_txt  = os.path.join(output_dir, "transcription_segments.txt")
    path_spk  = os.path.join(output_dir, "speakers.json")

    with open(path_full, "w", encoding="utf-8") as f:
        f.write(full_text)

    with open(path_json, "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    with open(path_txt, "w", encoding="utf-8") as f:
        for s in segments:
            # On n'insère pas les BLANCs ici: version "propre"
            if s["speaker"] != "BLANC":
                f.write(f"[{s['start']:.2f} → {s['end']:.2f}] {s['speaker']} : {s['text']}\n")

    # --- Détection & export des BLANCS internes (incluant blanc final si ajouté) ---
    blanks = compute_gaps(segments=[s for s in segments if s["speaker"] != "BLANC"], end_of_audio=audio_len, gap_min_s=min_gap_for_blank)
    path_blanks = os.path.join(output_dir, "blanks.json")
    with open(path_blanks, "w", encoding="utf-8") as f:
        json.dump([
            {"start": a, "end": b, "duration": d} for (a, b, d) in blanks
        ], f, ensure_ascii=False, indent=2)

    # --- Version "lisible" avec insertion explicite des blancs ---
    path_txt_with_blanks = os.path.join(output_dir, "transcription_segments_with_blanks.txt")
    with open(path_txt_with_blanks, "w", encoding="utf-8") as f:
        # ré-écrit en insérant des lignes [BLANC X.XXs] entre segments quand il y a gap
        clean_segments = [s for s in segments if s["speaker"] != "BLANC"]
        for i, s in enumerate(clean_segments):
            f.write(f"[{s['start']:.2f} → {s['end']:.2f}] {s['speaker']} : {s['text']}\n")
            if i < len(clean_segments) - 1:
                a_end = s["end"]
                b_start = clean_segments[i+1]["start"]
                if b_start > a_end + min_gap_for_blank - 1e-9:
                    f.write(f"[{a_end:.2f} → {b_start:.2f}] BLANC : (silence {b_start - a_end:.2f}s)\n")
        # blanc final si la diarisation coupe avant la fin:
        if audio_len > 0 and clean_segments:
            last_end = clean_segments[-1]["end"]
            if audio_len > last_end + min_gap_for_blank:
                f.write(f"[{last_end:.2f} → {audio_len:.2f}] BLANC : (silence {audio_len - last_end:.2f}s)\n")

    # --- speakers.json basique ---
    speakers_index: Dict[str, Dict[str, Any]] = {}
    for s in segments:
        speakers_index.setdefault(s["speaker"], {"num_segments": 0})
        speakers_index[s["speaker"]]["num_segments"] += 1
    with open(path_spk, "w", encoding="utf-8") as f:
        json.dump(speakers_index, f, ensure_ascii=False, indent=2)

    # --- Logs finaux utiles ---
    last_end_overall = max((s["end"] for s in segments), default=0.0)
    print("✅ Fichiers générés :")
    print(" -", path_full)
    print(" -", path_json)
    print(" -", path_txt)
    print(" -", path_txt_with_blanks)
    print(" -", path_blanks)
    print(" -", path_spk)
    print(f"⏱️ Couverture diarisation: last_end={hms(last_end_overall)} ; fin audio={hms(audio_len)} ; Δ={audio_len - last_end_overall:+.3f}s")

    return path_json, path_spk, path_txt, path_full, segments


# Petit test manuel (optionnel) :
if __name__ == "__main__":
    AUDIO = os.getenv("AUDIO_PATH", "audio_full.mp3")
    OUT = os.getenv("TRANSCRIPTS_DIR", "transcripts")
    # Si tu sais que c'est un appel téléphonique à 2 interlocuteurs principaux, essaie speakers_expected=2
    transcribe_segments_with_diarization(
        AUDIO, OUT,
        language="fr",
        speakers_expected=None,
        add_blank_tail=True,
        min_gap_for_blank=0.10
    )
