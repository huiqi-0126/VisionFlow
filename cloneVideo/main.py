"""cloneVideo - 室内装修视频复刻 CLI"""

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

    fh = logging.FileHandler(log_dir / "clonevideo.log", encoding="utf-8")
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
    """cloneVideo - 室内装修视频复刻工具"""
    _setup_logging(verbose)


# ── run ──────────────────────────────────────────────────────

@cli.command()
@click.argument("video_path", type=click.Path(exists=True), required=False)
@click.option("--resume", type=str, default="", help="基于已有项目 ID 恢复并继续(从生成素材开始)")
def run(video_path: str | None, resume: str) -> None:
    """复刻一个室内装修视频, 生成同风格不同房间的 4s 素材

    VIDEO_PATH: 源视频文件路径

    画幅自动跟随源视频(横屏/竖屏)。镜头数按视频时长智能推导(4~15个), 每个输出 1 张风格图 + 1 个 4s 视频。
    """
    from workflow.pipeline import CloneVideoPipeline

    settings = get_settings()
    pipeline = CloneVideoPipeline(settings)
    try:
        result = pipeline.run(video_path, resume_project_id=resume or None)
    except Exception as e:
        console.print(f"[red]错误: {e}[/]")
        return

    if result.get("status") == "done":
        images = result.get("images", [])
        clips = result.get("clips", [])
        profile = result.get("style_profile", {})
        console.print(Panel(
            f"[green]复刻完成[/]\n"
            f"项目 ID: {result.get('project_id', '')}\n"
            f"画幅: {result.get('aspect_ratio', '')}\n"
            f"风格: {profile.get('overall_style_cn', '')} / {profile.get('overall_style_en', '')}\n"
            f"镜头数: {len(result.get('shot_plan', []))}\n"
            f"素材: {len(images)} 张图 + {len(clips)} 个 4s 视频\n\n"
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


# ── replica ─────────────────────────────────────────────────────

@cli.command()
@click.argument("video_path", type=click.Path(exists=True), required=False)
@click.option("--resume", type=str, default="", help="基于已有项目 ID 恢复并继续(从生成片段开始)")
def replica(video_path: str | None, resume: str) -> None:
    """完全复刻: 关键帧作首帧 → 理解运镜 → 4s 片段 → 合并完整视频

    VIDEO_PATH: 源视频文件路径

    截取视频关键帧作为首帧, VLM 理解原片运镜方式, 逐帧生成 4s 片段,
    最后合并成与原片基本一样的完整视频(静音)。
    """
    from workflow.replica_pipeline import ReplicaPipeline

    settings = get_settings()
    pipeline = ReplicaPipeline(settings)
    try:
        result = pipeline.run(video_path, resume_project_id=resume or None)
    except Exception as e:
        console.print(f"[red]错误: {e}[/]")
        return

    if result.get("status") == "done":
        console.print(Panel(
            f"[green]完全复刻完成[/]\n"
            f"项目 ID: {result.get('project_id', '')}\n"
            f"画幅: {result.get('aspect_ratio', '')}\n"
            f"片段数: {len(result.get('clips', []))}\n"
            f"完整视频: {result.get('final_video', '')}",
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

    table = Table(title="室内复刻项目", show_lines=False)
    table.add_column("ID", style="cyan", width=10)
    table.add_column("状态", width=20)
    table.add_column("风格", width=18)
    table.add_column("画幅", width=8)
    table.add_column("镜头", width=8, justify="right")
    table.add_column("素材", width=10, justify="right")
    table.add_column("创建时间", width=19)

    status_styles = {
        "uploaded": "[blue]", "analyzing": "[yellow]", "style_profiled": "[yellow]",
        "planning": "[yellow]", "generating_images": "[yellow]", "generating_clips": "[yellow]",
        "done": "[green]", "failed": "[red]",
    }
    for p in reversed(projects):
        s = status_styles.get(p.get("status", ""), "")
        profile = p.get("style_profile", {}) or {}
        shots = p.get("shot_plan", []) or []
        clips_done = sum(1 for sh in shots if sh.get("clip_status") == "done")
        console_style = s
        table.add_row(
            p.get("project_id", ""),
            f"{console_style}{p.get('status', '')}[/]",
            profile.get("overall_style_cn", "-")[:18],
            p.get("aspect_ratio", "-"),
            f"{len(shots)}",
            f"{clips_done}/{len(shots)}",
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
@click.option("--port", default=8001, type=int, help="监听端口")
@click.option("--reload", is_flag=True, help="开发模式 (auto-reload)")
def web(host: str, port: int, reload: bool) -> None:
    """启动 Web 管理界面"""
    import uvicorn
    console.print(Panel(
        f"[bold green]cloneVideo 室内装修视频复刻[/]\n\n"
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


if __name__ == "__main__":
    cli()
