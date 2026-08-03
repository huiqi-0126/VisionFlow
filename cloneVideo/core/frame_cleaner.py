"""帧清理器 - 把真实视频截图去人/去字，保留房间布局与家具

新流程里，生成的图片要以"真实房间截图"作为基底（pic 输入），
但截图里可能有文字、字幕、水印、人物、手等——这些要清掉，
只保留干净的房间布局/家具，再拿去统一改造风格。

策略：
  - 用 VLM 生成的帧描述判断是否需要清理（有人/有文字才清理，省 API 调用）
  - 需要清理 → 上传 TOS → AI image-edit 去人/字（保留布局）→ 下载干净基底图
  - 干净帧 / 清理失败 → 直接用原帧作基底
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.cos_client import COSClient
from core.media_api import MediaAPIClient
from core.video_analyzer import KeyFrame

logger = logging.getLogger(__name__)


class FrameCleaner:
    """真实帧去人/去字清理器"""

    def __init__(
        self,
        media_api: MediaAPIClient,
        cos: COSClient,
        image_model: str = "gemini-3.0",
        size: str = "1080p",
    ) -> None:
        self.media_api = media_api
        self.cos = cos
        self.image_model = image_model
        self.size = size

    def clean_frames(self, frames: list[KeyFrame], output_dir: str | Path) -> int:
        """逐帧清理，设置 frame.clean_path，返回真正做了 AI 清理的帧数"""
        clean_dir = Path(output_dir) / "clean_frames"
        clean_dir.mkdir(parents=True, exist_ok=True)

        cleaned_count = 0
        for frame in frames:
            frame.clean_path = self._clean_one(frame, clean_dir)
            if frame.clean_path != frame.path:
                cleaned_count += 1

        logger.info(
            "帧清理完成: %d 帧, %d 帧做了 AI 清理, %d 帧本身干净",
            len(frames), cleaned_count, len(frames) - cleaned_count,
        )
        return cleaned_count

    # ── 单帧清理 ──────────────────────────────────────────────

    def _clean_one(self, frame: KeyFrame, clean_dir: Path) -> str:
        """清理单帧，返回干净基底图路径（清理失败/本干净则返回原帧路径）"""
        desc = frame.description or ""
        if not self._needs_clean(desc):
            return frame.path

        try:
            pic_url = self.cos.upload_file(frame.path)
            prompt = self._build_clean_prompt(desc)
            task_id = self.media_api.generate_image(
                prompt=prompt,
                model=self.image_model,
                size=self.size,
                pic=pic_url,  # 不传 aspect_ratio, 保持原帧比例
            )
            result = self.media_api.poll_image(task_id)
            if result.get("status") == "success" and result.get("url"):
                out = clean_dir / f"clean_{frame.index:03d}.png"
                self.media_api.download_media(result["url"], out)
                logger.info("帧 %d 清理成功: %s", frame.index, out.name)
                return str(out)
            logger.warning("帧 %d 清理返回非成功状态: %s", frame.index, result)
        except Exception as exc:
            logger.warning("帧 %d 清理失败, 回退原帧: %s", frame.index, exc)

        return frame.path

    def _build_clean_prompt(self, desc: str) -> str:
        """根据帧内容构建清理 prompt（去人 + 去字，保留布局家具）"""
        parts: list[str] = []
        if self._has_text(desc):
            parts.append(
                "Remove all text, captions, subtitles, watermarks, logos and corner icons "
                "from the image completely."
            )
        if self._has_person(desc):
            parts.append(
                "Remove all humans, people, faces, hands and any body parts from the image "
                "completely. The final image must contain ZERO people."
            )
        parts.append(
            "CRITICAL: Keep the original room layout, camera angle, furniture arrangement, "
            "furniture pieces, materials and background EXACTLY the same. Do NOT move, add, "
            "remove or replace anything else. Photorealistic, high quality, clear."
        )
        return " ".join(parts)

    # ── 判定 ──────────────────────────────────────────────────

    def _needs_clean(self, desc: str) -> bool:
        return self._has_person(desc) or self._has_text(desc)

    @staticmethod
    def _has_person(desc: str) -> bool:
        if not desc:
            return False
        keywords = ["人", "男", "女", "孩", "面", "脸", "手", "脚", "身影", "背影",
                    "特写", "近景", "人物", "person", "woman", "man", "face", "hand"]
        return any(kw in desc for kw in keywords)

    @staticmethod
    def _has_text(desc: str) -> bool:
        if not desc:
            return False
        text_kws = ["文字", "字幕", "水印", "logo", "图标", "角标", "台标", "标志",
                    "招牌", "标题", "text", "watermark", "logo", "caption"]
        neg_kws = ["无文字", "无字幕", "没有文字", "没有字幕", "无水印", "无logo", "无标志"]
        has_text = any(kw in desc for kw in text_kws)
        has_neg = any(kw in desc for kw in neg_kws)
        return has_text and not has_neg
