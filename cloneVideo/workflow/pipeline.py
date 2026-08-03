"""室内装修视频复刻流水线 - 核心编排器（真实帧风格统一改造）

新流程（收敛、真实）:
  上传视频 → 按时长推导镜头数 N(8~15) → 均匀抽 N 帧
  → LLM 分析帧画面 → 检测源视频画幅
  → AI 推荐统一目标风格 → 清理帧(去人/字, 保留布局家具)
  → 逐帧 image-edit(保持布局家具, 只改风格) → 统一风格图
  → 逐图生成 4s 视频(单图驱动+运镜)
  → 输出独立素材(images[] + clips[])

与旧版的差异:
  - 基底是【真实视频截图】(去人/字), 不是 LLM 想象的房间 → 收敛、真实
  - 生成是【风格改造】(image-edit, 保持布局家具), 不是 text-to-image 凭空生成 → 不发散
  - 所有图统一成 AI 推荐的同一个目标风格 → 风格一致
  - 画幅跟随源视频; 镜头数按时长推导(8~15)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

from config import Settings, get_settings
from core.cos_client import COSClient
from core.frame_cleaner import FrameCleaner
from core.llm_client import LLMClient
from core.media_api import MediaAPIClient
from core.project_manager import ProjectManager
from core.shot_planner import ShotPlanner, calc_num_shots
from core.style_analyzer import StyleAnalyzer
from core.video_analyzer import VideoAnalyzer, KeyFrame, aspect_ratio_from_resolution

logger = logging.getLogger(__name__)
console = Console()


def _cap_frames(frames: list[KeyFrame], n: int) -> list[KeyFrame]:
    """均匀截取最多 n 帧（保留首尾，覆盖全视频）"""
    if len(frames) <= n or n < 2:
        return frames
    step = (len(frames) - 1) / (n - 1)
    out: list[KeyFrame] = []
    seen = set()
    for i in range(n):
        idx = round(i * step)
        if idx not in seen and 0 <= idx < len(frames):
            seen.add(idx)
            out.append(frames[idx])
    return out or frames[:n]


class CloneVideoPipeline:
    """室内装修视频复刻全流程编排器（真实帧风格统一改造）"""

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
        self.style_analyzer = StyleAnalyzer(self.llm)
        self.frame_cleaner = FrameCleaner(
            self.media_api, self.cos,
            image_model=s.default_image_model,
            size=s.default_size,
        )
        self.shot_planner = ShotPlanner(clip_duration=s.clip_duration)

    def run(
        self,
        video_path: str | Path | None = None,
        resume_project_id: str | None = None,
    ) -> dict[str, Any]:
        """执行完整的室内装修视频复刻流程

        Args:
            video_path: 源视频路径 (新建项目时必填)
            resume_project_id: 已有项目 ID, 提供则跳过前 6 步, 直接从生成素材开始
        """
        if resume_project_id:
            project = self.project_mgr.get_project(resume_project_id)
            if not project:
                raise ValueError(f"找不到项目 {resume_project_id}")
            pid = resume_project_id
            output_dir = Path(project["output_dir"])
            console.print(f"\n[bold cyan]>> 恢复室内复刻项目:[/] [yellow]{pid}[/]")
            aspect_ratio = project.get("aspect_ratio") or self.settings.default_aspect_ratio
            self._phase_generate_images(pid, output_dir, aspect_ratio)
            project = self._phase_generate_clips(pid, output_dir, aspect_ratio)
            self.project_mgr.update_project(pid, status="done")
            return self.project_mgr.get_project(pid) or project

        # ── 新建 ──
        if not video_path:
            raise ValueError("新建项目必须提供源视频路径")
        video_path = Path(video_path)
        if not video_path.exists():
            raise FileNotFoundError(f"视频文件不存在: {video_path}")

        console.print(f"\n[bold cyan]>> 室内装修视频复刻:[/] [yellow]{video_path.name}[/]")
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
            self._phase_extract_and_analyze(video_path, pid, output_dir, duration, num_shots)
            self._phase_style(pid, output_dir)
            self._phase_clean(pid, output_dir)
            self._phase_plan(pid)
            self._phase_generate_images(pid, output_dir, aspect_ratio)
            project = self._phase_generate_clips(pid, output_dir, aspect_ratio)

            self.project_mgr.update_project(pid, status="done")
            project = self.project_mgr.get_project(pid) or {}
            console.print(
                f"\n  [bold green]v 复刻完成![/] "
                f"{len(project.get('images', []))} 张统一风格图 + {len(project.get('clips', []))} 个 4s 素材"
            )
            return project

        except Exception as exc:
            logger.error("复刻流程失败: %s", exc, exc_info=True)
            console.print(f"  [red]x 复刻失败: {exc}[/]")
            self.project_mgr.update_project(pid, status="failed", error=str(exc))
            raise

    # ════════════════════════════════════════════════════════════════
    # Step 1-2: 抽帧 + 画面分析
    # ════════════════════════════════════════════════════════════════

    def _phase_extract_and_analyze(
        self,
        video_path: Path,
        pid: str,
        output_dir: Path,
        duration: float,
        num_shots: int,
    ) -> None:
        # Step 1: 均匀抽 N 帧（interval = duration / N, 可为小数秒）
        interval = duration / num_shots if num_shots > 0 else self.settings.keyframe_interval
        console.print(f"  [dim]Step 1/7: 均匀抽帧 (目标 {num_shots} 帧, 间隔 {interval:.2f}s)...[/]")
        frames_dir = output_dir / "frames"
        frames = self.video_analyzer.extract_keyframes(video_path, frames_dir, interval=interval)
        if not frames:
            raise RuntimeError("未提取到任何关键帧")
        # 截取到目标帧数（均匀覆盖）
        frames = _cap_frames(frames, num_shots)
        console.print(f"  [green]v {len(frames)} 帧 (时长 {duration:.1f}s)[/]")

        # Step 2: 分析帧画面（用于清理判断 + 风格决策 + 视频场景）
        console.print("  [dim]Step 2/7: 分析帧画面...[/]")
        self.video_analyzer.analyze_all_frames(frames)

        self.project_mgr.update_project(
            pid,
            status="analyzing",
            video_duration=duration,
            keyframes=[
                {"timestamp": f.timestamp, "path": f.path, "index": f.index,
                 "description": f.description, "clean_path": f.clean_path}
                for f in frames
            ],
        )
        console.print("  [green]v 画面分析完成[/]")

    # ════════════════════════════════════════════════════════════════
    # Step 3-4: 画幅 + AI 推荐统一目标风格
    # ════════════════════════════════════════════════════════════════

    def _phase_style(self, pid: str, output_dir: Path) -> None:
        console.print("  [dim]Step 3/7: 画幅跟随源视频[/]")
        project = self.project_mgr.get_project(pid) or {}
        frames = self._load_keyframes(project)

        console.print("  [dim]Step 4/7: AI 推荐统一目标风格...[/]")
        self.project_mgr.update_project(pid, status="style_profiled")
        profile = self.style_analyzer.decide(frames)
        self.project_mgr.update_project(pid, style_profile=profile.to_dict())
        console.print(
            f"  [green]v 原风格 {profile.original_style_cn} → 目标风格 "
            f"{profile.overall_style_cn} / {profile.overall_style_en}[/]"
        )
        console.print(f"  [dim]目标风格锁:[/] {profile.style_descriptor_en[:160]}...")

    # ════════════════════════════════════════════════════════════════
    # Step 5: 清理基底帧（去人/字）
    # ════════════════════════════════════════════════════════════════

    def _phase_clean(self, pid: str, output_dir: Path) -> None:
        console.print("  [dim]Step 5/7: 清理基底帧 (去人/去字, 保留布局家具)...[/]")
        self.project_mgr.update_project(pid, status="cleaning")
        project = self.project_mgr.get_project(pid) or {}
        frames = self._load_keyframes(project)

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task("清理帧", total=len(frames))
            clean_dir = Path(output_dir) / "clean_frames"
            clean_dir.mkdir(parents=True, exist_ok=True)
            for frame in frames:
                progress.update(task, description=f"清理帧 {frame.index}/{len(frames)}")
                # 判断是否需 AI 清理（有人/有文字才调 API，干净帧直接用原帧）
                if self.frame_cleaner._needs_clean(frame.description or ""):
                    frame.clean_path = self.frame_cleaner._clean_one(frame, clean_dir)
                else:
                    frame.clean_path = frame.path
                progress.update(task, advance=1)

        # 回写 clean_path 到 keyframes
        self.project_mgr.update_project(
            pid,
            keyframes=[
                {"timestamp": f.timestamp, "path": f.path, "index": f.index,
                 "description": f.description, "clean_path": f.clean_path}
                for f in frames
            ],
        )
        cleaned = sum(1 for f in frames if f.clean_path != f.path)
        console.print(f"  [green]v 清理完成: {cleaned}/{len(frames)} 帧做了去人/字[/]")

    # ════════════════════════════════════════════════════════════════
    # Step 6: 构建镜头清单
    # ════════════════════════════════════════════════════════════════

    def _phase_plan(self, pid: str) -> None:
        console.print("  [dim]Step 6/7: 构建镜头清单 (基底帧 + 运镜 + 改造prompt)...[/]")
        project = self.project_mgr.get_project(pid) or {}
        frames = self._load_keyframes(project)
        profile = self._load_profile(project)
        shot_plan = self.shot_planner.build(frames, profile)
        self.project_mgr.update_project(pid, status="planning", shot_plan=shot_plan)
        console.print(f"  [green]v {len(shot_plan)} 个镜头[/]")

    # ════════════════════════════════════════════════════════════════
    # Step 6b: 逐帧风格改造（生成统一风格图）
    # ════════════════════════════════════════════════════════════════

    def _phase_generate_images(self, pid: str, output_dir: Path, aspect_ratio: str) -> None:
        console.print("  [dim]Step 7/7 (1/2): 风格改造 (保持布局家具, 统一风格)...[/]")
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
            task = progress.add_task("风格改造", total=len(shot_plan))
            for shot in shot_plan:
                sid = shot["shot_id"]
                progress.update(task, description=f"改造 shot_{sid:02d}/{len(shot_plan)}")
                if shot.get("image_status") == "done" and shot.get("image_path"):
                    progress.update(task, advance=1)
                    continue
                try:
                    img_path = self._generate_shot_image(shot, images_dir, aspect_ratio)
                    shot["image_path"] = img_path
                    shot["image_status"] = "done"
                except Exception as exc:
                    logger.warning("shot %d 风格改造失败: %s", sid, exc)
                    console.print(f"    [yellow]! shot_{sid:02d} 风格改造失败: {exc}[/]")
                    shot["image_status"] = "failed"
                self.project_mgr.update_project(pid, shot_plan=shot_plan)
                progress.update(task, advance=1)

        images = [s["image_path"] for s in shot_plan if s.get("image_status") == "done" and s.get("image_path")]
        self.project_mgr.update_project(pid, images=images)
        console.print(f"  [green]v {len(images)}/{len(shot_plan)} 张统一风格图完成[/]")

    # ════════════════════════════════════════════════════════════════
    # Step 7b: 逐图生成 4s 视频
    # ════════════════════════════════════════════════════════════════

    def _phase_generate_clips(self, pid: str, output_dir: Path, aspect_ratio: str) -> dict[str, Any]:
        console.print("  [dim]Step 7/7 (2/2): 生成 4s 视频素材...[/]")
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
            task = progress.add_task("生成视频", total=len(shot_plan))
            for shot in shot_plan:
                sid = shot["shot_id"]
                progress.update(task, description=f"视频 shot_{sid:02d}/{len(shot_plan)}")
                if shot.get("clip_status") == "done" and shot.get("clip_path"):
                    progress.update(task, advance=1)
                    continue
                if shot.get("image_status") != "done" or not shot.get("image_path"):
                    logger.warning("shot %d 图片缺失, 跳过视频生成", sid)
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
        console.print(f"  [green]v {len(clips)}/{len(shot_plan)} 个 4s 视频完成[/]")
        return self.project_mgr.get_project(pid) or {}

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
                description=kf.get("description", ""),
                clean_path=kf.get("clean_path", ""),
            ))
        return frames

    def _load_profile(self, project: dict[str, Any]) -> Any:
        """从 project 的 style_profile 字段重建 StyleProfile"""
        from core.style_analyzer import StyleProfile
        return StyleProfile.from_dict(project.get("style_profile", {}) or {})

    def _generate_shot_image(
        self,
        shot: dict[str, Any],
        images_dir: Path,
        aspect_ratio: str,
    ) -> str:
        """风格改造：pic=干净基底帧, prompt=保持布局只改风格"""
        sid = shot["shot_id"]
        prompt = shot.get("image_prompt", "")
        if not prompt:
            raise RuntimeError(f"shot {sid} 缺少 image_prompt")

        base_local = shot.get("clean_frame_path") or shot.get("base_frame_path", "")
        if not base_local or not Path(base_local).exists():
            raise RuntimeError(f"shot {sid} 基底帧不存在: {base_local}")

        # 上传基底帧到 TOS 作为风格改造参考图
        try:
            base_url = self.cos.upload_file(base_local)
        except Exception as exc:
            raise RuntimeError(f"shot {sid} 基底帧上传失败: {exc}") from exc

        console.print(f"    [cyan]风格改造 shot_{sid:02d}[/] | 基底: {Path(base_local).name}")
        logger.info("风格改造 shot_%02d | prompt=\n%s", sid, prompt)

        api_task_id = self.media_api.generate_image(
            prompt=prompt,
            model=self.settings.default_image_model,
            size=self.settings.default_size,
            aspect_ratio=aspect_ratio,
            pic=base_url,
        )
        result = self.media_api.poll_image(api_task_id)
        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"风格改造失败: {result}")

        img_path = images_dir / f"shot_{sid:02d}.png"
        self.media_api.download_media(result["url"], img_path)
        return str(img_path)

    def _generate_shot_clip(
        self,
        shot: dict[str, Any],
        clips_dir: Path,
        aspect_ratio: str,
    ) -> str:
        """单图驱动 4s 视频：pic=风格改造图, prompt=运镜"""
        sid = shot["shot_id"]
        prompt = shot.get("video_prompt", "")
        if not prompt:
            raise RuntimeError(f"shot {sid} 缺少 video_prompt")

        img_local = shot.get("image_path", "")
        if not img_local or not Path(img_local).exists():
            raise RuntimeError(f"shot {sid} 风格图不存在: {img_local}")

        try:
            pic_url = self.cos.upload_file(img_local)
        except Exception as exc:
            raise RuntimeError(f"shot {sid} 风格图上传失败: {exc}") from exc

        console.print(f"    [cyan]生成视频 shot_{sid:02d}[/] | 运镜: {shot.get('camera_move', '?')} | 4s")
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
