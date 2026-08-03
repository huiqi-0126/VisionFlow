# Project and shot-plan schema

Use this directory layout:

```text
project/
  project.json
  inputs/
  analysis/
    reference.json
    shot_plan.json
  generated/
    images/
    clips/
  audio/
  captions/
    words.json
    captions.json
  edit/
  output/
  logs/
```

`project.json`:

```json
{
  "reference_video": "/absolute/path/reference.mp4",
  "character": {"name": "Miami", "type": "soul", "id": null},
  "script": "Final approved words",
  "language": "en",
  "status": "intake_complete"
}
```

`analysis/shot_plan.json`:

```json
{
  "output": {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "duration": 15.0
  },
  "shots": [
    {
      "id": "S001",
      "reference_start": 0.0,
      "reference_end": 1.2,
      "output_start": 0.0,
      "output_end": 1.2,
      "framing": "medium close-up",
      "camera": "slow push-in",
      "subject_action": "speaks to lens",
      "speaking": true,
      "dialogue": "Script words assigned to this shot",
      "image_prompt": "Full still-image prompt",
      "video_prompt": "One camera move and one action",
      "image_job_id": null,
      "clip_job_id": null,
      "image_path": null,
      "clip_path": null,
      "approved": false
    }
  ]
}
```

Rules:

- Use seconds as numbers and frame-snap all output times.
- Keep shot IDs stable after image approval.
- Record real job IDs and paths; never fabricate completed artifacts.
- Set `speaking: true` only for shots where the visible face should articulate.
- Allocate every script word either to visible dialogue or voice-over.
- Preserve reference shot count unless a model constraint requires a documented
  split or merge.

