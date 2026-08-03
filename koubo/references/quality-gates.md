# Final quality gates

## Identity

- Compare every character shot against the approved Soul/Element and key image.
- Reject face shape, hair, age, skin tone, body, wardrobe, or accessory drift.
- Reject new people or duplicated body parts.

## Motion and lip sync

- Check the first, middle, and last frame of every clip.
- Check every speaking shot at normal speed and 0.5× speed.
- Mouth motion must begin and end with the audible phrase.
- Non-speaking shots must not move lips as if addressing the camera.

## Edit

- Compare reference and output cut timestamps in frames.
- Check speed ramps, holds, transition direction, beat hits, and total duration.
- Detect black frames, frozen spans, duplicated frames, and unintended silence.

## Captions

- Compare caption JSON against final mixed audio, not the draft script.
- Verify start/end errors are within 80ms when speech is clear.
- Check spelling, punctuation, line breaks, safe area, and edge clipping.
- Confirm caption animation never hides the word during most of its spoken duration.

## Technical

- Verify dimensions, sample aspect ratio, fps, duration, video codec, pixel format,
  audio codec, sample rate, and channel count with `ffprobe`.
- Prefer H.264 High, yuv420p, AAC, and fast-start MP4 unless the user requests
  another delivery format.
- Deliver a contact sheet or short QA report alongside the final MP4.

