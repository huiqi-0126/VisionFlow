"""Continuity-first, reference-timed outfit-switch video workflow."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import cv2
from PIL import Image

from config import Settings, get_settings
from core.cos_client import COSClient
from core.media_api import MediaAPIClient

logger = logging.getLogger(__name__)
Progress = Callable[[str], None]
REFERENCE_VIDEO = Path(__file__).resolve().parents[1] / "public" / "23.mp4"
TARGET_DURATION = 12.133
# Detailed, fixed person description used in "no model" mode (no person image).
# The SAME block is embedded in every text-to-video clip so the woman stays as
# consistent as possible across clips: a common Chinese internet-celebrity face,
# fixed hairstyle and ethnicity.
DEFAULT_PERSON = ("Photorealistic young East Asian (Chinese) woman, 24 years old, with a typical Chinese internet-celebrity (wanghong) face: "
                  "large bright double-lidded eyes, long natural eyelashes, high straight nose bridge, small heart-shaped face with a slim jaw, full lips, "
                  "smooth fair skin, soft natural gradient makeup; long straight glossy black hair, center-parted, falling past the shoulders; "
                  "slim toned proportional figure, about 170cm tall. Keep her identity, face, hair and body exactly the same in every shot.")


class EventLog:
    def __init__(self, path: Path, progress: Progress | None = None) -> None:
        self.path = path
        self.progress = progress
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, step: str, state: str, **data: Any) -> None:
        event = {"at": round(time.time(), 3), "step": step, "state": state, **data}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        logger.info("outfit_switch %s %s %s", step, state, data)

    def stage(self, value: str) -> None:
        self.write("stage", "progress", label=value)
        if self.progress:
            self.progress(value)


class OutfitSwitchPipeline:
    """Recreate 23.mp4's pace: short model motion + programmatic beat cuts."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        s = self.settings
        self.media = MediaAPIClient(s.gkapi_key, s.gkapi_baseurl, s.poll_interval, s.max_poll_attempts)
        self.cos = COSClient(s.secret_id, s.secret_key, s.region, s.bucket, s.cos_url)
        self.image_model, self.video_model = s.default_image_model, s.default_video_model
        configured = Path(s.ffmpeg_path)
        self.ffmpeg = str(configured) if configured.is_file() else shutil.which(s.ffmpeg_path) or shutil.which("ffmpeg")
        self.events: EventLog | None = None

    def run(
        self,
        job_dir: Path,
        model_image: Path | None,
        model_prompt: str,
        outfit_images: list[Path],
        outfit_prompts: list[str],
        on_progress: Progress | None = None,
        no_model: bool = False,
    ) -> dict[str, Any]:
        if not 2 <= len(outfit_prompts) <= 3:
            raise ValueError("Please provide 2 or 3 outfits")
        if not self.ffmpeg:
            raise RuntimeError("FFmpeg is required for outfit-video assembly")
        job_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(job_dir / "events.jsonl", on_progress)
        self.events.write("workflow", "started", reference=str(REFERENCE_VIDEO), outfits=len(outfit_prompts), no_model=no_model)
        reference_analysis = self._analyse_reference()
        (job_dir / "reference_analysis.json").write_text(json.dumps(reference_analysis, ensure_ascii=False, indent=2), encoding="utf-8")

        looks_dir, raw_dir, rendered_dir = job_dir / "looks", job_dir / "raw", job_dir / "segments"
        for directory in (looks_dir, raw_dir, rendered_dir):
            directory.mkdir(exist_ok=True)

        anchor_path: Path | None = None
        anchor_url: str | None = None
        if no_model:
            # No model image / description: skip anchor + look images (passing a
            # person image to the video model is what often fails). Build looks
            # from outfit text only; clips are generated text-to-video below.
            self.events.stage("1/4 Text-to-video mode: no model image")
            looks = [{"index": str(i), "outfit": o, "path": "", "url": ""} for i, o in enumerate(outfit_prompts, 1)]
        else:
            self.events.stage("1/5 Gemini: generating stable model anchor")
            anchor_path, anchor_url = self._make_anchor(looks_dir, model_image, model_prompt, outfit_prompts)
            looks = []
            for index, outfit in enumerate(outfit_prompts, 1):
                self.events.stage(f"2/5 Gemini: preparing outfit look {index}/{len(outfit_prompts)}")
                image = outfit_images[index - 1] if index <= len(outfit_images) else None
                result = self._make_look(looks_dir, index, anchor_path, anchor_url, outfit, image)
                if result is None:
                    continue  # this outfit failed; keep going with the rest
                path, url = result
                looks.append({"index": str(index), "outfit": outfit, "path": str(path), "url": url})
            if len(looks) < 2:
                raise RuntimeError(f"只有 {len(looks)} 套服装生成成功，至少需要 2 套才能做换装视频")

        beat_plan = self._beat_plan(looks)
        (job_dir / "beat_plan.json").write_text(json.dumps(beat_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        self.events.stage("3/5 Video model: generating high-energy dance clips")
        raw = self._make_dance_clips(raw_dir, anchor_url, looks, model_prompt, person=(DEFAULT_PERSON if no_model else None))
        self.events.stage("4/5 Building beat-synchronised 23.mp4 timeline")
        segments = self._render_timeline(rendered_dir, beat_plan, looks, raw)
        self.events.stage("5/5 Merging video with 23.mp4 audio")
        output = job_dir / "outfit_switch.mp4"
        self._assemble(segments, output)
        manifest = {
            "template": "23.mp4-reference-timed-v2",
            "reference_analysis": reference_analysis,
            "no_model": no_model,
            "anchor": str(anchor_path) if anchor_path else "",
            "looks": looks,
            "beat_plan": beat_plan,
            "raw_clips": raw,
            "segments": [str(item) for item in segments],
            "final_video": str(output),
            "event_log": str(job_dir / "events.jsonl"),
        }
        (job_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.events.write("workflow", "done", final_video=str(output))
        return manifest

    def _analyse_reference(self) -> dict[str, Any]:
        """Persist measurable source rhythm; this does not send the source model to an API."""
        if not REFERENCE_VIDEO.is_file():
            return {"available": False}
        cap = cv2.VideoCapture(str(REFERENCE_VIDEO))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        previous = None
        scene_hits: list[dict[str, float]] = []
        changes: list[float] = []
        frame = 0
        while True:
            ok, image = cap.read()
            if not ok:
                break
            if frame % 3 == 0:
                gray = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (160, 284))
                if previous is not None:
                    delta = float(cv2.absdiff(gray, previous).mean())
                    changes.append(delta)
                    if delta > 32:
                        scene_hits.append({"time": round(frame / fps, 3), "change": round(delta, 2)})
                previous = gray
            frame += 1
        cap.release()
        # These sections are intentionally retained as the versioned target pace.
        return {
            "available": True,
            "duration": TARGET_DURATION,
            "fps": fps,
            "detected_high_change_frames": scene_hits,
            "average_frame_change": round(sum(changes) / max(1, len(changes)), 3),
            "template_beats": [3.8, 0.4, 1.4, 0.25, 0.7, 1.2, 0.25, 0.7, 3.433],
        }

    def _make_anchor(self, out: Path, model_image: Path | None, prompt: str, outfits: list[str]) -> tuple[Path, str]:
        reference = self.cos.upload_file(model_image) if model_image else None
        # Look images must contain ONLY the model wearing the outfit — no racks,
        # no props, no other garments. The anchor sets a clean, reproducible base
        # POSE that every later look copies exactly.
        text = ("Full-body vertical fashion studio. One model stands centered, front-facing the camera, weight evenly on both feet, "
                "arms relaxed at her sides, neutral upright stance — a clean, reproducible base pose. "
                "The locked camera shows her entire body and a clean, empty studio floor — no clothing racks, no props, no other garments, nothing else in the scene. "
                "No text, no logos, no other people. " + prompt)
        # The anchor is essential (every later step references it), so retry a few
        # times on transient API timeouts/failures before giving up on the whole job.
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                task = self.media.generate_image(text, model=self.image_model, size=self.settings.default_size, aspect_ratio="9:16", pic=reference)
                result = self.media.poll_image(task)
                if result.get("status") != "success" or not result.get("url"):
                    raise RuntimeError(f"Dance-stage anchor failed: {result}")
                path = out / "dance_stage_anchor.png"
                self.media.download_media(result["url"], path)
                self.events.write("dance_stage_anchor", "done", task_id=task, path=str(path), wardrobe=outfits)
                return path, self.cos.upload_file(path)
            except Exception as exc:
                last_exc = exc
                self.events.write("dance_stage_anchor", "retry", attempt=attempt, error=str(exc))
                logger.warning("Anchor attempt %d failed: %s", attempt, exc)
                if attempt < 3:
                    time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"Anchor failed after 3 attempts: {last_exc}") from last_exc
    def _make_look(self, out: Path, number: int, anchor: Path, anchor_url: str, outfit: str, garment: Path | None) -> tuple[Path, str] | None:
        # Returns None on failure so one bad outfit image doesn't abort the whole
        # job — the caller skips it and continues with the remaining outfits.
        prompt = ("Use the reference for the exact same model identity, studio AND POSE. "
                  "Keep the EXACT same pose as the reference: identical body angle, identical arm, hand and leg positions, identical weight distribution and camera framing — do not change the pose at all, only the clothing changes. "
                  f"The model now wears this complete outfit: {outfit}. The scene contains ONLY the model in this outfit — no clothing racks, no props, no other garments, nothing else. "
                  "Full body, realistic clothing fit, no text, no logo, no extra people, no distorted hands.")
        try:
            task = self.media.generate_image(prompt, model=self.image_model, size=self.settings.default_size, aspect_ratio="9:16", pic=anchor_url)
            result = self.media.poll_image(task)
            if result.get("status") != "success" or not result.get("url"):
                raise RuntimeError(f"Outfit look {number} failed: {result}")
            path = out / f"look_{number:02d}.png"
            self.media.download_media(result["url"], path)
            self.events.write("dance_look", "done", number=number, task_id=task, path=str(path), garment_reference=bool(garment))
            return path, self.cos.upload_file(path)
        except Exception as exc:
            self.events.write("dance_look", "failed_skipped", number=number, error=str(exc))
            logger.warning("Outfit look %d failed, skipping: %s", number, exc)
            return None

    @staticmethod
    def _composite_reference(anchor: Path, garment: Path, out: Path) -> None:
        def fit(path: Path, height: int) -> Image.Image:
            im = Image.open(path).convert("RGB")
            return im.resize((max(1, int(im.width * height / im.height)), height), Image.LANCZOS)
        left, right = fit(anchor, 1024), fit(garment, 1024)
        image = Image.new("RGB", (left.width + right.width + 24, 1024), (235, 235, 235))
        image.paste(left, (0, 0)); image.paste(right, (left.width + 24, 0)); image.save(out)

    def _beat_plan(self, looks: list[dict[str, str]]) -> list[dict[str, Any]]:
        # Two-look hero loop: dance A -> spin/effect into B -> dance B -> spin/effect back into A.
        if len(looks) == 2:
            return [
                {"name":"dance_look_a", "source":"dance_1", "duration":1.933},
                {"name":"effect_change_a_to_b", "source":"spin_1", "duration":4.0},
                {"name":"dance_look_b", "source":"dance_2", "duration":2.2},
                {"name":"effect_change_b_to_a", "source":"spin_return", "duration":4.0},
            ]
        # Three looks keep the same fast no-warmup principle.
        return [
            {"name":"dance_look_a", "source":"dance_1", "duration":1.2},
            {"name":"effect_change_1", "source":"spin_1", "duration":4.0},
            {"name":"effect_change_2", "source":"spin_2", "duration":4.0},
            {"name":"dance_final", "source":"dance_3", "duration":2.933},
        ]
    def _make_dance_clips(self, out: Path, anchor_url: str | None, looks: list[dict[str, str]], model_prompt: str, person: str | None = None) -> dict[str, str]:
        clips: dict[str, str] = {}
        dance = ("Start dancing immediately with quick rhythmic footwork, sharp arm accents, hip movement and small hops. "
                 "The full body stays visible in one locked wide camera in a clean minimal studio. Keep identity, hair, face, body, lighting and camera stable. "
                 "No props, no other people, no text, no logo, no warped hands.")
        spin = ("STRICT 4-SECOND CHOREOGRAPHY. 0.00-1.35 seconds: complete one full clockwise spin wearing the source outfit. "
                "1.35-1.75 seconds: at maximum rotational motion, a vivid swirling fabric-ribbon plus short flash transformation effect wraps around the body and completely replaces the source outfit with the target outfit. "
                "1.75-4.00 seconds: the target outfit is fully visible; land facing the camera and dance fast with confident celebratory steps. "
                "One locked full-body wide camera. No cut, no props, no other people, no text, no logo, no distorted hands.")

        def attempt(key: str, prompt: str, output: Path, start: str | None, end: str | None = None) -> None:
            try:
                clips[key] = self._video(prompt, output, start, end)
            except Exception as exc:
                self.events.write("video_task", "failed_skipped", key=key, output=str(output), error=str(exc))
                logger.warning("Clip %s failed, skipping: %s", key, exc)

        text_only = bool(person)
        if text_only:
            for index, look in enumerate(looks, 1):
                attempt(f"dance_{index}", dance + " " + person + " Outfit: " + look["outfit"], out / f"dance_{index}.mp4", None)
                if index < len(looks):
                    attempt(f"spin_{index}", spin + " " + person + " Source outfit: " + look["outfit"] + ". Target outfit: " + looks[index]["outfit"], out / f"spin_{index}.mp4", None)
            attempt("dance_0", dance + " " + person + " Outfit: " + looks[0]["outfit"], out / "dance_0.mp4", None)
        else:
            for index, look in enumerate(looks, 1):
                attempt(f"dance_{index}", dance + " The model wears: " + look["outfit"], out / f"dance_{index}.mp4", look["url"])
                if index < len(looks):
                    attempt(f"spin_{index}", spin + " First frame equals source reference; final frame equals target reference.", out / f"spin_{index}.mp4", look["url"], looks[index]["url"])
            attempt("dance_0", dance + " The model wears: " + looks[0]["outfit"] + ". " + model_prompt, out / "dance_0.mp4", looks[0]["url"])
            if len(looks) == 2:
                attempt("spin_return", spin + " First frame equals source reference; final frame equals target reference.", out / "spin_return.mp4", looks[1]["url"], looks[0]["url"])
        return clips
    def _video(self, prompt: str, output: Path, start: str | None = None, end: str | None = None) -> str:
        # start=None → text-to-video (no person image), used in no-model mode.
        self.events.write("video_task", "params", output=str(output), pic=start or "", end_pic=end or "", prompt=prompt)
        task = self.media.generate_video(prompt, model=self.video_model, size=self.settings.default_size, duration="4", pic=start, end_pic=end, video_type="0")
        self.events.write("video_task", "submitted", task_id=task, output=str(output), has_end_frame=bool(end))
        result = self.media.poll_video(task)
        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"Video generation failed: {result}")
        self.media.download_media(result["url"], output)
        self.events.write("video_task", "done", task_id=task, output=str(output))
        return str(output)

    def _render_timeline(self, out: Path, plan: list[dict[str, Any]], looks: list[dict[str, str]], raw: dict[str, str]) -> list[Path]:
        out.mkdir(parents=True, exist_ok=True)
        segments: list[Path] = []
        for order, beat in enumerate(plan, 1):
            # Skip beats whose source clip failed to generate (tolerance), so a
            # single failed clip just shortens the final video instead of crashing.
            if beat["source"] not in raw:
                self.events.write("beat", "skipped_missing_source", order=order, **beat)
                logger.warning("Skipping beat %s: source clip %s missing", beat["name"], beat["source"])
                continue
            path = out / f"{order:02d}_{beat['name']}.mp4"
            duration = str(beat["duration"])
            # Every beat is a dance motion clip, trimmed to its beat duration and
            # hard-cut against its neighbours (no morphs, no static stills).
            self._run([self.ffmpeg, "-y", "-i", raw[beat["source"]], "-t", duration,
                       "-vf", "fps=30,scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",
                       "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)])
            segments.append(path)
            self.events.write("beat", "rendered", order=order, **beat, path=str(path))
        if not segments:
            raise RuntimeError("没有任何视频片段可拼接（全部片段生成失败）")
        return segments

    def _assemble(self, segments: list[Path], output: Path) -> None:
        listing = output.parent / "timeline_concat.txt"
        listing.write_text("".join(f"file '{path.as_posix()}'\n" for path in segments), encoding="utf-8")
        video = output.parent / "timeline_video.mp4"
        self._run([self.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(video)])
        cmd = [self.ffmpeg, "-y", "-i", str(video)]
        if REFERENCE_VIDEO.is_file():
            cmd += ["-stream_loop", "-1", "-i", str(REFERENCE_VIDEO), "-map", "0:v:0", "-map", "1:a:0?", "-t", str(TARGET_DURATION), "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"]
        else:
            cmd += ["-t", str(TARGET_DURATION), "-c:v", "copy"]
        self._run(cmd + ["-movflags", "+faststart", str(output)])

    def _run(self, command: list[str]) -> None:
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "")[-1200:]
            self.events.write("ffmpeg", "failed", command=command, detail=detail)
            raise RuntimeError(f"FFmpeg failed: {detail}") from exc