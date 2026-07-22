"""Continuity-first planning primitives for generated multi-shot video.

The video model is deliberately called once per shot.  The output of this
module is persisted with the script, so a user can inspect the exact visual
contract before spending generation credits.
"""
from __future__ import annotations

import re
from typing import Any


class ContinuityDirector:
    """Turns a loose script into a small, explicit continuity contract."""

    VERSION = "continuity-v1"

    def build(self, persona: Any, script: dict[str, Any]) -> dict[str, Any]:
        shots = script.get("shots") or []
        if not shots:
            return {}
        identity = getattr(persona, "portrait_description", "") or "same creator shown in the approved character reference"
        setting = script.get("core_topic", "the planned real-world location")
        wardrobe = script.get("wardrobe_lock") or "same practical outfit throughout; no wardrobe changes"
        props = script.get("prop_lock") or "keep every visible tool, furniture item and material in its last confirmed position"
        anchor = {
            "version": self.VERSION,
            "character_lock": identity,
            "wardrobe_lock": wardrobe,
            "scene_lock": f"One real location for this episode: {setting}. Same layout, weather, time of day and lighting direction.",
            "prop_lock": props,
            "camera_lock": "vertical 9:16 smartphone/gimbal, natural exposure, no impossible camera move",
            "generation_mode": "sequential_shots_with_handoff_frame",
            "shots": [],
        }
        previous_end = "Establish the locked setting in a stable, medium-wide frame."
        for index, shot in enumerate(shots, 1):
            duration = self._seconds(shot.get("duration", "5s"))
            action = shot.get("visual", "")
            start_state = previous_end
            end_state = f"Hold a stable final frame after: {action}. Keep all people, props and progress state visible and unchanged."
            anchor["shots"].append({
                "shot_id": shot.get("shot_id", index),
                "duration_seconds": duration,
                "start_state": start_state,
                "action": action,
                "end_state": end_state,
                "transition": "hard cut only on matching composition; next shot must use this shot's extracted final frame",
                "status": "planned",
            })
            previous_end = end_state
        return anchor

    def prompt_for_shot(self, contract: dict[str, Any], shot: dict[str, Any], reference_kind: str) -> str:
        return "\n".join([
            "Create ONE continuous, physically plausible vertical 9:16 shot. Do not add cuts, time lapses, transformations, text, logos, extra people or extra objects.",
            f"CHARACTER LOCK: {contract['character_lock']}",
            f"WARDROBE LOCK: {contract['wardrobe_lock']}",
            f"SCENE LOCK: {contract['scene_lock']}",
            f"PROP LOCK: {contract['prop_lock']}",
            f"CAMERA LOCK: {contract['camera_lock']}",
            f"REFERENCE: Use the supplied {reference_kind} as the exact starting state; preserve its identity, geometry and object positions.",
            f"START STATE: {shot['start_state']}",
            f"ONLY ACTION: {shot['action']}",
            f"END STATE: {shot['end_state']}",
            "Physics: hands visibly move objects; objects obey gravity and never intersect bodies or surfaces. End with 0.5 seconds of stable hold for the next handoff.",
        ])

    @staticmethod
    def _seconds(value: Any) -> int:
        match = re.search(r"\d+", str(value))
        return max(2, min(8, int(match.group()) if match else 5))
