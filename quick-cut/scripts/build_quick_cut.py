#!/usr/bin/env python3
"""Render a CLI-only quick-cut montage from a reference and local source clips."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from analyze_reference import analyze, probe


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def find_font(italic: bool = False) -> str:
    candidates = (
        [
            "C:/Windows/Fonts/arialbi.ttf",
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
        ]
        if italic
        else [
            "C:/Windows/Fonts/arialbd.ttf",
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    )
    return next((path for path in candidates if Path(path).exists()), candidates[-1])


def split_title(title: str, accent: str | None, all_white: bool) -> tuple[str, str]:
    title = " ".join(title.split())
    if all_white:
        return title, ""
    if accent:
        accent = " ".join(accent.split())
        if title.lower().endswith(accent.lower()):
            return title[: -len(accent)].strip(), accent
        return title, accent
    words = title.split(maxsplit=1)
    return (words[0], words[1]) if len(words) == 2 else (title, "")


def make_title_overlay(
    path: Path,
    title: str,
    accent: str | None,
    all_white: bool,
    width: int,
    height: int,
    white: str,
    yellow: str,
) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Pillow is required: python3 -m pip install Pillow") from exc

    primary, emphasis = split_title(title, accent, all_white)
    font_size = max(24, round(width * 0.034))
    font = ImageFont.truetype(find_font(False), font_size)
    italic = ImageFont.truetype(find_font(True), font_size)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    gap = round(font_size * 0.5) if primary and emphasis else 0
    pbox = draw.textbbox((0, 0), primary, font=font, stroke_width=0)
    ebox = draw.textbbox((0, 0), emphasis, font=italic, stroke_width=0)
    pw = pbox[2] - pbox[0]
    ew = ebox[2] - ebox[0]
    total = pw + gap + ew
    x = round((width - total) / 2)
    y = round(height * 0.5 - font_size * 0.55)
    shadow = (0, 0, 0, 150)
    if primary:
        draw.text((x + 1, y + 2), primary, font=font, fill=shadow)
        draw.text((x, y), primary, font=font, fill=white)
        x += pw + gap
    if emphasis:
        draw.text((x + 1, y + 2), emphasis, font=italic, fill=shadow)
        draw.text((x, y), emphasis, font=italic, fill=yellow)
    canvas.save(path)


def source_filter(view_index: int, source_count: int, width: int, height: int, fps: int, grade: str) -> str:
    is_alternate = view_index >= source_count
    if is_alternate:
        zoom = 1.10 if view_index == source_count else 1.13
        sw, sh = round(width * zoom / 2) * 2, round(height * zoom / 2) * 2
        x = round((sw - width) * (0.35 if view_index == source_count else 0.65))
        y = round((sh - height) * 0.5)
        framing = f"scale={sw}:{sh},crop={width}:{height}:x={x}:y={y}"
    else:
        framing = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    return f"{framing},{grade},fps={fps},format=yuv420p"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--sources", required=True, nargs="+", type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--accent")
    parser.add_argument("--all-white", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1920)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--scene-threshold", type=float, default=0.08)
    parser.add_argument("--min-shot-gap", type=float, default=0.055)
    parser.add_argument("--white", default="#FFFFFF")
    parser.add_argument("--yellow", default="#F1CF69")
    parser.add_argument(
        "--grade-filter",
        default="eq=contrast=1.035:brightness=-0.025:saturation=0.88:gamma=0.97,colorbalance=rs=.018:gs=.004:bs=-.012",
    )
    parser.add_argument("--no-reference-audio", action="store_true")
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args()

    if not args.title.strip():
        parser.error("--title cannot be empty; use --all-white for a single-color title")
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            parser.error(f"Required command not found: {binary}")
    missing = [str(path) for path in [args.reference, *args.sources] if not path.is_file()]
    if missing:
        parser.error("Missing input file(s): " + ", ".join(missing))

    info = analyze(args.reference, args.scene_threshold, args.min_shot_gap)
    source_info = [probe(path) for path in args.sources]
    duration = info["duration"]
    total_frames = max(1, math.ceil(duration * args.fps))
    frame_boundaries = sorted(
        set([0, total_frames] + [max(1, min(total_frames - 1, round(t * args.fps))) for t in info["cuts"][1:-1]])
    )
    if len(frame_boundaries) < 2:
        parser.error("Could not derive a usable timeline from the reference")

    work_parent = args.output.parent.resolve()
    work_parent.mkdir(parents=True, exist_ok=True)
    temp_context = tempfile.TemporaryDirectory(prefix="quick-cut-", dir=work_parent)
    work = Path(temp_context.name)
    segments = work / "segments"
    segments.mkdir()
    overlay = work / "title.png"
    montage = work / "montage.mp4"
    concat_list = work / "concat.txt"
    make_title_overlay(overlay, args.title, args.accent, args.all_white, args.width, args.height, args.white, args.yellow)

    source_count = len(args.sources)
    view_count = source_count + min(2, source_count)
    mapping: list[dict] = []
    concat_lines: list[str] = []

    try:
        for shot, (start_frame, end_frame) in enumerate(zip(frame_boundaries, frame_boundaries[1:])):
            frames = end_frame - start_frame
            view_index = shot % view_count
            source_index = view_index if view_index < source_count else view_index - source_count
            source = args.sources[source_index]
            segment_duration = frames / args.fps
            available = source_info[source_index]["duration"]
            nominal_start = 0.35 + view_index * 0.11
            source_start = max(0.0, min(nominal_start, available - segment_duration - 0.08))
            vf = source_filter(view_index, source_count, args.width, args.height, args.fps, args.grade_filter)
            segment = segments / f"segment_{shot + 1:03d}.mp4"
            run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{source_start:.6f}",
                    "-i",
                    str(source),
                    "-an",
                    "-vf",
                    vf,
                    "-frames:v",
                    str(frames),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "16",
                    "-pix_fmt",
                    "yuv420p",
                    "-color_primaries",
                    "bt709",
                    "-color_trc",
                    "bt709",
                    "-colorspace",
                    "bt709",
                    str(segment),
                    "-y",
                ]
            )
            concat_lines.append(f"file '{segment}'")
            mapping.append(
                {
                    "shot": shot + 1,
                    "timeline_start": round(start_frame / args.fps, 6),
                    "timeline_end": round(end_frame / args.fps, 6),
                    "source": str(source.resolve()),
                    "source_start": round(source_start, 6),
                    "alternate_crop": view_index >= source_count,
                }
            )

        concat_list.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                str(montage),
                "-y",
            ]
        )

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(montage),
            "-loop",
            "1",
            "-i",
            str(overlay),
        ]
        if not args.no_reference_audio:
            command += ["-i", str(args.reference)]
        command += [
            "-filter_complex",
            "[0:v][1:v]overlay=0:0:format=auto,format=yuv420p[v]",
            "-map",
            "[v]",
        ]
        if not args.no_reference_audio:
            command += ["-map", "2:a:0?"]
        command += [
            "-t",
            f"{total_frames / args.fps:.6f}",
            "-r",
            str(args.fps),
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
        ]
        if not args.no_reference_audio:
            command += ["-c:a", "aac", "-b:a", "256k", "-ar", "48000"]
        command += ["-movflags", "+faststart", str(args.output), "-y"]
        run(command)

        manifest = {
            "skill": "quick-cut",
            "reference_analysis": info,
            "title": args.title,
            "accent": args.accent,
            "output": str(args.output.resolve()),
            "width": args.width,
            "height": args.height,
            "fps": args.fps,
            "mapping": mapping,
        }
        manifest_path = args.output.with_suffix(".quick-cut.json")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(args.output.resolve())
        print(manifest_path.resolve())

        if args.keep_work:
            kept = args.output.with_name(args.output.stem + "_work")
            if kept.exists():
                shutil.rmtree(kept)
            shutil.copytree(work, kept)
            print(kept.resolve())
    except subprocess.CalledProcessError as exc:
        print(f"quick-cut render failed: {exc}", file=sys.stderr)
        return 1
    finally:
        temp_context.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
