---
name: reference-ip-reel-pipeline
description: "Turn a user-supplied reference video, a confirmed Higgsfield Soul character IP, and a new script into a finished AI reel: analyze the reference shot-by-shot, generate identity-consistent key images, animate them into clips, reproduce the reference editing grammar with original assets, synchronize dialogue and sound, add frame-accurate captions, and deliver a verified MP4. Use when a user asks for an end-to-end reference-video remake, AI character reel, property/lifestyle/product montage, image-to-video production, or reusable image-to-video-to-edit-to-caption workflow."
---

# Reference IP Reel Pipeline

Create a transformed reel from three required inputs while keeping one approved
Higgsfield character identity consistent from images through video.

## Mandatory intake gate

On the first turn after this skill triggers, ask only for these three items:

1. `请上传参考视频，或告诉我它的本地绝对路径。`
2. `请确认已连接 Higgsfield MCP，并告诉我要使用的 Soul 人物 IP 名称。若还没有 Soul，请告诉我是训练 Soul（5–20 张同一人物照片）还是临时 Element。`
3. `请粘贴最终脚本文案，并注明语言。`

Do not analyze or generate until all three are resolved. Echo the recognized
reference filename, exact character name/type, and script back once. Infer aspect
ratio, duration, pacing, and shot count from the reference. Ask another question
only when a missing choice would materially alter the result.

Never ask for an API key in chat. Use the configured Higgsfield MCP at
`https://mcp.higgsfield.ai/mcp`. If it is disconnected, ask the user to connect or
reauthorize it, then resume from the intake gate.

## Initialize the job

Create a project folder and run:

```bash
python3 scripts/init_project.py \
  --project PROJECT_DIR \
  --reference ABSOLUTE_REFERENCE_VIDEO \
  --character-ip "EXACT SOUL NAME" \
  --character-type soul \
  --script-file SCRIPT_TXT
```

Keep all artifacts under that project. Read
[references/project-schema.md](references/project-schema.md) before writing the
shot plan.

## Inspect and deconstruct the reference

Use `ffprobe` and frame extraction to record:

- exact dimensions, fps, duration, codecs, and audio layout;
- every hard cut, transition, freeze, speed change, and beat;
- shot duration, framing, camera height, lens character, subject direction, and
  camera movement;
- dialogue, voice-over, ambience, music, sound effects, caption timing, caption
  position, typography, and animation;
- which shots are front-facing speaking shots and which are silent action/B-roll.

Transcribe the reference audio with word timestamps when speech exists. Build
`analysis/shot_plan.json`; do not approximate the plan from a contact sheet alone.
Reference fidelity means matching timing and visual grammar with newly generated
media, not copying protected source frames into the final.

## Resolve the character identity

List ready Soul V2 characters and match the user's exact character name. Obtain
the `soul_id` from the tool response; never invent one.

- Use a trained Soul for one recurring person.
- Use a reference Element only when the user chose Element, the shot has multiple
  people, or the selected generation model does not support Soul.
- Never silently train or replace an identity.
- Keep a binding identity lock: face, age range, hair, body proportions, wardrobe
  family, and distinguishing details. Allow only script- or scene-required changes.

For local inputs in an Apps UI-capable client, invoke the Higgsfield upload widget.
For URLs, import them first. Never pass raw local paths or web URLs into remote
generation parameters.

## Prepare the generation workflow

Before any multi-step generation, load the Higgsfield workflow catalog, choose the
matching workflow, and read its full instructions. Query model recommendations and
constraints rather than assuming parameters.

Separate the production into:

1. reference analysis;
2. approved still-image keyframes;
3. image-to-video clips;
4. narration/dialogue and sound;
5. deterministic edit;
6. captions and final QA.

Preflight the total image and video cost. Present one consolidated estimate and
obtain confirmation immediately before paid generation. Do not silently use
credits or free-trial generations.

## Generate and approve key images

Generate one clean key image per planned shot. Use the confirmed Soul with Soul V2
or Soul Cinema when possible. Use the actual reference frame as composition/style
guidance only when the selected model declares that media role.

Each prompt must lock:

- confirmed character identity;
- shot composition and subject direction;
- location continuity, time of day, palette, lens, and lighting;
- wardrobe continuity;
- no text, captions, logos, watermark, extra people, or unexplained props.

Front-facing speaking shots must look naturally toward the lens. Non-front-facing
shots must continue the depicted action and must not turn toward the camera to
speak.

Generate low-count previews first. Build a labeled contact sheet with shot numbers,
show it to the user, and obtain image approval before video generation. Regenerate
only rejected shots.

## Animate approved images

Use each approved still as the start image. Select the best available image-to-video
model after inspecting current constraints. Preserve identity, framing, set, and
wardrobe; request one camera move and one subject action per clip.

- Match each clip's planned duration or generate longer and trim precisely.
- Use model-native dialogue/lip sync only for planned front-facing speaking shots.
- Keep B-roll/action shots silent on camera; carry narration as voice-over.
- Avoid internal cuts, morphs, new objects, face drift, extra limbs, or unsolicited
  camera motion.
- Poll every job through completion and record job IDs and returned media URLs.

Reject clips with identity drift, broken anatomy, incorrect gaze, timing mismatch,
or unwanted speech. Do not hide failed generation with a fast edit.

## Build audio and edit

Use the user's script verbatim unless they approved changes. Generate or ingest the
voice track before final caption timing. For on-camera dialogue, align phonemes and
mouth motion; for B-roll, keep the same voice as off-camera narration.

Reproduce the reference's:

- shot order and frame-snapped cut points;
- speed ramps, holds, camera transitions, and beat accents;
- music/ambience/SFX hierarchy;
- aspect ratio, duration, and platform-safe framing.

Use HyperFrames for the deterministic edit and captions unless the user supplied
an authorized AEP/Nexrender template. Do not add generic effects absent from the
reference. Preserve the approved generated clips rather than re-stylizing them.

## Generate captions from final audio

Transcribe the final mixed dialogue track with word timestamps. Never time captions
from script character counts.

- Snap boundaries to the output frame rate.
- Prefer readable phrases of 1–4 words; keep most events at least 0.50 seconds.
- Keep timing error within 80ms where speech is clear.
- Match the reference's font category, size, capitalization, placement, color,
  blend mode, and entrance/exit motion.
- Keep captions inside Reels/TikTok safe areas and prevent edge clipping.
- Preserve the original/mixed audio when rendering captions.

If the user supplies an AEP with `Main_Comp` and `Seed_Caption`, use the optional
AE/Nexrender path in
[references/caption-backends.md](references/caption-backends.md). Otherwise stay
cloud-native and do not require the user to own Nexrender.

## Validate and deliver

Read [references/quality-gates.md](references/quality-gates.md). Run:

```bash
python3 scripts/validate_pipeline.py --project PROJECT_DIR --final FINAL_MP4
```

Do not call the job complete until:

- the confirmed character remains recognizable in every applicable shot;
- speaking shots have credible lip sync and B-roll does not talk to camera;
- all cuts and caption starts are frame-snapped;
- captions match the audible words and do not clip;
- no black frames, frozen failures, silent gaps, or unexpected voices exist;
- final dimensions, fps, duration, codecs, and audio channels are verified;
- the user receives the playable MP4 plus `shot_plan.json` and `captions.json`.

