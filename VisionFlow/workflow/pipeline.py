"""视频复刻流水线 - 核心编排器

流程: 上传视频 → 提取关键帧 → LLM 分析 → 制作分镜 → 生成首帧图 → 生成分镜视频
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

from config import Settings, get_settings
from core.cos_client import COSClient
from core.llm_client import LLMClient
from core.media_api import MediaAPIClient
from core.project_manager import ProjectManager
from core.storyboard import StoryboardMaker
from core.video_analyzer import VideoAnalyzer

logger = logging.getLogger(__name__)
console = Console()


class ReplicationPipeline:
    """视频复刻全流程编排器"""

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
        # storyboard_maker 在 run() 中根据 mode 动态创建

    def run(self, video_path: str | Path | None = None, style_hint: str = "", mode: str = "auto", resume_project_id: str | None = None) -> dict[str, Any]:
        """执行完整的视频复刻流程

        Args:
            video_path: 源视频路径 (若为 resume 模式，可为 None)
            style_hint: 用户指定的风格方向（可选，如 "剧情演绎"、"质感内容"）
            resume_project_id: 项目 ID，若提供则从已有项目恢复，跳过前三步直接从生成分镜视频开始
        """
        if resume_project_id:
            project = self.project_mgr.get_project(resume_project_id)
            if not project:
                raise ValueError(f"找不到项目 {resume_project_id}")
            pid = resume_project_id
            output_dir = Path(project["output_dir"])
            console.print(f"\n[bold cyan]>> 恢复视频复刻项目:[/] [yellow]{pid}[/]")
            storyboard = project.get("storyboard", [])
            # Jump straight to Step 4/5
        else:
            if not video_path:
                raise ValueError("新建项目必须提供源视频路径")
            video_path = Path(video_path)
            if not video_path.exists():
                raise FileNotFoundError(f"视频文件不存在: {video_path}")

            # 创建项目
            project = self.project_mgr.create_project(str(video_path))
            pid = project["project_id"]
            output_dir = Path(project["output_dir"])

            console.print(f"\n[bold cyan]>> 视频复刻:[/] [yellow]{video_path.name}[/] (项目: {pid})")

            console.print()

        try:
            if not resume_project_id:
                # Step 1: 提取关键帧
                console.print("  [dim]Step 1/6: 提取关键帧...[/]")
                frames_dir = output_dir / "frames"
                frames = self.video_analyzer.extract_keyframes(video_path, frames_dir)
                if not frames:
                    raise RuntimeError("未提取到任何关键帧")

                # 获取视频时长
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

                # Step 2: 分析关键帧
                console.print("  [dim]Step 2/6: 分析关键帧画面...[/]")
                self.video_analyzer.analyze_all_frames(frames)

                # 更新关键帧描述
                self.project_mgr.update_project(
                    pid,
                    keyframes=[
                        {"timestamp": f.timestamp, "path": f.path, "index": f.index, "description": f.description}
                        for f in frames
                    ],
                )
                console.print("  [green]v 关键帧分析完成[/]")
                console.print(f"\n  [bold yellow]关键帧分析结果:[/]")
                for f in frames:
                    console.print(f"  [cyan]帧 {f.index:02d}[/] ({f.timestamp:.1f}s): {f.description[:200]}")
                console.print()

                # Step 3: 制作分镜
                console.print(f"  [dim]Step 3/6: 制作分镜表 (模式: {mode})...[/]")
                frame_dicts = [
                    {"timestamp": f.timestamp, "path": f.path, "index": f.index, "description": f.description}
                    for f in frames
                ]
                # 根据 mode 创建 StoryboardMaker
                if mode == "auto":
                    storyboard_maker = StoryboardMaker(self.llm, mode="auto")
                else:
                    storyboard_maker = StoryboardMaker(self.llm, max_scenes=3, clip_duration=15, mode="fixed")
                storyboard, storyboard_meta = storyboard_maker.create_storyboard(frame_dicts, duration, style_hint=style_hint)

                self.project_mgr.update_project(
                    pid,
                    status="generating_images",
                    storyboard=storyboard,
                    character_description=storyboard_meta.get("character_description", ""),
                    style_info=storyboard_meta.get("style_info", ""),
                )
                console.print(f"  [green]v {len(storyboard)} 个分镜[/]")
                style_info = storyboard_meta.get("style_info", "")
                if style_info:
                    console.print(f"\n  [bold yellow]风格识别:[/] {style_info}")
                char_desc = storyboard_meta.get("character_description", "")
                if char_desc:
                    console.print(f"\n  [bold yellow]锁定人物描述:[/]")
                    console.print(f"  {char_desc}\n")
                for scene in storyboard:
                    console.print(
                        f"  [bold]场景 {scene['scene_id']}:[/]"
                        f" {scene.get('title', '')} "
                        f"({scene['time_start']:.0f}s ~ {scene['time_end']:.0f}s)"
                    )
                    console.print(f"    [dim]script:[/] {scene.get('script', '')}")
                    console.print(f"    [dim]visual:[/] {scene.get('visual', '')}")
                    console.print(f"    [dim]camera:[/] {scene.get('camera', '')}")
                    console.print(f"    [dim]emotion:[/] {scene.get('emotion', '')}")
                    console.print()

            # Step 4: 上传分镜首尾帧到 COS
            # 我们使用原视频的真实高信息量帧作为首尾帧，无需 AI 生成，确保完美还原
            console.print("  [dim]Step 4/6: 上传分镜首尾帧...[/]")
            with Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeElapsedColumn(), console=console,
            ) as progress:
                upload_task = progress.add_task("上传图片", total=len(storyboard))
                for scene in storyboard:
                    progress.update(upload_task, description=f"上传帧 {scene['scene_id']}/{len(storyboard)}")
                    try:
                        sf_path = scene.get("start_frame")
                        ef_path = scene.get("end_frame")
                        sf_desc = scene.get("start_frame_desc", "")
                        ef_desc = scene.get("end_frame_desc", "")
                        
                        latest_project = self.project_mgr.get_project(pid) or project
                        keyframes = latest_project.get("keyframes", [])
                        
                        import os
                        if sf_path and os.path.exists(sf_path):
                            sf_path, sf_desc = self._get_safe_frame(sf_path, sf_desc, keyframes, mode)
                            force_remove = self._has_frontal_face(sf_desc) if mode == "auto" else self._has_person(sf_desc)
                            sf_path = self._clean_image_if_needed(sf_path, sf_desc, output_dir, f"sf_{scene['scene_id']}", force_remove_person=force_remove, mode=mode)
                            scene["start_pic_url"] = self.cos.upload_file(sf_path)
                            
                        if ef_path and os.path.exists(ef_path):
                            ef_path, ef_desc = self._get_safe_frame(ef_path, ef_desc, keyframes, mode)
                            force_remove = self._has_frontal_face(ef_desc) if mode == "auto" else self._has_person(ef_desc)
                            ef_path = self._clean_image_if_needed(ef_path, ef_desc, output_dir, f"ef_{scene['scene_id']}", force_remove_person=force_remove, mode=mode)
                            scene["end_pic_url"] = self.cos.upload_file(ef_path)
                            
                        scene["clip_status"] = "image_ready"
                        self.project_mgr.update_project(pid, storyboard=storyboard)
                    except Exception as exc:
                        logger.warning("场景 %d 首尾帧上传失败: %s", scene["scene_id"], exc)
                        console.print(f"    [yellow]! 场景 {scene['scene_id']} 首尾帧上传失败: {exc}[/]")
                        scene["clip_status"] = "image_failed"
                    progress.update(upload_task, advance=1)

            # Step 5: 生成分镜视频
            console.print("  [dim]Step 5/6: 生成分镜视频...[/]")
            self.project_mgr.update_project(pid, status="generating_clips", storyboard=storyboard)

            with Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeElapsedColumn(), console=console,
            ) as progress:
                clip_task = progress.add_task("生成视频", total=len(storyboard))
                for i, scene in enumerate(storyboard):
                    progress.update(clip_task, description=f"视频 {scene['scene_id']}/{len(storyboard)}")
                    try:
                        clip_path = self._generate_scene_clip(scene, output_dir)
                        scene["generated_clip"] = clip_path
                        scene["clip_status"] = "done"
                        self.project_mgr.update_project(pid, storyboard=storyboard)
                        
                        # Use the last frame of this clip as the first frame of the next scene
                        if i < len(storyboard) - 1:
                            next_scene = storyboard[i + 1]
                            last_frame_path = self.video_analyzer.extract_last_frame(clip_path, output_dir / f"scene_{scene['scene_id']:03d}_last_frame.jpg")
                            next_scene["start_pic_url"] = self.cos.upload_file(last_frame_path)
                            self.project_mgr.update_project(pid, storyboard=storyboard)
                            
                    except Exception as exc:
                        logger.warning("场景 %d 视频生成失败: %s", scene["scene_id"], exc)
                        console.print(f"    [yellow]! 场景 {scene['scene_id']} 视频失败: {exc}[/]")
                        scene["clip_status"] = "failed"
                    progress.update(clip_task, advance=1)

            # Step 6: 合并视频（带转场）
            final_clips = [s["generated_clip"] for s in storyboard if s.get("generated_clip")]
            merged_path = ""
            if len(final_clips) >= 2:
                console.print("  [dim]Step 6/6: 合并视频（带转场）...[/]")
                self.project_mgr.update_project(pid, status="merging", storyboard=storyboard)
                try:
                    merged_path = self.video_analyzer.merge_clips(
                        clips=final_clips,
                        output_path=output_dir / "merged_final.mp4",
                        transition="fade",
                        transition_duration=0.8,
                    )
                    console.print(f"  [green]v 合并完成: {merged_path}[/]")
                except Exception as exc:
                    logger.warning("视频合并失败（不影响单独的分镜视频）: %s", exc)
                    console.print(f"  [yellow]! 合并失败: {exc}（分镜视频已单独保存）[/]")
            else:
                merged_path = final_clips[0] if final_clips else ""
                console.print("  [dim]Step 6/6: 跳过合并（仅 1 个片段）[/]")

            self.project_mgr.update_project(
                pid,
                status="done",
                storyboard=storyboard,
                final_clips=final_clips,
                merged_video=merged_path,
            )
            console.print(
                f"\n  [bold green]v 复刻完成![/] {len(final_clips)}/{len(storyboard)} 个分镜视频"
                + (f" → 已合并为 {Path(merged_path).name}" if merged_path else "")
            )

        except Exception as exc:
            logger.error("复刻流程失败: %s", exc, exc_info=True)
            console.print(f"  [red]x 复刻失败: {exc}[/]")
            self.project_mgr.update_project(pid, status="failed", error=str(exc))
            raise

        return self.project_mgr.get_project(pid) or {}

    # ── 子步骤 ────────────────────────────────────────────────

    def _has_person(self, desc: str) -> bool:
        if not desc:
            return False
        # 最稳妥：只要有任何人和身体部位的特征，都算有“人物”
        keywords = ["人", "男", "女", "孩", "面", "脸", "手", "脚", "身影", "背影", "特写", "近景"]
        return any(kw in desc for kw in keywords)

    def _has_frontal_face(self, desc: str) -> bool:
        if not desc:
            return False
        # 智能模式放宽：只检测是否有正面的五官
        keywords = ["正脸", "正面", "五官", "脸部特写", "面部特写", "清晰的脸"]
        return any(kw in desc for kw in keywords)

    def _get_safe_frame(self, original_path: str, original_desc: str, keyframes: list, mode: str = "auto") -> tuple[str, str]:
        """尝试找到附近完全没有人的帧（或符合模式安全条件的帧）"""
        has_person_fn = self._has_frontal_face if mode == "auto" else self._has_person
        if not has_person_fn(original_desc):
            return original_path, original_desc
            
        orig_idx = -1
        for i, kf in enumerate(keyframes):
            if kf.get("path") == original_path:
                orig_idx = i
                break
                
        if orig_idx == -1:
            return original_path, original_desc
            
        offsets = [1, -1, 2, -2, 3, -3, 4, -4, 5, -5]
        for offset in offsets:
            check_idx = orig_idx + offset
            if 0 <= check_idx < len(keyframes):
                candidate = keyframes[check_idx]
                candidate_desc = candidate.get("description", "")
                if not has_person_fn(candidate_desc):
                    console.print(f"    [dim]原帧不符合安全条件，已替换为相邻的安全帧 (偏移 {offset})[/]")
                    return candidate.get("path", original_path), candidate_desc
                    
        return original_path, original_desc

    def _blackout_faces(self, img_path: str, output_path: str) -> bool:
        """使用 OpenCV 检测正脸并将五官部分抹黑"""
        try:
            import cv2
            import os
            
            img = cv2.imread(img_path)
            if img is None:
                return False
                
            cascade_path = os.path.join(cv2.data.haarcascades, 'haarcascade_frontalface_default.xml')
            face_cascade = cv2.CascadeClassifier(cascade_path)
            
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
            
            if len(faces) == 0:
                return False
                
            for (x, y, w, h) in faces:
                cv2.rectangle(img, (x, y), (x+w, y+h), (0, 0, 0), -1)
                
            cv2.imwrite(output_path, img)
            return True
        except Exception as exc:
            logger.warning(f"本地面部涂黑失败: {exc}")
            return False

    def _clean_image_if_needed(self, img_path: str, desc: str, output_dir: Path, suffix: str, force_remove_person: bool = False, mode: str = "auto") -> str:
        """如果描述中包含文字或需要移除人物，则处理图片（智能模式下仅使用本地涂黑不调大模型）"""
        if not desc or not img_path:
            return img_path
            
        desc_lower = desc.lower()
        has_text_or_logo = any(kw in desc_lower for kw in ["文字", "字幕", "水印", "logo", "图标", "角标", "台标", "标志"]) and not any(kw in desc_lower for kw in ["无文字", "无字幕", "没有文字", "没有字幕", "无水印", "无logo"])
        
        if not has_text_or_logo and not force_remove_person:
            return img_path
            
        if mode == "auto":
            if force_remove_person:
                console.print(f"    [dim]智能模式：检测到正脸，尝试本地程序涂黑...[/]")
                blackout_path = output_dir / f"{Path(img_path).stem}_blackout_{suffix}.png"
                if self._blackout_faces(str(img_path), str(blackout_path)):
                    console.print(f"    [green]v 成功通过程序涂黑正脸: {blackout_path.name}[/]")
                    return str(blackout_path)
                else:
                    console.print(f"    [dim]未检测到人脸框，直接使用原图[/]")
            return img_path # 智能模式下，不用大模型生成图片
            
        prompt_parts = []
        if has_text_or_logo:
            prompt_parts.append("Remove all text, captions, watermarks, and corner LOGOs from the image.")
        if force_remove_person:
            prompt_parts.append("Remove all humans, people, faces, and body parts from the image completely. Leave ONLY the background environment, landscape, and objects. The final image must have ZERO people.")
            
        prompt_parts.append("Keep the original background, environment, and objects exactly the same. High quality, clear, realistic.")
        prompt = " ".join(prompt_parts)
        
        reason = "文字/LOGO" if has_text_or_logo and not force_remove_person else "文字/LOGO与人物" if has_text_or_logo else "移除人物"
        logger.info(f"检测到图片需要处理 ({reason})，正在尝试处理: {img_path}")
        console.print(f"    [dim]检测到图片需要处理 ({reason})，正在尝试处理: {Path(img_path).name}...[/]")
        
        for attempt in range(2):
            try:
                pic_url = self.cos.upload_file(img_path)
                api_task_id = self.media_api.generate_image(
                    prompt=prompt,
                    model=self.settings.default_image_model,
                    size=self.settings.default_size,
                    aspect_ratio=self.settings.default_aspect_ratio,
                    pic=pic_url,
                )
                result = self.media_api.poll_image(api_task_id)
                if result.get("status") == "success" and result.get("url"):
                    clean_path = output_dir / f"{Path(img_path).stem}_clean_{suffix}_{attempt}.png"
                    self.media_api.download_media(result["url"], clean_path)
                    console.print(f"    [green]v 成功处理图片 ({reason}): {clean_path.name}[/]")
                    return str(clean_path)
                else:
                    raise RuntimeError(f"图片处理任务返回非成功状态或没有 URL: {result}")
            except Exception as exc:
                logger.warning(f"第 {attempt + 1} 次处理图片失败: {exc}")
                console.print(f"    [yellow]! 第 {attempt + 1} 次处理图片失败: {exc}[/]")
                if attempt < 1:
                    import time
                    time.sleep(2)
                    console.print(f"    [dim]正在重试处理图片...[/]")
                
        console.print(f"    [yellow]! 多次重试处理图片失败，将使用原图[/]")
        return img_path

    def _generate_scene_image(
        self,
        scene: dict[str, Any],
        output_dir: Path,
        ref_pic: str | None = None,
    ) -> str:
        """为单个场景生成首帧图片，返回本地路径

        Args:
            ref_pic: 锚定人物参考图 URL（scene_001 上传后的 COS URL），
                     用于后续场景确保人物一致。为 None 时使用原视频参考帧。
        """
        image_prompt = scene.get("image_prompt", "")
        if not image_prompt:
            raise RuntimeError(f"场景 {scene['scene_id']} 缺少 image_prompt")

        scene_id = scene["scene_id"]

        # 优先使用锚定人物图（ref_pic），没有时回退到原视频参考帧
        pic = ref_pic
        if not pic:
            ref_frame = scene.get("reference_frame", "")
            if ref_frame and Path(ref_frame).exists():
                # 原视频帧也需要上传到 COS 才能被 API 访问
                try:
                    pic = self.cos.upload_file(ref_frame)
                    logger.info("原视频帧上传: %s → %s", ref_frame, pic[:80])
                except Exception as exc:
                    logger.warning("原视频帧上传失败: %s", exc)
                    pic = None

        console.print(
            f"    [cyan]生成图片 scene_{scene_id:03d}[/] | ref: "
            f"{'anchor_url' if ref_pic else ('original_frame' if pic else 'none')}"
        )
        console.print(f"    [dim]image_prompt:[/]")
        console.print(f"    {image_prompt}")
        logger.info("生成图片 scene_%03d | pic=%s", scene_id, pic[:100] if pic else "NONE")
        logger.info("image_prompt:\n%s", image_prompt)

        api_task_id = self.media_api.generate_image(
            prompt=image_prompt,
            model=self.settings.default_image_model,
            size=self.settings.default_size,
            aspect_ratio=self.settings.default_aspect_ratio,
            pic=pic,
        )
        result = self.media_api.poll_image(api_task_id)

        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"图片生成失败: {result}")

        img_path = output_dir / f"scene_{scene_id:03d}.png"
        self.media_api.download_media(result["url"], img_path)
        return str(img_path)

    def _generate_scene_clip(
        self,
        scene: dict[str, Any],
        output_dir: Path,
    ) -> str:
        """为单个场景生成视频片段，返回本地路径

        视频生成 API 的 pic 参数必须是公网 URL（不能是本地路径）。
        这里直接使用已上传好的首尾帧 URL。
        """
        video_prompt = scene.get("video_prompt", "")
        if not video_prompt:
            raise RuntimeError(f"场景 {scene['scene_id']} 缺少 video_prompt")

        scene_id = scene["scene_id"]
        # 使用场景自身的时长（auto 模式下每个场景可能不同）
        # videoType: "1" = 15秒模式, "0" = 其他时长
        scene_dur = int(scene.get("duration", 15))
        duration = str(scene_dur)
        video_type = "1" if scene_dur == 15 else "0"

        pic_url = scene.get("start_pic_url")
        end_pic_url = scene.get("end_pic_url")

        console.print(
            f"    [cyan]生成视频 scene_{scene_id:03d}[/] | start: "
            f"{'YES' if pic_url else 'NONE'} | end: {'YES' if end_pic_url else 'NONE'}"
        )
        console.print(f"    [yellow]start_pic_url:[/] {pic_url}")
        if end_pic_url:
            console.print(f"    [yellow]end_pic_url:[/] {end_pic_url}")
        console.print(f"    [dim]video_prompt:[/]")
        console.print(f"    {video_prompt}")
        logger.info("生成视频 scene_%03d | pic=%s, end_pic=%s", scene_id, pic_url[:100] if pic_url else "NONE", end_pic_url[:100] if end_pic_url else "NONE")
        logger.info("video_prompt:\n%s", video_prompt)

        api_task_id = self.media_api.generate_video(
            prompt=video_prompt,
            model=self.settings.default_video_model,
            size=self.settings.default_size,
            duration=duration,
            aspect_ratio=self.settings.default_aspect_ratio,
            pic=pic_url,
            end_pic=end_pic_url,
            video_type=video_type,
        )
        result = self.media_api.poll_video(api_task_id)

        if result.get("status") != "success" or not result.get("url"):
            raise RuntimeError(f"视频生成失败: {result}")

        clip_path = output_dir / f"scene_{scene_id:03d}.mp4"
        self.media_api.download_media(result["url"], clip_path)
        return str(clip_path)

    # ── 辅助 ──────────────────────────────────────────────────

    def _upload_anchor_image(self, local_path: str) -> str:
        """将 scene_001 的图片上传到 COS，返回公网 URL

        这个 URL 会被后续所有场景作为 pic 参数传入图片/视频生成 API，
        确保所有场景中的人物面部特征与 scene_001 一致。
        """
        return self.cos.upload_file(local_path)

    def get_status(self) -> dict[str, Any]:
        return self.project_mgr.get_stats()
