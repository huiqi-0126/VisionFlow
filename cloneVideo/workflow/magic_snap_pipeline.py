"""Magic snap loop — a standalone outfit/scenery switch video.

A new capability that runs alongside the existing 23.mp4 outfit-switch mode and
does not touch it. Flow:
  1. Generate one model anchor (identity reference).
  2. For each outfit, generate a SCENE image = the same model standing in a
     beautiful outdoor location (snow mountain / lake / grassland / sea / ...)
     wearing that outfit.
  3. For each scene, generate a short image-to-image transition clip where the
     model smiles and snaps her fingers and the background + outfit transform
     into the next scene. The last scene wraps back to the first → a loop.
  4. Assemble the clips into one looping video (each segment ~2.5s).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from config import Settings, get_settings
from core.cos_client import COSClient
from core.media_api import MediaAPIClient

logger = logging.getLogger(__name__)
Progress = Callable[[str], None]

# Beautiful outdoor scenery cycled across the loop iterations.
SCENIC_BACKGROUNDS = [
    "snow-capped mountain peaks under a clear blue sky",
    "a calm turquoise mountain lake reflecting the sky",
    "endless green rolling grassland under blue sky",
    "a tropical ocean beach with turquoise water and white sand",
    "a dense misty pine forest",
    "golden desert sand dunes at sunset",
    "a vast colorful wildflower field",
    "a city skyline glowing at sunset",
]
SEG_DURATION = 2.5  # seconds kept from each 4s transition clip
class MagicSnapPipeline:
    """Generate a looping snap-to-transform video across scenic backgrounds."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        s = self.settings
        self.media = MediaAPIClient(s.gkapi_key, s.gkapi_baseurl, s.poll_interval, s.max_poll_attempts)
        self.cos = COSClient(s.secret_id, s.secret_key, s.region, s.bucket, s.cos_url)
        self.image_model, self.video_model = s.default_image_model, s.default_video_model
        configured = Path(s.ffmpeg_path)
        self.ffmpeg = str(configured) if configured.is_file() else shutil.which(s.ffmpeg_path) or shutil.which("ffmpeg")

    def run(
        self,
        job_dir: Path,
        model_image: Path | None,
        model_prompt: str,
        outfit_images: list[Path],
        outfit_prompts: list[str],
        on_progress: Progress | None = None,
    ) -> dict[str, Any]:
        if not self.ffmpeg:
            raise RuntimeError("FFmpeg is required for magic-snap assembly")
        if not 2 <= len(outfit_prompts) <= 6:
            raise ValueError("请提供 2–6 套服装")
        job_dir.mkdir(parents=True, exist_ok=True)

        def stage(msg: str) -> None:
            logger.info("magic_snap: %s", msg)
            if on_progress:
                on_progress(msg)

        stage("1/4 生成统一模特锚图")
        anchor_path, anchor_url = self._make_anchor(job_dir, model_image, model_prompt)

        n = len(outfit_prompts)
        scenes: list[dict[str, str]] = []
        for i, outfit in enumerate(outfit_prompts, 1):
            bg = SCENIC_BACKGROUNDS[(i - 1) % len(SCENIC_BACKGROUNDS)]
            stage(f"2/4 生成场景图 {i}/{n}：{bg}")
            path, url = self._make_scene(job_dir, i, anchor_url, outfit, bg)
            scenes.append({"index": str(i), "outfit": outfit, "background": bg, "path": str(path), "url": url})

        stage("3/4 生成响指变换片段（循环）")
        clips: list[str] = []
        for i in range(n):
            src, dst = scenes[i], scenes[(i + 1) % n]
            stage(f"片段 {i + 1}/{n}：{src['background']} → {dst['background']}")
            clips.append(self._snap_clip(job_dir, i + 1, src["url"], dst["url"]))

        stage("4/4 拼接循环成片")
        output = job_dir / "magic_snap.mp4"
        self._assemble_loop(clips, output)

        manifest = {
            "template": "magic-snap-loop",
            "anchor": str(anchor_path),
            "scenes": scenes,
            "clips": clips,
            "segment_duration": SEG_DURATION,
            "final_video": str(output),
        }
        (job_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    # ── steps ────────────────────────────────────────────────

    def _make_anchor(self, job_dir: Path, model_image: Path | None, model_prompt: str) -> tuple[Path, str]:
        reference = self.cos.upload_file(model_image) if model_image else None
        text = ("Medium half-body (waist-up) portrait of one fashion model, front-facing, centered in the frame, clean simple studio background as an identity reference. "
                + (model_prompt or ""))
        task = self.media.generate_image(text, model=self.image_model, size=self.settings.default_image_size, aspect_ratio="9:16", pic=reference)
        result = self.media.poll_image(task)
        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"Anchor failed: {result}")
        path = job_dir / "model_anchor.png"
        self.media.download_media(result["url"], path)
        return path, self.cos.upload_file(path)
    def _make_scene(self, job_dir: Path, number: int, anchor_url: str, outfit: str, background: str) -> tuple[Path, str]:
        text = ("Keep the exact same model as the reference (same face, hair, body). "
                "Medium half-body shot from the waist up, the model centered in the frame, front-facing the camera. "
                "Behind her is a beautiful outdoor location: " + background + ". She wears: " + outfit + ". "
                "Photorealistic, natural lighting. The model stays centered and upright with the same pose; the scenic background fills the area behind her. "
                "No text, no logo, no other people.")
        task = self.media.generate_image(text, model=self.image_model, size=self.settings.default_image_size, aspect_ratio="9:16", pic=anchor_url)
        result = self.media.poll_image(task)
        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"Scene {number} failed: {result}")
        path = job_dir / f"scene_{number:02d}.png"
        self.media.download_media(result["url"], path)
        return path, self.cos.upload_file(path)
    def _snap_clip(self, job_dir: Path, number: int, start_url: str, end_url: str) -> str:
        prompt = ("The exact same model, centered in a locked half-body frame, smiles sweetly — a warm natural smile — and snaps her fingers. "
                  "Her pose, position, body and face stay completely fixed and motionless except for the smile and the finger snap; only the background scenery behind her and her outfit change instantly at the snap into the target scene and outfit. "
                  "One continuous locked-camera shot, no cut, no camera move, no body movement. No text, no other people, no warped hands.")
        task = self.media.generate_video(prompt, model=self.video_model, size=self.settings.default_size, duration="4", pic=start_url, end_pic=end_url, video_type="0")
        result = self.media.poll_video(task)
        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"Snap clip {number} failed: {result}")
        out = job_dir / f"snap_{number:02d}.mp4"
        self.media.download_media(result["url"], out)
        return str(out)
    def _assemble_loop(self, clips: list[str], output: Path) -> None:
        norm = output.parent / "normalized"
        norm.mkdir(exist_ok=True)
        segs: list[Path] = []
        for i, clip in enumerate(clips, 1):
            seg = norm / f"seg_{i:02d}.mp4"
            subprocess.run(
                [self.ffmpeg, "-y", "-i", clip, "-t", str(SEG_DURATION),
                 "-vf", "fps=30,scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",
                 "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(seg)],
                check=True, capture_output=True, text=True,
            )
            segs.append(seg)
        concat = output.parent / "concat.txt"
        concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8")
        video = output.parent / "loop_video.mp4"
        subprocess.run(
            [self.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(video)],
            check=True, capture_output=True, text=True,
        )
        total = round(SEG_DURATION * len(clips), 3)
        subprocess.run(
            [self.ffmpeg, "-y", "-i", str(video), "-t", str(total),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)],
            check=True, capture_output=True, text=True,
        )
