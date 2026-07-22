"""30天内容规划执行管线

流程:
  1. 用户定义人设 → 创建规划项目
  2. LLM 生成 30 天日历（4 大板块、7 天小周期）
  3. 逐天生成详细脚本（标题/封面/镜头/台词/品牌推荐）
  4. 逐天生成视频 prompt（用于 seedance-2.0-fast）
  5. （可选）逐天生成封面图 + 视频
  6. 汇总爆款 TOP10 + 高频品牌清单
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from config import Settings, get_settings
from core.content_planner import ContentPlanner, Persona
from core.cos_client import COSClient
from core.llm_client import HumanizerClient, LLMClient
from core.media_api import MediaAPIClient
from core.plan_manager import PlanManager

logger = logging.getLogger(__name__)
console = Console()


class PlannerPipeline:
    """30天内容规划全流程编排器"""

    def __init__(self, settings: Settings | None = None, plan_mgr: PlanManager | None = None) -> None:
        self.settings = settings or get_settings()
        s = self.settings

        self.llm = LLMClient(
            api_key=s.al_api_key or s.qwen_api_key,
            base_url=s.al_baseurl or s.qwen_baseurl,
            model=s.al_model or s.qwen_model,
        )
        # 改稿客户端(minimax,anthropic 兼容):图文脚本生成后把 caption 真人化,降低 AI 味。
        # 未配置(humanize_api_key/base_url 为空)时 is_configured()=False,改稿自动跳过。
        self.humanizer = HumanizerClient(
            api_key=s.humanize_api_key,
            base_url=s.humanize_base_url,
            model=s.humanize_model,
        )
        self.plan_mgr = plan_mgr or PlanManager(s.data_dir, s.projects_dir)
        # 把 plan_mgr 作为 feedback_mgr 注入 ContentPlanner，启用 few-shot 闭环：
        # generate_video_prompt 会从 video_feedback 表拉同 track 的 positive 样本。
        # 数据不足时 (<3 条) 自动跳过，向后兼容。
        self.planner = ContentPlanner(
            self.llm,
            feedback_mgr=self.plan_mgr,
            humanizer=self.humanizer,
        )

        # 视频生成客户端（可选，按需初始化）
        self._media_api: MediaAPIClient | None = None
        self._cos: COSClient | None = None

    def _ensure_media_clients(self) -> None:
        """延迟初始化媒体 API 客户端（仅在需要生成视频时）"""
        if self._media_api is None:
            s = self.settings
            self._media_api = MediaAPIClient(
                api_key=s.gkapi_key,
                base_url=s.gkapi_baseurl,
                poll_interval=s.poll_interval,
                max_poll_attempts=s.max_poll_attempts,
            )
        if self._cos is None:
            s = self.settings
            self._cos = COSClient(
                secret_id=s.secret_id,
                secret_key=s.secret_key,
                region=s.region,
                bucket=s.bucket,
                base_url=s.cos_url,
            )

    # ── 主流程 ────────────────────────────────────────────────

    def run(
        self,
        persona: Persona,
        content_tracks: dict[str, str] | None = None,
        generate_videos: bool = False,
        resume_plan_id: str | None = None,
    ) -> dict[str, Any]:
        """执行完整的 30 天内容规划流程

        Args:
            persona: 完整人设
            content_tracks: 自定义内容板块（None 使用默认）
            generate_videos: 是否生成视频（默认仅生成脚本）
            resume_plan_id: 恢复已有规划项目，跳过已完成步骤

        Returns:
            规划项目字典
        """
        if resume_plan_id:
            existing = self.plan_mgr.get_plan(resume_plan_id)
            if not existing:
                raise ValueError(f"找不到规划项目 {resume_plan_id}")
            plan_id = resume_plan_id
            output_dir = Path(existing["output_dir"])
            calendar = existing.get("calendar", [])
            scripts = list(existing.get("scripts", []))
            summary = (existing.get("summary") or {}).get("calendar_summary", {})

            console.print(
                f"\n[bold cyan]>> 恢复30天内容规划:[/] "
                f"[yellow]{persona.occupation}[/] (项目: {plan_id})"
            )
            console.print(
                f"  已有: {len(calendar)} 日历, {len(scripts)} 脚本"
            )
        else:
            # Step 1: 创建项目
            plan = self.plan_mgr.create_plan(
                persona.to_dict(),
                content_tracks,
            )
            plan_id = plan["plan_id"]
            output_dir = Path(plan["output_dir"])
            calendar = []
            scripts = []
            summary = {}

            console.print(
                f"\n[bold cyan]>> 30天内容规划:[/] "
                f"[yellow]{persona.occupation}[/] (项目: {plan_id})"
            )

        try:
            # Step 1.5: 生成人设专属肖像 (Portrait)
            portrait_url = plan.get("portrait_image_url")
            if not portrait_url and persona.portrait_description:
                console.print("  [dim]Step 1.5: 生成人设专属肖像...[/]")
                try:
                    self._ensure_media_clients()
                    image_prompt = (
                        f"Vertical 9:16 social media portrait. "
                        f"Character: {persona.portrait_description}. "
                        f"Style: Eye-catching, professional, high contrast. "
                        f"American social media aesthetic."
                    )
                    api_task_id = self._media_api.generate_image(
                        prompt=image_prompt,
                        model=self.settings.default_image_model,
                        size=self.settings.default_size,
                        aspect_ratio=self.settings.default_aspect_ratio,
                    )
                    img_result = self._media_api.poll_image(api_task_id)
                    if img_result.get("status") == "success" and img_result.get("url"):
                        portrait_url = img_result["url"]
                        self.plan_mgr.update_plan(plan_id, portrait_image_url=portrait_url)
                        img_path = output_dir / "persona_portrait.png"
                        self._media_api.download_media(portrait_url, img_path)
                        self.plan_mgr.update_plan(plan_id, portrait_image_file=str(img_path))
                        console.print("  [green]v 人设肖像已生成[/]")
                except Exception as e:
                    console.print(f"  [yellow]! 肖像生成失败: {e}[/]")

            # Step 2: 生成日历（如果尚未生成）
            if not calendar:
                console.print("  [dim]Step 2/4: 生成30天内容日历...[/]")
                self.plan_mgr.update_plan(plan_id, status="generating_calendar")

                calendar_data = self.planner.generate_calendar(persona, content_tracks)
                calendar = calendar_data.get("calendar", [])
                summary = calendar_data.get("summary", {})

                if not calendar:
                    raise RuntimeError("日历生成失败，未返回任何内容")

                self.plan_mgr.update_plan(
                    plan_id,
                    calendar=calendar,
                    summary=summary,
                    status="generating_scripts",
                )
                console.print(f"  [green]v 日历生成完成: {len(calendar)} 条视频[/]")
            else:
                console.print(f"  [green]v 日历已存在: {len(calendar)} 条[/]")

            # Step 3: 逐天生成详细脚本（跳过已有脚本的日期）
            existing_days = {s.get("day") for s in scripts}
            remaining = [
                e for e in calendar
                if e.get("day") not in existing_days
            ]

            if remaining:
                console.print(
                    f"  [dim]Step 2/4: 生成详细脚本 "
                    f"({len(scripts)}/{len(calendar)} 已有, "
                    f"还需 {len(remaining)} 条)...[/]"
                )
            else:
                console.print(f"  [green]v 脚本已全部完成: {len(scripts)} 条[/]")

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("脚本生成", total=len(calendar))
                progress.update(task, completed=len(scripts))

                for entry in remaining:
                    day = entry.get("day", 0)
                    progress.update(
                        task,
                        description=f"Day {day}/{len(calendar)}",
                    )

                    try:
                        script = self.planner.generate_script(persona, entry)
                        if script:
                            scripts.append(script)
                            self.plan_mgr.update_plan(
                                plan_id,
                                scripts=list(scripts),
                            )
                    except Exception as exc:
                        # 单天脚本生成失败(含改稿/解析失败)不中断整体流程,跳过继续下一天
                        logger.warning("Day %s 脚本生成异常(已跳过): %s", day, exc)
                        console.print(f"  [yellow]! Day {day} 脚本失败,已跳过继续[/]")

                    progress.update(task, advance=1)

            console.print(f"  [green]v 脚本生成完成: {len(scripts)} 条[/]")

            # Step 4: 汇总
            console.print("  [dim]Step 3/4: 汇总爆款TOP10与品牌清单...[/]")
            final_summary = self.planner.generate_summary(calendar, scripts)
            # 合并日历元数据中的 summary
            final_summary["calendar_summary"] = summary

            self.plan_mgr.update_plan(
                plan_id,
                summary=final_summary,
                status="generating_videos" if generate_videos else "done",
            )
            console.print(f"  [green]v 汇总完成[/]")

            if ContentPlanner._is_text_platform(persona):
                # 图文平台（reddit/Twitter/FB）：规划阶段只生成图文脚本（含预生成的 image_prompt），
                # 不生成真实配图。真实配图由 Web 端手动触发 generate_images_for_day。
                console.print("  [dim](图文平台：配图请到 Web 端手动生成)[/]")
                self.plan_mgr.update_plan(plan_id, status="done")
            elif not generate_videos:
                console.print("  [dim](跳过视频生成)[/]")
                self.plan_mgr.update_plan(plan_id, status="done")
            else:
                # Step 5: 逐天生成视频 prompt + 封面图 + 视频
                console.print("  [dim]Step 4/4: 生成视频 prompt 和视频...[/]")
                self._generate_all_videos(plan_id, persona, scripts, output_dir)
                self.plan_mgr.update_plan(plan_id, status="done")

            # 导出 JSON 文件
            self._export_plan_json(plan_id, output_dir)

            console.print(
                f"\n  [bold green]v 30天内容规划完成![/] "
                f"{len(scripts)} 条脚本"
            )

        except Exception as exc:
            logger.error("规划流程失败: %s", exc, exc_info=True)
            console.print(f"  [red]x 规划失败: {exc}[/]")
            self.plan_mgr.update_plan(plan_id, status="failed", error=str(exc))
            raise

        return self.plan_mgr.get_plan(plan_id) or {}

    def generate_video_for_day(self, plan_id: str, day: int) -> dict[str, Any]:
        """为特定的一天生成视频"""
        plan = self.plan_mgr.get_plan(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")
        
        persona_data = plan.get("persona", {})
        persona = Persona.from_dict(persona_data)
        scripts = plan.get("scripts", [])
        
        target_script = next((s for s in scripts if s.get("day") == day), None)
        if not target_script:
            raise ValueError(f"Script for day {day} not found")

        output_dir = Path(plan["output_dir"])
        self._generate_all_videos(plan_id, persona, [target_script], output_dir)
        
        # update plan and return the updated script
        updated_plan = self.plan_mgr.get_plan(plan_id)
        updated_script = next((s for s in updated_plan.get("scripts", []) if s.get("day") == day), None)
        return updated_script or {}

    def generate_images_for_day(self, plan_id: str, day: int) -> dict[str, Any]:
        """为特定的一天生成配图（图文平台，由 Web 端手动触发）

        读取该天的图文脚本，为每个 frame 调用图片生成 API，下载到
        output_dir/images/day_XX_frame_N.png，并更新 script 的 image_file/image_status。

        Args:
            plan_id: 规划项目 ID
            day: 日期 (1-30)

        Returns:
            更新后的 script（含每个 frame 的 image_file / image_status）
        """
        plan = self.plan_mgr.get_plan(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")

        scripts = plan.get("scripts", [])
        target_script = next((s for s in scripts if s.get("day") == day), None)
        if not target_script:
            raise ValueError(f"Script for day {day} not found")

        frames = target_script.get("frames") or []
        if not frames:
            raise ValueError(f"Day {day} 不是图文脚本（无 frames），无法生成配图")

        output_dir = Path(plan["output_dir"])
        images_dir = output_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        self._ensure_media_clients()
        assert self._media_api is not None

        # 参考图机制: 第一张图正常生成, 后续图都拿第一张的 URL 作为 pic 参考,
        # 让 Nano Banana Pro 锁定人物/宠物/环境的外观一致性(纯文字 prompt 无法保证跨图一致)
        reference_url = None
        for idx, frame in enumerate(frames):
            fid = frame.get("frame_id", idx + 1)
            # 优先用预生成的 image_prompt；缺失时回退到 image_description
            image_prompt = frame.get("image_prompt") or frame.get("image_description", "")
            if not image_prompt:
                logger.warning("Day %d frame %s 无 image_prompt，跳过", day, fid)
                frame["image_status"] = "failed"
                continue

            console.print(f"  [dim]Day {day} frame {fid} 配图生成中...[/]")
            try:
                api_task_id = self._media_api.generate_image(
                    prompt=image_prompt,
                    model=self.settings.default_image_model,
                    size=self.settings.default_size,
                    aspect_ratio=self.settings.default_aspect_ratio,
                    pic=reference_url,
                )
                img_result = self._media_api.poll_image(api_task_id)
                if img_result.get("status") == "success" and img_result.get("url"):
                    img_url = img_result["url"]
                    img_path = images_dir / f"day_{day:02d}_frame_{fid}.png"
                    self._media_api.download_media(img_url, img_path)
                    frame["image_file"] = str(img_path)
                    frame["image_status"] = "done"
                    # 链式参考: 每张图都成为下一张的参考图
                    # (比"所有图都参考第一张"更好: 环境/器物连贯渐变, 适合做菜步骤等连续场景,
                    #  同一厨房台面只是食材状态变化, 而不是每张都从第一张重新发散)
                    reference_url = img_url
                    console.print(f"  [green]v Day {day} frame {fid} 完成[/]")
                else:
                    frame["image_status"] = "failed"
                    console.print(f"  [yellow]! Day {day} frame {fid} 生成失败[/]")
            except Exception as exc:
                logger.warning("Day %d frame %s 配图生成失败: %s", day, fid, exc)
                frame["image_status"] = "failed"
                console.print(f"  [yellow]! Day {day} frame {fid} 失败: {exc}[/]")

            # 每个 frame 完成后立即落库，避免中途失败丢失已生成的配图
            self.plan_mgr.update_day_script(plan_id, day, target_script)

        return target_script

    def regenerate_script_for_day(self, plan_id: str, day: int) -> dict[str, Any]:
        """重新生成特定某一天的脚本"""
        plan = self.plan_mgr.get_plan(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")
        
        persona_data = plan.get("persona", {})
        persona = Persona.from_dict(persona_data)
        calendar = plan.get("calendar", [])
        scripts = plan.get("scripts", [])

        # 找到日历条目
        target_entry = next((e for e in calendar if e.get("day") == day), None)
        if not target_entry:
            raise ValueError(f"Calendar entry for day {day} not found")

        # 调用 LLM 重新生成
        new_script = self.planner.generate_script(persona, target_entry)
        if not new_script:
            raise RuntimeError("LLM script generation failed")

        # 替换原有的 script
        updated_scripts = [s for s in scripts if s.get("day") != day]
        updated_scripts.append(new_script)
        # 按照 day 排序
        updated_scripts.sort(key=lambda x: x.get("day", 0))

        self.plan_mgr.update_plan(plan_id, scripts=updated_scripts)
        
        # 更新 JSON
        output_dir = Path(plan["output_dir"])
        self._export_plan_json(plan_id, output_dir)
        
        return new_script

    def regenerate_all_for_day(self, plan_id: str, day: int) -> dict[str, Any]:
        """完全重新生成特定某一天的数据（日历和脚本）"""
        plan = self.plan_mgr.get_plan(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")
        
        persona_data = plan.get("persona", {})
        persona = Persona.from_dict(persona_data)
        calendar = plan.get("calendar", [])
        scripts = plan.get("scripts", [])

        # 重新生成日历条目
        new_entry = self.planner.generate_single_calendar_entry(persona, day, calendar)
        if not new_entry:
            raise RuntimeError("LLM calendar generation failed")

        # 替换原有的日历条目
        updated_calendar = [e for e in calendar if e.get("day") != day]
        updated_calendar.append(new_entry)
        updated_calendar.sort(key=lambda x: x.get("day", 0))

        # 根据新日历条目生成脚本
        new_script = self.planner.generate_script(persona, new_entry)
        if not new_script:
            raise RuntimeError("LLM script generation failed")

        # 替换原有的脚本
        updated_scripts = [s for s in scripts if s.get("day") != day]
        updated_scripts.append(new_script)
        updated_scripts.sort(key=lambda x: x.get("day", 0))

        # 重新汇总
        summary = self.planner.generate_summary(updated_calendar, updated_scripts)
        if "summary" in plan and "calendar_summary" in plan["summary"]:
            summary["calendar_summary"] = plan["summary"]["calendar_summary"]

        self.plan_mgr.update_plan(
            plan_id, 
            calendar=updated_calendar, 
            scripts=updated_scripts,
            summary=summary
        )
        
        # 更新 JSON
        output_dir = Path(plan["output_dir"])
        self._export_plan_json(plan_id, output_dir)
        
        return new_script

    def regenerate_all_scripts(self, plan_id: str) -> dict[str, Any]:
        """重新生成全部 30 天的脚本(保留日历不变,只重做 scripts)

        清空现有 scripts,遍历 calendar 每天调 generate_script(含 image_prompt/
        改稿/简化等完整管线)。每生成一个就落库,避免中途失败全丢。
        """
        plan = self.plan_mgr.get_plan(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")

        persona = Persona.from_dict(plan.get("persona", {}))
        calendar = plan.get("calendar", [])
        if not calendar:
            raise RuntimeError("日历为空,无法生成脚本")

        console.print(
            f"\n[bold cyan]>> 重新生成全部脚本:[/] {len(calendar)} 天 (项目: {plan_id})"
        )

        scripts: list[dict[str, Any]] = []
        total = len(calendar)
        for i, entry in enumerate(calendar, 1):
            day = entry.get("day", 0)
            console.print(f"  [dim]({i}/{total}) Day {day} 脚本生成中...[/]")
            try:
                script = self.planner.generate_script(persona, entry)
                if script:
                    scripts.append(script)
                    # 每天落库,中途失败也不丢已生成的
                    self.plan_mgr.update_plan(plan_id, scripts=list(scripts))
            except Exception as exc:
                logger.warning("Day %d 脚本生成失败(跳过): %s", day, exc)

        # 重新汇总
        summary = self.planner.generate_summary(calendar, scripts)
        if "summary" in plan and "calendar_summary" in plan["summary"]:
            summary["calendar_summary"] = plan["summary"]["calendar_summary"]

        self.plan_mgr.update_plan(
            plan_id, scripts=scripts, summary=summary, status="done"
        )

        # 导出
        output_dir = Path(plan["output_dir"])
        self._export_plan_json(plan_id, output_dir)

        console.print(f"  [green]v 全部脚本重新生成完成: {len(scripts)}/{total} 条[/]")
        return {"status": "done", "scripts_count": len(scripts), "total": total}

    # ── 视频生成（可选） ──────────────────────────────────────

    def _generate_all_videos(
        self,
        plan_id: str,
        persona: Persona,
        scripts: list[dict[str, Any]],
        output_dir: Path,
    ) -> None:
        """逐天生成视频 prompt + 封面图 + 视频"""
        self._ensure_media_clients()
        assert self._media_api is not None
        assert self._cos is not None

        videos_dir = output_dir / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)

        for script in scripts:
            day = script.get("day", 0)
            console.print(f"  [dim]正在处理 Day {day} 视频生成...[/]")

            try:
                # 获取已生成的视频 prompt，如果不存在则临时生成
                video_prompt = script.get("video_prompt")
                if not video_prompt:
                    video_prompt = self.planner.generate_video_prompt(
                        persona, script
                    )
                    script["video_prompt"] = video_prompt

                # 获取计划级别的人设肖像
                pic_url = None
                p = self.plan_mgr.get_plan(plan_id)
                if p:
                    pic_url = p.get("portrait_image_url")

                if video_prompt:
                    api_task_id = self._media_api.generate_video(
                        prompt=video_prompt,
                        model=self.settings.default_video_model,
                        size=self.settings.default_size,
                        duration="15",
                        aspect_ratio=self.settings.default_aspect_ratio,
                        video_type="1",
                        pic=pic_url,
                    )
                    vid_result = self._media_api.poll_video(api_task_id)
                    if vid_result.get("status") == "success" and vid_result.get("url"):
                        vid_path = videos_dir / f"day_{day:02d}.mp4"
                        self._media_api.download_media(
                            vid_result["url"], vid_path
                        )
                        script["video_file"] = str(vid_path)
                        script["video_status"] = "done"
                    else:
                        script["video_status"] = "failed"

                self.plan_mgr.update_day_script(plan_id, day, script)

            except Exception as exc:
                logger.warning("Day %d 视频/图片生成失败: %s", day, exc)
                console.print(
                    f"    [yellow]! Day {day} 生成失败: {exc}[/]"
                )
                script["video_status"] = "failed"
                self.plan_mgr.update_day_script(plan_id, day, script)

        console.print(f"  [green]v 视频生成批次处理完毕[/]")

    # ── 导出 ──────────────────────────────────────────────────

    def _export_plan_json(self, plan_id: str, output_dir: Path) -> None:
        """导出完整规划 JSON 文件到项目目录"""
        plan = self.plan_mgr.get_plan(plan_id)
        if not plan:
            return

        export_path = output_dir / "plan_export.json"
        export_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("规划已导出: %s", export_path)
