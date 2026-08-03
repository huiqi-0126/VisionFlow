"""图片模式流水线 - 单张客厅图 → 同风格其他房间图 → 每图 4s 视频

流程:
  上传一张客厅图片 → 检测画幅 → VLM 分析风格
  → 上传客厅图到 TOS 作为风格锚定 pic
  → 规划"同风格其他房间"清单(默认7个)
  → 逐房间 image-edit(pic=客厅图, prompt=同风格不同房间) → 其他房间图
  → 逐图生成 4s 视频(单图驱动+运镜)
  → 输出独立素材(images[] + clips[])

与视频模式(CloneVideoPipeline)的差异:
  - 输入是单张图片而非视频, 无抽帧/清理步骤
  - 生成的是【其他房间】(不同房间类型), 但用客厅参考图 pic 锚定风格 → 收敛不发散
  - 风格靠 pic(参考图) + style_descriptor_en(文本) 双重锁定
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
from core.room_planner import RoomPlanner
from core.style_analyzer import StyleAnalyzer
from core.video_analyzer import KeyFrame, aspect_ratio_from_resolution

logger = logging.getLogger(__name__)
console = Console()


def _image_resolution(image_path: str | Path) -> tuple[int, int]:
    """读取图片分辨率 (width, height)"""
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            return img.size  # (width, height)
    except Exception as exc:
        logger.warning("读取图片分辨率失败 %s: %s", image_path, exc)
        return 0, 0


class ImageClonePipeline:
    """图片模式：单张客厅图 → 同风格其他房间 → 4s 视频素材"""

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
        self.style_analyzer = StyleAnalyzer(self.llm)
        self.room_planner = RoomPlanner(clip_duration=s.clip_duration)

    def run(
        self,
        image_path: str | Path | None = None,
        num_rooms: int = 7,
        source_room_id: str = "living_room",
        resume_project_id: str | None = None,
    ) -> dict[str, Any]:
        """执行图片模式复刻流程

        Args:
            image_path: 客厅参考图路径(新建项目必填)
            num_rooms: 生成几个其他房间(默认7)
            source_room_id: 参考图房间类型(默认客厅)
            resume_project_id: 恢复已有项目(跳过分析/规划, 直接从生成素材开始)
        """
        if resume_project_id:
            project = self.project_mgr.get_project(resume_project_id)
            if not project:
                raise ValueError(f"找不到项目 {resume_project_id}")
            pid = resume_project_id
            output_dir = Path(project["output_dir"])
            aspect_ratio = project.get("aspect_ratio") or self.settings.default_aspect_ratio
            console.print(f"\n[bold cyan]>> 恢复图片模式项目:[/] [yellow]{pid}[/]")
            self._phase_generate_images(pid, output_dir, aspect_ratio)
            project = self._phase_generate_clips(pid, output_dir, aspect_ratio)
            self.project_mgr.update_project(pid, status="done")
            return self.project_mgr.get_project(pid) or project

        # ── 新建 ──
        if not image_path:
            raise ValueError("新建项目必须提供图片路径")
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(f"图片文件不存在: {image_path}")

        console.print(f"\n[bold cyan]>> 图片模式复刻:[/] [yellow]{image_path.name}[/]")
        width, height = _image_resolution(image_path)
        aspect_ratio = aspect_ratio_from_resolution(width, height)
        console.print(f"  参考图: {width}x{height} → {aspect_ratio} | 生成 {num_rooms} 个其他房间")

        project = self.project_mgr.create_project(
            mode="image",
            source_image=str(image_path),
            source_resolution=(width, height),
            aspect_ratio=aspect_ratio,
        )
        pid = project["project_id"]
        output_dir = Path(project["output_dir"])

        try:
            self._phase_style(image_path, pid)
            ref_pic_url = self._phase_upload_reference(image_path, pid)
            self._phase_plan(pid, num_rooms, source_room_id)
            self._phase_generate_images(pid, output_dir, aspect_ratio, ref_pic_url)
            project = self._phase_generate_clips(pid, output_dir, aspect_ratio)

            self.project_mgr.update_project(pid, status="done")
            project = self.project_mgr.get_project(pid) or {}
            console.print(
                f"\n  [bold green]v 完成![/] "
                f"{len(project.get('images', []))} 张其他房间图 + {len(project.get('clips', []))} 个 4s 素材"
            )
            return project

        except Exception as exc:
            logger.error("图片模式流程失败: %s", exc, exc_info=True)
            console.print(f"  [red]x 流程失败: {exc}[/]")
            self.project_mgr.update_project(pid, status="failed", error=str(exc))
            raise

    # ════════════════════════════════════════════════════════════════
    # Step 1: 分析客厅图风格
    # ════════════════════════════════════════════════════════════════

    def _phase_style(self, image_path: Path, pid: str) -> None:
        console.print("  [dim]Step 1/4: 分析参考图风格...[/]")
        self.project_mgr.update_project(pid, status="style_profiled")
        # 构造单帧, 复用 StyleAnalyzer(它会发图给 VLM 分析)
        single_frame = KeyFrame(timestamp=0.0, path=str(image_path), index=1)
        profile = self.style_analyzer.decide([single_frame])
        self.project_mgr.update_project(pid, style_profile=profile.to_dict())
        console.print(
            f"  [green]v 参考图风格: {profile.overall_style_cn} / {profile.overall_style_en}[/]"
        )
        console.print(f"  [dim]风格锁:[/] {profile.style_descriptor_en[:160]}...")

    # ════════════════════════════════════════════════════════════════
    # Step 2: 上传客厅图作为风格锚定 pic
    # ════════════════════════════════════════════════════════════════

    def _phase_upload_reference(self, image_path: Path, pid: str) -> str:
        console.print("  [dim]Step 2/4: 上传参考图(风格锚定)...[/]")
        try:
            url = self.cos.upload_file(image_path)
            self.project_mgr.update_project(pid, reference_frames=[str(image_path)])
            console.print("  [green]v 参考图已上传[/]")
            return url
        except Exception as exc:
            raise RuntimeError(f"参考图上传 TOS 失败: {exc}") from exc

    # ════════════════════════════════════════════════════════════════
    # Step 3: 规划其他房间清单
    # ════════════════════════════════════════════════════════════════

    def _phase_plan(self, pid: str, num_rooms: int, source_room_id: str) -> None:
        console.print(f"  [dim]Step 3/4: 规划 {num_rooms} 个同风格其他房间...[/]")
        project = self.project_mgr.get_project(pid) or {}
        profile = self._load_profile(project)
        room_plan = self.room_planner.plan(
            profile, source_room_id=source_room_id, num_rooms=num_rooms,
        )
        self.project_mgr.update_project(pid, status="planning", shot_plan=room_plan)
        console.print(f"  [green]v {len(room_plan)} 个房间: "
                      f"{', '.join(s['room_type'] for s in room_plan)}[/]")

    # ════════════════════════════════════════════════════════════════
    # Step 4a: 逐房间生成图片
    # ════════════════════════════════════════════════════════════════

    def _phase_generate_images(
        self,
        pid: str,
        output_dir: Path,
        aspect_ratio: str,
        ref_pic_url: str | None = None,
    ) -> None:
        console.print("  [dim]Step 4/4 (1/2): 生成同风格其他房间图...[/]")
        self.project_mgr.update_project(pid, status="generating_images")
        project = self.project_mgr.get_project(pid)
        if not project:
            raise RuntimeError(f"项目丢失: {pid}")
        room_plan = project.get("shot_plan", [])

        # resume 时 ref_pic_url 为空, 需重新上传参考图
        if not ref_pic_url:
            ref_pic_url = self._reupload_reference(project)

        images_dir = output_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task("生成房间图", total=len(room_plan))
            for shot in room_plan:
                sid = shot["shot_id"]
                progress.update(task, description=f"房间图 {shot.get('room_type','?')} {sid}/{len(room_plan)}")
                if shot.get("image_status") == "done" and shot.get("image_path"):
                    progress.update(task, advance=1)
                    continue
                try:
                    img_path = self._generate_room_image(shot, images_dir, ref_pic_url, aspect_ratio)
                    shot["image_path"] = img_path
                    shot["image_status"] = "done"
                except Exception as exc:
                    logger.warning("房间 %s 图片生成失败: %s", shot.get("room_type"), exc)
                    console.print(f"    [yellow]! {shot.get('room_type')} 图片失败: {exc}[/]")
                    shot["image_status"] = "failed"
                self.project_mgr.update_project(pid, shot_plan=room_plan)
                progress.update(task, advance=1)

        images = [s["image_path"] for s in room_plan if s.get("image_status") == "done" and s.get("image_path")]
        self.project_mgr.update_project(pid, images=images)
        console.print(f"  [green]v {len(images)}/{len(room_plan)} 张房间图完成[/]")

    # ════════════════════════════════════════════════════════════════
    # Step 4b: 逐图生成 4s 视频
    # ════════════════════════════════════════════════════════════════

    def _phase_generate_clips(self, pid: str, output_dir: Path, aspect_ratio: str) -> dict[str, Any]:
        console.print("  [dim]Step 4/4 (2/2): 生成 4s 视频素材...[/]")
        self.project_mgr.update_project(pid, status="generating_clips")
        project = self.project_mgr.get_project(pid)
        if not project:
            raise RuntimeError(f"项目丢失: {pid}")
        room_plan = project.get("shot_plan", [])

        clips_dir = output_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(), console=console,
        ) as progress:
            task = progress.add_task("生成视频", total=len(room_plan))
            for shot in room_plan:
                sid = shot["shot_id"]
                progress.update(task, description=f"视频 {shot.get('room_type','?')} {sid}/{len(room_plan)}")
                if shot.get("clip_status") == "done" and shot.get("clip_path"):
                    progress.update(task, advance=1)
                    continue
                if shot.get("image_status") != "done" or not shot.get("image_path"):
                    logger.warning("shot %d 图片缺失, 跳过视频", sid)
                    shot["clip_status"] = "skipped"
                    progress.update(task, advance=1)
                    continue
                try:
                    clip_path = self._generate_room_clip(shot, clips_dir, aspect_ratio)
                    shot["clip_path"] = clip_path
                    shot["clip_status"] = "done"
                except Exception as exc:
                    logger.warning("shot %d 视频生成失败: %s", sid, exc)
                    console.print(f"    [yellow]! shot_{sid:02d} 视频失败: {exc}[/]")
                    shot["clip_status"] = "failed"
                self.project_mgr.update_project(pid, shot_plan=room_plan)
                progress.update(task, advance=1)

        clips = [s["clip_path"] for s in room_plan if s.get("clip_status") == "done" and s.get("clip_path")]
        self.project_mgr.update_project(pid, clips=clips)
        console.print(f"  [green]v {len(clips)}/{len(room_plan)} 个 4s 视频完成[/]")
        return self.project_mgr.get_project(pid) or {}

    # ════════════════════════════════════════════════════════════════
    # 子步骤
    # ════════════════════════════════════════════════════════════════

    def _load_profile(self, project: dict[str, Any]) -> Any:
        from core.style_analyzer import StyleProfile
        return StyleProfile.from_dict(project.get("style_profile", {}) or {})

    def _reupload_reference(self, project: dict[str, Any]) -> str | None:
        """resume 时重新上传参考图得到 pic URL"""
        ref_frames = project.get("reference_frames", [])
        source_image = project.get("source_image", "")
        for local in ([source_image] + list(ref_frames)):
            if local and Path(local).exists():
                try:
                    return self.cos.upload_file(local)
                except Exception as exc:
                    logger.warning("参考图上传失败: %s", exc)
        logger.warning("resume 无可用参考图, 图片生成将不传 pic(纯文本风格锁)")
        return None

    def _generate_room_image(
        self,
        shot: dict[str, Any],
        images_dir: Path,
        ref_pic_url: str | None,
        aspect_ratio: str,
    ) -> str:
        """生成单个房间图: pic=客厅参考图(风格锚定), prompt=同风格不同房间"""
        sid = shot["shot_id"]
        prompt = shot.get("image_prompt", "")
        if not prompt:
            raise RuntimeError(f"shot {sid} 缺少 image_prompt")

        console.print(f"    [cyan]生成房间图 {shot.get('room_type','?')}[/] | ref: {'YES' if ref_pic_url else 'none'}")
        logger.info("生成房间图 shot_%02d (%s) | prompt=\n%s", sid, shot.get("room_type"), prompt)

        api_task_id = self.media_api.generate_image(
            prompt=prompt,
            model=self.settings.default_image_model,
            size=self.settings.default_size,
            aspect_ratio=aspect_ratio,
            pic=ref_pic_url,
        )
        result = self.media_api.poll_image(api_task_id)
        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"房间图生成失败: {result}")

        img_path = images_dir / f"room_{shot.get('room_type','x')}_{sid:02d}.png"
        self.media_api.download_media(result["url"], img_path)
        return str(img_path)

    def _generate_room_clip(
        self,
        shot: dict[str, Any],
        clips_dir: Path,
        aspect_ratio: str,
    ) -> str:
        """单图驱动 4s 视频: pic=房间图, prompt=运镜"""
        sid = shot["shot_id"]
        prompt = shot.get("video_prompt", "")
        if not prompt:
            raise RuntimeError(f"shot {sid} 缺少 video_prompt")

        img_local = shot.get("image_path", "")
        if not img_local or not Path(img_local).exists():
            raise RuntimeError(f"shot {sid} 房间图不存在: {img_local}")

        try:
            pic_url = self.cos.upload_file(img_local)
        except Exception as exc:
            raise RuntimeError(f"shot {sid} 房间图上传失败: {exc}") from exc

        console.print(f"    [cyan]生成视频 {shot.get('room_type','?')}[/] | 运镜: {shot.get('camera_move','?')} | 4s")
        logger.info("生成视频 shot_%02d (%s) | prompt=\n%s", sid, shot.get("room_type"), prompt)

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

        clip_path = clips_dir / f"room_{shot.get('room_type','x')}_{sid:02d}.mp4"
        self.media_api.download_media(result["url"], clip_path)
        return str(clip_path)

    def get_status(self) -> dict[str, Any]:
        return self.project_mgr.get_stats()
