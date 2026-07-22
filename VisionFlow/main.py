"""VisionFlow - 视频复刻工具 CLI"""

from __future__ import annotations

import io
import logging
import os
import sys
from pathlib import Path

# Windows 终端 UTF-8 兼容
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import get_settings

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    log_dir = _PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)

    fh = logging.FileHandler(log_dir / "visionflow.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING if not verbose else logging.DEBUG)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(fh)
    root.addHandler(ch)


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="显示详细日志")
def cli(verbose: bool) -> None:
    """VisionFlow - 视频复刻工具"""
    _setup_logging(verbose)


# ── replicate ─────────────────────────────────────────────────

@cli.command()
@click.argument("video_path", type=click.Path(exists=True), required=False)
@click.option("--resume", type=str, help="基于已有项目 ID 恢复并继续运行")
@click.option("--mode", type=click.Choice(["fixed", "auto"]), default="fixed",
              help="分镜模式: fixed=固定3×15s, auto=智能分镜(自动决定分镜数和时长)")
def replicate(video_path: str | None, resume: str | None, mode: str) -> None:
    """复刻一个视频文件

    VIDEO_PATH: 源视频文件路径

    --mode fixed: 固定 3 个分镜 × 15 秒 (默认)
    --mode auto: 智能分析原视频，自动决定分镜数(2-5)和每个分镜时长(4-15s)
    """
    from workflow.pipeline import ReplicationPipeline

    settings = get_settings()
    pipeline = ReplicationPipeline(settings)
    try:
        result = pipeline.run(video_path, mode=mode, resume_project_id=resume)
    except Exception as e:
        console.print(f"[red]错误: {e}[/]")
        return

    if result.get("status") == "done":
        clips = result.get("final_clips", [])
        console.print(Panel(
            f"[green]复刻完成[/]\n"
            f"项目 ID: {result.get('project_id', '')}\n"
            f"分镜数量: {len(result.get('storyboard', []))}\n"
            f"生成视频: {len(clips)} 个\n\n"
            + "\n".join(f"  {c}" for c in clips),
            title="[bold]完成[/]",
            border_style="green",
        ))
    else:
        console.print(Panel(
            f"[red]复刻失败[/]\n{result.get('error', '未知错误')}",
            title="失败",
            border_style="red",
        ))


# ── projects ──────────────────────────────────────────────────

@cli.command("projects")
def list_projects() -> None:
    """列出所有复刻项目"""
    from core.project_manager import ProjectManager

    settings = get_settings()
    mgr = ProjectManager(settings.data_dir, settings.projects_dir)
    projects = mgr.list_projects()

    if not projects:
        console.print("[yellow]暂无项目[/]")
        return

    table = Table(title="复刻项目", show_lines=False)
    table.add_column("ID", style="cyan", width=10)
    table.add_column("状态", width=18)
    table.add_column("源视频", width=30)
    table.add_column("分镜", width=6, justify="right")
    table.add_column("时长", width=8, justify="right")
    table.add_column("创建时间", width=19)

    status_styles = {
        "uploaded": "[blue]", "analyzing": "[yellow]", "storyboarding": "[yellow]",
        "generating_images": "[yellow]", "generating_clips": "[yellow]",
        "done": "[green]", "failed": "[red]",
    }
    for p in reversed(projects):
        s = status_styles.get(p.get("status", ""), "")
        storyboard = p.get("storyboard", [])
        clips_count = sum(1 for s in storyboard if s.get("clip_status") == "done")
        table.add_row(
            p.get("project_id", ""),
            f"{s}{p.get('status', '')}[/]",
            Path(p.get("source_video", "")).name[:30],
            f"{clips_count}/{len(storyboard)}",
            f"{p.get('video_duration', 0):.1f}s",
            p.get("created_at", "")[:19],
        )
    console.print(table)


# ── plans ─────────────────────────────────────────────────────

@cli.command("plans")
def list_plans() -> None:
    """列出所有内容规划项目"""
    from core.plan_manager import PlanManager

    settings = get_settings()
    mgr = PlanManager(settings.data_dir, settings.projects_dir)
    plans = mgr.list_plans()

    if not plans:
        console.print("[yellow]暂无规划项目[/]")
        return

    table = Table(title="内容规划项目", show_lines=False)
    table.add_column("ID", style="cyan", width=10)
    table.add_column("状态", width=18)
    table.add_column("职业", width=25)
    table.add_column("脚本数", width=8, justify="right")
    table.add_column("创建时间", width=19)

    status_styles = {
        "created": "[blue]", "generating_calendar": "[yellow]", "generating_scripts": "[yellow]",
        "generating_videos": "[yellow]",
        "done": "[green]", "failed": "[red]",
    }
    # list_plans() 默认已经按创建时间降序（或者逆序）返回，这里为了展示最新的在上面可能不再需要 reverse()。
    # 根据 core/plan_manager.py 中 list_plans() 返回的是较早的项目在前面，我们 reverse 以显示最新。
    for p in reversed(plans):
        s = status_styles.get(p.get("status", ""), "")
        scripts = p.get("scripts", [])
        persona = p.get("persona", {})
        occupation = persona.get("occupation", "未知")
        table.add_row(
            p.get("plan_id", ""),
            f"{s}{p.get('status', '')}[/]",
            occupation[:25],
            str(len(scripts)),
            p.get("created_at", "")[:19],
        )
    console.print(table)


# ── status ────────────────────────────────────────────────────

@cli.command()
def status() -> None:
    """查看项目统计"""
    from core.project_manager import ProjectManager

    settings = get_settings()
    mgr = ProjectManager(settings.data_dir, settings.projects_dir)
    stats = mgr.get_stats()

    console.print(Panel(
        f"总项目数: [bold]{stats['total']}[/]\n"
        + "\n".join(f"  {k}: [green]{v}[/]" for k, v in stats["by_status"].items()),
        title="[bold]项目统计[/]",
        border_style="cyan",
    ))


# ── web ───────────────────────────────────────────────────────

@cli.command()
@click.option("--host", default="0.0.0.0", help="监听地址")
@click.option("--port", default=8000, type=int, help="监听端口")
@click.option("--reload", is_flag=True, help="开发模式 (auto-reload)")
def web(host: str, port: int, reload: bool) -> None:
    """启动 Web 管理界面"""
    import uvicorn
    console.print(Panel(
        f"[bold green]VisionFlow 视频复刻工具[/]\n\n"
        f"URL: [cyan]http://localhost:{port}[/]\n"
        f"Host: {host}\n"
        f"Reload: {reload}",
        title="Starting Server",
        border_style="green",
    ))
    if reload:
        uvicorn.run("web.app:app", host=host, port=port, reload=True)
    else:
        from web.app import app
        uvicorn.run(app, host=host, port=port)


# ── plan ──────────────────────────────────────────────────────

@cli.command()
@click.option("--age", type=int, default=28, help="年龄")
@click.option("--gender", default="female", help="性别")
@click.option("--ethnicity", default="Indian American", help="种族/背景")
@click.option("--location", default="California, USA", help="所在地")
@click.option("--occupation", default="luxury real estate agent", help="职业")
@click.option("--language", default="English", help="语言")
@click.option("--accent", default="slight Indian accent", help="口音")
@click.option("--tags", default="", help="个人标签（逗号分隔）")
@click.option("--audience", default="", help="目标受众")
@click.option("--personality", default="", help="性格特点")
@click.option("--style", default="", help="内容风格")
@click.option("--portrait", default="", help="肖像描述（用于视频生成）")
@click.option("--extra", default="", help="额外信息")
@click.option("--videos", is_flag=True, help="同时生成视频（耗时较长）")
@click.option("--platform", default="TK",
              help="目标平台: TK/YT(视频) | reddit/Twitter/FB(图文帖子)")
@click.option("--resume", type=str, default="", help="恢复已有规划项目 ID")
def plan(
    age: int,
    gender: str,
    ethnicity: str,
    location: str,
    occupation: str,
    language: str,
    accent: str,
    tags: str,
    audience: str,
    personality: str,
    style: str,
    portrait: str,
    extra: str,
    videos: bool,
    platform: str,
    resume: str,
) -> None:
    """生成30天内容规划

    基于自定义人设，自动规划30天全套短视频脚本。
    所有参数都有默认值，也可以自定义覆盖。

    示例:
      python main.py plan --occupation "fitness coach" --age 30 --gender male
      python main.py plan --occupation "chef" --portrait "30yo Asian man, short hair" --videos
      python main.py plan --resume c951ad8c --occupation "luxury real estate agent"
    """
    from core.content_planner import Persona
    from workflow.planner_pipeline import PlannerPipeline

    persona = Persona(
        age=age,
        gender=gender,
        ethnicity=ethnicity,
        location=location,
        language=language,
        accent=accent,
        occupation=occupation,
        personal_tags=tags,
        target_audience=audience,
        personality=personality,
        content_style=style,
        portrait_description=portrait,
        extra_info=extra,
        platform=platform,
    )

    console.print(Panel(
        f"[bold]人设[/]\n{persona.to_prompt_text()}",
        title="[bold cyan]30天内容规划[/]",
        border_style="cyan",
    ))

    pipe = PlannerPipeline()
    try:
        result = pipe.run(persona, generate_videos=videos, resume_plan_id=resume or None)
    except Exception as e:
        console.print(f"[red]错误: {e}[/]")
        return

    if result.get("status") == "done":
        scripts = result.get("scripts", [])
        summary = result.get("summary", {})
        console.print(Panel(
            f"[green]规划完成[/]\n"
            f"规划 ID: {result.get('plan_id', '')}\n"
            f"脚本数量: {len(scripts)} 条\n"
            f"爆款 TOP10: {len(summary.get('top10_viral', []))} 条\n"
            f"高频品牌: {len(summary.get('frequent_brands', []))} 个\n\n"
            f"导出: {result.get('output_dir', '')}/plan_export.json",
            title="[bold]完成[/]",
            border_style="green",
        ))
    else:
        console.print(Panel(
            f"[red]规划失败[/]\n{result.get('error', '未知错误')}",
            title="失败",
            border_style="red",
        ))


# ── 入口 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
