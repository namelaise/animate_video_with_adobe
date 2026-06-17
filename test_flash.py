#!/usr/bin/env python3
"""Test script that runs ffmpeg synchronously and writes results to a file."""
import subprocess, os, sys, json

WORK_DIR = r"C:\Users\n.amelaise\Desktop\martinV2\video_improvements_worktree"
os.chdir(WORK_DIR)

RESULTS = []

def run(label, cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    ok = r.returncode == 0
    RESULTS.append({
        "label": label,
        "ok": ok,
        "stderr_tail": r.stderr[-300:] if not ok else "",
    })
    if ok:
        out = cmd[-1]
        size = os.path.getsize(out) / 1024 / 1024 if os.path.exists(out) else 0
        RESULTS[-1]["size_mb"] = round(size, 2)
    return ok

# Step 1: create 15s short clip
run("create_short", ["ffmpeg", "-y", "-i", "test_video.mp4", "-t", "15", "-c", "copy", "test_short.mp4"])

# Step 2: test overlay with gte*lte enable (lowercase t = PTS in seconds for enable expressions)
fg = (
    "color=color=white:size=1080x1920:rate=60/1,format=rgba,colorchannelmixer=aa=0.500[white];"
    "[0:v][white]overlay=enable='gte(t,4.925)*lte(t,5.075)':shortest=0[v1]"
)
ok = run("overlay_gte_lte", ["ffmpeg", "-y", "-i", "test_short.mp4",
    "-filter_complex", fg,
    "-map", "[v1]", "-map", "0:a", "-c:a", "copy", "-preset", "fast",
    "test_short_flash2.mp4"])

if not ok:
    # Try between(t,...) lowercase
    fg2 = (
        "color=color=white:size=1080x1920:rate=60/1,format=rgba,colorchannelmixer=aa=0.500[white];"
        "[0:v][white]overlay=enable='between(t,4.925,5.075)':shortest=0[v1]"
    )
    run("overlay_between_lower", ["ffmpeg", "-y", "-i", "test_short.mp4",
        "-filter_complex", fg2,
        "-map", "[v1]", "-map", "0:a", "-c:a", "copy", "-preset", "fast",
        "test_short_flash3.mp4"])

# Write results
with open("test_flash_results.json", "w") as f:
    json.dump(RESULTS, f, indent=2)
print("Done. Results written to test_flash_results.json")
for r in RESULTS:
    print(r)
