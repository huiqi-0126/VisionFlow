"""完全复刻流水线 - 关键帧作首帧 → 理解运镜 → 4s 片段 → 合并成片

流程（收敛、高还原）:
  上传视频 → 按时长推导镜头数 N(8~15) → 均匀抽 N 帧（作首帧）
  → VLM 理解每段运镜（对比相邻帧）
  → 逐帧: 关键帧直接上传作 pic 首帧 → AI 生成 4s 片段（运镜还原）
  → ffmpeg concat 合并 N 个 4s → 完整视频（静音）

与 CloneVideoPipeline（室内复刻）的差异:
  - 不做风格分析/推荐（保留原片风格）
  - 不做帧清理（关键帧原样使用作首帧）
  - 不做风格改造图片生成（首帧即关键帧本身）
  - 运镜从源视频【理解】还原，不从注册表轮询分配
  - 最终输出合并的完整视频，不是独立素材

设计目标: 输出与上传视频「基本一样」的完整视频。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TimeElapsedColumn,
)

from config import Settings, get_settings
from core.camera_analyzer import CameraAnalyzer, CameraSegment
from core.cos_client import COSClient
from core.llm_client import LLMClient
from core.media_api import MediaAPIClient
from core.project_manager import ProjectManager
from core.shot_planner import calc_num_shots
from core.video_analyzer import VideoAnalyzer, KeyFrame, aspect_ratio_from_resolution
from core.video_merger import VideoMerger

logger = logging.getLogger(__name__)
console = Console()


def _cap_frames(frames: list[KeyFrame], n: int) -> list[KeyFrame]:
    """均匀截取最多 n 帧（保留首尾，覆盖全视频）"""
    if len(frames) <= n or n < 2:
        return frames
    step = (len(frames) - 1) / (n - 1)
    out: list[KeyFrame] = []
    seen: set[int] = set()
    for i in range(n):
        idx = round(i * step)
        if idx not in seen and 0 <= idx < len(frames):
            seen.add(idx)
            out.append(frames[idx])
    return out or frames[:n]


class ReplicaPipeline:
    """完全复刻流水线 - 关键帧首帧驱动 + 运镜还原 + 合并成片"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        s = self.settings

        self.llm = LLMClient(
            api_key=s.al_api_key or s.qwen_api_key,
            base_url=s.al_baseurl or s.qwen_baseurl,
            model=s.al_model or s.qwen_model,
        )
        self.media_api = MediaAPIClient(
            api_key=s.gkapi_key,
            base_url=s.gkapi_baseurl,
            poll_interval=s.poll_interval,
            max_poll_attempts=s.max_poll_attempts,
        )
        self.cos = COSClient(
            secret_id=s.secret_id,
            secret_key=s.secret_key,
            region=s.region,
            bucket=s.bucket,
            base_url=s.cos_url,
        )
        self.project_mgr = ProjectManager(s.data_dir, s.projects_dir)
        self.video_analyzer = VideoAnalyzer(self.llm, s.keyframe_interval, s.ffmpeg_path)
        self.camera_analyzer = CameraAnalyzer(self.llm, clip_duration=s.clip_duration)
        self.video_merger = VideoMerger(ffmpeg_path=s.ffmpeg_path)

    def run(
        self,
        video_path: str | Path | None = None,
        resume_project_id: str | None = None,
    ) -> dict[str, Any]:
        """执行完整的视频复刻流程

        Args:
            video_path: 源视频路径 (新建项目时必填)
            resume_project_id: 已有项目 ID, 提供则跳过前 2 步, 直接从生成片段开始

        Returns:
            项目状态字典, 含 status / project_id / final_video / clips 等
        """
        if resume_project_id:
            project = self.project_mgr.get_project(resume_project_id)
            if not project:
                raise ValueError(f"找不到项目 {resume_project_id}")
            pid = resume_project_id
            output_dir = Path(project["output_dir"])
            console.print(f"\n[bold cyan]>> 恢复完全复刻项目:[/] [yellow]{pid}[/]")
            aspect_ratio = project.get("aspect_ratio") or self.settings.default_aspect_ratio
            self._phase_generate_clips(pid, output_dir, aspect_ratio)
            self._phase_merge(pid, output_dir)
            self.project_mgr.update_project(pid, status="done")
            return self.project_mgr.get_project(pid) or project

        # ── 新建 ──
        if not video_path:
            raise ValueError("新建项目必须提供源视频路径")
        video_path = Path(video_path)
        if not video_path.exists():
            raise FileNotFoundError(f"视频文件不存在: {video_path}")

        console.print(f"\n[bold cyan]>> 完全复刻:[/] [yellow]{video_path.name}[/]")
        width, height = self.video_analyzer.get_video_resolution(video_path)
        aspect_ratio = aspect_ratio_from_resolution(width, height)
        duration = self.video_analyzer.get_video_duration(video_path)
        num_shots = calc_num_shots(duration, self.settings.min_shots, self.settings.max_shots)
        console.print(
            f"  源视频: {width}x{height} → {aspect_ratio} | 时长 {duration:.1f}s "
            f"→ 镜头数 {num_shots} (范围 {self.settings.min_shots}~{self.settings.max_shots})"
        )

        project = self.project_mgr.create_project(
            source_video=str(video_path),
            source_resolution=(width, height),
            aspect_ratio=aspect_ratio,
            mode="replica",
        )
        pid = project["project_id"]
        output_dir = Path(project["output_dir"])

        try:
            self._phase_extract(video_path, pid, output_dir, duration, num_shots)
            self._phase_analyze_camera(pid)
            self._phase_generate_clips(pid, output_dir, aspect_ratio)
            self._phase_merge(pid, output_dir)

            self.project_mgr.update_project(pid, status="done")
            project = self.project_mgr.get_project(pid) or {}
            console.print(
                f"\n  [bold green]v 完全复刻完成![/] "
                f"{len(project.get('clips', []))} 个片段已合并 → "
                f"{project.get('final_video', '')}"
            )
            return project

        except Exception as exc:
            logger.error("完全复刻流程失败: %s", exc, exc_info=True)
            console.print(f"  [red]x 复刻失败: {exc}[/]")
            self.project_mgr.update_project(pid, status="failed", error=str(exc))
            raise

    # ════════════════════════════════════════════════════════════════
    # Step 1: 抽帧（关键帧作首帧）
    # ════════════════════════════════════════════════════════════════

    def _phase_extract(
        self,
        video_path: Path,
        pid: str,
        output_dir: Path,
        duration: float,
        num_shots: int,
    ) -> None:
        console.print(f"  [dim]Step 1/4: 均匀抽帧作首帧 (目标 {num_shots} 帧)...[/]")
        interval = duration / num_shots if num_shots > 0 else self.settings.keyframe_interval
        frames_dir = output_dir / "frames"
        frames = self.video_analyzer.extract_keyframes(video_path, frames_dir, interval=interval)
        if not frames:
            raise RuntimeError("未提取到任何关键帧")
        frames = _cap_frames(frames, num_shots)
        console.print(f"  [green]v {len(frames)} 帧已抽取 (将作首帧)[/]")

        self.project_mgr.update_project(
            pid,
            status="analyzing",
            video_duration=duration,
            keyframes=[
                {"timestamp": f.timestamp, "path": f.path, "index": f.index}
                for f in frames
            ],
        )

    # ════════════════════════════════════════════════════════════════
    # Step 2: 运镜理解
    # ════════════════════════════════════════════════════════════════

    def _phase_analyze_camera(self, pid: str) -> None:
        console.print("  [dim]Step 2/4: 理解运镜方式 (VLM 对比相邻帧)...[/]")
        project = self.project_mgr.get_project(pid) or {}
        frames = self._load_keyframes(project)

        segments = self.camera_analyzer.analyze(frames)
        if not segments:
            raise RuntimeError("运镜分析无结果")

        # 展示运镜分布
        moves = [s.camera_move for s in segments]
        console.print(f"  [green]v 运镜还原:[/] {', '.join(moves)}")

        shot_plan = [
            {
                "shot_id": s.shot_id,
                "frame_path": s.frame_path,
                "timestamp": s.timestamp,
                "camera_move": s.camera_move,
                "motion_detail_en": s.motion_detail_en,
                "scene_desc_cn": s.scene_desc_cn,
                "video_prompt": s.video_prompt,
                "duration": self.settings.clip_duration,
                "clip_status": "pending",
                "clip_path": "",
            }
            for s in segments
        ]
        self.project_mgr.update_project(pid, status="planning", shot_plan=shot_plan)

    # ════════════════════════════════════════════════════════════════
    # Step 3: 逐帧生成 4s 片段（关键帧直接作首帧 pic）
    # ════════════════════════════════════════════════════════════════

    def _phase_generate_clips(
        self,
        pid: str,
        output_dir: Path,
        aspect_ratio: str,
    ) -> None:
        console.print("  [dim]Step 3/4: 逐帧生成 4s 片段 (首帧驱动)...[/]")
        self.project_mgr.update_project(pid, status="generating_clips")
        project = self.project_mgr.get_project(pid)
        if not project:
            raise RuntimeError(f"项目丢失: {pid}")
        shot_plan = project.get("shot_plan", [])

        clips_dir = output_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("生成片段", total=len(shot_plan))
            for shot in shot_plan:
                sid = shot["shot_id"]
                progress.update(task, description=f"片段 shot_{sid:02d}/{len(shot_plan)}")
                if shot.get("clip_status") == "done" and shot.get("clip_path"):
                    progress.update(task, advance=1)
                    continue
                try:
                    clip_path = self._generate_segment_clip(shot, clips_dir, aspect_ratio)
                    shot["clip_path"] = clip_path
                    shot["clip_status"] = "done"
                except Exception as exc:
                    logger.warning("shot %d 片段生成失败: %s", sid, exc)
                    console.print(f"    [yellow]! shot_{sid:02d} 片段失败: {exc}[/]")
                    shot["clip_status"] = "failed"
                self.project_mgr.update_project(pid, shot_plan=shot_plan)
                progress.update(task, advance=1)

        clips = [
            s["clip_path"]
            for s in shot_plan
            if s.get("clip_status") == "done" and s.get("clip_path")
        ]
        self.project_mgr.update_project(pid, clips=clips)
        console.print(f"  [green]v {len(clips)}/{len(shot_plan)} 个 4s 片段完成[/]")

    # ════════════════════════════════════════════════════════════════
    # Step 4: 合并成完整视频
    # ════════════════════════════════════════════════════════════════

    def _phase_merge(self, pid: str, output_dir: Path) -> None:
        console.print("  [dim]Step 4/4: 合并为完整视频 (静音)...[/]")
        self.project_mgr.update_project(pid, status="merging")
        project = self.project_mgr.get_project(pid)
        if not project:
            raise RuntimeError(f"项目丢失: {pid}")

        shot_plan = project.get("shot_plan", [])
        clip_paths = [
            s["clip_path"]
            for s in shot_plan
            if s.get("clip_status") == "done" and s.get("clip_path")
        ]
        if not clip_paths:
            raise RuntimeError("无可用片段可合并")

        final_path = output_dir / "final_replica.mp4"
        try:
            self.video_merger.merge_clips(clip_paths, final_path)
        except Exception as exc:
            raise RuntimeError(f"视频合并失败: {exc}") from exc

        self.project_mgr.update_project(
            pid,
            final_video=str(final_path),
        )
        console.print(f"  [green]v 完整视频已生成: {final_path.name}[/]")

    # ════════════════════════════════════════════════════════════════
    # 子步骤
    # ════════════════════════════════════════════════════════════════

    def _load_keyframes(self, project: dict[str, Any]) -> list[KeyFrame]:
        """从 project 的 keyframes 字段重建 KeyFrame 列表"""
        frames: list[KeyFrame] = []
        for i, kf in enumerate(project.get("keyframes", [])):
            frames.append(KeyFrame(
                timestamp=kf.get("timestamp", 0.0),
                path=kf.get("path", ""),
                index=kf.get("index", i + 1),
            ))
        return frames

    def _generate_segment_clip(
        self,
        shot: dict[str, Any],
        clips_dir: Path,
        aspect_ratio: str,
    ) -> str:
        """关键帧作首帧生成 4s 片段：pic=关键帧原图, prompt=运镜"""
        sid = shot["shot_id"]
        prompt = shot.get("video_prompt", "")
        if not prompt:
            raise RuntimeError(f"shot {sid} 缺少 video_prompt")

        frame_local = shot.get("frame_path", "")
        if not frame_local or not Path(frame_local).exists():
            raise RuntimeError(f"shot {sid} 首帧不存在: {frame_local}")

        # 关键帧原样上传作首帧 pic（不清理、不改风格）
        try:
            pic_url = self.cos.upload_file(frame_local)
        except Exception as exc:
            raise RuntimeError(f"shot {sid} 首帧上传失败: {exc}") from exc

        console.print(
            f"    [cyan]生成片段 shot_{sid:02d}[/] | "
            f"首帧: {Path(frame_local).name} | 运镜: {shot.get('camera_move', '?')} | 4s"
        )
        logger.info("生成片段 shot_%02d | prompt=\n%s", sid, prompt)

        api_task_id = self.media_api.generate_video(
            prompt=prompt,
            model=self.settings.default_video_model,
            size=self.settings.default_size,
            duration=str(self.settings.clip_duration),
            aspect_ratio=aspect_ratio,
            pic=pic_url,
            video_type="0",
        )
        result = self.media_api.poll_video(api_task_id)
        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"片段生成失败: {result}")

        clip_path = clips_dir / f"shot_{sid:02d}.mp4"
        self.media_api.download_media(result["url"], clip_path)
        return str(clip_path)

    def get_status(self) -> dict[str, Any]:
        return self.project_mgr.get_stats()
