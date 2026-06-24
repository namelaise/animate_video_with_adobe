#!/usr/bin/env python3
"""Kill any running ffmpeg, then run add_transitions on full video."""
import subprocess, os, json, time, signal

WORK_DIR = r"C:\Users\n.amelaise\Desktop\martinV2\video_improvements_worktree"
os.chdir(WORK_DIR)

# Kill any running ffmpeg
subprocess.run(["taskkill", "/f", "/im", "ffmpeg.exe"], capture_output=True)
print("Killed any running ffmpeg")

import sys
sys.path.insert(0, WORK_DIR)
from add_transitions import apply_transitions

timestamps = [5.0, 12.0, 20.0, 30.0, 42.0]
input_video = "test_video.mp4"
output_video = "test_video_transitions.mp4"

t0 = time.time()
try:
    apply_transitions(input_video, output_video, timestamps)
    elapsed = round(time.time() - t0, 1)
    size_mb = round(os.path.getsize(output_video) / 1024 / 1024, 2) if os.path.exists(output_video) else 0
    result = {"ok": True, "elapsed_s": elapsed, "size_mb": size_mb, "timestamps": timestamps}
except SystemExit:
    elapsed = round(time.time() - t0, 1)
    result = {"ok": False, "elapsed_s": elapsed}

with open(os.path.join(WORK_DIR, "run_final_results.json"), "w") as f:
    json.dump(result, f, indent=2)

print(f"Result: {result}")
