# key_and_overlay.py
# Retire le fond vert d'une vidéo "acteur" et l'overlay sur un background
# Correction des points :
# - pas de scale2ref (déprécié) → scale vers la taille de l'acteur (probe via ffprobe)
# - overlay final unique [vout], mappé proprement
# - rotation BG iPhone (-90°) gérée (transpose auto si besoin)
# - "whites transparent" corrigés avec alpha-floor
# - HDR→SDR (HLG/BT.2020) optionnel via zscale (si compilé)
#
# Exemples :
#   python key_and_overlay.py --actor "video_final.mp4" --bg-dir "./video_background" --out "composite.mp4" \
#          --key-color "#00B140" --similarity 0.18 --blend 0.06 --alpha-floor 0.70 --verbose-ffmpeg
#
#   (max fidélité couleurs) :
#   python key_and_overlay.py --actor "video_final.mp4" --bg-dir "./video_background" --out "composite_rgb.mp4" \
#          --preserve-colors-strict

import os
import re
import random
import argparse
import subprocess
from pathlib import Path

# ---------- Utils ----------
def ffprobe_duration_seconds(path: str) -> float:
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

def ffprobe_wh(path: str) -> tuple[int, int]:
    """Retourne (width, height) du premier flux vidéo."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0", path
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8", "ignore").strip()
    m = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", out)
    if not m:
        return 0, 0
    return int(m.group(1)), int(m.group(2))

def hms(sec: float) -> str:
    ms = int(round(sec * 1000))
    h = ms // 3600000; ms %= 3600000
    m = ms // 60000;   ms %= 60000
    s = ms / 1000.0
    return f"{h:02d}:{m:02d}:{s:06.3f}"

def norm_color_hex(c: str) -> str:
    c = c.strip()
    if c.startswith("#") and len(c) == 7:
        return "0x" + c[1:]
    if re.match(r"^0x[0-9A-Fa-f]{6}$", c):
        return c
    raise SystemExit("key_color doit être '#RRGGBB' ou '0xRRGGBB'")

def list_bg_files(bg_dir: Path):
    files = []
    for ext in ("*.mp4", "*.mov", "*.mkv", "*.webm"):
        files += list(bg_dir.glob(ext))
    return files

# ---------- Core ----------
def key_and_overlay(
    actor_path: str,
    output_path: str,
    *,
    bg_path: str | None = None,
    bg_dir: str | None = "./video_background",
    key_color: str = "#00FF00",
    similarity: float = 0.16,
    blend: float = 0.06,
    alpha_floor: float = 0.65,     # protège les blancs (>= seuil → alpha=1)
    seed: int | None = None,
    preserve_colors_strict: bool = False,  # encode final en RGB (libx264rgb + range PC)
    final_pix_fmt: str = "yuv444p",        # si non-strict
    crf: int = 18,
    preset: str = "veryfast",
    verbose_ffmpeg: bool = False,
    hdr_to_sdr: bool = False,              # HLG/DV iPhone → SDR BT.709 via zscale (si dispo)
):
    """
    - Entrée 0: acteur (fond vert), Entrée 1: background
    - Key en RGBA → masque lissé + alpha-floor → alphamerge
    - Orientation : transpose auto si actor est vertical et BG horizontal (et vice-versa)
    - BG scaled aux dimensions exactes de l'acteur
    - Overlay final unique [vout]
    - Audio : on garde 0:a:0? (si présent), BG muet
    """
    if seed is not None:
        random.seed(seed)

    act = Path(actor_path)
    if not act.exists():
        raise SystemExit(f"[ERR] Acteur introuvable: {actor_path}")

    # Choix du background
    if bg_path:
        bg = Path(bg_path)
        if not bg.exists():
            raise SystemExit(f"[ERR] Background introuvable: {bg}")
    else:
        bgd = Path(bg_dir or ".")
        if not bgd.exists():
            raise SystemExit(f"[ERR] Dossier background introuvable: {bgd}")
        candidates = list_bg_files(bgd)
        if not candidates:
            raise SystemExit(f"[ERR] Aucun fichier vidéo dans {bgd}")
        bg = random.choice(candidates)

    kcol = norm_color_hex(key_color)

    # Durées et tailles
    actor_len = ffprobe_duration_seconds(str(act))
    bg_len    = ffprobe_duration_seconds(str(bg))
    if actor_len <= 0:
        raise SystemExit("[ERR] Durée acteur invalide (ffprobe).")

    actor_w, actor_h = ffprobe_wh(str(act))
    bg_w, bg_h       = ffprobe_wh(str(bg))
    if actor_w <= 0 or actor_h <= 0:
        raise SystemExit("[ERR] Dimensions acteur invalides (ffprobe).")

    # Décide si on boucle le BG
    if bg_len >= actor_len + 0.5:
        use_loop = False
        max_start = max(0.0, bg_len - actor_len)
        bg_start = random.uniform(0.0, max_start)
    else:
        use_loop = True
        bg_start = random.uniform(0.0, max(0.0, bg_len - 1.0)) if bg_len > 1.0 else 0.0

    # Orientation : si l'acteur est vertical (h>w) et le BG horizontal (w>h) → transpose BG (clockwise)
    #               si acteur horizontal et BG vertical → transpose BG (anticlockwise)
    need_transpose = False
    transpose_dir = None  # "clock" ou "cclock"
    if actor_h > actor_w and bg_w > bg_h:
        need_transpose = True
        transpose_dir = "clock"
    elif actor_h < actor_w and bg_h > bg_w:
        need_transpose = True
        transpose_dir = "cclock"

    # Filter graph (linéaire, une seule sortie [vout])
    steps = []

    # 0 = acteur
    steps.append("[0:v]setpts=PTS-STARTPTS,setsar=1,format=rgba[fg0]")

    # 1 = background
    bg_chain = "[1:v]setpts=PTS-STARTPTS,setsar=1"
    if hdr_to_sdr:
        # Conversion HLG/BT.2020 → BT.709 (si zscale dispo)
        # iPhone 14 HLG: transferin=arib-std-b67, primariesin=bt2020
        bg_chain += ",zscale=transferin=arib-std-b67:transfer=bt709:primariesin=bt2020:primaries=bt709"
    if need_transpose:
        if transpose_dir == "clock":
            bg_chain += ",transpose=clock"
        else:
            bg_chain += ",transpose=cclock"
    # Scale vers EXACT actor_w x actor_h
    bg_chain += f",scale={actor_w}:{actor_h}:flags=bicubic,format=rgb24[bgs]"
    steps.append(bg_chain)

    # Key + matte cleanup + alpha-floor
    # clé primaire
    steps.append(f"[fg0]chromakey={kcol}:{similarity:.3f}:{blend:.3f},format=rgba[fgk]")
    # masque (pas de deflate qui peut "manger" les blancs)
    steps.append("[fgk]alphaextract,boxblur=2:1[amask0]")
    # alpha-floor : >= seuil → 255 (opaque)
    thr = int(round(max(0.0, min(1.0, alpha_floor)) * 255))
    steps.append(f"[amask0]lut='if(gte(val,{thr}),255,val)'[amask]")
    # réinjection
    steps.append("[fgk][amask]alphamerge[fgfix]")

    # Overlay final
    steps.append("[bgs][fgfix]overlay=shortest=1:format=auto[vout]")

    filter_complex = ";".join(steps)

    # Commande ffmpeg
    ffmpeg_loglvl = "info" if verbose_ffmpeg else "error"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", ffmpeg_loglvl, "-y",
        # 0: acteur
        "-i", str(act),
    ]
    # 1: background (avec/ sans loop)
    if use_loop:
        cmd += ["-stream_loop", "-1", "-ss", f"{bg_start:.3f}", "-t", f"{actor_len:.3f}", "-i", str(bg)]
    else:
        cmd += ["-ss", f"{bg_start:.3f}", "-t", f"{actor_len:.3f}", "-i", str(bg)]

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "0:a:0?",          # garde l'audio de l'acteur si présent
    ]

    if preserve_colors_strict:
        # Fidélité couleur max (RGB)
        cmd += [
            "-c:v", "libx264rgb",
            "-pix_fmt", "rgb24",
            "-color_range", "pc",
            "-crf", str(crf),
            "-preset", preset,
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        ]
    else:
        # YUV 4:4:4 pour bonnes couleurs (ou passe --final-pix-fmt yuv420p si compat stricte)
        cmd += [
            "-c:v", "libx264",
            "-profile:v", "high444",
            "-pix_fmt", final_pix_fmt,
            "-color_range", "pc",
            "-crf", str(crf),
            "-preset", preset,
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        ]

    cmd += [
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-video_track_timescale", "90000",
        str(output_path)
    ]

    print(f"▶️  Key & Overlay… BG='{bg.name}', loop={use_loop}, start≈{bg_start:.2f}s, "
          f"cover={hms(actor_len)}, actor={actor_w}x{actor_h}, bg={bg_w}x{bg_h}, transpose={'yes' if need_transpose else 'no'}")

    if verbose_ffmpeg:
        print("— filter_complex —")
        print(filter_complex)

    run = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if verbose_ffmpeg or run.returncode != 0:
        print(run.stdout)
    if run.returncode != 0 or (not Path(output_path).exists()) or Path(output_path).stat().st_size == 0:
        raise SystemExit("❌ échec FFmpeg: key/overlay")

    print("✅ Composite OK →", output_path)
    return str(output_path)

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actor", required=True, help="Chemin de la vidéo acteur (fond vert)")
    ap.add_argument("--out",   required=True, help="Chemin de sortie du composite")
    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument("--bg", type=str, default=None, help="Chemin d'une vidéo de background")
    g.add_argument("--bg-dir", type=str, default="./video_background", help="Dossier contenant des backgrounds")
    ap.add_argument("--key-color", type=str, default="#00FF00", help="Couleur clé (ex. #00B140)")
    ap.add_argument("--similarity", type=float, default=0.16, help="Tolérance de clé (0.12–0.22 typique)")
    ap.add_argument("--blend", type=float, default=0.06, help="Adoucissement des bords (0.03–0.10)")
    ap.add_argument("--alpha-floor", type=float, default=0.65, help="Seuil (0..1) ≥ lequel alpha=1 (protège les blancs)")
    ap.add_argument("--seed", type=int, default=None, help="Seed aléatoire pour le choix/offset du BG")
    ap.add_argument("--preserve-colors-strict", action="store_true",
                    help="Encode final en RGB (libx264rgb + range PC) pour fidélité couleur maximale")
    ap.add_argument("--final-pix-fmt", type=str, default="yuv444p",
                    help="Pix fmt final si non-strict (ex: yuv444p, yuv420p). Par défaut yuv444p.")
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--preset", type=str, default="veryfast")
    ap.add_argument("--verbose-ffmpeg", action="store_true")
    ap.add_argument("--hdr-to-sdr", action="store_true",
                    help="Convertit HLG/BT.2020 -> BT.709 via zscale si ta build le supporte")

    args = ap.parse_args()

    key_and_overlay(
        actor_path=args.actor,
        output_path=args.out,
        bg_path=args.bg,
        bg_dir=args.bg_dir,
        key_color=args.key_color,
        similarity=args.similarity,
        blend=args.blend,
        alpha_floor=args.alpha_floor,
        seed=args.seed,
        preserve_colors_strict=args.preserve_colors_strict,
        final_pix_fmt=args.final_pix_fmt,
        crf=args.crf,
        preset=args.preset,
        verbose_ffmpeg=args.verbose_ffmpeg,
        hdr_to_sdr=args.hdr_to_sdr,
    )

if __name__ == "__main__":
    main()
