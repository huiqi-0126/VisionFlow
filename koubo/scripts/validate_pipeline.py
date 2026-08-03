#!/usr/bin/env python3
"""Validate required artifacts and technical properties of a finished reel."""

import argparse
import json
import subprocess
from fractions import Fraction
from pathlib import Path


def load_json(path: Path):
    if not path.is_file():
        raise SystemExit(f"Missing required file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def probe(path: Path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_name,codec_type,width,height,"
            "r_frame_rate,sample_rate,channels,pix_fmt",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--final")
    args = parser.parse_args()

    root = Path(args.project).expanduser().resolve()
    manifest = load_json(root / "project.json")
    for key in ("reference_video", "character", "script", "language"):
        if not manifest.get(key):
            raise SystemExit(f"project.json missing: {key}")

    character = manifest["character"]
    if not character.get("name") or character.get("type") not in ("soul", "element"):
        raise SystemExit("Invalid character lock in project.json")

    plan = load_json(root / "analysis" / "shot_plan.json")
    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        raise SystemExit("shot_plan.json has no shots")

    previous_end = 0.0
    for index, shot in enumerate(shots, 1):
        label = shot.get("id") or f"shot {index}"
        for key in ("output_start", "output_end", "image_prompt", "video_prompt"):
            if key not in shot:
                raise SystemExit(f"{label} missing: {key}")
        start = float(shot["output_start"])
        end = float(shot["output_end"])
        if end <= start:
            raise SystemExit(f"{label} has invalid duration")
        if start + 0.001 < previous_end:
            raise SystemExit(f"{label} overlaps the previous shot")
        if not shot.get("approved"):
            raise SystemExit(f"{label} is not approved")
        if not (shot.get("image_path") or shot.get("image_job_id")):
            raise SystemExit(f"{label} has no generated image record")
        if not (shot.get("clip_path") or shot.get("clip_job_id")):
            raise SystemExit(f"{label} has no generated clip record")
        previous_end = end

    report = {
        "project": str(root),
        "shots": len(shots),
        "planned_duration": previous_end,
        "final": None,
    }
    if args.final:
        final_path = Path(args.final).expanduser().resolve()
        if not final_path.is_file():
            raise SystemExit(f"Final video not found: {final_path}")
        caption_path = root / "captions" / "captions.json"
        load_json(caption_path)
        final_probe = probe(final_path)
        report["final"] = final_probe

        output = plan.get("output") or {}
        streams = final_probe.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not video:
            raise SystemExit("Final file has no video stream")
        if not audio:
            raise SystemExit("Final file has no audio stream")

        expected_width = int(output.get("width", 0))
        expected_height = int(output.get("height", 0))
        expected_fps = float(output.get("fps", 0))
        expected_duration = float(output.get("duration", previous_end))
        if expected_width and int(video.get("width", 0)) != expected_width:
            raise SystemExit("Final width does not match shot plan")
        if expected_height and int(video.get("height", 0)) != expected_height:
            raise SystemExit("Final height does not match shot plan")
        actual_fps = float(Fraction(video.get("r_frame_rate", "0/1")))
        if expected_fps and abs(actual_fps - expected_fps) > 0.001:
            raise SystemExit("Final fps does not match shot plan")
        actual_duration = float(final_probe["format"]["duration"])
        tolerance = 1.5 / expected_fps if expected_fps else 0.06
        if abs(actual_duration - expected_duration) > tolerance:
            raise SystemExit("Final duration does not match shot plan")

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
