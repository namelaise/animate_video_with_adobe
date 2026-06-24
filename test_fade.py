#!/usr/bin/env python3
"""Test fade filter with enable on short clip — much faster than overlay or geq."""
import subprocess, os, json

WORK_DIR = r"C:\Users\n.amelaise\Desktop\martinV2\video_improvements_worktree"
os.chdir(WORK_DIR)

results = {}

# Test: fade in+out centered on t=5.0 using enable
# fade=type=in : brightens toward color (white)
# We chain fade_in + fade_out
# fade in: start at t=4.925, nb_frames = duration/2 * fps = 0.075 * 60 = 4.5 -> 5 frames
# fade out: start at t=5.0, nb_frames = 4.5 -> 5 frames, color=white

# Actually: to make a white flash centered at t:
#   fade=type=in:color=white:start_frame=N:nb_frames=F
#   But fade needs frame-based start, not time-based.
#   Better: use start_time parameter (available in newer ffmpeg)

# Test start_time parameter with fade
fg1 = "fade=type=in:color=white:start_time=4.925:duration=0.075,fade=type=out:color=white:start_time=5.0:duration=0.075"

cmd1 = ["ffmpeg", "-y", "-i", "test_short.mp4",
        "-vf", fg1, "-c:a", "copy", "-preset", "fast", "test_fade_flash.mp4"]

r1 = subprocess.run(cmd1, capture_output=True, text=True)
ok1 = r1.returncode == 0
results["fade_start_time"] = {
    "ok": ok1,
    "stderr": r1.stderr[-300:] if not ok1 else "OK",
    "size_mb": round(os.path.getsize("test_fade_flash.mp4")/1024/1024, 2) if ok1 and os.path.exists("test_fade_flash.mp4") else 0
}

with open(os.path.join(WORK_DIR, "test_fade_results.json"), "w") as f:
    json.dump(results, f, indent=2)

for k, v in results.items():
    print(f"{k}: {v}")
