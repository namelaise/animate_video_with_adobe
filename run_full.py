#!/usr/bin/env python3
"""Run add_transitions on the full video and write result to a JSON file."""
import subprocess, os, sys, json, time

WORK_DIR = r"C:\Users\n.amelaise\Desktop\martinV2\video_improvements_worktree"
os.chdir(WORK_DIR)
sys.path.insert(0, WORK_DIR)

from add_transitions import get_video_info, build_flash_filtergraph

timestamps = [5.0, 12.0, 20.0, 30.0, 42.0]
input_video = "test_video.mp4"
output_video = "test_video_transitions.mp4"

width, height, fps = get_video_info(input_video)
filtergraph, out_pad = build_flash_filtergraph(
    width, height, fps, timestamps, duration=0.15, intensity=0.50
)

cmd = [
    "ffmpeg", "-y",
    "-i", input_video,
    "-filter_complex", filtergraph,
    "-map", out_pad,
    "-map", "0:a",
    "-c:a", "copy",
    "-preset", "fast",
    output_video,
]

print(f"Video: {width}x{height} @ {fps}")
print(f"Timestamps: {timestamps}")
print(f"Running ffmpeg...")

t0 = time.time()
r = subprocess.run(cmd, capture_output=True, text=True)
elapsed = round(time.time() - t0, 1)

result = {
    "timestamps": timestamps,
    "returncode": r.returncode,
    "elapsed_s": elapsed,
    "stderr_tail": r.stderr[-500:] if r.returncode != 0 else "",
}

if r.returncode == 0 and os.path.exists(output_video):
    size_mb = round(os.path.getsize(output_video) / 1024 / 1024, 2)
    result["output"] = output_video
    result["size_mb"] = size_mb
    print(f"OK -> {output_video} ({size_mb} Mo) in {elapsed}s")
else:
    print(f"FAIL after {elapsed}s")
    print(r.stderr[-500:])

with open("run_full_results.json", "w") as f:
    json.dump(result, f, indent=2)
print("Results written to run_full_results.json")
