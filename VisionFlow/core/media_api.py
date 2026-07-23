"""视频/图片生成 API 客户端 - 对接 GKAPI"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)


class MediaAPIClient:
    """图片/视频生成 API 客户端"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        poll_interval: int = 5,
        max_poll_attempts: int = 120,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.max_poll_attempts = max_poll_attempts

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ── 图片生成 ───────────────────────────────────────────

    def generate_image(
        self,
        prompt: str,
        model: str = "gemini-3.0",
        size: str = "1080p",
        aspect_ratio: str | None = None,
        pic: str | list[str] | None = None,
    ) -> str:
        """提交图片生成任务，返回 task_id"""
        url = f"{self.base_url}/v1/images/generations"
        body: dict[str, Any] = {
            "prompt": prompt,
            "model": model,
            "size": size,
        }
        if aspect_ratio:
            body["aspectRatio"] = aspect_ratio
        if pic:
            body["pic"] = pic

        resp = requests.post(url, headers=self._headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        # 检查错误
        if "error" in data:
            raise RuntimeError(f"图片生成请求失败: {data['error'].get('message', data)}")

        task_id = data.get("id", "")
        if not task_id:
            raise RuntimeError(f"API 未返回任务 ID: {data}")

        logger.info("图片生成任务已提交: %s", task_id)
        return task_id

    def poll_image(self, task_id: str) -> dict[str, Any]:
        """轮询图片任务状态，返回 {status, url, ...}"""
        return self._poll_task(f"/v1/images/generations/{task_id}", task_id)

    # ── 视频生成 ───────────────────────────────────────────

    def generate_video(
        self,
        prompt: str,
        model: str = "v6",
        size: str = "1080p",
        duration: str = "8",
        audio: bool = False,
        aspect_ratio: str | None = None,
        pic: str | None = None,
        end_pic: str | None = None,
        pics: list[str] | None = None,
        video_type: str = "1",
    ) -> str:
        """提交视频生成任务，返回 task_id

        Args:
            video_type: 首尾帧模式的视频类型。"1" = 固定15秒，"0" = 其他时长
        """
        url = f"{self.base_url}/v1/videos/generations"
        body: dict[str, Any] = {
            "prompt": prompt,
            "model": model,
            "size": size,
            "duration": duration,
        }
        if aspect_ratio:
            body["aspectRatio"] = aspect_ratio
        if audio:
            body["audio"] = False
        if pic:
            body["pic"] = pic
        if end_pic:
            body["pic2"] = end_pic
            body["videoType"] = video_type
        if pics:
            body["pics"] = pics

        resp = requests.post(url, headers=self._headers, json=body, timeout=30)
        if not resp.ok:
            detail = resp.text.strip()[:1500]
            raise RuntimeError(f"Video generation request failed (HTTP {resp.status_code}): {detail or 'empty response'}")
        data = resp.json()

        if "error" in data:
            raise RuntimeError(f"Video generation request failed: {data['error'].get('message', data)}")

        task_id = data.get("id", "")
        if not task_id:
            raise RuntimeError(f"API 未返回任务 ID: {data}")

        logger.info("视频生成任务已提交: %s", task_id)
        return task_id

    def poll_video(self, task_id: str) -> dict[str, Any]:
        """轮询视频任务状态，返回 {status, url, ...}"""
        return self._poll_task(f"/v1/videos/generations/{task_id}", task_id)

    # ── 通用轮询 ───────────────────────────────────────────

    def _poll_task(self, endpoint: str, task_id: str) -> dict[str, Any]:
        """轮询任务直到完成，返回最终结果"""
        url = f"{self.base_url}{endpoint}"

        for attempt in range(1, self.max_poll_attempts + 1):
            try:
                resp = requests.get(url, headers=self._headers, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("轮询失败 (task=%s, 第 %d 次): %s", task_id, attempt, exc)
                time.sleep(self.poll_interval)
                continue

            status = data.get("status", "")

            if status == "success":
                logger.info("任务完成: %s → %s", task_id, data.get("url", "")[:80])
                return data

            if status == "failed":
                logger.error("任务失败: %s → %s", task_id, data)
                return data

            # processing / pending → 继续等待
            if attempt % 6 == 0:
                elapsed = attempt * self.poll_interval
                logger.info(
                    "任务 %s 状态: %s (已等待 %ds)", task_id, status, elapsed,
                )
            time.sleep(self.poll_interval)

        raise TimeoutError(
            f"任务 {task_id} 超时 (>{self.max_poll_attempts * self.poll_interval}s)"
        )

    # ── 文件下载 ───────────────────────────────────────────

    @staticmethod
    def download_media(url: str, save_path: str | Path) -> str:
        """下载媒体文件到本地

        如果是 MP4 视频，下载后自动用 ffmpeg 将 moov atom 移到文件头部，
        并且增加重试机制（最多重试2次，共3次尝试）以确保本地视频完整可播放。
        """
        import shutil
        import subprocess
        import time

        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)

        max_attempts = 3
        last_exc = None

        for attempt in range(1, max_attempts + 1):
            try:
                if attempt == 1:
                    logger.info("下载: %s → %s", url[:80], save_path)
                else:
                    logger.info("下载 (第 %d 次尝试): %s → %s", attempt, url[:80], save_path)
                    
                resp = requests.get(url, stream=True, timeout=120)
                resp.raise_for_status()

                expected_size = int(resp.headers.get("content-length", 0))

                with open(save_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)

                actual_size = save_path.stat().st_size
                size_mb = actual_size / (1024 * 1024)
                
                if expected_size > 0 and actual_size < expected_size:
                    raise RuntimeError(f"文件大小不完整，期望 {expected_size} 字节，实际 {actual_size} 字节")
                
                if actual_size == 0:
                    raise RuntimeError("下载文件为空")

                logger.info("下载完成: %.2f MB → %s", size_mb, save_path)

                # MP4 视频自动 faststart：把 moov atom 移到文件头部，并校验完整性
                if save_path.suffix.lower() == ".mp4":
                    tmp_path = save_path.with_suffix(".tmp.mp4")
                    ffmpeg = shutil.which("ffmpeg")
                    if not ffmpeg:
                        logger.warning("未找到 ffmpeg，跳过视频完整性校验")
                        return str(save_path)

                    cmd = [
                        ffmpeg, "-y", "-i", str(save_path),
                        "-c", "copy",           # 不重编码，只复制流
                        "-movflags", "+faststart",  # 把 moov 移到头部
                        str(tmp_path),
                    ]
                    try:
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                        if result.returncode == 0 and tmp_path.exists():
                            tmp_path.replace(save_path)
                            logger.info("faststart 及完整性校验完成: %s", save_path.name)
                            return str(save_path)
                        else:
                            if tmp_path.exists():
                                tmp_path.unlink()
                            err_msg = result.stderr[:200] if result.stderr else "unknown"
                            raise RuntimeError(f"视频完整性校验失败 (faststart failed): {err_msg}")
                    except Exception as exc:
                        if isinstance(exc, RuntimeError):
                            raise exc
                        logger.warning("运行 ffmpeg 出现异常，跳过校验: %s", exc)
                        return str(save_path)

                # 图片自动后处理: 加手机照片瑕疵(模糊/降饱和/JPEG压缩痕迹),
                # 破坏 AI 图的干净纹理, 降低被识别为 AI 生成的概率
                if save_path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    try:
                        _add_phone_photo_imperfections(save_path)
                    except Exception as pexc:
                        logger.warning("图片后处理失败(已跳过,保留原图): %s", pexc)

                return str(save_path)

            except Exception as exc:
                last_exc = exc
                logger.warning("下载尝试 %d 失败: %s", attempt, exc)
                if attempt < max_attempts:
                    time.sleep(2)
                else:
                    logger.error("下载最终失败，已达到最大尝试次数。")
                    raise last_exc

        return str(save_path)


def _add_phone_photo_imperfections(save_path) -> None:
    """给下载的图片加"手机照片瑕疵",破坏 AI 图的干净纹理

    AI 生成图通常是干净 PNG(锐利/饱和/完美),一眼能看出是 AI。
    本函数模拟真实手机照片的瑕疵:
    1. 轻微高斯模糊(手机镜头) — 破坏 AI 图的锐利边缘
    2. 降饱和/对比/锐度(手机相机调校)
    3. JPEG 压缩痕迹(先存低质量 JPEG 再读回,块状瑕疵烙印进图片)

    随机化参数,避免每张图瑕疵一模一样。
    Pillow 未安装时静默跳过(保留原图)。
    """
    import random
    try:
        from PIL import Image, ImageFilter, ImageEnhance
        import io
    except ImportError:
        logger.info("Pillow 未安装, 跳过图片后处理: %s", save_path.name)
        return

    img = Image.open(save_path).convert("RGB")

    # 1. 轻微高斯模糊(模拟手机镜头, 破坏 AI 图的过锐纹理)
    img = img.filter(ImageFilter.GaussianBlur(radius=random.uniform(0.3, 0.7)))

    # 2. 降饱和度 + 降对比度 + 降锐度(模拟手机相机调校, 去"完美感")
    img = ImageEnhance.Color(img).enhance(random.uniform(0.82, 0.95))
    img = ImageEnhance.Contrast(img).enhance(random.uniform(0.88, 0.98))
    img = ImageEnhance.Sharpness(img).enhance(random.uniform(0.85, 0.95))

    # 3. JPEG 压缩痕迹: 先存低质量 JPEG 再读回, 块状瑕疵烙印进图片
    #    (AI 图通常是干净 PNG, JPEG 瑕疵让它看起来像经过手机传输/压缩)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=random.randint(78, 90))
    buf.seek(0)
    img_final = Image.open(buf).convert("RGB")

    # 存回原路径(保留原格式后缀, 但内容已带瑕疵)
    img_final.save(save_path)
    logger.info("图片后处理完成(加瑕疵): %s", save_path.name)
