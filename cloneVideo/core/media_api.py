"""视频/图片生成 API 客户端 - 对接 GKAPI

提供:
  - 图片生成 (generate_image) — 支持 pic 参考图做风格保持 (image edit)
  - 视频生成 (generate_video) — 支持单图 pic 驱动 4s 短视频 + 运镜 prompt
  - 异步任务轮询 (poll_image / poll_video)
  - 媒体下载 (download_media) — 含 MP4 faststart 完整性校验

注意: 本项目输出为剪辑素材, 图片保持原始高清画质, 不做"手机照片瑕疵"后处理。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
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

    def _post_json_with_retry(self, url: str, body: dict[str, Any], *, attempts: int = 4, read_timeout: float = 60.0) -> Any:
        """POST JSON with retry on network errors / timeouts / 5xx.

        4xx responses are returned immediately so the caller can raise a precise
        error. Transient failures (read timeout, connection reset, 5xx) are
        retried with exponential backoff so a single slow response no longer
        kills the whole job. If 5xx exhausts the retries the last response is
        returned (caller raises the HTTP error); if a network error exhausts
        them, RuntimeError is raised.
        """
        last_resp: Any = None
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = requests.post(url, headers=self._headers, json=body, timeout=(10, read_timeout))
                if resp.ok or 400 <= resp.status_code < 500:
                    return resp
                last_resp = resp
                logger.warning("POST %s HTTP %d (attempt %d/%d)", url, resp.status_code, attempt, attempts)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                logger.warning("POST %s network error (attempt %d/%d): %s", url, attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 10))
        if last_resp is not None:
            return last_resp
        raise RuntimeError(f"POST {url} failed after {attempts} attempts: {last_exc}") from last_exc

    # ── 图片生成 ───────────────────────────────────────────

    def generate_image(
        self,
        prompt: str,
        model: str = "gemini-3.0",
        size: str = "1080p",
        aspect_ratio: str | None = None,
        pic: str | list[str] | None = None,
    ) -> str:
        """提交图片生成任务，返回 task_id

        Args:
            pic: 参考图公网 URL（风格保持）。gemini-3.0 (Nano Banana) 会据此保持风格。
        """
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

        resp = self._post_json_with_retry(url, body)
        if not resp.ok:
            detail = resp.text.strip()[:1500]
            raise RuntimeError(f"Image generation request failed (HTTP {resp.status_code}): {detail or 'empty response'}")
        data = resp.json()

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
        model: str = "seedance-2.0-fast",
        size: str = "1080p",
        duration: str = "4",
        audio: bool = False,
        aspect_ratio: str | None = None,
        pic: str | None = None,
        end_pic: str | None = None,
        pics: list[str] | None = None,
        video_type: str = "0",
    ) -> str:
        """提交视频生成任务，返回 task_id

        Args:
            pic: 首帧参考图公网 URL（本项目用生成的风格图作为单图驱动）
            duration: 默认 "4" 秒素材
            video_type: "0" = 非固定时长模式（用于 4s）
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

        logger.info(
            "VIDEO GEN request | model=%s | size=%s | duration=%s | aspect=%s | pic=%s | end_pic=%s | pics=%s | prompt=%s",
            model, size, duration, aspect_ratio, pic, end_pic, pics, prompt,
        )
        resp = self._post_json_with_retry(url, body)
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
                resp = requests.get(url, headers=self._headers, timeout=30)
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

        对 MP4 视频自动用 ffmpeg 把 moov atom 移到文件头部（faststart），
        并做完整性校验，含重试机制（最多 3 次尝试）。
        图片保持原始高清画质，不做任何后处理（素材用途）。
        """
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)

        max_attempts = 3
        last_exc: Exception | None = None

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

                # MP4 视频自动 faststart：把 moov atom 移到头部，并校验完整性
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

                # 图片保持原始画质，不做后处理
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
