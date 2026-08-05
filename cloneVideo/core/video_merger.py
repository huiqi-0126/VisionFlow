"""视频合并器 - ffmpeg concat 把多个 4s 片段拼成完整视频（静音）

完全复刻模式专用：把逐帧生成的 4s 片段按顺序拼接成一条完整视频。
输出固定静音（纯画面），H.264 + faststart。

策略：
  1. 优先 concat demuxer (-c copy) — 同源片段通常编码一致，无损且秒级完成
  2. 失败则回退 concat filter (重编码归一化) — 兜底不同分辨率/编码的情况
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class VideoMerger:
    """把多个短视频片段合并为一条完整视频（静音输出）"""

    def __init__(self, ffmpeg_path: str = "ffmpeg") -> None:
        self.ffmpeg_path = ffmpeg_path
        # ffprobe 与 ffmpeg 同目录，自动推导
        if ffmpeg_path and ffmpeg_path != "ffmpeg":
            self.ffprobe_path = str(
                Path(ffmpeg_path).parent
                / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
            )
        else:
            self.ffprobe_path = "ffprobe"

    # ── 对外 ──────────────────────────────────────────────────

    def merge_clips(
        self,
        clip_paths: list[str | Path],
        output_path: str | Path,
    ) -> str:
        """合并片段为完整视频

        Args:
            clip_paths: 按播放顺序排列的片段路径列表
            output_path: 输出 MP4 路径

        Returns:
            输出文件路径字符串

        Raises:
            RuntimeError: 合并失败
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 过滤不存在的片段
        valid = [str(Path(p)) for p in clip_paths if p and Path(p).exists()]
        if not valid:
            raise RuntimeError("没有可合并的视频片段")
        if len(valid) == 1:
            # 单片段：直接用 ffmpeg 转封装为静音 MP4
            logger.info("仅 1 个片段，直接转为静音输出")
            return self._strip_audio_to_mp4(valid[0], output_path)

        logger.info("合并 %d 个片段 → %s", len(valid), output_path.name)

        # 策略 1: concat demuxer（快，无损）
        try:
            self._merge_with_demuxer(valid, output_path)
            logger.info("concat demuxer 合并成功: %s", output_path)
            return str(output_path)
        except Exception as exc:
            logger.warning("concat demuxer 失败 (%s)，回退到 concat filter 重编码", exc)

        # 策略 2: concat filter（重编码，归一化）
        self._merge_with_filter(valid, output_path)
        logger.info("concat filter 合并成功: %s", output_path)
        return str(output_path)

    # ── 策略 1: concat demuxer ────────────────────────────────

    def _merge_with_demuxer(self, clips: list[str], output_path: Path) -> None:
        """用 concat demuxer 无损拼接（要求编码一致）"""
        # 写 filelist
        fd, list_path = tempfile.mkstemp(suffix=".txt", prefix="concat_list_")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                for clip in clips:
                    # concat demuxer 要求路径转义：单引号包裹，内部单引号翻倍
                    safe = clip.replace("'", r"'\''")
                    f.write(f"file '{safe}'\n")

            cmd = [
                self.ffmpeg_path, "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", list_path,
                "-c", "copy",
                "-an",                       # 丢弃所有音频
                "-movflags", "+faststart",
                str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise RuntimeError(
                    f"concat demuxer 失败 (rc={result.returncode}): "
                    f"{result.stderr[-500:]}"
                )
            if not output_path.exists() or output_path.stat().st_size == 0:
                raise RuntimeError("concat demuxer 输出为空")
        finally:
            try:
                Path(list_path).unlink(missing_ok=True)
            except OSError:
                pass

    # ── 策略 2: concat filter 重编码 ──────────────────────────

    def _merge_with_filter(self, clips: list[str], output_path: Path) -> None:
        """用 concat filter 重编码拼接（归一化分辨率/编码，兜底方案）"""
        width, height, fps = self._probe_uniform_format(clips[0])
        target_w = width if width > 0 else 1080
        target_h = height if height > 0 else 1920
        target_fps = fps if fps > 0 else 30

        inputs: list[str] = []
        filters: list[str] = []
        concat_nodes: list[str] = []
        for idx, _ in enumerate(clips):
            inputs.extend(["-i", clips[idx]])
            filters.append(
                f"[{idx}:v]fps={target_fps},"
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h},setsar=1[v{idx}]"
            )
            concat_nodes.append(f"[v{idx}]")

        n = len(clips)
        filters.append(
            "".join(concat_nodes) + f"concat=n={n}:v=1:a=0[vconcat]"
        )
        filter_complex = ";".join(filters)

        cmd = [
            self.ffmpeg_path, "-y",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[vconcat]",
            "-an",                               # 无音频
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(
                f"concat filter 失败 (rc={result.returncode}): {result.stderr[-800:]}"
            )
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("concat filter 输出为空")

    # ── 单片段静音化 ─────────────────────────────────────────

    def _strip_audio_to_mp4(self, clip: str, output_path: Path) -> str:
        """单片段直接转封装为静音 MP4（-c copy -an）"""
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", clip,
            "-c", "copy",
            "-an",
            "-movflags", "+faststart",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            # copy 可能因编码不支持而失败，回退重编码
            logger.warning("copy 转封装失败，回退重编码: %s", result.stderr[-300:])
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", clip,
                "-an",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "18",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise RuntimeError(
                    f"单片段静音化失败: {result.stderr[-500:]}"
                )
        return str(output_path)

    # ── ffprobe ──────────────────────────────────────────────

    def _probe_uniform_format(self, video_path: str) -> tuple[int, int, int]:
        """探测视频的 width / height / fps，用于 concat filter 归一化"""
        cmd = [
            self.ffprobe_path, "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "csv=s=x:p=0",
            str(video_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                parts = result.stdout.strip().split("x")
                if len(parts) >= 2:
                    w = int(parts[0])
                    # height 和 fps 用 'x' 分隔的格式：WxHxFPS/N 或 W H FPS
                    # csv=s=x 输出形如 1920x1080x30/1
                    rest = parts[1].split("x") if "x" in parts[1] else [parts[1]]
                    h = int(rest[0])
                    fps = 30
                    if len(rest) > 1 and "/" in rest[1]:
                        num, den = rest[1].split("/")
                        fps = int(float(num) / float(den)) if float(den) != 0 else 30
                    elif len(rest) > 1:
                        fps = int(float(rest[1]))
                    return w, h, fps
        except Exception as exc:
            logger.warning("探测视频格式失败 %s: %s", video_path, exc)
        return 0, 0, 0
