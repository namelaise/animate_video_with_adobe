#!/usr/bin/env python3
"""
add_hook_overlay.py — Badge TikTok style : fond rouge arrondi + texte blanc gras Impact.
Usage: python add_hook_overlay.py <input.mp4> <output.mp4> [texte]
"""
import sys
import os
import subprocess
import tempfile
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/ariblk.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

def load_font(size: int) -> ImageFont.FreeTypeFont:
    for fp in FONT_CANDIDATES:
        if os.path.exists(fp):
            print(f"Font utilisée : {fp}")
            return ImageFont.truetype(fp, size)
    raise RuntimeError("Aucune font trouvée sur ce système")

def wrap_text(text: str, font, max_width: int) -> list:
    """Coupe en 2 lignes si le texte dépasse max_width."""
    dummy = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy)
    bbox = draw.textbbox((0, 0), text, font=font)
    if bbox[2] - bbox[0] <= max_width:
        return [text]
    words = text.split()
    best_split = 1
    best_diff = float('inf')
    for i in range(1, len(words)):
        line1 = ' '.join(words[:i])
        line2 = ' '.join(words[i:])
        b1 = draw.textbbox((0, 0), line1, font=font)[2]
        b2 = draw.textbbox((0, 0), line2, font=font)[2]
        diff = abs(b1 - b2)
        if diff < best_diff:
            best_diff = diff
            best_split = i
    return [' '.join(words[:best_split]), ' '.join(words[best_split:])]

def create_badge_png(text: str, out_path: str, font_size: int = 64, max_badge_width: int = 900) -> tuple:
    font = load_font(font_size)
    lines = wrap_text(text, font, max_badge_width - 68)  # 68 = 2*pad_x

    dummy = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy)

    # Mesure précise avec getmetrics pour éviter l'espace mort d'Impact
    ascent, descent = font.getmetrics()
    line_h = ascent + descent  # hauteur réelle du glyphe
    line_spacing = 8
    pad_x, pad_y = 34, 16

    # Largeur = max des largeurs de chaque ligne
    line_widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_widths.append(bbox[2] - bbox[0])

    badge_w = max(line_widths) + 2 * pad_x
    badge_h = len(lines) * line_h + (len(lines) - 1) * line_spacing + 2 * pad_y
    radius = badge_h // 2

    img = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([0, 0, badge_w - 1, badge_h - 1], radius=radius, fill=(220, 28, 28, 235))

    for i, line in enumerate(lines):
        lw = line_widths[i]
        x = (badge_w - lw) // 2
        y = pad_y + i * (line_h + line_spacing)
        # outline
        for ox in range(-2, 3):
            for oy in range(-2, 3):
                if ox != 0 or oy != 0:
                    draw.text((x + ox, y + oy), line, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))

    img.save(out_path, "PNG")
    print(f"Badge : {badge_w}x{badge_h}px, {len(lines)} ligne(s)")
    return badge_w, badge_h

def apply_badge_to_video(
    input_video: str,
    output_video: str,
    badge_png: str,
    badge_w: int,
    badge_h: int,
    video_w: int = 1080,
    y_pos: int = 120,
    display_seconds: int = 15,
):
    x_pos = (video_w - badge_w) // 2
    filter_complex = (
        f"[1:v]format=rgba[badge];"
        f"[0:v][badge]overlay={x_pos}:{y_pos}:enable=lte(t\\,{display_seconds})"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", input_video,
        "-i", badge_png,
        "-filter_complex", filter_complex,
        "-c:a", "copy",
        "-preset", "fast",
        output_video,
    ]
    print("CMD:", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("STDERR:", result.stderr[-800:])
        sys.exit(1)
    size_mb = os.path.getsize(output_video) / 1024 / 1024
    print(f"OK -> {output_video} ({size_mb:.1f} Mo)")

def main():
    input_path  = sys.argv[1] if len(sys.argv) > 1 else "test_video.mp4"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "test_video_hook.mp4"
    hook_text   = sys.argv[3] if len(sys.argv) > 3 else "Ce patron n'en revient toujours pas..."

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        badge_path = tmp.name

    try:
        badge_w, badge_h = create_badge_png(hook_text, badge_path)
        apply_badge_to_video(input_path, output_path, badge_path, badge_w, badge_h)
    finally:
        if os.path.exists(badge_path):
            os.remove(badge_path)

if __name__ == "__main__":
    main()
