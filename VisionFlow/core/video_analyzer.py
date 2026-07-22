"""视频解析器 - 关键帧提取 + 多模态 LLM 画面分析"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.llm_client import LLMClient

logger = logging.getLogger(__name__)


@dataclass
class KeyFrame:
    """关键帧"""
    timestamp: float          # 秒
    path: str                 # 图片路径
    index: int                # 序号
    description: str = ""     # LLM 生成的画面描述


class VideoAnalyzer:
    """视频解析：提取关键帧 + 多模态 LLM 分析"""

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

    # ── 关键帧提取 ────────────────────────────────────────────

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

    def extract_keyframes(
        self,
        video_path: str | Path,
        output_dir: str | Path,
    ) -> list[KeyFrame]:
        """从视频中按固定间隔提取关键帧

        使用 ffmpeg: ffmpeg -i input.mp4 -vf fps=1/N -q:v 2 output/frame_%04d.jpg
        """
        video_path = Path(video_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        fps_filter = f"fps=1/{self.keyframe_interval}"
        output_pattern = str(output_dir / "frame_%04d.jpg")

        cmd = [
            self.ffmpeg_path, "-y",
            "-i", str(video_path),
            "-vf", fps_filter,
            "-q:v", "2",
            output_pattern,
        ]

        logger.info("提取关键帧: %s (间隔 %ds)", video_path.name, self.keyframe_interval)
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
            # 从文件名提取序号
            try:
                idx = int(img_file.stem.split("_")[1])
            except (IndexError, ValueError):
                idx = len(frames) + 1
            timestamp = (idx - 1) * self.keyframe_interval
            frames.append(KeyFrame(
                timestamp=timestamp,
                path=str(img_file),
                index=idx,
            ))

        logger.info("提取了 %d 个关键帧", len(frames))
        return frames

    def extract_last_frame(self, video_path: str | Path, output_path: str | Path) -> str:
        """提取视频的最后一帧并保存到指定路径"""
        video_path = Path(video_path)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            self.ffmpeg_path, "-y",
            "-sseof", "-3", # Look at the last 3 seconds
            "-i", str(video_path),
            "-update", "1",
            "-q:v", "2",
            str(output_path)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.error("提取最后一帧失败: %s", result.stderr)
                raise RuntimeError("ffmpeg 提取最后一帧失败")
        except Exception as exc:
            logger.error("提取最后一帧异常: %s", exc)
            raise
            
        return str(output_path)

    # ── 多模态 LLM 分析 ────────────────────────────────────────

    def analyze_frame(self, frame: KeyFrame) -> str:
        """用多模态 LLM 分析单个关键帧"""
        system_prompt = (
            "你是一个专业的视频内容分析师。请详细描述这个视频帧的画面内容，包括：\n"
            "1. 场景/环境（室内/室外、地点类型）\n"
            "2. 主体/人物（数量、动作、表情、穿着）\n"
            "3. 构图/镜头（景别、角度、运动方向）\n"
            "4. 光线/色彩（色调、光线类型）\n"
            "5. 文字/字幕（如有）\n"
            "6. 整体氛围/情绪\n\n"
            "请用中文简洁描述，100-200字。"
        )
        user_message = f"这是视频第 {frame.timestamp:.1f} 秒的关键帧（第 {frame.index} 帧），请描述画面内容。"

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

    def merge_clips(
        self,
        clips: list[str],
        output_path: str | Path,
        transition: str = "fade",
        transition_duration: float = 0.5,
    ) -> str:
        """用 ffmpeg 将多个视频片段合并为一个，片段之间加转场动画

        流程：先统一每个片段的分辨率/fps/格式 → 再用 xfade 转场合并。
        这样可以避免因源视频参数不一致导致 xfade 失败。

        Args:
            clips: 视频片段文件路径列表（按顺序）
            output_path: 合并后的输出路径
            transition: 转场类型（fade, fadeblack, fadewhite, slideleft, slideright,
                        slideup, slidedown, circlecrop, circleopen, dissolve 等）
            transition_duration: 转场持续时间（秒）

        Returns:
            合并后视频的本地路径
        """
        if not clips:
            raise ValueError("没有视频片段可合并")
        if len(clips) == 1:
            # 单个视频直接复制
            import shutil
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(clips[0], str(output_path))
            logger.info("单个片段，直接复制: %s", output_path)
            return str(output_path)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # ── Step 1: 统一每个片段的分辨率/fps/格式 ──
        # 取第一个视频的分辨率作为基准
        target_w, target_h = self._get_clip_resolution(clips[0])
        # 如果获取失败，用默认值
        if target_w <= 0 or target_h <= 0:
            target_w, target_h = 1080, 1920  # 竖屏 9:16

        normalized: list[str] = []
        for i, clip in enumerate(clips):
            norm_path = str(output_path.parent / f"_norm_{i}.mp4")
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", clip,
                "-vf", f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p",
                "-r", "30",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-an",  # 去掉音频（生成的视频没有音轨）
                norm_path,
            ]
            logger.info("标准化片段 %d: %s → %dx%d", i, Path(clip).name, target_w, target_h)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                logger.error("标准化失败 (clip %d): %s", i, result.stderr[-300:])
                raise RuntimeError(f"片段标准化失败 (clip {i}): {result.stderr[-200:]}")
            normalized.append(norm_path)

        # ── Step 2: 获取标准化后的时长 ──
        durations = []
        for clip in normalized:
            try:
                dur = self._get_clip_duration(clip)
                durations.append(dur)
            except Exception as exc:
                logger.warning("无法获取片段时长 %s: %s", clip, exc)
                durations.append(15.0)

        logger.info("合并 %d 个片段 (转场: %s, 时长: %.1fs)", len(normalized), transition, transition_duration)

        # ── Step 3: xfade 转场合并 ──
        if len(normalized) == 2:
            offset = durations[0] - transition_duration
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", normalized[0],
                "-i", normalized[1],
                "-filter_complex",
                f"[0:v][1:v]xfade=transition={transition}:duration={transition_duration}:offset={offset:.3f}[v]",
                "-map", "[v]",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                str(output_path),
            ]
        else:
            # 链式 xfade
            filter_parts = []
            cum_offset = durations[0] - transition_duration
            prev_label = "0"

            for i in range(1, len(normalized)):
                out_label = f"v{i-1}" if i < len(normalized) - 1 else "v"
                filter_parts.append(
                    f"[{prev_label}:v][{i}:v]xfade=transition={transition}:"
                    f"duration={transition_duration}:offset={cum_offset:.3f}[{out_label}]"
                )
                prev_label = out_label
                cum_offset += durations[i] - transition_duration

            filter_complex = ";".join(filter_parts)

            inputs = []
            for clip in normalized:
                inputs.extend(["-i", clip])

            cmd = [
                self.ffmpeg_path, "-y",
                *inputs,
                "-filter_complex", filter_complex,
                "-map", "[v]",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                str(output_path),
            ]

        logger.info("ffmpeg xfade 命令: %s", " ".join(cmd[:8]) + "...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                # 提取 stderr 中的实际错误行（跳过版本/配置信息）
                err_lines = [l for l in result.stderr.splitlines() if l.strip() and not l.startswith(("ffmpeg version", "built with", "configuration:", "  "))]
                err_summary = "\n".join(err_lines[-10:]) if err_lines else result.stderr[-300:]
                logger.error("ffmpeg merge stderr (tail):\n%s", err_summary)
                # 尝试 concat demuxer 作为 fallback
                logger.info("xfade 失败，尝试 concat demuxer fallback...")
                return self._merge_concat_fallback(normalized, output_path)
        except FileNotFoundError:
            raise RuntimeError("ffmpeg 未安装或不在 PATH 中")
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffmpeg 合并视频超时")

        # 清理临时标准化文件
        for tmp in normalized:
            try:
                Path(tmp).unlink()
            except Exception:
                pass

        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info("合并完成: %.2f MB → %s", size_mb, output_path)
        return str(output_path)

    def _merge_concat_fallback(
        self,
        normalized_clips: list[str],
        output_path: Path,
    ) -> str:
        """concat demuxer fallback：不做转场，直接拼接已标准化的视频片段"""
        import tempfile

        # 写 concat list 文件
        list_content = "\n".join(f"file '{c.replace(chr(92), '/')}'" for c in normalized_clips)
        list_file = output_path.parent / "_concat_list.txt"
        list_file.write_text(list_content, encoding="utf-8")

        cmd = [
            self.ffmpeg_path, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]

        logger.info("concat fallback 命令: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                err_lines = [l for l in result.stderr.splitlines() if l.strip() and not l.startswith(("ffmpeg version", "built with", "configuration:", "  "))]
                err_summary = "\n".join(err_lines[-5:]) if err_lines else result.stderr[-200:]
                raise RuntimeError(f"concat fallback 也失败: {err_summary}")
        finally:
            try:
                list_file.unlink()
            except Exception:
                pass

        # 清理临时标准化文件
        for tmp in normalized_clips:
            try:
                Path(tmp).unlink()
            except Exception:
                pass

        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info("concat 合并完成（无转场）: %.2f MB → %s", size_mb, output_path)
        return str(output_path)

    def _get_clip_duration(self, clip_path: str) -> float:
        """获取单个视频片段的时长"""
        cmd = [
            self.ffprobe_path, "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            clip_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        result.check_returncode()
        return float(result.stdout.strip())

    def _get_clip_resolution(self, clip_path: str) -> tuple[int, int]:
        """获取视频的宽高，返回 (width, height)"""
        cmd = [
            self.ffprobe_path, "-v", "quiet",
            "-show_entries", "stream=width,height",
            "-select_streams", "v:0",
            "-of", "csv=s=x:p=0",
            clip_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            result.check_returncode()
            parts = result.stdout.strip().split("x")
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        except Exception as exc:
            logger.warning("获取分辨率失败 %s: %s", clip_path, exc)
        return 0, 0

    def get_video_overview(self, frames: list[KeyFrame]) -> str:
        """用多模态 LLM 批量分析帧，生成视频整体概述

        每次最多发送 8 张帧给 LLM，合并多批次结果。
        """
        batch_size = 8
        overviews: list[str] = []

        for start in range(0, len(frames), batch_size):
            batch = frames[start:start + batch_size]
            if not batch:
                break

            system_prompt = (
                "你是一个专业的视频内容分析师。以下是一个视频的一组关键帧截图。"
                "请分析这组帧画面，总结这段视频的内容、风格、节奏和叙事结构。\n"
                "包括：\n"
                "- 视频主题和内容类型\n"
                "- 画面风格和视觉特征\n"
                "- 场景变化和叙事结构\n"
                "- 节奏和情感基调\n\n"
                "请用中文描述，200字以内。"
            )
            time_range = f"第 {batch[0].timestamp:.1f}s ~ {batch[-1].timestamp:.1f}s"
            user_message = f"这是视频 {time_range} 的关键帧截图，请总结这段视频的内容。"

            try:
                overview = self.llm.chat_with_images(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    image_paths=[f.path for f in batch],
                )
                overviews.append(overview)
            except Exception as exc:
                logger.warning("视频概述生成失败 (batch %d): %s", start // batch_size, exc)

        if len(overviews) <= 1:
            return overviews[0] if overviews else ""

        # 多批次合并
        system_prompt = "请将以下多段视频概述合并为一段完整的视频内容总结，200字以内。"
        user_message = "\n\n".join(f"段落 {i+1}:\n{o}" for i, o in enumerate(overviews))
        try:
            return self.llm.chat(system_prompt, user_message)
        except Exception as exc:
            logger.warning("概述合并失败: %s", exc)
            return "\n".join(overviews)
