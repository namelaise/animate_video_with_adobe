#!/usr/bin/env python3
"""
add_progress_bar.py — Barre de progression rouge en bas de la vidéo.
Usage: python add_progress_bar.py <input.mp4> <output.mp4>
"""
import sys
import os
import subprocess


def apply_progress_bar(input_video: str, output_video: str, bar_height: int = 8, bar_y_offset: int = 12):
    """
    Ajoute une barre de progression rouge en bas.
    - Fond noir semi-transparent sur toute la largeur
    - Barre rouge qui s'étend de gauche à droite selon t/duration
    """
    # On utilise drawbox deux fois :
    # 1. fond noir semi-transparent sur toute la largeur
    # 2. barre rouge dont la largeur = iw * (t / duration)
    # duration récupérée via ffprobe
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_video],
        capture_output=True, text=True
    )
    duration = float(result.stdout.strip())
    print(f"Durée vidéo : {duration:.2f}s")

    bar_y = f"ih-{bar_y_offset}"  # position y = hauteur - offset

    vf = (
        # Fond noir semi-transparent derrière toute la largeur
        f"drawbox=x=0:y={bar_y}:w=iw:h={bar_height}:color=black@0.5:t=fill,"
        # Barre rouge progressive
        f"drawbox=x=0:y={bar_y}:w=iw*t/{duration:.3f}:h={bar_height}:color=0xDC1C1C@0.85:t=fill"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", input_video,
        "-vf", vf,
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_video,
    ]
    print("VF:", vf)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("ERREUR ffmpeg (returncode={})".format(proc.returncode))
        stderr_lines = proc.stderr.strip().splitlines()
        print("\n".join(stderr_lines[:5]))
        print("--- tentative avec geq ---")
        _apply_progress_bar_geq(input_video, output_video, duration, bar_height, bar_y_offset)
        return
    size_mb = os.path.getsize(output_video) / 1024 / 1024
    print(f"OK -> {output_video} ({size_mb:.1f} Mo)")


def _apply_progress_bar_geq(input_video: str, output_video: str, duration: float,
                              bar_height: int = 8, bar_y_offset: int = 12):
    """
    Fallback : utilise geq pour dessiner la barre de progression.
    geq opère pixel par pixel avec accès à X, Y, W, H, T (temps en secondes).
    - Zone barre : ih-bar_y_offset <= Y < ih-bar_y_offset+bar_height
    - Fond noir semi-transparent partout dans la zone
    - Barre rouge là où X < iw*T/duration
    """
    # geq avec blending : on écrase les pixels dans la zone barre
    # r/g/b/a expressions — on travaille sur chaque canal
    # Dans la zone barre :
    #   si X < W*T/duration → rouge DC1C1C @ 85% blend
    #   sinon → pixel original assombri à 50% (fond noir semi-transparent)
    # Hors zone : pixel original inchangé

    bar_top = f"(H-{bar_y_offset})"
    bar_bot = f"(H-{bar_y_offset}+{bar_height})"

    # in_bar = 1 si dans la zone verticale de la barre
    in_zone = f"between(Y,{bar_top},{bar_bot})"
    # in_progress = 1 si dans la partie progression
    in_prog = f"(X<W*T/{duration:.3f})"

    # Pour chaque canal :
    # pixel original * (1 - in_zone) + in_zone * (in_prog * bar_color + (1-in_prog) * pixel*0.5)
    # rouge du fond de barre : 0xDC = 220, 0x1C = 28
    r_expr = f"r(X,Y)*(1-{in_zone}) + {in_zone}*({in_prog}*(0.85*220 + 0.15*r(X,Y)) + (1-{in_prog})*(0.5*r(X,Y)))"
    g_expr = f"g(X,Y)*(1-{in_zone}) + {in_zone}*({in_prog}*(0.85*28  + 0.15*g(X,Y)) + (1-{in_prog})*(0.5*g(X,Y)))"
    b_expr = f"b(X,Y)*(1-{in_zone}) + {in_zone}*({in_prog}*(0.85*28  + 0.15*b(X,Y)) + (1-{in_prog})*(0.5*b(X,Y)))"

    vf = f"geq=r='{r_expr}':g='{g_expr}':b='{b_expr}'"

    cmd = [
        "ffmpeg", "-y",
        "-i", input_video,
        "-vf", vf,
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_video,
    ]
    print("VF (geq):", vf[:200], "...")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("ERREUR geq (returncode={})".format(proc.returncode))
        stderr_lines = proc.stderr.strip().splitlines()
        print("\n".join(stderr_lines[:5]))
        sys.exit(1)
    size_mb = os.path.getsize(output_video) / 1024 / 1024
    print(f"OK (geq) -> {output_video} ({size_mb:.1f} Mo)")


def main():
    inp = sys.argv[1] if len(sys.argv) > 1 else "test_video.mp4"
    out = sys.argv[2] if len(sys.argv) > 2 else "test_video_progress.mp4"
    apply_progress_bar(inp, out)


if __name__ == "__main__":
    main()
