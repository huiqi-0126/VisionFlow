"""Opt-in continuity-first video generation pipeline.

This deliberately replaces VisionFlow's one-request-per-15-second strategy
with a reviewed sequence of short clips.  It reuses PlannerPipeline's script
generation, MediaAPIClient, Settings and COS adapter.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from core.continuity import ContinuityDirector
from core.content_planner import Persona
from workflow.planner_pipeline import PlannerPipeline


class ContinuityPlannerPipeline(PlannerPipeline):
    """A manual-generation pipeline with identity, scene and prop handoffs."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.continuity = ContinuityDirector()

    def prepare_day(self, plan_id: str, day: int) -> dict[str, Any]:
        """Create the auditable blueprint; this method never calls a video model."""
        plan = self.plan_mgr.get_plan(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")
        script = next((item for item in plan.get("scripts", []) if item.get("day") == day), None)
        if not script:
            raise ValueError(f"Script for day {day} not found")
        script["continuity"] = self.continuity.build(Persona.from_dict(plan["persona"]), script)
        script["video_status"] = "ready_for_continuity_review"
        self.plan_mgr.update_day_script(plan_id, day, script)
        return script

    def _generate_all_videos(self, plan_id: str, persona: Persona, scripts: list[dict[str, Any]], output_dir: Path) -> None:
        self._ensure_media_clients()
        assert self._media_api is not None
        videos_dir = output_dir / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        plan = self.plan_mgr.get_plan(plan_id) or {}
        for script in scripts:
            day = script.get("day", 0)
            contract = script.get("continuity") or self.continuity.build(persona, script)
            if not contract:
                raise ValueError(f"Day {day} has no shots")
            script["continuity"] = contract
            work_dir = videos_dir / f"day_{day:02d}_shots"
            work_dir.mkdir(parents=True, exist_ok=True)
            reference_url = plan.get("portrait_image_url")
            clips: list[Path] = []
            try:
                for position, shot in enumerate(contract["shots"], 1):
                    task_id = self._media_api.generate_video(
                        prompt=self.continuity.prompt_for_shot(
                            contract, shot,
                            "previous shot's final frame" if position > 1 else "approved character reference",
                        ),
                        model=self.settings.default_video_model,
                        size=self.settings.default_size,
                        duration=str(shot["duration_seconds"]),
                        aspect_ratio=self.settings.default_aspect_ratio,
                        pic=reference_url,
                    )
                    result = self._media_api.poll_video(task_id)
                    if result.get("status") != "success" or not result.get("url"):
                        raise RuntimeError(f"Shot {position} failed")
                    clip = work_dir / f"shot_{position:02d}.mp4"
                    self._media_api.download_media(result["url"], clip)
                    handoff = work_dir / f"shot_{position:02d}_end.jpg"
                    self._extract_final_frame(clip, handoff)
                    shot.update({"status": "done", "video_file": str(clip), "handoff_frame": str(handoff), "model_task_id": task_id})
                    # GKAPI requires URL references; COS makes the locally extracted
                    # last frame available to the next request.
                    if self._cos is not None:
                        reference_url = self._cos.upload_file(handoff)
                    clips.append(clip)
                final = videos_dir / f"day_{day:02d}.mp4"
                self._concat(clips, final, work_dir)
                script.update({"video_file": str(final), "video_status": "done"})
            except Exception as exc:
                script["video_status"] = "failed"
                script["generation_error"] = str(exc)
            self.plan_mgr.update_day_script(plan_id, day, script)

    def _extract_final_frame(self, clip: Path, frame: Path) -> None:
        result = subprocess.run([
            self.settings.ffmpeg_path, "-y", "-sseof", "-0.15", "-i", str(clip),
            "-frames:v", "1", "-q:v", "2", str(frame),
        ], capture_output=True, text=True, timeout=60)
        if result.returncode or not frame.exists():
            raise RuntimeError("Unable to extract final frame for continuity handoff")

    def _concat(self, clips: list[Path], output: Path, work_dir: Path) -> None:
        manifest = work_dir / "concat.txt"
        manifest.write_text("".join(f"file '{clip.as_posix()}'\n" for clip in clips), encoding="utf-8")
        result = subprocess.run([
            self.settings.ffmpeg_path, "-y", "-f", "concat", "-safe", "0", "-i", str(manifest),
            "-c", "copy", "-movflags", "+faststart", str(output),
        ], capture_output=True, text=True, timeout=180)
        if result.returncode or not output.exists():
            raise RuntimeError("Unable to join generated clips")
