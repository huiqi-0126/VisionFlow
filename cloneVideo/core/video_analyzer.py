"""视频解析器 - 关键帧提取 + 多模态 LLM 画面分析 + 分辨率检测

精简版：去掉视频合并 (merge_clips) 和单帧提取 (extract_last_frame)，
因为本项目输出是独立素材，不合并成片。
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.llm_client import LLMClient

logger = logging.getLogger(__name__)


@dataclass
class KeyFrame:
    """关键帧"""
    timestamp: float          # 秒
    path: str                 # 图片路径
    index: int                # 序号
    description: str = ""     # LLM 生成的画面描述
    clean_path: str = ""      # 去人/字后的干净基底图路径(风格改造用)


def aspect_ratio_from_resolution(width: int, height: int) -> str:
    """从分辨率推导最接近的常见 "W:H" 比例字符串

    用于让生成图的画幅跟随源视频。gkapi 的 aspectRatio 接受任意 "W:H"，
    但映射到常见比例可避免个别模型只认固定选项的问题。
    """
    if width <= 0 or height <= 0:
        return "16:9"

    from math import gcd
    g = gcd(width, height)
    rw, rh = width // g, height // g

    # 常见比例归一化（容忍微小偏差）
    ratio = width / height
    common = [
        (16, 9, 16 / 9),       # 横屏
        (9, 16, 9 / 16),       # 竖屏
        (1, 1, 1.0),           # 方形
        (4, 3, 4 / 3),         # 老式横屏
        (3, 4, 3 / 4),         # 老式竖屏
        (21, 9, 21 / 9),       # 超宽
    ]
    best = min(common, key=lambda c: abs(c[2] - ratio))
    # 若原始比例与某个常见比例足够接近(容差 0.05, 覆盖 ultrawide 21:9 等), 用常见值
    # 生成模型对 "21:9" 这类标称值比 "64:27" 这种精确约分更友好
    if abs(best[2] - ratio) < 0.05:
        return f"{best[0]}:{best[1]}"
    # 否则用约分后的真实比例（如奇特的 5:4）
    return f"{rw}:{rh}"


class VideoAnalyzer:
    """视频解析：提取关键帧 + 多模态 LLM 分析 + 分辨率检测"""

    def __init__(
        self,
        llm: LLMClient,
        keyframe_interval: int = 2,
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        self.llm = llm
        self.keyframe_interval = keyframe_interval
        self.ffmpeg_path = ffmpeg_path
        # ffprobe 和 ffmpeg 同目录，自动推导
        import os
        if ffmpeg_path and ffmpeg_path != "ffmpeg":
            ffmpeg_dir = str(Path(ffmpeg_path).parent)
            self.ffprobe_path = str(Path(ffmpeg_dir) / "ffprobe.exe" if os.name == "nt" else Path(ffmpeg_dir) / "ffprobe")
        else:
            self.ffprobe_path = "ffprobe"

    # ── 视频基础信息 ─────────────────────────────────────────

    def get_video_duration(self, video_path: str | Path) -> float:
        """用 ffprobe 获取视频时长（秒）"""
        cmd = [
            self.ffprobe_path, "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            result.check_returncode()
            return float(result.stdout.strip())
        except Exception as exc:
            logger.error("获取视频时长失败: %s", exc)
            raise RuntimeError(f"无法获取视频时长: {exc}") from exc

    def get_video_resolution(self, video_path: str | Path) -> tuple[int, int]:
        """获取视频分辨率，返回 (width, height)

        用于让生成素材的画幅跟随源视频（横屏/竖屏/方形自适应）。
        """
        cmd = [
            self.ffprobe_path, "-v", "quiet",
            "-show_entries", "stream=width,height",
            "-select_streams", "v:0",
            "-of", "csv=s=x:p=0",
            str(video_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            result.check_returncode()
            parts = result.stdout.strip().split("x")
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        except Exception as exc:
            logger.warning("获取分辨率失败 %s: %s", video_path, exc)
        return 0, 0

    # ── 关键帧提取 ────────────────────────────────────────────

    def extract_keyframes(
        self,
        video_path: str | Path,
        output_dir: str | Path,
        interval: float | None = None,
    ) -> list[KeyFrame]:
        """从视频中按间隔提取关键帧(支持小数秒, 用于按目标帧数均匀覆盖整段视频)

        使用 ffmpeg: ffmpeg -i input.mp4 -vf fps=1/N -q:v 2 output/frame_%04d.jpg
        interval 不传时用构造时的 keyframe_interval。
        """
        video_path = Path(video_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        iv = float(interval) if interval else float(self.keyframe_interval)
        iv = max(iv, 0.1)  # 防止过小/除零
        fps_filter = f"fps={1.0 / iv:.4f}"
        output_pattern = str(output_dir / "frame_%04d.jpg")

        cmd = [
            self.ffmpeg_path, "-y",
            "-i", str(video_path),
            "-vf", fps_filter,
            "-q:v", "2",
            output_pattern,
        ]

        logger.info("提取关键帧: %s (间隔 %.2fs)", video_path.name, iv)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                logger.error("ffmpeg stderr: %s", result.stderr[:500])
                raise RuntimeError(f"ffmpeg 提取关键帧失败: {result.stderr[:200]}")
        except FileNotFoundError:
            raise RuntimeError("ffmpeg 未安装或不在 PATH 中")
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffmpeg 提取关键帧超时")

        # 收集生成的帧文件
        frames: list[KeyFrame] = []
        for img_file in sorted(output_dir.glob("frame_*.jpg")):
            try:
                idx = int(img_file.stem.split("_")[1])
            except (IndexError, ValueError):
                idx = len(frames) + 1
            timestamp = round((idx - 1) * iv, 3)
            frames.append(KeyFrame(
                timestamp=timestamp,
                path=str(img_file),
                index=idx,
            ))

        logger.info("提取了 %d 个关键帧", len(frames))
        return frames

    # ── 多模态 LLM 分析 ────────────────────────────────────────

    def analyze_frame(self, frame: KeyFrame) -> str:
        """用多模态 LLM 分析单个关键帧（室内装修场景侧重描述）"""
        system_prompt = (
            "你是一个专业的室内设计画面分析师。请详细描述这个视频帧的室内装修画面，包括：\n"
            "1. 空间类型（客厅/卧室/厨房/卫生间/餐厅/书房/玄关/阳台等）\n"
            "2. 整体装修风格（如现代简约、北欧、侘寂、轻奢、日式、工业、美式等）\n"
            "3. 主色调与色彩搭配\n"
            "4. 主要材质（木材/石材/金属/布艺/微水泥/玻璃等）\n"
            "5. 家具款式与摆放\n"
            "6. 灯光设计（自然光/主灯/无主灯/氛围灯，色温冷暖）\n"
            "7. 构图与镜头（景别、角度、视角）\n"
            "8. 装饰细节（绿植、挂画、摆件等）\n\n"
            "请用中文简洁描述，150-250字。"
        )
        user_message = f"这是视频第 {frame.timestamp:.1f} 秒的关键帧（第 {frame.index} 帧），请描述室内装修画面。"

        try:
            return self.llm.chat_with_images(
                system_prompt=system_prompt,
                user_message=user_message,
                image_paths=[frame.path],
            )
        except Exception as exc:
            logger.warning("分析帧 %d 失败: %s", frame.index, exc)
            return f"[分析失败: {exc}]"

    def analyze_all_frames(self, frames: list[KeyFrame]) -> list[KeyFrame]:
        """逐帧分析所有关键帧，填充 description"""
        total = len(frames)
        for i, frame in enumerate(frames, 1):
            logger.info("分析关键帧 %d/%d (t=%.1fs)", i, total, frame.timestamp)
            frame.description = self.analyze_frame(frame)
        return frames
