#!/usr/bin/env python3
"""
add_transitions.py — Fondu noir très court aux changements de speaker.
Usage: python add_transitions.py <input.mp4> <output.mp4> [timestamps_csv]
timestamps_csv: ex "4.2,8.5,12.1"
"""
import sys
import os
import subprocess

def build_fade_filter(timestamps: list, duration: float = 0.10) -> str:
    """
    Pour chaque timestamp t, crée un dip vers le noir :
    - fade out (vers noir) de t-half à t
    - fade in  (depuis noir) de t à t+half
    Chaque fade est restreint à sa fenêtre via enable= pour ne pas affecter le reste.
    """
    half = round(duration / 2, 4)
    filters = []
    for t in timestamps:
        t0 = round(t - half, 4)
        t1 = round(t, 4)
        t2 = round(t + half, 4)
        # fade to black — actif uniquement entre t0 et t1
        filters.append(
            f"fade=type=out:color=black:start_time={t0}:duration={half}:enable='gte(t,{t0})*lte(t,{t1})'"
        )
        # fade from black — actif uniquement entre t1 et t2
        filters.append(
            f"fade=type=in:color=black:start_time={t1}:duration={half}:enable='gte(t,{t1})*lte(t,{t2})'"
        )
    return ",".join(filters)


def apply_transitions(input_video: str, output_video: str, timestamps: list):
    vf = build_fade_filter(timestamps, duration=0.10)
    print("Timestamps:", timestamps)

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

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("ERREUR ffmpeg:")
        print(result.stderr[-2000:])
        sys.exit(1)
    size_mb = os.path.getsize(output_video) / 1024 / 1024
    print(f"OK -> {output_video} ({size_mb:.1f} Mo)")


def main():
    input_path  = sys.argv[1] if len(sys.argv) > 1 else "test_video.mp4"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "test_video_transitions.mp4"

    if len(sys.argv) > 3:
        timestamps = [float(t) for t in sys.argv[3].split(",")]
    else:
        timestamps = [5.0, 12.0, 20.0, 30.0, 42.0]

    apply_transitions(input_path, output_path, timestamps)


if __name__ == "__main__":
    main()
