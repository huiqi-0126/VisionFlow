#!/usr/bin/env python3
"""Initialize a non-destructive reference-to-reel project."""

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--character-ip", required=True)
    parser.add_argument("--character-type", choices=("soul", "element"), default="soul")
    script_group = parser.add_mutually_exclusive_group(required=True)
    script_group.add_argument("--script-file")
    script_group.add_argument("--script-text")
    parser.add_argument("--language", default="en")
    args = parser.parse_args()

    project = Path(args.project).expanduser().resolve()
    reference = Path(args.reference).expanduser().resolve()
    if not reference.is_file():
        raise SystemExit(f"Reference video not found: {reference}")

    if args.script_file:
        script_path = Path(args.script_file).expanduser().resolve()
        if not script_path.is_file():
            raise SystemExit(f"Script file not found: {script_path}")
        script = script_path.read_text(encoding="utf-8").strip()
    else:
        script = args.script_text.strip()
    if not script:
        raise SystemExit("Script is empty")

    for relative in (
        "inputs",
        "analysis",
        "generated/images",
        "generated/clips",
        "audio",
        "captions",
        "edit",
        "output",
        "logs",
    ):
        (project / relative).mkdir(parents=True, exist_ok=True)

    manifest = {
        "reference_video": str(reference),
        "character": {
            "name": args.character_ip,
            "type": args.character_type,
            "id": None,
        },
        "script": script,
        "language": args.language,
        "status": "intake_complete",
    }
    (project / "project.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(project / "project.json")


if __name__ == "__main__":
    main()

