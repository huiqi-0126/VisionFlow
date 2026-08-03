---
name: before-after-cloud-editor
description: "Create a precise vertical renovation Before/After MP4 from exactly six paired Before images and six paired After images. Use when a user asks Codex to reproduce this locked 17-second photo-edit template, make a 毛坯房/装修后 montage, alternate before and after room images, or render the same caption, music, timing, and output style on macOS or Windows. This skill is editing-only: it does not generate or modify source images."
---

# Before/After Cloud Editor

Build the locked edit with cloud FFmpeg. Do not use HyperFrames, Remotion, local FFmpeg, or an image/video generation model.

## Intake

If the 12 inputs are not already unambiguous, ask only:

> 请告诉我 6 张 Before（毛坯）图片在哪里，以及对应的 6 张 After（装修后）图片在哪里。请使用相同编号或文件名配对，例如 `01-before.jpg` ↔ `01-after.jpg`。默认成片总时长 17 秒、9:16，并使用模板字幕和背景音乐；如需修改请同时告诉我。

Accept:

- two folders, one for Before and one for After;
- twelve explicit paths;
- twelve uploaded images;
- twelve confirmed media IDs or HTTPS URLs.

On an Apps UI client, use the media upload widget for local images. Ask for Before and After in two separate six-image batches so their roles cannot be confused.

## Validate and pair

Require exactly six Before and six After images. Never silently omit, duplicate, or invent an image.

For local folders, run:

```bash
python3 scripts/validate_pairs.py --before "<before-folder>" --after "<after-folder>"
```

The script is cross-platform and outputs ordered JSON. Natural-sort by filename. Pair item 1 with item 1, and so on. Show the six resulting pairs to the user only when filenames are ambiguous or pairing confidence is low; otherwise continue.

Reject the render until:

- both groups contain exactly six supported raster images;
- every file is readable and non-empty;
- the order is deterministic;
- Before and After roles are known.

## Locked edit specification

- Canvas: 720×1280, square pixels, 9:16.
- Frame rate: 30 fps.
- Total duration: exactly 17.000 seconds, 510 video frames.
- Images: all 12, ordered `B1,A1,B2,A2,...,B6,A6`.
- Timing: distribute 510 frames across 12 shots as `43,42,43,42,43,42,43,42,43,42,43,42`. This gives each pair exactly 85 frames and avoids drift.
- Visual fitting: scale to cover, then center-crop to 720×1280. Never stretch.
- Cuts: hard cuts only. No flash, dissolve, morph, or generated transition.
- Motion: static photographs. Do not add Ken Burns motion.
- Caption text: `Before and after`.
- Caption placement: horizontally centered; baseline near 84% of frame height; white semibold sans-serif; subtle black shadow; safe inside the bottom UI zone.
- Caption duration: entire 17 seconds.
- Background music: use `assets/template-bgm.m4a`; start at 0, normalize conservatively, fade out over the final 0.5 seconds.
- Audio output: AAC, 44.1 kHz stereo, 192 kb/s.
- Video output: H.264 High, yuv420p, CRF 18, `+faststart`.
- Output container: MP4.

Use an available Montserrat or Metropolis semibold font in the cloud sandbox. Keep the rendered caption visually equivalent if the exact font file differs.

## Cloud workflow

1. Resolve or upload all 12 images and the bundled BGM to URLs accessible by the cloud sandbox.
2. Request a video upload URL for the finished MP4.
3. Use the Higgsfield cloud sandbox at `https://mcp.higgsfield.ai/mcp` and its `sandbox_exec` tool for all FFmpeg work.
4. Download the 13 inputs inside the sandbox.
5. Build each still with `-loop 1`, `scale=720:1280:force_original_aspect_ratio=increase`, `crop=720:1280`, `setsar=1`, and the exact frame count above.
6. Concatenate in locked alternating order.
7. Overlay the caption after concatenation so it stays stable across cuts.
8. Trim/mix the BGM to 17 seconds and apply the final fade.
9. Upload and confirm the MP4.

Keep the render, probe, and upload in one sandbox call when practical because cloud sandbox storage is temporary.

## Verification gate

Do not deliver until all checks pass:

```text
width=720
height=1280
r_frame_rate=30/1
nb_frames=510
duration=17.000000
video codec=h264
audio codec=aac
```

Also extract one checkpoint frame from the center of every shot and hash it. Require 12 checkpoint images and confirm adjacent hashes differ. Visually inspect at least the first pair, a middle pair, and the final pair to verify:

- Before is followed immediately by its matching After;
- no image is missing;
- the caption is visible and not clipped;
- no black frame or accidental white flash exists.

If a check fails, rerender before sharing the file. Return the direct MP4 link and summarize image count, pair count, exact duration, and cloud-render status.

## User overrides

Allow the user to change total duration, caption text, caption visibility, or BGM. Preserve all other locked rules. For a different total duration, compute `round(duration × 30)` total frames and distribute them as evenly as possible across all 12 shots, with any extra frames assigned from the first shot onward. Report the resulting per-shot timing before rendering.
