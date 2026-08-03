---
name: home-peace-cloud-replica
description: "Create a calm tropical-minimalist home montage from user-supplied home-style reference images: generate a fixed seven-shot image set, animate every image into a three-second vertical clip, and cloud-edit a 21-second 1080p video with reference audio and a centered title. Use when a user asks for a Homeaholic-style home video, seven-scene home reel, AI home-tour montage, or the reusable seven-shot cloud workflow."
---

# Home Peace Cloud Replica

Create one 9:16 home-lifestyle montage with seven fixed scenes. Preserve the user's supplied home architecture, materials, palette, lighting, furniture language, climate, and camera mood across every scene.

## Mandatory opening

On the first turn after this skill triggers, ask only:

1. `请发给我你的家居风格参考图（建议 3–10 张，需能看清空间、材质、配色和光线）。`
2. `请连接你的 Higgsfield MCP。若不用 Higgsfield，请告诉我视频模型供应商，并通过环境变量或该平台的密钥管理器配置凭据；不要把 API 密钥直接粘贴到聊天里。`

Do not generate until both requirements are satisfied. If Higgsfield MCP is available, use `https://mcp.higgsfield.ai/mcp` and do not request an API key.

Also ask for an optional audio/reference video only when none was supplied. Default title: `HOMEAHOLIC`. Default output: 1080×1920, 30 fps, 21 seconds.

## Lock the style

Inspect every reference image before generation. Write a compact style lock containing:

- architectural type and room proportions;
- dominant materials and furniture forms;
- palette and contrast;
- daylight, practical lights, weather, and time of day;
- lens height, focal character, handheld character, and grain;
- recurring resident appearance only when the references contain a person.

The style lock is binding. Do not replace the user's home with a generic luxury interior. Each image prompt must explicitly say to preserve the supplied reference home's architecture, materials, palette, furniture language, and lighting.

## Generate the seven images

Read [references/shot-blueprint.md](references/shot-blueprint.md). Generate exactly one 9:16 image for each shot, in order.

For Higgsfield:

1. Upload the reference images through `media_upload_widget`.
2. Use `nano_banana_pro`, `aspect_ratio: 9:16`, `resolution: 2k`, `count: 1`.
3. Use the user's home references as `image` media inputs whenever the model permits.
4. Preflight cost before the first paid request.
5. Remove baked captions, logos, watermarks, and readable UI text from generated images.
6. Show all seven images and obtain approval before video generation.

If a reference image triggers a false safety rejection, do not bypass the filter. Recreate that shot from the style lock and precise text-only composition. Explain the substitution.

For another provider, choose its best image-to-image model and keep the same seven-shot blueprint. Never solicit a raw secret in chat.

## Animate the approved images

Generate seven clips, each exactly three seconds:

- aspect ratio: 9:16;
- target: 1080×1920;
- no generated audio;
- subtle realistic motion only;
- no internal cuts, morphing, new objects, text, or identity drift.

Prefer `kling3_0`, `mode: pro`, `duration: 3`, `sound: off` on Higgsfield. Preflight total cost and ask for confirmation immediately before submitting paid video jobs.

Use these motions:

1. slow push into the kitchen; resident makes a tiny natural movement;
2. slow downward/forward drift over the breakfast tray;
3. smooth forward glide with a slight right pan along the counter;
4. slow push toward the stairs; resident takes two calm steps;
5. subtle first-person push toward the laptop; fingers type naturally;
6. slow crane-like descent into the living room;
7. very slow push toward the exterior facade.

Reject unrelated preset recommendations and generate literally when the user requested reference fidelity.

## Cloud edit

Use the Higgsfield cloud sandbox when available. Read and run [scripts/assemble_7x3.sh](scripts/assemble_7x3.sh) with:

```text
assemble_7x3.sh OUTPUT.mp4 REFERENCE_AUDIO_OR_VIDEO TITLE CLIP1 ... CLIP7
```

The script must:

- trim each clip to 3.000 seconds;
- normalize to 1080×1920, 30 fps, square pixels;
- hard-cut in the seven-shot order;
- loop the supplied reference audio to 21 seconds;
- add the centered white uppercase title throughout;
- encode H.264 High, yuv420p, AAC, fast-start MP4.

If no audio is supplied, export silent video. Do not invent music without permission.

Upload and confirm the final MP4, then provide both a cloud URL and a local downloadable copy when possible. Verify duration, dimensions, frame rate, video codec, and audio codec before delivery.

## Completion contract

Do not call the work complete until:

- seven approved images exist;
- seven three-second clips completed successfully;
- the final duration is approximately 21.0 seconds;
- output is 1080×1920 at 30 fps;
- title and audio behavior match the brief;
- the user receives a playable/downloadable MP4.
