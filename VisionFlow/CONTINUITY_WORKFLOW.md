# Continuity-first workflow

Use `ContinuityPlannerPipeline` for new video projects. It retains VisionFlow's
LLM planner, settings, GKAPI client and TOS/COS adapter, but changes the
generation unit from one 15-second request to several short, state-linked
requests.

1. Create the date plan and scripts normally.
2. Call `prepare_day(plan_id, day)` and show `script["continuity"]` to the
   user. This is a no-credit review step: character, wardrobe, scene, prop,
   camera, each shot's start/end state, and transition are explicit.
3. Only after approval call `generate_video_for_day(plan_id, day)`. Each shot
   begins from the preceding clip's extracted last frame, uploaded through the
   existing COS adapter; clips are then losslessly concatenated locally.
4. If a shot fails review, regenerate that shot and downstream shots instead
   of regenerating the entire episode. Keep the approved upstream handoff.

The design avoids impossible internal cuts, makes state handoff inspectable,
and constrains each model call to one physically simple action. It also leaves
the legacy `PlannerPipeline` unchanged for rollback.

## Enable in the web entry point

Replace the import in `web/app.py`:

```python
from workflow.continuity_pipeline import ContinuityPlannerPipeline as PlannerPipeline
```

Add a review action that calls `prepare_day` before exposing the existing
"confirm script and generate video" button.  This is intentionally a small,
reversible integration switch.
