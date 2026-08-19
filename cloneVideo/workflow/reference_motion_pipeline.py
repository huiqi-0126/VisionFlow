"""Reference-motion video generation.

A standalone module: upload a reference video (required) + optionally 2 reference
images + a text prompt, then generate a new video that REUSES the reference
video's full motion / action / background / scene.

Before the reference video is sent to the video model it is analyzed and any
sensitive regions are masked (faces blacked out; exposed-skin regions masked by
contour). The text prompt sent to the video model is the user's own prompt,
used as-is (no fixed instructions or safety suffix are injected).
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
import numpy as np

from config import Settings, get_settings
from core.cos_client import COSClient
from core.media_api import MediaAPIClient

logger = logging.getLogger(__name__)
Progress = Callable[[str], None]

# Video API supported aspect ratios → numeric value (used to keep the output
# aspect ratio consistent with the uploaded reference video).
SUPPORTED_ASPECT_RATIOS = {
    "1:1": 1.0, "4:3": 4 / 3, "3:2": 3 / 2, "16:9": 16 / 9, "21:9": 21 / 9,
    "3:4": 3 / 4, "2:3": 2 / 3, "9:16": 9 / 16, "4:5": 4 / 5, "5:4": 5 / 4,
}

# Models that support the reference-video mode (`video` param). minimax-h3
# DOES support uploading a reference video.
REFERENCE_VIDEO_MODELS = {"seedance-2.0-fast", "seedance-2.0-standard", "minimax-h3"}


class ReferenceMotionPipeline:
    """Reuse a reference video's motion to generate a new video."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        s = self.settings
        self.media = MediaAPIClient(s.gkapi_key, s.gkapi_baseurl, s.poll_interval, s.max_poll_attempts)
        self.cos = COSClient(s.secret_id, s.secret_key, s.region, s.bucket, s.cos_url)
        # Resolve ffmpeg robustly: configured file → configured dir → PATH.
        # Never leave it None (a None entry in subprocess args would crash with a
        # cryptic "expected str, bytes or os.PathLike object, not NoneType").
        configured = Path(s.ffmpeg_path)
        if configured.is_file():
            self.ffmpeg = str(configured)
        elif configured.is_dir():
            self.ffmpeg = shutil.which("ffmpeg", path=str(configured)) or "ffmpeg"
        else:
            self.ffmpeg = shutil.which(s.ffmpeg_path) or shutil.which("ffmpeg") or "ffmpeg"

    def run(
        self,
        job_dir: Path,
        ref_video: Path,
        images: list[Path],
        user_prompt: str,
        progress: Progress | None = None,
        mask_mode: str = "light",
        mask_skin: bool = False,
        model: str = "minimax-h3",
        size: str = "768p",
        durations: list[int] | None = None,
    ) -> dict[str, Any]:
        if not ref_video.is_file():
            raise RuntimeError("参考视频不存在")
        job_dir.mkdir(parents=True, exist_ok=True)

        def stage(msg: str) -> None:
            logger.info("reference_motion: %s", msg)
            if progress:
                progress(msg)

        # ffmpeg is required to mask / re-encode the reference video. Fail with
        # an actionable message instead of a cryptic NoneType crash.
        if not (Path(self.ffmpeg).is_file() or shutil.which(self.ffmpeg)):
            raise RuntimeError(
                "未找到 ffmpeg，无法处理参考视频。请安装 ffmpeg 并加入 PATH，"
                f"或修正 .env 中的 FFMPEG_PATH（当前值: {self.settings.ffmpeg_path!r}）"
            )

        # 1) Analyze + mask sensitive regions in the reference video.
        stage(f"1/3 处理参考视频（遮挡方式: {mask_mode}）")
        masked_path, summary = self._mask_sensitive(ref_video, job_dir / "reference_masked.mp4", mode=mask_mode, mask_skin=mask_skin)
        stage(f"参考视频已处理：{summary} -> {masked_path}")
        ref_url = self.cos.upload_file(Path(masked_path))

        # 2) Upload optional reference images (0–2).
        image_urls: list[str] = []
        for i, img in enumerate(images[:2], 1):
            if img and img.is_file():
                image_urls.append(self.cos.upload_file(img))

        # 3) Build the prompt: the user's own prompt + a fixed suffix that binds
        # the reference image (subject) to the reference video (motion/scene), so
        # the model actually REPLACES the person instead of ignoring the image.
        prompt = self._build_prompt(user_prompt, has_subject_image=bool(image_urls))
        stage("2/3 视频模型：生成视频")
        ref_dur = float(self._reference_duration(ref_video))
        duration = self._pick_duration(durations, ref_dur) if durations else str(int(ref_dur))
        aspect = self._reference_aspect_ratio(ref_video)

        # 4) Generate.
        # The FIRST reference image is passed as `pic` (first-frame) AND all
        # images (including the first) are passed as `pics` (reference_image role).
        # `pic` anchors scene 1; `pics` keeps the SAME person consistent across
        # every scene, so a multi-scene reference video replaces the person
        # everywhere instead of only in the first scene. The reference video goes
        # to `video` for motion/scene reuse.
        kwargs: dict[str, Any] = dict(
            prompt=prompt,
            model=model,
            size=size,
            duration=duration,
            aspect_ratio=aspect,
            pic=image_urls[0] if image_urls else None,
            pics=image_urls or None,
        )
        if model in REFERENCE_VIDEO_MODELS:
            kwargs["video"] = ref_url
        task = self.media.generate_video(**kwargs)
        result = self.media.poll_video(task)
        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"参考视频生成失败: {result}")
        output = job_dir / "reference_motion.mp4"
        self.media.download_media(result["url"], output)
        stage("3/3 完成")

        manifest = {
            "template": "reference-motion",
            "reference_video": str(ref_video),
            "reference_masked": str(masked_path),
            "mask_summary": summary,
            "reference_video_url": ref_url,
            "images": image_urls,
            "prompt": prompt,
            "duration": duration,
            "final_video": str(output),
        }
        (job_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    # ── masking ──────────────────────────────────────────────

    def _run_ffmpeg(self, cmd: list[str]) -> None:
        """Run an ffmpeg command; on failure surface stderr instead of a bare
        "non-zero exit status" so the real cause (missing DLLs, bad codec,
        corrupt input, …) is visible in the error message."""
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip() or (exc.stdout or "").strip()
            if len(detail) > 1200:
                detail = detail[-1200:]
            raise RuntimeError(
                f"ffmpeg 执行失败（exit={exc.returncode}）: {detail or '无错误输出'}"
            ) from exc

    @staticmethod
    def _skin_mask(frame) -> "np.ndarray":
        """Precise skin mask: YCrCb skin range AND a saturation gate.

        The old broad HSV range (H 0–25, S 20–255, V 70–255) also matched warm
        beige/cream walls, wooden sofas and food. YCrCb (Cr 133–173, Cb 77–127)
        plus S > 40 keeps actual skin while dropping most low-saturation surfaces.
        """
        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        skin = cv2.inRange(ycrcb, np.array([0, 133, 77], dtype=np.uint8), np.array([255, 173, 127], dtype=np.uint8))
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        sat = cv2.inRange(hsv, np.array([0, 40, 0], dtype=np.uint8), np.array([180, 255, 255], dtype=np.uint8))
        return cv2.bitwise_and(skin, sat)

    def _mask_sensitive(self, video_path: Path, output_path: Path, mode: str = "blur", mask_skin: bool = False) -> tuple[str, str]:
        """Mask faces (Haar) and, optionally, exposed-skin regions (contour) frame-by-frame.

        `mode`: "none" / "light" / "medium" / "heavy" (three blur strengths).
        `mask_skin`: when False only FACES are masked (skin-color detection
        over-masks warm furniture, so it is off by default).
        Returns (masked_path, summary_string).
        """
        # Pre-downscale high-res sources (e.g. 4K) to ≤720p: seedance rejects 4K
        # reference video, and masking 4K frames is far slower.
        src = video_path
        probe = cv2.VideoCapture(str(video_path))
        sw = probe.get(cv2.CAP_PROP_FRAME_WIDTH)
        sh = probe.get(cv2.CAP_PROP_FRAME_HEIGHT)
        probe.release()
        if sw and sh and (sw > 1280 or sh > 1280):
            tmp = video_path.with_name(video_path.stem + "_small.mp4")
            self._run_ffmpeg(
                [self.ffmpeg, "-y", "-i", str(video_path),
                 "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", "-an", str(tmp)],
            )
            src = tmp

        cap = cv2.VideoCapture(str(src))
        if not cap.isOpened():
            logger.warning("mask_sensitive: cannot open %s, using original", video_path)
            return str(video_path), "无法解析视频，使用原视频"
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cascade = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
        writer = cv2.VideoWriter(str(output_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))

        faces_total = 0
        skin_total = 0
        frames = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frames += 1

            if mode == "none":
                writer.write(frame)
                continue

            mask = np.zeros((h, w), dtype=np.uint8)

            # faces → fill rects into the mask
            try:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = cascade.detectMultiScale(gray, scaleFactor=1.3, minNeighbors=5)
            except Exception:
                faces = []
            for (x, y, fw, fh) in faces:
                faces_total += 1
                pad = int(0.2 * max(fw, fh))
                x0, y0 = max(0, x - pad), max(0, y - pad)
                x1, y1 = min(w, x + fw + pad), min(h, y + fh + pad)
                cv2.rectangle(mask, (x0, y0), (x1, y1), 255, -1)

            # exposed skin → fill contours into the mask (ONLY when requested:
            # color detection over-masks warm furniture/food, so off by default)
            if mask_skin:
                try:
                    skin_mask = self._skin_mask(frame)
                    skin_mask = cv2.GaussianBlur(skin_mask, (5, 5), 0)
                    contours, _ = cv2.findContours(skin_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    frame_area = w * h
                    for c in contours:
                        area = cv2.contourArea(c)
                        # body parts are mid-sized; ignore tiny specks AND huge surfaces
                        if 0.005 * frame_area < area < 0.25 * frame_area:
                            skin_total += 1
                            cv2.drawContours(mask, [c], -1, 255, -1)
                except Exception:
                    pass

            if np.any(mask):
                # Three blur strengths; lighter keeps more structure so the model
                # can reconstruct a clean face instead of copying a smudge.
                sigma = {"light": 8, "medium": 18, "heavy": 40}.get(mode, 18)
                blurred = cv2.GaussianBlur(frame, (0, 0), sigmaX=sigma)
                frame[mask > 0] = blurred[mask > 0]

            writer.write(frame)

        cap.release()
        writer.release()
        # The video model rejects MPEG-4/mp4v reference video as "not compliant";
        # re-encode to standard H.264 MP4 before upload.
        h264_path = output_path.with_name(output_path.stem + "_h264.mp4")
        self._reencode_h264(output_path, h264_path)
        if mode == "none":
            summary = f"共 {frames} 帧，未遮挡（原始画面）"
        elif mask_skin:
            summary = f"共 {frames} 帧，人脸遮挡 {faces_total} 处，肤色轮廓遮挡 {skin_total} 处（方式: {mode}）"
        else:
            summary = f"共 {frames} 帧，人脸遮挡 {faces_total} 处（方式: {mode}，未启用肤色遮挡）"
        return str(h264_path), summary

    def _reencode_h264(self, src: Path, dst: Path) -> None:
        # Downscale to ≤720p (long side 1280) — seedance rejects 4K reference video;
        # -r 30 normalizes fps (24-60 required, VFR drifts); -t 15 caps duration.
        self._run_ffmpeg(
            [self.ffmpeg, "-y", "-i", str(src),
             "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-r", "30", "-t", "15", "-movflags", "+faststart", "-an", str(dst)],
        )

    # ── prompt / duration ────────────────────────────────────

    @staticmethod
    def _clean_prompt(user_prompt: str) -> str:
        """Return the user's prompt verbatim, with whitespace collapsed."""
        return " ".join((user_prompt or "").split())

    @staticmethod
    def _build_prompt(user_prompt: str, has_subject_image: bool) -> str:
        """Compose the final prompt sent to the video model.

        When a subject image is uploaded, append a fixed instruction that binds
        the image (subject/character) to the reference video (motion/scene), so
        the model REPLACES the video's person with the image's person instead of
        merely reusing the video's motion. The user's own prompt is preserved
        verbatim at the front.
        """
        prompt = " ".join((user_prompt or "").split())
        if not has_subject_image:
            return prompt
        prompt += (
            "。人物主体严格以参考图片中的人物为准，人物的五官、发型、服装与参考图片完全一致；"
            "全片所有场景（含场景切换）中的人物始终是同一个人，外貌不随场景变化；"
            "动作、运镜、背景与场景完全复用参考视频；去掉画面中的所有文字"
        )
        return prompt

    @staticmethod
    def _reference_duration(video_path: Path) -> str:
        cap = cv2.VideoCapture(str(video_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        cap.release()
        secs = frames / fps if fps else 8.0
        # minimax-h3 supports 5–15s
        secs = max(5.0, min(15.0, round(secs)))
        return str(int(secs))

    @staticmethod
    def _reference_aspect_ratio(video_path: Path) -> str | None:
        """Return the API aspect-ratio string closest to the reference video's ratio."""
        cap = cv2.VideoCapture(str(video_path))
        w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        cap.release()
        if not w or not h:
            return None
        ratio = w / h
        best = min(SUPPORTED_ASPECT_RATIOS.items(), key=lambda kv: abs(kv[1] - ratio))
        return best[0]

    @staticmethod
    def _pick_duration(allowed: list[int] | None, ref_secs: float) -> str:
        """Choose the allowed duration (from the model's `durations`) nearest to the
        reference video length, clamped to the model's min/max."""
        ds = sorted(int(x) for x in (allowed or []) if x)
        if not ds:
            return str(max(5, min(15, round(ref_secs))))
        target = max(ds[0], min(ds[-1], round(ref_secs)))
        return str(min(ds, key=lambda d: abs(d - target)))
