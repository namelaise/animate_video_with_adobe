# === assemblage_tail.py (Version B — segments exacts par boucles, durée vidéo = durée audio) ===
import os
import re
import subprocess
import tempfile
from pathlib import Path

# ------------------------- Utilitaires -------------------------

def ffprobe_duration_seconds(path: str) -> float:
    """Retourne la durée (s) via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        path,
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8", "ignore").strip()
    try:
        return float(out)
    except Exception:
        return 0.0

def hms(sec: float) -> str:
    """Format hh:mm:ss.mmm lisible."""
    ms = int(round(sec * 1000))
    h = ms // 3600000; ms %= 3600000
    m = ms // 60000;   ms %= 60000
    s = ms / 1000.0
    return f"{h:02d}:{m:02d}:{s:06.3f}"

def parse_transcript_times(transcript_path: str):
    """
    Extrait les (start, end) à partir de lignes du type :
    [0.01 → 0.69] Mr Martin : ...
    Retourne la liste des tuples (start: float, end: float) dans l'ordre.
    """
    rx = re.compile(r"\[\s*(\d+(?:[.,]\d+)?)\s*[→\-]\s*(\d+(?:[.,]\d+)?)\s*\]")
    times = []
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            m = rx.search(line)
            if not m:
                continue
            start = float(m.group(1).replace(",", "."))
            end   = float(m.group(2).replace(",", "."))
            if end < start:
                start, end = end, start
            times.append((start, end))
    return times

def list_videos_sorted_by_trailing_index(video_dir: str):
    """
    Récupère *.mp4 et trie par le dernier nombre avant .mp4
    ex: 'Mr Martin - homme - 12.mp4' -> index 12
    """
    files = [p for p in Path(video_dir).glob("*.mp4")]
    def last_num(name: str):
        m = re.search(r"(\d+)(?=\.mp4$)", name)
        return int(m.group(1)) if m else 10**9
    return sorted(files, key=lambda p: last_num(p.name))

# ------------------------- Assembleur (Version B) -------------------------

def assemble_from_tail_with_transcript(
    video_segments_dir: str,
    transcript_path: str,
    output_path: str,
    *,
    full_audio_path: str | None = None,      # chemin de audio_full.mp3 (si None, on essaie BASE_DIR/audio/audio_full.mp3)
    crf: int = 18,
    preset: str = "veryfast",
    min_keep_sec: float = 0.10,
    unify_fps: float | None = None,          # ex: 24.0 / 25.0 / 30.0 ; None pour VFR natif
    force_fps: float | None = None,          # alias rétro-compat (équiv. unify_fps)
    audio_bitrate: str = "192k",
    cleanup_temp: bool = True,
    verbose_ffmpeg: bool = False,
):
    """
    Refonte "propre" :
      - Segment i = durée EXACTE = end_i - end_{i-1} (avec end_{-1}=0) → couvre le blanc initial.
      - On prend la TAIL (fin) du clip i. Si c'est trop court, on BOUCLE proprement (split/concat) jusqu'à la durée cible (sans freeze).
      - On ajuste le DERNIER segment (±) pour que somme(segments) == durée audio (± ~1 frame).
      - Pas de '-r' en sortie (pas de drift). Si besoin d'unifier la cadence, on applique fps=... dans le filtre.
      - Étape A : encode vidéo seule ; Étape B : mux avec audio (sans -shortest).
    """
    import math
    from pathlib import Path

    # ---------- Préparation des entrées ----------
    if unify_fps is None and force_fps is not None:
        unify_fps = force_fps  # rétro-compat

    segment_files = list_videos_sorted_by_trailing_index(video_segments_dir)
    times = parse_transcript_times(transcript_path)

    if not segment_files:
        print(f"❌ Aucun MP4 trouvé dans: {video_segments_dir}"); return None
    if not times:
        print(f"❌ Aucune ligne parsée depuis: {transcript_path}"); return None

    # déduire full_audio_path si absent
    if full_audio_path is None:
        base_dir = os.getenv("BASE_DIR") or ""
        candidate = Path(base_dir) / "audio" / "audio_full.mp3"
        full_audio_path = str(candidate)
    if not Path(full_audio_path).exists():
        print(f"❌ Audio complet introuvable : {full_audio_path}")
        print("   -> Passe un chemin valide via full_audio_path=...")
        return None

    n_pairs = min(len(segment_files), len(times))
    if len(segment_files) != len(times):
        print(f"⚠️ {len(segment_files)} vidéos vs {len(times)} segments transcript. Utilisation des {n_pairs} premiers.")

    # ---------- Durées cibles cumulatives : dur_i = end_i - end_{i-1} (end_{-1}=0) ----------
    ends = [e for (_, e) in times[:n_pairs]]
    targets = []
    prev_end = 0.0
    for e in ends:
        dur = max(min_keep_sec, e - prev_end)
        targets.append(dur)
        prev_end = e

    # ---------- Longueurs réelles des clips + durée audio ----------
    audio_len = ffprobe_duration_seconds(full_audio_path)
    clip_lens = [ffprobe_duration_seconds(str(p)) for p in segment_files]
    sum_targets = sum(targets)

    # ---------- Ajustement du DERNIER segment pour caler la vidéo sur l'audio ----------
    delta = audio_len - sum_targets
    if abs(delta) > 1e-3:
        old_last = targets[-1]
        new_last = max(min_keep_sec, old_last + delta)
        targets[-1] = new_last
        print(f"ℹ️ Ajustement dernier segment de {old_last:.3f}s → {new_last:.3f}s (delta total {delta:+.3f}s) "
              f"pour caler vidéo={audio_len:.3f}s sur audio.")

    # ---------- Construction du filter_complex : boucles exactes, sans freeze ----------
    ffmpeg_loglvl = "info" if verbose_ffmpeg else "error"
    cmd_inputs = ["ffmpeg", "-hide_banner", "-loglevel", ffmpeg_loglvl, "-y", "-fflags", "+genpts"]
    for p in segment_files[:n_pairs]:
        cmd_inputs += ["-i", str(p)]

    filter_parts = []
    concat_inputs = []

    print("=== Plan de coupe (TAIL + boucles, durées EXACTES) ===")
    for i in range(n_pairs):
        clip_len = max(0.0, clip_lens[i])
        target   = targets[i]

        # portion de base = TAIL du clip, au plus target
        base_len = min(max(min_keep_sec, clip_len), target) if clip_len > 0.0 else max(min_keep_sec, min(0.2, target))
        start_cut = max(0.0, clip_len - base_len)
        end_cut   = clip_len if clip_len > 0.0 else base_len

        # Découpe de la tail + remise à zéro des timestamps
        chain = f"[{i}:v]trim=start={start_cut:.6f}:end={end_cut:.6f},setpts=PTS-STARTPTS"
        if unify_fps is not None:
            chain += f",fps={unify_fps}"
        # On reste en RGB dans le filtre (neutre pour la couleur) ; la sortie sera encodée en YUV ensuite
        chain += f",format=rgb24[base{i}]"
        filter_parts.append(chain)

        if target <= base_len + 1e-6:
            # juste couper à la cible
            filter_parts.append(f"[base{i}]trim=start=0:end={target:.6f},setpts=PTS-STARTPTS[seg{i}]")
            loops = 1
        else:
            # on réplique base{i} suffisamment de fois, puis on coupe à 'target'
            reps = max(2, int(math.ceil(target / max(1e-3, base_len))))
            outs = "".join([f"[b{i}_{k}]" for k in range(reps)])
            filter_parts.append(f"[base{i}]split={reps}{outs}")
            filter_parts.append(f"{outs}concat=n={reps}:v=1:a=0[tmp{i}]")
            filter_parts.append(f"[tmp{i}]trim=start=0:end={target:.6f},setpts=PTS-STARTPTS[seg{i}]")
            loops = reps

        concat_inputs.append(f"[seg{i}]")

        def fmt(t):
            ms=int(round(t*1000)); h=ms//3600000; ms%=3600000; m=ms//60000; ms%=60000; s=ms/1000
            return f"{h:02d}:{m:02d}:{s:06.3f}"
        print(f"- [{i}] {segment_files[i].name} | Lclip={fmt(clip_len)} | cible={fmt(target)} "
              f"→ tail {fmt(base_len)} + loop x{loops}")

    # Concat de tous les segments
    filter_parts.append("".join(concat_inputs) + f"concat=n={n_pairs}:v=1:a=0[vcat]")
    filter_complex = ";".join(filter_parts)

    # ---------- Étape A : encode vidéo seule (pas de -r, pas d'audio) ----------
    tmp_dir = Path(tempfile.gettempdir())
    tmp_video_noaudio = tmp_dir / "concat_tail_video_nofreeze.mp4"
    try:
        tmp_video_noaudio.unlink(missing_ok=True)
    except Exception:
        pass

    cmd_a = cmd_inputs + [
        "-filter_complex", filter_complex,
        "-map", "[vcat]",
        "-an",
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", preset,
        "-pix_fmt", "yuv420p",          # compatible plateformes ; passe à 'yuv444p' si tu veux une fidélité couleur max
        "-movflags", "+faststart",
        "-video_track_timescale", "90000",
        str(tmp_video_noaudio)
    ]

    print("\n▶️  Étape A: build vidéo (boucles, durées exactes)…")
    run_a = subprocess.run(cmd_a, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if verbose_ffmpeg or run_a.returncode != 0:
        print(run_a.stdout)
    if run_a.returncode != 0 or (not tmp_video_noaudio.exists()) or tmp_video_noaudio.stat().st_size == 0:
        print("❌ Assemblage vidéo (sans audio) échoué."); return None

    # ---------- Étape B : mux vidéo + audio (durées égales par conception) ----------
    output_path = str(Path(output_path))
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd_b = [
        "ffmpeg", "-hide_banner", "-loglevel", ffmpeg_loglvl, "-y",
        "-i", str(tmp_video_noaudio),
        "-i", str(full_audio_path),
        "-map", "0:v:0",     # vidéo depuis le concat
        "-map", "1:a:0",     # audio depuis audio_full.mp3
        "-c:v", "copy",      # pas de ré-encodage vidéo à cette étape
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        # pas de '-shortest' : on a calé la durée vidéo sur la durée audio
        output_path
    ]

    print("\n▶️  Étape B: mux vidéo + full audio…")
    run_b = subprocess.run(cmd_b, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if verbose_ffmpeg or run_b.returncode != 0:
        print(run_b.stdout)
    if run_b.returncode != 0 or (not Path(output_path).exists()) or Path(output_path).stat().st_size == 0:
        print("❌ Mux final échoué."); return None

    # ---------- Vérifs ----------
    video_len = ffprobe_duration_seconds(output_path)
    audio_len2 = ffprobe_duration_seconds(full_audio_path)
    print(f"\n⏱️  Durée vidéo finale : {hms(video_len)} ({video_len:.3f} s)")
    print(f"🔊  Durée audio full   : {hms(audio_len2)} ({audio_len2:.3f} s)")
    print(f"🎯  Somme cibles (après ajustement dernier) : {hms(sum(targets))} ({sum(targets):.3f} s)")
    print(f"Δ   (vidéo - audio)    : {video_len - audio_len2:+.3f} s (attendu ≈ 0)")

    # ---------- Nettoyage ----------
    if cleanup_temp:
        try:
            tmp_video_noaudio.unlink(missing_ok=True)
        except Exception:
            pass

    return output_path

# Exemple d'utilisation (à commenter si intégré dans un autre script) :
# if __name__ == "__main__":
#     assemble_from_tail_with_transcript(
#         video_segments_dir=r"C:\path\to\video_segments",
#         transcript_path=r"C:\path\to\transcripts\transcription_segments_intervenants.txt",
#         output_path=r"C:\path\to\output\video_final.mp4",
#         full_audio_path=r"C:\path\to\audio\audio_full.mp3",
#         crf=18,
#         preset="veryfast",
#         min_keep_sec=0.10,
#         unify_fps=24,        # ou None pour garder la cadence native (VFR)
#         audio_bitrate="192k",
#         cleanup_temp=True,
#         verbose_ffmpeg=False,
#     )
