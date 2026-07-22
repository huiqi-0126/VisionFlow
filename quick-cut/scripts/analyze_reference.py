#!/usr/bin/env python3
"""Analyze a reference video's metadata and hard-cut timing with FFmpeg."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=True)


def probe(path: Path) -> dict:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,width,height,avg_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ]
    )
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not video:
        raise ValueError(f"No video stream found: {path}")
    duration = float(data["format"]["duration"])
    rate = video.get("avg_frame_rate", "0/1")
    fps = float(Fraction(rate)) if rate != "0/0" else 0.0
    return {
        "duration": duration,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": fps,
        "video_codec": video.get("codec_name"),
        "audio": audio,
    }


def detect_cuts(path: Path, threshold: float, min_gap: float, duration: float) -> list[float]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(path),
        "-vf",
        f"select='gt(scene,{threshold})',showinfo",
        "-an",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    times = [float(value) for value in PTS_RE.findall(result.stderr)]
    cuts = [0.0]
    for value in times:
        if value <= 0 or value >= duration:
            continue
        if value - cuts[-1] >= min_gap:
            cuts.append(value)
    cuts.append(duration)
    return cuts


def analyze(reference: Path, threshold: float = 0.08, min_gap: float = 0.055) -> dict:
    if not reference.is_file():
        raise FileNotFoundError(reference)
    metadata = probe(reference)
    cuts = detect_cuts(reference, threshold, min_gap, metadata["duration"])
    intervals = [round(cuts[i + 1] - cuts[i], 6) for i in range(len(cuts) - 1)]
    return {
        "reference": str(reference.resolve()),
        **metadata,
        "scene_threshold": threshold,
        "min_shot_gap": min_gap,
        "cuts": [round(value, 6) for value in cuts],
        "intervals": intervals,
        "shot_count": len(intervals),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scene-threshold", type=float, default=0.08)
    parser.add_argument("--min-shot-gap", type=float, default=0.055)
    args = parser.parse_args()

    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            parser.error(f"Required command not found: {binary}")

    try:
        result = analyze(args.reference, args.scene_threshold, args.min_shot_gap)
    except Exception as exc:
        print(f"quick-cut analysis failed: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
