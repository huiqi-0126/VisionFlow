---
name: quick-cut
description: Reproduce the pacing and visual grammar of a user-supplied reference video with local source clips, using a CLI-only workflow to analyze cut timing, cycle/reframe footage, retain or match impact audio, add a compact Caption Title, color-grade, render, and verify a vertical short-form MP4. Use when a user asks for 快剪, 参考剪辑, reference-style editing, rhythm matching, fast-cut montages, or a finished edit without opening AE, CapCut, Premiere, or another GUI editor.
---

# 快剪

Build a finished short-form edit from one reference video and multiple source clips without opening a GUI editing app.

## Required input gate

Confirm these inputs before rendering:

1. Reference video path.
2. Source video paths or a source folder.
3. Caption Title shown on screen.

If the Caption Title is missing, pause before rendering and ask exactly one concise question:

> 请提供画面中显示的 Caption Title 文字；如需双色，请同时说明白色主词和黄色强调词。

Treat Caption Title as required unless the user explicitly requests no title. Do not silently invent marketing copy.

## Workflow

1. Validate that the reference and every source file exist. Preserve all originals.
2. Run `scripts/analyze_reference.py` to extract duration, aspect ratio, frame rate, scene boundaries, and shot intervals.
3. Generate a contact sheet and inspect the reference for:
   - hard cuts versus transitions;
   - repeated-view order;
   - crop, push-in, speed, exposure, and color behavior;
   - title position, scale, case, color split, and persistence;
   - whether audio transients align with cuts.
4. Map all supplied source clips to the detected view sequence. When the reference uses more views than the user supplied, create alternate crops from the strongest source clips instead of omitting footage.
5. Render with `scripts/build_quick_cut.py` as the baseline. Adjust its scene threshold, grade filter, title colors, or crop strategy when inspection shows the reference needs it.
6. Reuse the supplied reference audio only when the user has provided the file and requested its rhythm or sound design. Otherwise retain suitable source audio or use user-authorized audio.
7. Verify the delivered MP4 with `ffprobe`, a full decode test, loudness inspection, and a final contact sheet.
8. Deliver the MP4 plus an optional preview contact sheet and timing manifest. Link only deliverables, never temporary segments.

## Commands

Analyze:

```bash
python3 scripts/analyze_reference.py \
  --reference "/path/reference.mp4" \
  --output "/path/reference-analysis.json"
```

Render:

```bash
python3 scripts/build_quick_cut.py \
  --reference "/path/reference.mp4" \
  --sources "/path/1.mp4" "/path/2.mp4" "/path/3.mp4" \
  --title "FOCUS ON DESIGN" \
  --accent "ON DESIGN" \
  --output "/path/final.mp4"
```

Use `--all-white` when no accent color is wanted. Use `--no-reference-audio` when the reference soundtrack must not be carried into the output.

## Output standard

- Default to 1080 × 1920 and 30 fps for vertical delivery unless the user specifies otherwise.
- Encode H.264 video and AAC 48 kHz audio with BT.709 metadata.
- Keep timing within one output frame of the detected reference boundary.
- Keep titles inside the central safe area and readable without dominating the subject.
- Do not modify or delete source files.

Read `references/input-contract.md` when explaining required inputs or handing this Skill to another user.
