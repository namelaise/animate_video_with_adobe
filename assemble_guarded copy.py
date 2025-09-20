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

import math
import tempfile
from pathlib import Path
import subprocess
import os
import re

# ---------- Helpers ----------
def run_ffmpeg(cmd: list[str], verbose: bool=False):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if verbose or p.returncode != 0:
        print(p.stdout)
    if p.returncode != 0:
        raise RuntimeError("FFmpeg failed")

def build_looped_segment(base_clip: Path, target_sec: float, out_path: Path, fps: float | None):
    """
    Construit un segment exactement 'target_sec' en bouclant base_clip.
    Utilise -stream_loop + -t. Re-encode H.264 pour éviter VFR foireux.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base_len = max(0.001, ffprobe_duration_seconds(str(base_clip)))
    loop_count = max(0, math.ceil(target_sec / base_len) - 1)  # ffmpeg attend nb boucles (sans la 1re)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if loop_count > 0:
        cmd += ["-stream_loop", str(loop_count)]
    cmd += ["-i", str(base_clip), "-t", f"{target_sec:.6f}", "-an"]
    vf = []
    if fps is not None:
        vf.append(f"fps={fps}")
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += [
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-threads", "2",
        str(out_path),
    ]
    run_ffmpeg(cmd)

# ---------- Version sous-processus par segment ----------
def assemble_from_tail_with_transcript(
    video_segments_dir: str,
    transcript_path: str,
    output_path: str,
    *,
    full_audio_path: str | None = None,
    crf: int = 18,                  # utilisé seulement à l'étape A si tu veux ré-encoder; ici on copie au concat
    preset: str = "veryfast",
    min_keep_sec: float = 0.10,
    unify_fps: float | None = None,
    force_fps: float | None = None,
    audio_bitrate: str = "192k",
    cleanup_temp: bool = True,
    verbose_ffmpeg: bool = False,
):
    if unify_fps is None and force_fps is not None:
        unify_fps = force_fps

    segment_files = list_videos_sorted_by_trailing_index(video_segments_dir)
    times = parse_transcript_times(transcript_path)

    if not segment_files:
        print(f"❌ Aucun MP4 trouvé dans: {video_segments_dir}"); return None
    if not times:
        print(f"❌ Aucune ligne parsée depuis: {transcript_path}"); return None

    if full_audio_path is None:
        base_dir = os.getenv("BASE_DIR") or ""
        candidate = Path(base_dir) / "audio" / "audio_full.mp3"
        full_audio_path = str(candidate)
    if not Path(full_audio_path).exists():
        print(f"❌ Audio complet introuvable : {full_audio_path}")
        return None

    n_pairs = min(len(segment_files), len(times))
    if len(segment_files) != len(times):
        print(f"⚠️ {len(segment_files)} vidéos vs {len(times)} segments transcript. Utilisation des {n_pairs} premiers.")

    # Durées cibles (end_i - end_{i-1})
    ends = [e for (_, e) in times[:n_pairs]]
    targets = []
    prev_end = 0.0
    for e in ends:
        dur = max(min_keep_sec, e - prev_end)
        targets.append(dur)
        prev_end = e

    audio_len = ffprobe_duration_seconds(full_audio_path)
    clip_lens = [ffprobe_duration_seconds(str(p)) for p in segment_files]
    sum_targets = sum(targets)

    # Ajuster le dernier segment pour coller pile à l'audio
    delta = audio_len - sum_targets
    if abs(delta) > 1e-3:
        old_last = targets[-1]
        targets[-1] = max(min_keep_sec, old_last + delta)
        print(f"ℹ️ Ajustement dernier segment {old_last:.3f}s → {targets[-1]:.3f}s (delta {delta:+.3f}s)")

    tmp_dir = Path(tempfile.gettempdir())
    built_segments = []

    print("=== Construction segments individuels (tail + loop) ===")
    for i in range(n_pairs):
        clip_len = max(0.0, clip_lens[i])
        target   = targets[i]

        base_len = min(max(min_keep_sec, clip_len), target) if clip_len > 0.0 else max(min_keep_sec, min(0.2, target))
        start_cut = max(0.0, clip_len - base_len)
        end_cut   = clip_len if clip_len > 0.0 else base_len

        # 1) extraire la TAIL dans base_i.mp4
        base_i = tmp_dir / f"tail_base_{i}.mp4"
        cut_cmd = [
            "ffmpeg","-hide_banner","-loglevel","error","-y",
            "-ss", f"{start_cut:.6f}",
            "-to", f"{end_cut:.6f}",
            "-i", str(segment_files[i]),
            "-an",
        ]
        vf = []
        if unify_fps is not None:
            vf.append(f"fps={unify_fps}")
        if vf:
            cut_cmd += ["-vf", ",".join(vf)]
        cut_cmd += [
            "-c:v","libx264","-crf", str(crf), "-preset", preset,
            "-pix_fmt","yuv420p","-movflags","+faststart",
            "-threads","2",
            str(base_i),
        ]
        run_ffmpeg(cut_cmd, verbose_ffmpeg)

        # 2) boucler jusqu'à target dans seg_i.mp4
        seg_i = tmp_dir / f"seg_{i}.mp4"
        build_looped_segment(base_i, target, seg_i, unify_fps)
        built_segments.append(seg_i)

        # Nettoyage du base_i pour libérer disque/handles tôt
        try: base_i.unlink(missing_ok=True)
        except: pass

    # 3) concat final vidéo-seule (copy)
    concat_list = tmp_dir / "concat_list.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for p in built_segments:
            f.write(f"file '{p.as_posix()}'\n")

    tmp_video_noaudio = tmp_dir / "concat_tail_video_nofreeze.mp4"
    print("\n▶️  Étape A: build vidéo (concat demuxer)…")
    cmd_concat = [
        "ffmpeg","-hide_banner","-loglevel","error","-y",
        "-f","concat","-safe","0","-i", str(concat_list),
        "-c","copy",
        "-movflags","+faststart",
        str(tmp_video_noaudio),
    ]
    run_ffmpeg(cmd_concat, verbose_ffmpeg)

    # 4) mux vidéo + audio (copie vidéo)
    output_path = str(Path(output_path))
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    print("\n▶️  Étape B: mux vidéo + full audio…")
    cmd_mux = [
        "ffmpeg","-hide_banner","-loglevel","error","-y",
        "-i", str(tmp_video_noaudio),
        "-i", str(full_audio_path),
        "-map","0:v:0","-map","1:a:0",
        "-c:v","copy",
        "-c:a","aac","-b:a", audio_bitrate,
        "-movflags","+faststart",
        "-threads","2",
        output_path,
    ]
    run_ffmpeg(cmd_mux, verbose_ffmpeg)

    # Vérifs (optionnel)
    video_len = ffprobe_duration_seconds(output_path)
    audio_len2 = ffprobe_duration_seconds(full_audio_path)
    print(f"\n⏱️  Durée vidéo finale : {hms(video_len)} ({video_len:.3f} s)")
    print(f"🔊  Durée audio full   : {hms(audio_len2)} ({audio_len2:.3f} s)")
    print(f"Δ   (vidéo - audio)    : {video_len - audio_len2:+.3f} s (attendu ≈ 0)")

    # Nettoyage
    if cleanup_temp:
        try: tmp_video_noaudio.unlink(missing_ok=True)
        except: pass
        try: concat_list.unlink(missing_ok=True)
        except: pass
        for p in built_segments:
            try: p.unlink(missing_ok=True)
            except: pass

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
