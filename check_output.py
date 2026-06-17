#!/usr/bin/env python3
"""Check the output video and write results."""
import subprocess, os, json

WORK_DIR = r"C:\Users\n.amelaise\Desktop\martinV2\video_improvements_worktree"
os.chdir(WORK_DIR)

files = ["test_video_transitions.mp4", "test_short_flash2.mp4", "test_short.mp4"]
results = {}
for f in files:
    if os.path.exists(f):
        size = os.path.getsize(f)
        # ffprobe to check duration
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration,size",
             "-of", "json", f],
            capture_output=True, text=True
        )
        data = json.loads(r.stdout) if r.returncode == 0 else {}
        duration = float(data.get("format", {}).get("duration", 0))
        results[f] = {"size_bytes": size, "size_mb": round(size/1024/1024, 2), "duration_s": round(duration, 2)}
    else:
        results[f] = "NOT FOUND"

with open(os.path.join(WORK_DIR, "check_output_results.json"), "w") as fp:
    json.dump(results, fp, indent=2)

for k, v in results.items():
    print(f"{k}: {v}")
