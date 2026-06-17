#!/usr/bin/env python3
"""
post_process_video.py — Applique tous les effets visuels en une seule passe ffmpeg.
Usage: python post_process_video.py <input.mp4> <output.mp4> <hook_text> [timestamps_csv]
Ex:    python post_process_video.py video_final.mp4 video_processed.mp4 "Ce patron flippe..." "4.2,8.5,20.1"
"""
import sys
import os
import subprocess
import tempfile
from PIL import Image, ImageDraw, ImageFont

# ── Paramètres ────────────────────────────────────────────────────────────────
BADGE_FONT_SIZE   = 64
BADGE_MAX_WIDTH   = 900
BADGE_Y_POS       = 120
BADGE_DISPLAY_SEC = 15
FADE_DURATION     = 0.10   # secondes, dip noir
BAR_HEIGHT        = 8
BAR_Y_OFFSET      = 12

FONT_CANDIDATES = [
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/ariblk.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

# ── Badge PNG (Pillow) ────────────────────────────────────────────────────────

def load_font(size):
    for fp in FONT_CANDIDATES:
        if os.path.exists(fp):
            return ImageFont.truetype(fp, size)
    raise RuntimeError("Aucune font trouvée")

def wrap_text(text, font, max_width):
    dummy = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy)
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return [text]
    words = text.split()
    best_split, best_diff = 1, float('inf')
    for i in range(1, len(words)):
        b1 = draw.textbbox((0, 0), ' '.join(words[:i]), font=font)[2]
        b2 = draw.textbbox((0, 0), ' '.join(words[i:]), font=font)[2]
        diff = abs(b1 - b2)
        if diff < best_diff:
            best_diff, best_split = diff, i
    return [' '.join(words[:best_split]), ' '.join(words[best_split:])]

def create_badge_png(text, out_path, font_size=BADGE_FONT_SIZE, max_width=BADGE_MAX_WIDTH):
    font = load_font(font_size)
    lines = wrap_text(text, font, max_width - 68)
    dummy = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    line_spacing, pad_x, pad_y = 8, 34, 16
    line_widths = [draw.textbbox((0, 0), l, font=font)[2] for l in lines]
    badge_w = max(line_widths) + 2 * pad_x
    badge_h = len(lines) * line_h + (len(lines)-1) * line_spacing + 2 * pad_y
    radius = badge_h // 2
    img = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, badge_w-1, badge_h-1], radius=radius, fill=(220, 28, 28, 235))
    for i, line in enumerate(lines):
        x = (badge_w - line_widths[i]) // 2
        y = pad_y + i * (line_h + line_spacing)
        for ox in range(-2, 3):
            for oy in range(-2, 3):
                if ox != 0 or oy != 0:
                    draw.text((x+ox, y+oy), line, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
    img.save(out_path, "PNG")
    return badge_w, badge_h

# ── Filtres ffmpeg ────────────────────────────────────────────────────────────

def build_fade_filter(timestamps, duration=FADE_DURATION):
    half = round(duration / 2, 4)
    parts = []
    for t in timestamps:
        t0, t1, t2 = round(t-half, 4), round(t, 4), round(t+half, 4)
        parts.append(f"fade=type=out:color=black:start_time={t0}:duration={half}:enable='gte(t,{t0})*lte(t,{t1})'")
        parts.append(f"fade=type=in:color=black:start_time={t1}:duration={half}:enable='gte(t,{t1})*lte(t,{t2})'")
    return ",".join(parts) if parts else "null"

def get_duration(video_path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True)
    return float(r.stdout.strip())

# ── Pipeline principal ────────────────────────────────────────────────────────

def post_process(input_video, output_video, hook_text, timestamps):
    import time
    t_start = time.time()

    duration = get_duration(input_video)
    print(f"Durée : {duration:.2f}s | Timestamps : {timestamps}")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        badge_path = tmp.name

    try:
        badge_w, badge_h = create_badge_png(hook_text, badge_path)
        x_badge = (1080 - badge_w) // 2

        # Filtres vidéo chaînés sur [0:v]
        fade_filter = build_fade_filter(timestamps)
        progress_filter = (
            f"drawbox=x=0:y=ih-{BAR_Y_OFFSET}:w=iw:h={BAR_HEIGHT}:color=black@0.5:t=fill,"
            f"drawbox=x=0:y=ih-{BAR_Y_OFFSET}:w=iw*t/{duration:.3f}:h={BAR_HEIGHT}:color=0xDC1C1C@0.85:t=fill"
        )

        # filter_complex :
        # 1. Applique fades + progress sur [0:v] → [vfx]
        # 2. Overlay badge PNG sur [vfx] → [out]
        filter_complex = (
            f"[0:v]{fade_filter},{progress_filter}[vfx];"
            f"[1:v]format=rgba[badge];"
            f"[vfx][badge]overlay={x_badge}:{BADGE_Y_POS}:enable='lte(t,{BADGE_DISPLAY_SEC})'[out]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", input_video,
            "-i", badge_path,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "0:a",
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            output_video,
        ]

        print("Lancement ffmpeg...")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print("ERREUR ffmpeg:")
            print(proc.stderr[-2000:])
            sys.exit(1)

        elapsed = time.time() - t_start
        size_mb = os.path.getsize(output_video) / 1024 / 1024
        print(f"OK -> {output_video} ({size_mb:.1f} Mo) en {elapsed:.1f}s")

    finally:
        if os.path.exists(badge_path):
            os.remove(badge_path)


def main():
    if len(sys.argv) < 4:
        print("Usage: python post_process_video.py <input.mp4> <output.mp4> <hook_text> [timestamps_csv]")
        sys.exit(1)
    inp  = sys.argv[1]
    out  = sys.argv[2]
    text = sys.argv[3]
    ts   = [float(t) for t in sys.argv[4].split(",")] if len(sys.argv) > 4 else [5.0, 12.0, 20.0, 30.0, 42.0, 60.0, 80.0, 100.0]
    post_process(inp, out, text, ts)

if __name__ == "__main__":
    main()
