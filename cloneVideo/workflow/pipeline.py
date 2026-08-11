"""室内装修视频复刻流水线 - 轻微创意变化 + 运镜还原 + 合并成片

基于完全复刻(ReplicaPipeline)的逻辑。与完全复刻的唯一差异:
  - 关键帧先做「轻微创意变化」(image-edit, 保持构图风格, 只换小细节)
  - 首帧是创意变化后的图(而非原始关键帧)

流程:
  上传视频 → 抽 N 帧 → VLM 理解运镜
  → 逐帧轻微创意变化(保留布局/风格, 只换装饰小细节)
  → 逐帧用变化图作首帧生成 4s 片段(运镜还原)
  → ffmpeg 合并成完整视频(静音)

不做风格分析/推荐, 不做帧清理(去人/字)。
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


# ── 轻微创意变化 prompt（保持构图风格, 只换小细节）──────────────────

_CREATIVE_EDIT_PROMPT = (
    "Make subtle creative changes to this photo while preserving "
    "the overall composition, style, and layout.\n\n"
    "RULES (must follow exactly):\n"
    "- Keep the EXACT same room layout, camera angle, viewpoint, wall colors, "
    "flooring, and all large furniture pieces in their original positions.\n"
    "- Keep the SAME overall design style, color palette, and lighting mood.\n"
    "- Change SMALL details to give a fresh look. You MAY swap or adjust any of:\n"
    "  * throw pillows, blankets, cushions, upholstery accents\n"
    "  * artwork, framed photos, posters, wall decorations\n"
    "  * potted plants, flower arrangements, branches in vases\n"
    "  * tabletop accessories: books, magazines, vases, candles, bowls, trays\n"
    "  * rug patterns, curtain fabrics, small textile choices\n"
    "  * clothing on visible mannequins, draped garments, or laundry\n"
    "  * plated food, tableware, cups, cutlery on a dining or kitchen surface\n"
    "  * fresh ingredients, fruit bowls, snacks, beverages on countertops\n"
    "  * small lamps, pendant shades, candle holders (not main ceiling lights)\n"
    "  * toiletries, towels, soap dispensers in bathroom scenes\n"
    "  * children's toys, storage baskets, decorative boxes\n"
    "  * seasonal decor: wreaths, garlands, holiday accents\n"
    "- Do NOT change wall colors, ceiling, flooring, windows, doors, or any "
    "built-in fixtures.\n"
    "- Do NOT move, remove, or replace any major furniture (sofa, bed, table, "
    "cabinets, chairs, appliances).\n"
    "- The result must look like the SAME room, SAME style, just with different "
    "styling accents — as if a stylist refreshed the scene.\n"
    "- No people in the result.\n\n"
    "Photorealistic, professional photography, natural lighting matching the original."
)


class CloneVideoPipeline:
    """轻微创意变化复刻 - 保持构图风格, 换小细节 → 运镜还原 → 合并成片"""

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
        """执行完整的轻微创意变化复刻流程

        Args:
            video_path: 源视频路径 (新建项目时必填)
            resume_project_id: 已有项目 ID, 提供则从未完成的步骤继续
        """
        if resume_project_id:
            project = self.project_mgr.get_project(resume_project_id)
            if not project:
                raise ValueError(f"找不到项目 {resume_project_id}")
            pid = resume_project_id
            output_dir = Path(project["output_dir"])
            console.print(f"\n[bold cyan]>> 恢复创意变化项目:[/] [yellow]{pid}[/]")
            aspect_ratio = project.get("aspect_ratio") or self.settings.default_aspect_ratio
            self._phase_generate_images(pid, output_dir, aspect_ratio)
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

        console.print(f"\n[bold cyan]>> 创意变化复刻:[/] [yellow]{video_path.name}[/]")
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
        )
        pid = project["project_id"]
        output_dir = Path(project["output_dir"])

        try:
            self._phase_extract(video_path, pid, output_dir, duration, num_shots)
            self._phase_analyze_camera(pid)
            self._phase_build_plan(pid)
            self._phase_generate_images(pid, output_dir, aspect_ratio)
            self._phase_generate_clips(pid, output_dir, aspect_ratio)
            self._phase_merge(pid, output_dir)

            self.project_mgr.update_project(pid, status="done")
            project = self.project_mgr.get_project(pid) or {}
            console.print(
                f"\n  [bold green]v 创意变化复刻完成![/] "
                f"{len(project.get('images', []))} 张变化图 + "
                f"{len(project.get('clips', []))} 个片段 → "
                f"{project.get('final_video', '')}"
            )
            return project

        except Exception as exc:
            logger.error("创意变化流程失败: %s", exc, exc_info=True)
            console.print(f"  [red]x 复刻失败: {exc}[/]")
            self.project_mgr.update_project(pid, status="failed", error=str(exc))
            raise

    # ════════════════════════════════════════════════════════════════
    # Step 1: 抽帧
    # ════════════════════════════════════════════════════════════════

    def _phase_extract(
        self,
        video_path: Path,
        pid: str,
        output_dir: Path,
        duration: float,
        num_shots: int,
    ) -> None:
        console.print(f"  [dim]Step 1/6: 均匀抽帧 (目标 {num_shots} 帧)...[/]")
        interval = duration / num_shots if num_shots > 0 else self.settings.keyframe_interval
        frames_dir = output_dir / "frames"
        frames = self.video_analyzer.extract_keyframes(video_path, frames_dir, interval=interval)
        if not frames:
            raise RuntimeError("未提取到任何关键帧")
        frames = _cap_frames(frames, num_shots)
        console.print(f"  [green]v {len(frames)} 帧已抽取[/]")

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
    # Step 2: 运镜理解（从源视频还原）
    # ════════════════════════════════════════════════════════════════

    def _phase_analyze_camera(self, pid: str) -> None:
        console.print("  [dim]Step 2/6: 理解运镜方式 (VLM 对比相邻帧)...[/]")
        project = self.project_mgr.get_project(pid) or {}
        frames = self._load_keyframes(project)

        segments = self.camera_analyzer.analyze(frames)
        if not segments:
            raise RuntimeError("运镜分析无结果")

        moves = [s.camera_move for s in segments]
        console.print(f"  [green]v 运镜还原:[/] {', '.join(moves)}")
        self._camera_segments: list[CameraSegment] = segments

    # ════════════════════════════════════════════════════════════════
    # Step 3: 构建镜头清单（轻微变化 prompt + 运镜）
    # ════════════════════════════════════════════════════════════════

    def _phase_build_plan(self, pid: str) -> None:
        console.print("  [dim]Step 3/6: 构建镜头清单 (轻微变化 + 运镜)...[/]")
        project = self.project_mgr.get_project(pid) or {}
        segments = getattr(self, "_camera_segments", [])

        shot_plan: list[dict[str, Any]] = []
        for seg in segments:
            shot_plan.append({
                "shot_id": seg.shot_id,
                "frame_path": seg.frame_path,            # 原始关键帧（变化基底）
                "base_frame_path": seg.frame_path,       # 兼容旧字段
                "clean_frame_path": seg.frame_path,      # 兼容模板
                "timestamp": seg.timestamp,
                "camera_move": seg.camera_move,
                "motion_detail_en": seg.motion_detail_en,
                "scene_desc_cn": seg.scene_desc_cn,
                "image_prompt": _CREATIVE_EDIT_PROMPT,   # 固定的轻微变化 prompt
                "video_prompt": seg.video_prompt,        # 直接用运镜 prompt（不追加风格）
                "duration": self.settings.clip_duration,
                "image_status": "pending",
                "image_path": "",
                "clip_status": "pending",
                "clip_path": "",
            })

        self.project_mgr.update_project(pid, status="planning", shot_plan=shot_plan)
        console.print(f"  [green]v {len(shot_plan)} 个镜头 (轻微变化 + 运镜已锁定)[/]")

    # ════════════════════════════════════════════════════════════════
    # Step 4: 逐帧轻微创意变化（image-edit）
    # ════════════════════════════════════════════════════════════════

    def _phase_generate_images(self, pid: str, output_dir: Path, aspect_ratio: str) -> None:
        console.print("  [dim]Step 4/6: 逐帧轻微创意变化 (保持构图, 换小细节)...[/]")
        self.project_mgr.update_project(pid, status="generating_images")
        project = self.project_mgr.get_project(pid)
        if not project:
            raise RuntimeError(f"项目丢失: {pid}")
        shot_plan = project.get("shot_plan", [])

        images_dir = output_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task("创意变化", total=len(shot_plan))
            for shot in shot_plan:
                sid = shot["shot_id"]
                progress.update(task, description=f"变化 shot_{sid:02d}/{len(shot_plan)}")
                if shot.get("image_status") == "done" and shot.get("image_path"):
                    progress.update(task, advance=1)
                    continue
                try:
                    img_path = self._generate_shot_image(shot, images_dir, aspect_ratio)
                    shot["image_path"] = img_path
                    shot["image_status"] = "done"
                except Exception as exc:
                    logger.warning("shot %d 创意变化失败: %s", sid, exc)
                    console.print(f"    [yellow]! shot_{sid:02d} 变化失败: {exc}[/]")
                    shot["image_status"] = "failed"
                self.project_mgr.update_project(pid, shot_plan=shot_plan)
                progress.update(task, advance=1)

        images = [s["image_path"] for s in shot_plan if s.get("image_status") == "done" and s.get("image_path")]
        self.project_mgr.update_project(pid, images=images)
        console.print(f"  [green]v {len(images)}/{len(shot_plan)} 张变化图完成[/]")

    # ════════════════════════════════════════════════════════════════
    # Step 5: 逐帧用变化图作首帧生成 4s 片段
    # ════════════════════════════════════════════════════════════════

    def _phase_generate_clips(self, pid: str, output_dir: Path, aspect_ratio: str) -> None:
        console.print("  [dim]Step 5/6: 生成 4s 视频片段 (变化图首帧驱动)...[/]")
        self.project_mgr.update_project(pid, status="generating_clips")
        project = self.project_mgr.get_project(pid)
        if not project:
            raise RuntimeError(f"项目丢失: {pid}")
        shot_plan = project.get("shot_plan", [])

        clips_dir = output_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task("生成片段", total=len(shot_plan))
            for shot in shot_plan:
                sid = shot["shot_id"]
                progress.update(task, description=f"片段 shot_{sid:02d}/{len(shot_plan)}")
                if shot.get("clip_status") == "done" and shot.get("clip_path"):
                    progress.update(task, advance=1)
                    continue
                if shot.get("image_status") != "done" or not shot.get("image_path"):
                    logger.warning("shot %d 变化图缺失, 跳过视频", sid)
                    shot["clip_status"] = "skipped"
                    progress.update(task, advance=1)
                    continue
                try:
                    clip_path = self._generate_shot_clip(shot, clips_dir, aspect_ratio)
                    shot["clip_path"] = clip_path
                    shot["clip_status"] = "done"
                except Exception as exc:
                    logger.warning("shot %d 视频生成失败: %s", sid, exc)
                    console.print(f"    [yellow]! shot_{sid:02d} 视频失败: {exc}[/]")
                    shot["clip_status"] = "failed"
                self.project_mgr.update_project(pid, shot_plan=shot_plan)
                progress.update(task, advance=1)

        clips = [s["clip_path"] for s in shot_plan if s.get("clip_status") == "done" and s.get("clip_path")]
        self.project_mgr.update_project(pid, clips=clips)
        console.print(f"  [green]v {len(clips)}/{len(shot_plan)} 个 4s 片段完成[/]")

    # ════════════════════════════════════════════════════════════════
    # Step 6: 合并为完整视频
    # ════════════════════════════════════════════════════════════════

    def _phase_merge(self, pid: str, output_dir: Path) -> None:
        console.print("  [dim]Step 6/6: 合并为完整视频 (静音)...[/]")
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

        final_path = output_dir / "final_clone.mp4"
        try:
            self.video_merger.merge_clips(clip_paths, final_path)
        except Exception as exc:
            raise RuntimeError(f"视频合并失败: {exc}") from exc

        self.project_mgr.update_project(pid, final_video=str(final_path))
        console.print(f"  [green]v 完整视频已生成: {final_path.name}[/]")

    # ════════════════════════════════════════════════════════════════
    # 子步骤
    # ════════════════════════════════════════════════════════════════

    def _load_keyframes(self, project: dict[str, Any]) -> list[KeyFrame]:
        frames: list[KeyFrame] = []
        for i, kf in enumerate(project.get("keyframes", [])):
            frames.append(KeyFrame(
                timestamp=kf.get("timestamp", 0.0),
                path=kf.get("path", ""),
                index=kf.get("index", i + 1),
            ))
        return frames

    def _generate_shot_image(
        self,
        shot: dict[str, Any],
        images_dir: Path,
        aspect_ratio: str,
    ) -> str:
        """轻微创意变化：pic=原始关键帧, prompt=保持构图换小细节"""
        sid = shot["shot_id"]
        prompt = shot.get("image_prompt", "")
        if not prompt:
            raise RuntimeError(f"shot {sid} 缺少 image_prompt")

        base_local = (
            shot.get("frame_path")
            or shot.get("clean_frame_path")
            or shot.get("base_frame_path", "")
        )
        if not base_local or not Path(base_local).exists():
            raise RuntimeError(f"shot {sid} 基底帧不存在: {base_local}")

        try:
            base_url = self.cos.upload_file(base_local)
        except Exception as exc:
            raise RuntimeError(f"shot {sid} 基底帧上传失败: {exc}") from exc

        console.print(f"    [cyan]创意变化 shot_{sid:02d}[/] | 基底: {Path(base_local).name}")
        logger.info("创意变化 shot_%02d", sid)

        api_task_id = self.media_api.generate_image(
            prompt=prompt,
            model=self.settings.default_image_model,
            size=self.settings.default_size,
            aspect_ratio=aspect_ratio,
            pic=base_url,
        )
        result = self.media_api.poll_image(api_task_id)
        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"创意变化失败: {result}")

        img_path = images_dir / f"shot_{sid:02d}.png"
        self.media_api.download_media(result["url"], img_path)
        return str(img_path)

    def _generate_shot_clip(
        self,
        shot: dict[str, Any],
        clips_dir: Path,
        aspect_ratio: str,
    ) -> str:
        """变化图作首帧生成 4s：pic=变化图, prompt=运镜"""
        sid = shot["shot_id"]
        prompt = shot.get("video_prompt", "")
        if not prompt:
            raise RuntimeError(f"shot {sid} 缺少 video_prompt")

        img_local = shot.get("image_path", "")
        if not img_local or not Path(img_local).exists():
            raise RuntimeError(f"shot {sid} 变化图不存在: {img_local}")

        try:
            pic_url = self.cos.upload_file(img_local)
        except Exception as exc:
            raise RuntimeError(f"shot {sid} 变化图上传失败: {exc}") from exc

        console.print(
            f"    [cyan]生成视频 shot_{sid:02d}[/] | "
            f"运镜: {shot.get('camera_move', '?')} | 4s"
        )
        logger.info("生成视频 shot_%02d | prompt=\n%s", sid, prompt)

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
            raise RuntimeError(f"视频生成失败: {result}")

        clip_path = clips_dir / f"shot_{sid:02d}.mp4"
        self.media_api.download_media(result["url"], clip_path)
        return str(clip_path)

    def get_status(self) -> dict[str, Any]:
        return self.project_mgr.get_stats()
