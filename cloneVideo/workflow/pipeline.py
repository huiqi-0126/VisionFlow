"""室内装修视频复刻流水线 - 核心编排器

流程:
  上传视频 → 提取关键帧 → 分析画面 → 检测源视频画幅
  → 风格画像 + 挑参考帧 → 按时长智能推导镜头清单
  → 逐 shot 生成风格图(参考帧锚定) → 逐 shot 生成 4s 视频(单图驱动)
  → 输出独立素材(images[] + clips[])

与 VisionFlow 的差异:
  - 输出是【独立素材包】而非合并成片(不做 ffmpeg merge)
  - 画幅跟随源视频(横屏/竖屏自适应)
  - 风格一致性靠【风格锁 prompt + 参考帧 pic】双重锁定
  - 镜头数按视频时长智能推导(4~15个), 每个素材 4 秒
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

from config import Settings, get_settings
from core.cos_client import COSClient
from core.llm_client import LLMClient
from core.media_api import MediaAPIClient
from core.project_manager import ProjectManager
from core.shot_planner import ShotPlanner, calc_num_shots
from core.style_analyzer import StyleAnalyzer
from core.video_analyzer import VideoAnalyzer, aspect_ratio_from_resolution

logger = logging.getLogger(__name__)
console = Console()


class CloneVideoPipeline:
    """室内装修视频复刻全流程编排器"""

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
        # shot_planner 在 run() 中按配置创建

    def run(
        self,
        video_path: str | Path | None = None,
        resume_project_id: str | None = None,
    ) -> dict[str, Any]:
        """执行完整的室内装修视频复刻流程

        Args:
            video_path: 源视频路径 (新建项目时必填)
            resume_project_id: 已有项目 ID, 提供则跳过前 4 步, 直接从生成素材开始
        """
        if resume_project_id:
            project = self.project_mgr.get_project(resume_project_id)
            if not project:
                raise ValueError(f"找不到项目 {resume_project_id}")
            pid = resume_project_id
            output_dir = Path(project["output_dir"])
            console.print(f"\n[bold cyan]>> 恢复室内复刻项目:[/] [yellow]{pid}[/]")
        else:
            if not video_path:
                raise ValueError("新建项目必须提供源视频路径")
            video_path = Path(video_path)
            if not video_path.exists():
                raise FileNotFoundError(f"视频文件不存在: {video_path}")

            # 先检测源视频画幅（让生成尺寸跟随源视频）
            console.print(f"\n[bold cyan]>> 室内装修视频复刻:[/] [yellow]{video_path.name}[/]")
            width, height = self.video_analyzer.get_video_resolution(video_path)
            aspect_ratio = aspect_ratio_from_resolution(width, height)
            console.print(f"  源视频画幅: {width}x{height} → aspect_ratio={aspect_ratio}")

            project = self.project_mgr.create_project(
                source_video=str(video_path),
                source_resolution=(width, height),
                aspect_ratio=aspect_ratio,
            )
            pid = project["project_id"]
            output_dir = Path(project["output_dir"])

        aspect_ratio = project.get("aspect_ratio") or self.settings.default_aspect_ratio

        try:
            if not resume_project_id:
                # 此分支逻辑保证 video_path 非 None（resume 路径已在上文处理并跳过本块）
                if not video_path:
                    raise ValueError("缺少源视频路径")
                src_video = Path(video_path)
                project = self._phase_analyze(src_video, pid, output_dir, project)
                project = self._phase_style_and_plan(src_video, pid, output_dir, project, aspect_ratio)

            # Step 6/7: 生成图片素材
            project = self._phase_generate_images(pid, output_dir, aspect_ratio)

            # Step 7/7: 生成视频素材
            project = self._phase_generate_clips(pid, output_dir, aspect_ratio)

            self.project_mgr.update_project(pid, status="done")
            project = self.project_mgr.get_project(pid) or {}
            images = project.get("images", [])
            clips = project.get("clips", [])
            console.print(
                f"\n  [bold green]v 复刻完成![/] {len(images)} 张风格图 + {len(clips)} 个 4s 素材"
            )
            return project

        except Exception as exc:
            logger.error("复刻流程失败: %s", exc, exc_info=True)
            console.print(f"  [red]x 复刻失败: {exc}[/]")
            self.project_mgr.update_project(pid, status="failed", error=str(exc))
            raise

    # ════════════════════════════════════════════════════════════════
    # 阶段一: 抽帧 + 画面分析
    # ════════════════════════════════════════════════════════════════

    def _phase_analyze(
        self,
        video_path: Path,
        pid: str,
        output_dir: Path,
        project: dict[str, Any],
    ) -> dict[str, Any]:
        # Step 1: 提取关键帧
        console.print("  [dim]Step 1/7: 提取关键帧...[/]")
        frames_dir = output_dir / "frames"
        frames = self.video_analyzer.extract_keyframes(video_path, frames_dir)
        if not frames:
            raise RuntimeError("未提取到任何关键帧")

        duration = self.video_analyzer.get_video_duration(video_path)
        self.project_mgr.update_project(
            pid,
            status="analyzing",
            video_duration=duration,
            keyframes=[
                {"timestamp": f.timestamp, "path": f.path, "index": f.index, "description": f.description}
                for f in frames
            ],
        )
        console.print(f"  [green]v {len(frames)} 个关键帧 (时长 {duration:.1f}s)[/]")

        # Step 2: 分析关键帧画面
        console.print("  [dim]Step 2/7: 分析关键帧画面...[/]")
        self.video_analyzer.analyze_all_frames(frames)
        self.project_mgr.update_project(
            pid,
            keyframes=[
                {"timestamp": f.timestamp, "path": f.path, "index": f.index, "description": f.description}
                for f in frames
            ],
        )
        console.print("  [green]v 画面分析完成[/]")
        return self.project_mgr.get_project(pid) or project

    # ════════════════════════════════════════════════════════════════
    # 阶段二: 风格画像 + 参考帧 + 镜头规划
    # ════════════════════════════════════════════════════════════════

    def _phase_style_and_plan(
        self,
        video_path: Path,
        pid: str,
        output_dir: Path,
        project: dict[str, Any],
        aspect_ratio: str,
    ) -> dict[str, Any]:
        # Step 3: 画幅已在 run() 检测, 这里只打印确认
        console.print(f"  [dim]Step 3/7: 画幅跟随源视频 = {aspect_ratio}[/]")

        # Step 4: 风格画像 + 挑参考帧
        console.print("  [dim]Step 4/7: 提取风格画像 + 挑选参考帧...[/]")
        from core.video_analyzer import KeyFrame
        keyframe_dicts = project.get("keyframes", [])
        frames = [
            KeyFrame(
                timestamp=kf.get("timestamp", 0.0),
                path=kf.get("path", ""),
                index=kf.get("index", i + 1),
                description=kf.get("description", ""),
            )
            for i, kf in enumerate(keyframe_dicts)
        ]
        profile, ref_frames = self.style_analyzer.analyze(frames)

        # 复制参考帧到项目目录(便于追溯), 记录本地路径
        import shutil
        ref_local_paths: list[str] = []
        ref_dir = output_dir / "reference"
        ref_dir.mkdir(parents=True, exist_ok=True)
        for i, rf in enumerate(ref_frames, 1):
            if rf.path and Path(rf.path).exists():
                dst = ref_dir / f"reference_{i:02d}.jpg"
                shutil.copy2(rf.path, dst)
                ref_local_paths.append(str(dst))

        self.project_mgr.update_project(
            pid,
            status="style_profiled",
            style_profile=profile.to_dict(),
            reference_frames=ref_local_paths,
        )
        console.print(f"  [green]v 风格: {profile.overall_style_cn} / {profile.overall_style_en}[/]")
        console.print(f"  [green]v 参考帧: {len(ref_local_paths)} 张[/]")
        console.print(f"  [dim]风格锁:[/] {profile.style_descriptor_en[:160]}...")

        # Step 5: 规划镜头清单 (镜头数按源视频时长智能推导)
        num_shots = calc_num_shots(
            project.get("video_duration", 0),
            min_shots=self.settings.min_shots,
            max_shots=self.settings.max_shots,
        )
        console.print(
            f"  [dim]Step 5/7: 规划 {num_shots} 个镜头 "
            f"(按时长 {project.get('video_duration', 0):.1f}s 推导, 范围 "
            f"{self.settings.min_shots}~{self.settings.max_shots})...[/]"
        )
        planner = ShotPlanner(
            self.llm,
            num_shots=num_shots,
            clip_duration=self.settings.clip_duration,
        )
        shot_plan = planner.plan(profile)
        self.project_mgr.update_project(pid, status="planning", shot_plan=shot_plan)
        console.print(f"  [green]v {len(shot_plan)} 个镜头[/]")
        for s in shot_plan:
            console.print(
                f"    [cyan]shot_{s['shot_id']:02d}[/] "
                f"{s.get('room_type', '?')} · {s.get('viewpoint', '?')} · {s.get('camera_move', '?')}"
            )
        return self.project_mgr.get_project(pid) or project

    # ════════════════════════════════════════════════════════════════
    # 阶段三: 生成图片素材
    # ════════════════════════════════════════════════════════════════

    def _phase_generate_images(
        self,
        pid: str,
        output_dir: Path,
        aspect_ratio: str,
    ) -> dict[str, Any]:
        console.print("  [dim]Step 6/7: 生成风格图片素材...[/]")
        self.project_mgr.update_project(pid, status="generating_images")

        project = self.project_mgr.get_project(pid)
        if not project:
            raise RuntimeError(f"项目丢失: {pid}")
        shot_plan = project.get("shot_plan", [])

        # 上传参考帧到 TOS, 取第一张作为风格锚定 pic
        ref_pic_url = self._upload_reference_pic(project)

        images_dir = output_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task("生成图片", total=len(shot_plan))
            for shot in shot_plan:
                sid = shot["shot_id"]
                progress.update(task, description=f"图片 shot_{sid:02d}/{len(shot_plan)}")
                if shot.get("image_status") == "done" and shot.get("image_path"):
                    progress.update(task, advance=1)
                    continue
                try:
                    img_path = self._generate_shot_image(shot, images_dir, ref_pic_url, aspect_ratio)
                    shot["image_path"] = img_path
                    shot["image_status"] = "done"
                except Exception as exc:
                    logger.warning("shot %d 图片生成失败: %s", sid, exc)
                    console.print(f"    [yellow]! shot_{sid:02d} 图片失败: {exc}[/]")
                    shot["image_status"] = "failed"
                self.project_mgr.update_project(pid, shot_plan=shot_plan)
                progress.update(task, advance=1)

        # 汇总成功的图片路径
        images = [s["image_path"] for s in shot_plan if s.get("image_status") == "done" and s.get("image_path")]
        self.project_mgr.update_project(pid, images=images)
        console.print(f"  [green]v {len(images)}/{len(shot_plan)} 张图片完成[/]")
        return self.project_mgr.get_project(pid) or {}

    # ════════════════════════════════════════════════════════════════
    # 阶段四: 生成 4s 视频素材
    # ════════════════════════════════════════════════════════════════

    def _phase_generate_clips(
        self,
        pid: str,
        output_dir: Path,
        aspect_ratio: str,
    ) -> dict[str, Any]:
        console.print("  [dim]Step 7/7: 生成 4s 视频素材...[/]")
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
                # 图片没成功的 shot 跳过视频
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
        console.print(f"  [green]v {len(clips)}/{len(shot_plan)} 个视频完成[/]")
        return self.project_mgr.get_project(pid) or {}

    # ════════════════════════════════════════════════════════════════
    # 子步骤
    # ════════════════════════════════════════════════════════════════

    def _upload_reference_pic(self, project: dict[str, Any]) -> str | None:
        """上传第一张参考帧到 TOS, 返回公网 URL 作为风格锚定 pic"""
        ref_frames = project.get("reference_frames", [])
        for local in ref_frames:
            if local and Path(local).exists():
                try:
                    url = self.cos.upload_file(local)
                    logger.info("参考帧上传: %s", url[:100])
                    return url
                except Exception as exc:
                    logger.warning("参考帧上传失败: %s", exc)
        logger.warning("无可用参考帧, 图片生成将不传 pic(纯文本风格锁)")
        return None

    def _generate_shot_image(
        self,
        shot: dict[str, Any],
        images_dir: Path,
        ref_pic_url: str | None,
        aspect_ratio: str,
    ) -> str:
        """为单个 shot 生成风格图片, 返回本地路径"""
        sid = shot["shot_id"]
        prompt = shot.get("image_prompt", "")
        if not prompt:
            raise RuntimeError(f"shot {sid} 缺少 image_prompt")

        console.print(f"    [cyan]生成图片 shot_{sid:02d}[/] | ref: {'YES' if ref_pic_url else 'none'}")
        logger.info("生成图片 shot_%02d | prompt=\n%s", sid, prompt)

        api_task_id = self.media_api.generate_image(
            prompt=prompt,
            model=self.settings.default_image_model,
            size=self.settings.default_size,
            aspect_ratio=aspect_ratio,
            pic=ref_pic_url,
        )
        result = self.media_api.poll_image(api_task_id)
        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"图片生成失败: {result}")

        img_path = images_dir / f"shot_{sid:02d}.png"
        self.media_api.download_media(result["url"], img_path)
        return str(img_path)

    def _generate_shot_clip(
        self,
        shot: dict[str, Any],
        clips_dir: Path,
        aspect_ratio: str,
    ) -> str:
        """为单个 shot 生成 4s 视频, 返回本地路径

        视频用该 shot 自己生成的图片(上传 TOS 后的 URL)作为单图驱动 pic。
        """
        sid = shot["shot_id"]
        prompt = shot.get("video_prompt", "")
        if not prompt:
            raise RuntimeError(f"shot {sid} 缺少 video_prompt")

        img_local = shot.get("image_path", "")
        if not img_local or not Path(img_local).exists():
            raise RuntimeError(f"shot {sid} 图片不存在: {img_local}")

        # 上传该 shot 的图片到 TOS 得 pic URL
        try:
            pic_url = self.cos.upload_file(img_local)
        except Exception as exc:
            raise RuntimeError(f"shot {sid} 图片上传 TOS 失败: {exc}") from exc

        console.print(f"    [cyan]生成视频 shot_{sid:02d}[/] | pic: uploaded | dur: 4s")
        logger.info("生成视频 shot_%02d | prompt=\n%s", sid, prompt)

        api_task_id = self.media_api.generate_video(
            prompt=prompt,
            model=self.settings.default_video_model,
            size=self.settings.default_size,
            duration=str(self.settings.clip_duration),
            aspect_ratio=aspect_ratio,
            pic=pic_url,
            video_type="0",   # 非固定时长模式(用于 4s)
        )
        result = self.media_api.poll_video(api_task_id)
        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"视频生成失败: {result}")

        clip_path = clips_dir / f"shot_{sid:02d}.mp4"
        self.media_api.download_media(result["url"], clip_path)
        return str(clip_path)

    def get_status(self) -> dict[str, Any]:
        return self.project_mgr.get_stats()
