"""Reference-video-driven outdoor instant outfit change (loop, 2–6 outfits).

Before the reference video is sent to the video model, any human faces in it are
masked (blacked out) with OpenCV — video generation models disallow recognizable
people in reference videos (IP / copyright risk). The masked video path is
returned so it can be reviewed.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import cv2

from config import Settings, get_settings
from core.cos_client import COSClient
from core.media_api import MediaAPIClient

logger = logging.getLogger(__name__)
Progress = Callable[[str], None]
REFERENCE_VIDEO = Path(__file__).resolve().parents[1] / "public" / "28.mp4"
SEG_DURATION = 5  # seconds kept from each transition clip


class ReferenceVideoOutfitPipeline:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        s = self.settings
        self.media = MediaAPIClient(s.gkapi_key, s.gkapi_baseurl, s.poll_interval, s.max_poll_attempts)
        self.cos = COSClient(s.secret_id, s.secret_key, s.region, s.bucket, s.cos_url)
        configured = Path(s.ffmpeg_path)
        self.ffmpeg = str(configured) if configured.is_file() else shutil.which(s.ffmpeg_path) or shutil.which("ffmpeg")

    def run(self, job_dir: Path, model_image: Path | None, model_prompt: str, outfits: list[str], progress: Progress | None = None) -> dict[str, Any]:
        if not 2 <= len(outfits) <= 6:
            raise ValueError("参考视频换装模式支持 2–6 套服装")
        if not REFERENCE_VIDEO.is_file():
            raise RuntimeError("public/28.mp4 缺失")
        if not self.ffmpeg:
            raise RuntimeError("FFmpeg is required")
        job_dir.mkdir(parents=True, exist_ok=True)

        def stage(msg: str) -> None:
            logger.info("reference_video_outfit: %s", msg)
            if progress:
                progress(msg)

        # 1) Chain look images (each keeps the previous woman's identity/scene).
        stage("1/4 Gemini: 生成各套户外造型图")
        seed = self.cos.upload_file(model_image) if model_image else None
        looks: list[tuple[Path, str]] = []
        prev_url: str | None = None
        for i, outfit in enumerate(outfits, 1):
            ref = seed if i == 1 else prev_url
            previous_outfit = outfits[i - 2] if i >= 2 else None
            path, url = self._look(job_dir, i, ref, model_prompt, outfit, previous_outfit)
            looks.append((path, url))
            prev_url = url

        # 2) Mask faces in the reference video before upload (IP-safe).
        stage("2/4 对参考视频做人脸遮挡（避免侵权）")
        masked_path, had_faces = self._mask_faces(REFERENCE_VIDEO, job_dir / "reference_masked.mp4")
        stage(f"参考视频已处理（检测到人脸: {had_faces}）：{masked_path}")
        ref_url = self.cos.upload_file(Path(masked_path))

        # 3) Transition clips chained into a loop (last wraps to first).
        stage("3/4 视频模型：生成瞬间换装片段（循环）")
        n = len(looks)
        clips: list[str] = []
        for i in range(n):
            a_url = looks[i][1]
            b_url = looks[(i + 1) % n][1]
            stage(f"片段 {i + 1}/{n}：{outfits[i]} → {outfits[(i + 1) % n]}")
            clips.append(self._transition_clip(job_dir, i + 1, a_url, b_url, ref_url))

        # 4) Assemble the loop.
        stage("4/4 拼接循环成片")
        output = job_dir / "reference_video_outfit.mp4"
        self._assemble(clips, output)

        manifest = {
            "template": "28.mp4-reference-video-outdoor-outfit",
            "reference_video": str(REFERENCE_VIDEO),
            "reference_masked": str(masked_path),
            "reference_masked_had_faces": had_faces,
            "reference_video_url": ref_url,
            "looks": [{"path": str(p), "url": u} for p, u in looks],
            "clips": clips,
            "final_video": str(output),
        }
        (job_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    # ── steps ────────────────────────────────────────────────

    def _look(self, d: Path, n: int, ref: str | None, person: str, outfit: str, previous: str | None) -> tuple[Path, str]:
        continuity = ("Keep the exact same woman, outdoor location, full-body camera framing, lighting and pose as the reference. " if ref else
                      "Create a full-body photorealistic beautiful woman at a real outdoor scenic viewpoint, natural daylight, medium-wide travel photograph composition. ")
        change = (f"She changes from {previous} to " if previous else "She wears ")
        prompt = continuity + change + outfit + ". Realistic garment fit, detailed scenic background with foreground, middle distance and far distance; no text, logo or extra people. " + person
        task = self.media.generate_image(prompt, model=self.settings.default_image_model, size=self.settings.default_image_size, aspect_ratio="9:16", pic=ref)
        r = self.media.poll_image(task)
        if r.get("status") != "success" or not r.get("url"):
            raise RuntimeError(f"Look {n} failed: {r}")
        p = d / f"look_{n}.png"
        self.media.download_media(r["url"], p)
        return p, self.cos.upload_file(p)

    def _transition_clip(self, job_dir: Path, number: int, a_url: str, b_url: str, ref_url: str) -> str:
        prompt = ("Use the reference video for its camera choreography, natural body motion and timing only. "
                  "Reconstruct any blurred/masked face or body regions into a clear natural appearance — never keep blur, mosaic or masking in the result. "
                  "Create a photorealistic outdoor fashion video of the exact same beautiful woman from the two image references. "
                  "She starts in outfit A in a real scenic outdoor location, makes the same key movement as the reference video, "
                  "and at the peak motion instantly transforms into outfit B with a clean cinematic fabric-spark transition. "
                  "Keep face, hair, body proportions, full-body framing, natural daylight and scenery perspective consistent. "
                  "No text, logos, extra people, distorted hands, abrupt camera jump or dissolve.")
        task = self.media.generate_video(prompt, model="seedance-2.0-fast", size=self.settings.default_size, duration=str(SEG_DURATION),
                                         pic=a_url, end_pic=b_url, video=ref_url, video_type="0")
        result = self.media.poll_video(task)
        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"Transition clip {number} failed: {result}")
        out = job_dir / f"clip_{number:02d}.mp4"
        self.media.download_media(result["url"], out)
        return str(out)

    def _mask_faces(self, video_path: Path, output_path: Path) -> tuple[str, bool]:
        """Black out any detected faces frame-by-frame. Returns (masked_path, any_face_found).
        Falls back to the original path if OpenCV cannot open the video."""
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            logger.warning("mask_faces: cannot open %s, using original", video_path)
            return str(video_path), False
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cascade = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
        writer = cv2.VideoWriter(str(output_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        any_face = False
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            try:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = cascade.detectMultiScale(gray, scaleFactor=1.3, minNeighbors=5)
            except Exception:
                faces = []
            for (x, y, fw, fh) in faces:
                any_face = True
                pad = int(0.15 * max(fw, fh))
                x0, y0 = max(0, x - pad), max(0, y - pad)
                x1, y1 = min(w, x + fw + pad), min(h, y + fh + pad)
                # Heavy blur (color-preserving) instead of a black box, so the
                # generated video doesn't inherit visible black patches.
                roi = frame[y0:y1, x0:x1]
                frame[y0:y1, x0:x1] = cv2.GaussianBlur(roi, (0, 0), sigmaX=18)
            writer.write(frame)
        cap.release()
        writer.release()
        # Re-encode mp4v → H.264; the video model rejects non-H.264 reference video.
        h264 = output_path.with_name(output_path.stem + "_h264.mp4")
        subprocess.run(
            [self.ffmpeg, "-y", "-i", str(output_path), "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", "-an", str(h264)],
            check=True, capture_output=True, text=True,
        )
        return str(h264), any_face

    def _assemble(self, clips: list[str], output: Path) -> None:
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
        video = output.parent / "loop.mp4"
        subprocess.run(
            [self.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(video)],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [self.ffmpeg, "-y", "-i", str(video), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)],
            check=True, capture_output=True, text=True,
        )
