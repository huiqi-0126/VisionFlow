#!/usr/bin/env python3
"""Validate and naturally order six Before/After image pairs."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".avif"}


def natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", path.name)]


def images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise ValueError(f"Not a directory: {folder}")
    found = sorted(
        (p for p in folder.iterdir()
         if p.is_file() and p.suffix.casefold() in EXTENSIONS),
        key=natural_key,
    )
    if len(found) != 6:
        raise ValueError(f"{folder}: expected exactly 6 images, found {len(found)}")
    empty = [str(p) for p in found if p.stat().st_size == 0]
    if empty:
        raise ValueError(f"Empty image file(s): {', '.join(empty)}")
    return [p.resolve() for p in found]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    args = parser.parse_args()
    try:
        before = images(args.before)
        after = images(args.after)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    payload = {
        "image_count": 12,
        "pair_count": 6,
        "sequence": [
            {"pair": i + 1, "before": str(before[i]), "after": str(after[i])}
            for i in range(6)
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
