"""cloneVideo Web - 室内装修视频复刻 Web 界面"""

from __future__ import annotations

import json
import logging
import sys
import threading
import uuid
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import get_settings
from core.project_manager import ProjectManager
from core.media_api import MediaAPIClient
from workflow.pipeline import CloneVideoPipeline
from workflow.image_pipeline import ImageClonePipeline
from workflow.replica_pipeline import ReplicaPipeline
from workflow.outfit_switch_pipeline import OutfitSwitchPipeline
from workflow.magic_snap_pipeline import MagicSnapPipeline
from workflow.reference_motion_pipeline import ReferenceMotionPipeline
from workflow.reference_video_outfit_pipeline import ReferenceVideoOutfitPipeline

logger = logging.getLogger(__name__)

# ── 初始化 ──────────────────────────────────────────────────────

settings = get_settings()
pipeline = CloneVideoPipeline(settings)
image_pipeline = ImageClonePipeline(settings)
replica_pipeline = ReplicaPipeline(settings)
project_mgr = ProjectManager(settings.data_dir, settings.projects_dir)

WEB_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = _PROJECT_ROOT / "output" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_OUTFIT_DIR = _PROJECT_ROOT / "public" / "outfit_defaults"
DEFAULT_OUTFIT_MANIFEST = DEFAULT_OUTFIT_DIR / "manifest.json"

# A curated batch of common outfit descriptions (clothing only: top + bottom,
# no shoes/accessories). Surfaced via /api/outfit-switch/outfit-suggestions so
# the "+" button can auto-pick one instead of making the user type detail.
OUTFIT_SUGGESTIONS = [
    {"label": "运动百褶套装", "garment": "a color-block outdoor windbreaker jacket with a pleated sports mini skirt"},
    {"label": "卫衣工装套装", "garment": "a loose hooded sweatshirt with khaki cargo pants"},
    {"label": "条纹牛仔套装", "garment": "a striped short-sleeve t-shirt with light blue denim shorts"},
    {"label": "衬衫阔腿套装", "garment": "a white oversized button-up shirt with beige wide-leg trousers"},
    {"label": "针织半裙套装", "garment": "a fitted ribbed knit sweater with a satin midi slip skirt"},
    {"label": "西服短裤套装", "garment": "a black blazer with matching tailored shorts"},
    {"label": "T恤牛仔套装", "garment": "a graphic crew-neck t-shirt with straight blue jeans"},
    {"label": "背心工装套装", "garment": "a white ribbed tank top with olive cargo pants"},
    {"label": "开衫连衣裙套装", "garment": "a long cardigan over a fitted midi dress"},
    {"label": "皮衣牛仔套装", "garment": "a black faux-leather jacket with skinny jeans"},
    {"label": "运动卫裤套装", "garment": "a cropped sports hoodie with matching track pants"},
    {"label": "吊带阔腿套装", "garment": "a satin camisole with high-waisted wide-leg pants"},
]

app = FastAPI(title="cloneVideo - 室内装修视频复刻", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
app.mount("/public", StaticFiles(directory=str(_PROJECT_ROOT / "public")), name="public")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
# 让 output/projects 下的素材可通过 URL 访问
app.mount("/output", StaticFiles(directory=str(_PROJECT_ROOT / "output")), name="output")

# ── 后台任务追踪 ────────────────────────────────────────────────

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _run_clone(job_id: str, video_path: str) -> None:
    try:
        result = pipeline.run(video_path)
        with _jobs_lock:
            _jobs[job_id]["status"] = result.get("status", "failed")
            _jobs[job_id]["project_id"] = result.get("project_id", "")
            _jobs[job_id]["result"] = result
    except Exception as exc:
        logger.error("Job %s failed: %s", job_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)


def _run_resume(job_id: str, project_id: str) -> None:
    try:
        result = pipeline.run(resume_project_id=project_id)
        with _jobs_lock:
            _jobs[job_id]["status"] = result.get("status", "failed")
            _jobs[job_id]["project_id"] = result.get("project_id", "")
            _jobs[job_id]["result"] = result
    except Exception as exc:
        logger.error("Resume job %s failed: %s", job_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)


def _run_image_clone(job_id: str, image_path: str, num_rooms: int) -> None:
    """图片模式后台任务: 单张客厅图→同风格其他房间→4s视频"""
    try:
        result = image_pipeline.run(image_path, num_rooms=num_rooms)
        with _jobs_lock:
            _jobs[job_id]["status"] = result.get("status", "failed")
            _jobs[job_id]["project_id"] = result.get("project_id", "")
            _jobs[job_id]["result"] = result
    except Exception as exc:
        logger.error("Image clone job %s failed: %s", job_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)


def _run_replica(job_id: str, video_path: str) -> None:
    """完全复刻后台任务: 关键帧作首帧→运镜还原→4s片段→合并完整视频"""
    try:
        result = replica_pipeline.run(video_path)
        with _jobs_lock:
            _jobs[job_id]["status"] = result.get("status", "failed")
            _jobs[job_id]["project_id"] = result.get("project_id", "")
            _jobs[job_id]["result"] = result
    except Exception as exc:
        logger.error("Replica job %s failed: %s", job_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)


def _run_replica_resume(job_id: str, project_id: str) -> None:
    """完全复刻恢复任务: 从生成片段开始继续"""
    try:
        result = replica_pipeline.run(resume_project_id=project_id)
        with _jobs_lock:
            _jobs[job_id]["status"] = result.get("status", "failed")
            _jobs[job_id]["project_id"] = result.get("project_id", "")
            _jobs[job_id]["result"] = result
    except Exception as exc:
        logger.error("Replica resume job %s failed: %s", job_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)


def _run_quick_cut(job_id: str, project_id: str, clip_paths: list[str], title: str, accent: str, skill: str = "quick-cut") -> None:
    try:
        output_path = _PROJECT_ROOT / "output" / "projects" / project_id / "quick_cut_output.mp4"
        ref_video = _PROJECT_ROOT / "example.mp4"
        
        import os
        import shutil
        env = os.environ.copy()
        ffmpeg_setting = settings.ffmpeg_path
        ffmpeg_path_obj = Path(ffmpeg_setting)

        ffmpeg_bin_dir = ""
        if ffmpeg_path_obj.is_file() or ffmpeg_path_obj.name.lower().endswith(".exe"):
            ffmpeg_bin_dir = str(ffmpeg_path_obj.parent)
            ffmpeg_exe = str(ffmpeg_path_obj)
        elif ffmpeg_path_obj.is_dir():
            ffmpeg_bin_dir = str(ffmpeg_path_obj)
            ffmpeg_exe = shutil.which("ffmpeg", path=ffmpeg_bin_dir) or "ffmpeg"
        else:
            ffmpeg_exe = shutil.which("ffmpeg") or "ffmpeg"

        if ffmpeg_bin_dir:
            env["PATH"] = ffmpeg_bin_dir + os.pathsep + env.get("PATH", "")

        # 自动兼容跨机器搬迁后的历史素材路径
        resolved_clip_paths = []
        for clip in clip_paths:
            cp = Path(clip)
            if not cp.exists():
                normalized = clip.replace("\\", "/")
                if "/output/" in normalized:
                    rel = normalized.split("/output/", 1)[1]
                    candidate = _PROJECT_ROOT / "output" / rel
                    if candidate.exists():
                        cp = candidate
                elif normalized.startswith("output/"):
                    rel = normalized.split("output/", 1)[1]
                    candidate = _PROJECT_ROOT / "output" / rel
                    if candidate.exists():
                        cp = candidate
            resolved_clip_paths.append(str(cp))
        clip_paths = resolved_clip_paths

        logger.info(f"Running video synthesis with skill={skill} for project {project_id}")

        if skill == "before-after-cloud-editor":
            # Before/After Montage editing mode
            inputs = []
            filters = []
            concat_nodes = []
            for idx, clip in enumerate(clip_paths):
                inputs.extend(["-i", clip])
                filters.append(f"[{idx}:v]fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v{idx}]")
                concat_nodes.append(f"[v{idx}]")
            n_clips = len(clip_paths)
            filters.append("".join(concat_nodes) + f"concat=n={n_clips}:v=1:a=0[vconcat]")
            caption = f"BEFORE & AFTER | {title}" if title else "BEFORE & AFTER"
            clean_caption = caption.replace("'", "'\\''").replace(":", "\\:")
            filters.append(f"[vconcat]drawtext=text='{clean_caption}':fontcolor=white:fontsize=32:x=(w-text_w)/2:y=h*0.84:shadowcolor=black@0.4:shadowx=2:shadowy=2[outv]")
            filter_complex = ";".join(filters)

            cmd = [
                ffmpeg_exe, "-y",
                *inputs,
                "-stream_loop", "-1", "-i", str(ref_video),
                "-filter_complex", filter_complex,
                "-map", "[outv]",
                "-map", f"{n_clips}:a:0",
                "-shortest",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                str(output_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=env)

        elif skill == "koubo":
            # IP Reel fast cut editing mode
            inputs = []
            filters = []
            concat_nodes = []
            for idx, clip in enumerate(clip_paths):
                inputs.extend(["-i", clip])
                filters.append(f"[{idx}:v]fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v{idx}]")
                concat_nodes.append(f"[v{idx}]")
            n_clips = len(clip_paths)
            filters.append("".join(concat_nodes) + f"concat=n={n_clips}:v=1:a=0[vconcat]")
            clean_title = title.replace("'", "'\\''").replace(":", "\\:")
            if accent:
                clean_accent = accent.replace("'", "'\\''").replace(":", "\\:")
                filters.append(f"[vconcat]drawtext=text='{clean_title}':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=h*0.12:shadowcolor=black@0.5:shadowx=2:shadowy=2,drawtext=text='{clean_accent}':fontcolor=yellow:fontsize=28:x=(w-text_w)/2:y=h*0.18:shadowcolor=black@0.5:shadowx=2:shadowy=2[outv]")
            else:
                filters.append(f"[vconcat]drawtext=text='{clean_title}':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=h*0.12:shadowcolor=black@0.5:shadowx=2:shadowy=2[outv]")
            filter_complex = ";".join(filters)

            cmd = [
                ffmpeg_exe, "-y",
                *inputs,
                "-stream_loop", "-1", "-i", str(ref_video),
                "-filter_complex", filter_complex,
                "-map", "[outv]",
                "-map", f"{n_clips}:a:0",
                "-shortest",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                str(output_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=env)

        elif skill == "shenghuo":
            # Calm lifestyle montage editing mode (7x3 style)
            inputs = []
            filters = []
            concat_nodes = []
            for idx, clip in enumerate(clip_paths):
                inputs.extend(["-i", clip])
                filters.append(f"[{idx}:v]trim=duration=3,setpts=PTS-STARTPTS,fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v{idx}]")
                concat_nodes.append(f"[v{idx}]")
            n_clips = len(clip_paths)
            filters.append("".join(concat_nodes) + f"concat=n={n_clips}:v=1:a=0[vconcat]")
            clean_title = title.replace("'", "'\\''").replace(":", "\\:")
            filters.append(f"[vconcat]drawtext=text='{clean_title}':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2:shadowcolor=black@0.3:shadowx=2:shadowy=2[outv]")
            filter_complex = ";".join(filters)

            cmd = [
                ffmpeg_exe, "-y",
                *inputs,
                "-stream_loop", "-1", "-i", str(ref_video),
                "-filter_complex", filter_complex,
                "-map", "[outv]",
                "-map", f"{n_clips}:a:0",
                "-shortest",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                str(output_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=env)

        else: # default: quick-cut
            cmd = [
                sys.executable,
                str(_PROJECT_ROOT.parent / "quick-cut" / "scripts" / "build_quick_cut.py"),
                "--reference", str(ref_video),
                "--title", title,
                "--output", str(output_path)
            ]
            if accent:
                cmd.extend(["--accent", accent])
            cmd.extend(["--sources"])
            cmd.extend(clip_paths)
            logger.info(f"Running quick_cut for {project_id}: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=env)
        
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["output_path"] = str(output_path)
    except subprocess.CalledProcessError as exc:
        logger.error(f"Video synthesis job {job_id} failed: {exc.stderr}")
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = exc.stderr
    except Exception as exc:
        logger.error(f"Video synthesis job {job_id} failed: {exc}")
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)

# ═════════════════════════════════════════════════════════════════
# 页面路由
# ═════════════════════════════════════════════════════════════════

def _load_outfit_defaults() -> dict[str, Any]:
    if not DEFAULT_OUTFIT_MANIFEST.is_file():
        return {"models": [], "outfits": []}
    try:
        return json.loads(DEFAULT_OUTFIT_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Cannot load outfit defaults manifest", exc_info=True)
        return {"models": [], "outfits": []}


def _run_generate_outfit_defaults(job_id: str) -> None:
    """Regenerate OUTFIT presets only.

    Existing MODEL presets are preserved (their images + manifest entries are
    reused unchanged) — only the garment product shots are rebuilt. If no model
    presets exist yet (first run), models are generated too.
    """
    # Model definitions are only used the FIRST time (when a model preset is
    # missing). On later regenerations the existing model images are kept as-is.
    model_defs = [
        ("model_asian_editorial.png", "都市轻奢女模特", "Photorealistic full-body East Asian female fashion model, 25 years old, long black center-parted hair, natural refined makeup, confident neutral expression, wearing a fitted white t-shirt, light blue high-waisted jeans, and clean white sneakers. Front-facing standing pose, arms relaxed, warm light-gray seamless fashion studio cyclorama, soft editorial lighting, vertical 9:16, full body visible, clear face and correct hands, no jewelry, no text, no logo, no watermark.", ""),
        ("model_european_editorial.png", "极简金发女模特", "Photorealistic full-body European female fashion model, 26 years old, shoulder-length dark blonde bob, minimal makeup, calm neutral expression, wearing a ribbed beige knit top, dark tailored trousers, and minimal white sneakers. Front-facing standing pose, arms relaxed, warm light-gray seamless fashion studio cyclorama, soft editorial lighting, vertical 9:16, full body visible, clear face and correct hands, no jewelry, no text, no logo, no watermark.", ""),
    ]
    # Outfit presets are COMPLETE LOOKS (top + bottom + shoes + accessories) as
    # clean product shots: pure solid white background, clothing only, no
    # mannequin, no model. `garment` dresses the model in _make_look.
    outfit_defs = [
        ("outfit_sport_windbreaker.png", "运动百褶套装", "Professional e-commerce flat-lay product photo of a complete women's casual sporty outfit arranged together: a color-block outdoor windbreaker jacket and a pleated sports mini skirt. Pure seamless white background, clothing only, no shoes, no accessories, no mannequin, no person, no model, every garment clearly visible, realistic fabric texture, soft even studio lighting, vertical 9:16, no text, no logo, no watermark.", "a color-block outdoor windbreaker jacket with a pleated sports mini skirt"),
        ("outfit_hoodie_cargo.png", "卫衣工装套装", "Professional e-commerce flat-lay product photo of a complete women's casual street outfit arranged together: a loose hooded sweatshirt (hoodie) and khaki cargo pants. Pure seamless white background, clothing only, no shoes, no accessories, no mannequin, no person, no model, every garment clearly visible, realistic fabric texture, soft even studio lighting, vertical 9:16, no text, no logo, no watermark.", "a loose hooded sweatshirt with khaki cargo pants"),
        ("outfit_striped_denim.png", "条纹牛仔套装", "Professional e-commerce flat-lay product photo of a complete women's casual summer outfit arranged together: a striped short-sleeve t-shirt and light blue denim shorts. Pure seamless white background, clothing only, no shoes, no accessories, no mannequin, no person, no model, every garment clearly visible, realistic fabric texture, soft even studio lighting, vertical 9:16, no text, no logo, no watermark.", "a striped short-sleeve t-shirt with light blue denim shorts"),
    ]
    try:
        DEFAULT_OUTFIT_DIR.mkdir(parents=True, exist_ok=True)
        media = MediaAPIClient(settings.gkapi_key, settings.gkapi_baseurl, settings.poll_interval, settings.max_poll_attempts)
        manifest: dict[str, list[dict[str, str]]] = {"models": [], "outfits": []}

        # 1) Models: reuse existing entries + images; only generate if missing.
        existing = _load_outfit_defaults()
        existing_models = {m.get("id"): m for m in existing.get("models", [])}
        for filename, label, prompt, garment in model_defs:
            mid = filename.removesuffix(".png")
            if mid in existing_models and (DEFAULT_OUTFIT_DIR / filename).is_file():
                manifest["models"].append(existing_models[mid])  # keep as-is, do not regenerate
                continue
            with _jobs_lock:
                _jobs[job_id]["stage"] = f"Gemini 生成模特：{label}"
            task_id = media.generate_image(prompt=prompt, model=settings.default_image_model, size=settings.default_size, aspect_ratio="9:16", pic=None)
            image = media.poll_image(task_id)
            if image.get("status") != "success" or not image.get("url"):
                raise RuntimeError(f"Gemini 模特生成失败：{label}")
            media.download_media(image["url"], DEFAULT_OUTFIT_DIR / filename)
            manifest["models"].append({"id": mid, "label": label, "path": f"/public/outfit_defaults/{filename}", "prompt": prompt, "garment": garment})

        # 2) Outfits: always regenerate (full looks, solid bg, no model).
        for index, (filename, label, prompt, garment) in enumerate(outfit_defs, 1):
            with _jobs_lock:
                _jobs[job_id]["stage"] = f"Gemini 生成服装预设 {index}/{len(outfit_defs)}：{label}"
            task_id = media.generate_image(prompt=prompt, model=settings.default_image_model, size=settings.default_size, aspect_ratio="9:16", pic=None)
            image = media.poll_image(task_id)
            if image.get("status") != "success" or not image.get("url"):
                raise RuntimeError(f"Gemini 服装预设生成失败：{label}")
            media.download_media(image["url"], DEFAULT_OUTFIT_DIR / filename)
            manifest["outfits"].append({"id": filename.removesuffix(".png"), "label": label, "path": f"/public/outfit_defaults/{filename}", "prompt": prompt, "garment": garment})

        # Preserve user-added custom outfits (any id not in the default set), so
        # "regenerate defaults" doesn't wipe outfits added via the +/× UI.
        default_ids = {filename.removesuffix(".png") for filename, *_ in outfit_defs}
        for entry in existing.get("outfits", []):
            if entry.get("id") not in default_ids:
                manifest["outfits"].append(entry)

        DEFAULT_OUTFIT_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with _jobs_lock:
            _jobs[job_id].update({"status": "done", "stage": "服装预设已生成（模特保留未变）", "result": manifest})
    except Exception as exc:
        logger.exception("Outfit defaults job %s failed", job_id)
        with _jobs_lock:
            _jobs[job_id].update({"status": "failed", "stage": "预设生成失败", "error": str(exc)})

def _run_add_outfit(job_id: str, label: str, garment: str, image_bytes: bytes | None, image_suffix: str | None) -> None:
    """Add one outfit preset. If an image was uploaded it is used directly;
    otherwise Gemini generates a white-background flat-lay product shot from
    the garment description. The entry is appended to the manifest.
    """
    try:
        DEFAULT_OUTFIT_DIR.mkdir(parents=True, exist_ok=True)
        media = MediaAPIClient(settings.gkapi_key, settings.gkapi_baseurl, settings.poll_interval, settings.max_poll_attempts)
        oid = "outfit_" + uuid.uuid4().hex[:10]
        ext = image_suffix if (image_bytes and image_suffix) else ".png"
        filename = f"{oid}{ext}"
        prompt = ""
        if image_bytes:
            (DEFAULT_OUTFIT_DIR / filename).write_bytes(image_bytes)
        else:
            with _jobs_lock:
                _jobs[job_id]["stage"] = f"Gemini 生成服装：{label}"
            prompt = ("Professional e-commerce flat-lay product photo of a complete women's outfit arranged together: "
                      f"{garment}. Pure seamless white background, clothing only, no shoes, no accessories, no mannequin, no person, no model, every garment clearly visible, realistic fabric texture, soft even studio lighting, vertical 9:16, no text, no logo, no watermark.")
            task_id = media.generate_image(prompt=prompt, model=settings.default_image_model, size=settings.default_size, aspect_ratio="9:16", pic=None)
            image = media.poll_image(task_id)
            if image.get("status") != "success" or not image.get("url"):
                raise RuntimeError(f"服装生成失败：{label}")
            media.download_media(image["url"], DEFAULT_OUTFIT_DIR / filename)
        entry = {"id": oid, "label": label, "path": f"/public/outfit_defaults/{filename}", "prompt": prompt, "garment": garment}
        manifest = _load_outfit_defaults()
        manifest.setdefault("outfits", []).append(entry)
        DEFAULT_OUTFIT_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with _jobs_lock:
            _jobs[job_id].update({"status": "done", "stage": f"已添加服装：{label}", "result": entry})
    except Exception as exc:
        logger.exception("Add outfit job %s failed", job_id)
        with _jobs_lock:
            _jobs[job_id].update({"status": "failed", "stage": "添加服装失败", "error": str(exc)})

def _run_outfit_switch(
    job_id: str,
    job_dir: Path,
    model_image: Path | None,
    model_prompt: str,
    outfit_images: list[Path],
    outfit_prompts: list[str],
    no_model: bool = False,
) -> None:
    """Run the independent 23.mp4-style outfit-switch workflow in the background."""
    try:
        with _jobs_lock:
            _jobs[job_id]["stage"] = "准备素材（无模特文本模式）" if no_model else "生成统一模特与服装参考图"
        def report_stage(stage: str) -> None:
            with _jobs_lock:
                _jobs[job_id]["stage"] = stage

        result = OutfitSwitchPipeline(settings).run(
            job_dir, model_image, model_prompt, outfit_images, outfit_prompts,
            on_progress=report_stage, no_model=no_model,
        )
        with _jobs_lock:
            _jobs[job_id].update({
                "status": "done",
                "stage": "成片已生成",
                "output_path": result["final_video"],
                "manifest_path": str(job_dir / "manifest.json"),
                "event_log_path": str(job_dir / "events.jsonl"),
                "reference_analysis_path": str(job_dir / "reference_analysis.json"),
                "beat_plan_path": str(job_dir / "beat_plan.json"),
                "result": result,
            })
    except Exception as exc:
        logger.exception("Outfit switch job %s failed", job_id)
        with _jobs_lock:
            _jobs[job_id].update({"status": "failed", "stage": "生成失败", "error": str(exc)})


def _run_magic_snap(
    job_id: str,
    job_dir: Path,
    model_image: Path | None,
    model_prompt: str,
    outfit_images: list[Path],
    outfit_prompts: list[str],
) -> None:
    """Run the standalone magic-snap loop workflow in the background."""
    try:
        with _jobs_lock:
            _jobs[job_id]["stage"] = "魔法响指循环：准备素材"

        def report_stage(stage: str) -> None:
            with _jobs_lock:
                _jobs[job_id]["stage"] = stage

        result = MagicSnapPipeline(settings).run(
            job_dir, model_image, model_prompt, outfit_images, outfit_prompts,
            on_progress=report_stage,
        )
        with _jobs_lock:
            _jobs[job_id].update({
                "status": "done",
                "stage": "成片已生成",
                "output_path": result["final_video"],
                "manifest_path": str(job_dir / "manifest.json"),
                "result": result,
            })
    except Exception as exc:
        logger.exception("Magic snap job %s failed", job_id)
        with _jobs_lock:
            _jobs[job_id].update({"status": "failed", "stage": "生成失败", "error": str(exc)})

def _run_reference_video_outfit(job_id: str, job_dir: Path, model_image: Path | None, model_prompt: str, outfits: list[str]) -> None:
    try:
        def report(stage: str) -> None:
            with _jobs_lock: _jobs[job_id]["stage"] = stage
        result = ReferenceVideoOutfitPipeline(settings).run(job_dir, model_image, model_prompt, outfits, report)
        with _jobs_lock:
            _jobs[job_id].update({"status":"done","stage":"成片已生成","output_path":result["final_video"],"manifest_path":str(job_dir/"manifest.json"),"result":result})
    except Exception as exc:
        logger.exception("Reference-video outfit job %s failed",job_id)
        with _jobs_lock: _jobs[job_id].update({"status":"failed","stage":"生成失败","error":str(exc)})


def _run_reference_motion(job_id: str, job_dir: Path, ref_video: Path, images: list[Path], prompt: str, mask_mode: str = "blur") -> None:
    """Run the standalone reference-motion (参数视频) workflow in the background."""
    try:
        def report(stage: str) -> None:
            with _jobs_lock: _jobs[job_id]["stage"] = stage
        result = ReferenceMotionPipeline(settings).run(job_dir, ref_video, images, prompt, report, mask_mode=mask_mode)
        with _jobs_lock:
            _jobs[job_id].update({
                "status": "done", "stage": "成片已生成",
                "output_path": result["final_video"],
                "manifest_path": str(job_dir / "manifest.json"),
                "reference_masked_path": result["reference_masked"],
                "result": result,
            })
    except Exception as exc:
        logger.exception("Reference-motion job %s failed", job_id)
        with _jobs_lock: _jobs[job_id].update({"status": "failed", "stage": "生成失败", "error": str(exc)})


@app.post("/api/reference-motion")
async def api_reference_motion(
    prompt: str = Form(""),
    mask_mode: str = Form("blur"),
    video: UploadFile = File(...),
    image1: UploadFile | None = File(None),
    image2: UploadFile | None = File(None),
):
    """参考视频驱动生成：上传参考视频(必填) + 可选2张图 + 提示词 → 复用动作生成视频。"""
    if mask_mode not in {"none", "blur", "mosaic", "fill"}:
        mask_mode = "blur"
    if not video or not video.filename:
        return JSONResponse({"error": "请上传参考视频"}, status_code=400)
    video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
    vsuffix = Path(video.filename).suffix.lower()
    if vsuffix not in video_exts:
        return JSONResponse({"error": "参考视频仅支持 MP4/MOV/AVI/MKV/WEBM"}, status_code=400)
    image_exts = {".jpg", ".jpeg", ".png", ".webp"}

    job_id = uuid.uuid4().hex[:12]
    job_dir = _PROJECT_ROOT / "output" / "reference_motions" / job_id
    uploads_dir = job_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    ref_path = uploads_dir / f"reference{vsuffix}"
    ref_path.write_bytes(await video.read())

    images: list[Path] = []
    for idx, img in enumerate([image1, image2], 1):
        if img and img.filename:
            suffix = Path(img.filename).suffix.lower()
            if suffix not in image_exts:
                return JSONResponse({"error": "参考图仅支持 JPG、PNG、WEBP"}, status_code=400)
            p = uploads_dir / f"image_{idx}{suffix}"
            p.write_bytes(await img.read())
            images.append(p)

    with _jobs_lock:
        _jobs[job_id] = {"id": job_id, "type": "reference_motion", "status": "running", "stage": "分析参考视频并遮挡敏感区域", "output_dir": str(job_dir)}
    threading.Thread(target=_run_reference_motion, args=(job_id, job_dir, ref_path, images, prompt.strip(), mask_mode), daemon=True).start()
    return {"job_id": job_id, "status": "started"}


@app.get("/", response_class=HTMLResponse)
async def page_index(request: Request):
    projects = project_mgr.list_projects()
    stats = project_mgr.get_stats()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "projects": list(reversed(projects)),
        "stats": stats,
        "active_jobs": {k: v for k, v in _jobs.items() if v.get("status") == "running"},
    })


@app.get("/outfit-switch", response_class=HTMLResponse)
async def page_outfit_switch(request: Request):
    return templates.TemplateResponse("outfit_switch.html", {"request": request, "defaults": _load_outfit_defaults()})


@app.get("/reference-motion", response_class=HTMLResponse)
async def page_reference_motion(request: Request):
    return templates.TemplateResponse("reference_motion.html", {"request": request})


@app.post("/api/outfit-switch/generate-defaults")
async def api_generate_outfit_defaults():
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"id": job_id, "type": "outfit_defaults", "status": "running", "stage": "准备 Gemini 默认预设"}
    threading.Thread(target=_run_generate_outfit_defaults, args=(job_id,), daemon=True).start()
    return {"job_id": job_id, "status": "started"}


@app.post("/api/outfit-switch/outfits")
async def api_add_outfit(
    label: str = Form(...),
    garment: str = Form(...),
    image: UploadFile | None = File(None),
):
    """Add one outfit preset — upload an image, or let Gemini generate a white-bg flat-lay."""
    label = label.strip()
    garment = garment.strip()
    if not label or not garment:
        return JSONResponse({"error": "请填写名称和服装描述"}, status_code=400)
    image_bytes: bytes | None = None
    image_suffix: str | None = None
    if image and image.filename:
        suffix = Path(image.filename).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            return JSONResponse({"error": "图片仅支持 JPG/PNG/WEBP"}, status_code=400)
        image_bytes = await image.read()
        image_suffix = suffix
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"id": job_id, "type": "add_outfit", "status": "running", "stage": "准备添加服装"}
    threading.Thread(target=_run_add_outfit, args=(job_id, label, garment, image_bytes, image_suffix), daemon=True).start()
    return {"job_id": job_id, "status": "started"}


@app.delete("/api/outfit-switch/outfits/{outfit_id}")
async def api_delete_outfit(outfit_id: str):
    """Delete one outfit preset — removes the manifest entry and its image file."""
    manifest = _load_outfit_defaults()
    outfits = manifest.get("outfits", [])
    remaining = [o for o in outfits if o.get("id") != outfit_id]
    if len(remaining) == len(outfits):
        return JSONResponse({"error": "服装预设不存在"}, status_code=404)
    removed = next(o for o in outfits if o.get("id") == outfit_id)
    try:
        png = DEFAULT_OUTFIT_DIR / Path(removed.get("path", "")).name
        if png.is_file():
            png.unlink()
    except OSError:
        logger.warning("Could not delete outfit image %s", removed.get("path"))
    manifest["outfits"] = remaining
    DEFAULT_OUTFIT_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "outfits": remaining}


@app.get("/api/outfit-switch/outfit-suggestions")
async def api_outfit_suggestions():
    """Return the curated batch of common outfit descriptions for the '+' button."""
    return OUTFIT_SUGGESTIONS


@app.get("/api/outfit-switch/history")
async def api_outfit_switch_history():
    """List completed outfit-switch runs (newest first), scanned from disk.

    Only directories that have BOTH manifest.json and outfit_switch.mp4 are
    returned, so failed/incomplete runs are hidden. The in-memory _jobs dict is
    not used because it is lost on restart.
    """
    base = _PROJECT_ROOT / "output" / "outfit_switches"
    items: list[dict[str, Any]] = []
    if base.is_dir():
        for directory in base.iterdir():
            if not directory.is_dir():
                continue
            manifest_file = directory / "manifest.json"
            video_file = directory / "outfit_switch.mp4"
            if not (manifest_file.is_file() and video_file.is_file()):
                continue
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = {}
            looks = manifest.get("looks", []) or []
            thumb = manifest.get("anchor") or (looks[0].get("path") if looks else "") or ""
            # Reference images used by each segment, surfaced so the viewer can
            # compare the generated video against its inputs (diagnose bad results).
            refs: list[dict[str, str]] = []
            anchor_path = manifest.get("anchor")
            if anchor_path:
                refs.append({"label": "模特锚图(默认衣)", "path": str(Path(anchor_path))})
            for i, look in enumerate(looks, 1):
                look_path = look.get("path") if isinstance(look, dict) else None
                if look_path:
                    refs.append({"label": f"第{i}套参考图", "path": str(Path(look_path))})
            items.append({
                "id": directory.name,
                "created": directory.stat().st_mtime,
                "video": str(video_file),
                "thumb": str(Path(thumb)) if thumb else "",
                "outfits": len(looks),
                "template": manifest.get("template", ""),
                "refs": refs,
            })
    items.sort(key=lambda x: x.get("created", 0), reverse=True)
    return items

@app.post("/api/outfit-switch")
async def api_outfit_switch(
    model_prompt: str = Form(""),
    outfit_prompts: str = Form(""),
    default_model: str = Form(""),
    default_outfits: str = Form(""),
    model_image: UploadFile | None = File(None),
    outfit_images: list[UploadFile] = File(default=[]),
):
    """Create a standalone 23.mp4-style outfit transformation job."""
    defaults = _load_outfit_defaults()
    model_items = {item["id"]: item for item in defaults.get("models", [])}
    outfit_items = {item["id"]: item for item in defaults.get("outfits", [])}
    selected_model = model_items.get(default_model)
    selected_outfit_ids = [value for value in default_outfits.split(",") if value]
    selected_outfits = [outfit_items[value] for value in selected_outfit_ids if value in outfit_items]
    if selected_outfit_ids and len(selected_outfits) != len(selected_outfit_ids):
        return JSONResponse({"error": "默认服装预设无效，请刷新页面后重试"}, status_code=400)
    if selected_outfits:
        outfit_prompts = "\n".join((item.get("garment") or item.get("prompt", "")) for item in selected_outfits)
    if selected_model:
        model_prompt = selected_model["prompt"]
    prompt_list = [line.strip() for line in outfit_prompts.splitlines() if line.strip()]
    outfit_image_count = sum(1 for img in outfit_images if img and img.filename)
    # Allow image-only outfits: when no text descriptions are provided but garment
    # images are uploaded, each image counts as one outfit and is described
    # generically. The actual garment is conveyed by the image reference passed
    # to the image model in OutfitSwitchPipeline._make_look (pic=[anchor, garment]).
    if not prompt_list and outfit_image_count >= 2:
        prompt_list = ["the outfit shown in the reference image"] * outfit_image_count
    # Model is OPTIONAL: if no preset / upload / description is provided, run in
    # "no model" text-to-video mode. Passing a person image to the video model is
    # what often fails, so this mode skips the person image entirely and uses a
    # detailed default person description instead.
    no_model = not model_prompt.strip() and not (model_image and model_image.filename)
    if not 2 <= len(prompt_list) <= 3:
        return JSONResponse({"error": "请填写 2 到 3 套服装描述，或上传 2–3 张服装图片"}, status_code=400)
    if outfit_image_count > len(prompt_list):
        return JSONResponse({"error": "服装图片数量不能超过服装数量"}, status_code=400)

    image_exts = {".jpg", ".jpeg", ".png", ".webp"}
    job_id = uuid.uuid4().hex[:12]
    job_dir = _PROJECT_ROOT / "output" / "outfit_switches" / job_id
    uploads_dir = job_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    saved_model: Path | None = None
    if model_image and model_image.filename:
        suffix = Path(model_image.filename).suffix.lower()
        if suffix not in image_exts:
            return JSONResponse({"error": "模特参考图仅支持 JPG、PNG、WEBP"}, status_code=400)
        saved_model = uploads_dir / f"model{suffix}"
        saved_model.write_bytes(await model_image.read())

    saved_outfits: list[Path] = []
    for index, outfit_image in enumerate(outfit_images, 1):
        if not outfit_image.filename:
            continue
        suffix = Path(outfit_image.filename).suffix.lower()
        if suffix not in image_exts:
            return JSONResponse({"error": "服装参考图仅支持 JPG、PNG、WEBP"}, status_code=400)
        path = uploads_dir / f"outfit_{index:02d}{suffix}"
        path.write_bytes(await outfit_image.read())
        saved_outfits.append(path)

    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "outfit_switch",
            "status": "running",
            "stage": "准备素材",
            "output_dir": str(job_dir),
        }
    threading.Thread(
        target=_run_outfit_switch,
        args=(job_id, job_dir, saved_model, model_prompt.strip(), saved_outfits, prompt_list, no_model),
        daemon=True,
    ).start()
    return {"job_id": job_id, "status": "started"}


@app.post("/api/outfit-switch/magic-snap")
async def api_magic_snap(
    model_prompt: str = Form(""),
    outfit_prompts: str = Form(""),
    default_model: str = Form(""),
    default_outfits: str = Form(""),
    model_image: UploadFile | None = File(None),
    outfit_images: list[UploadFile] = File(default=[]),
):
    """Create a magic-snap LOOP job (background+outfit change per snap). Allows 2–6 outfits."""
    defaults = _load_outfit_defaults()
    model_items = {item["id"]: item for item in defaults.get("models", [])}
    outfit_items = {item["id"]: item for item in defaults.get("outfits", [])}
    selected_model = model_items.get(default_model)
    selected_outfit_ids = [value for value in default_outfits.split(",") if value]
    selected_outfits = [outfit_items[value] for value in selected_outfit_ids if value in outfit_items]
    if selected_outfit_ids and len(selected_outfits) != len(selected_outfit_ids):
        return JSONResponse({"error": "默认服装预设无效，请刷新页面后重试"}, status_code=400)
    if selected_outfits:
        outfit_prompts = "\n".join((item.get("garment") or item.get("prompt", "")) for item in selected_outfits)
    if selected_model:
        model_prompt = selected_model["prompt"]
    prompt_list = [line.strip() for line in outfit_prompts.splitlines() if line.strip()]
    outfit_image_count = sum(1 for img in outfit_images if img and img.filename)
    if not prompt_list and outfit_image_count >= 2:
        prompt_list = ["the outfit shown in the reference image"] * outfit_image_count
    if not 2 <= len(prompt_list) <= 6:
        return JSONResponse({"error": "魔法响指循环支持 2–6 套服装"}, status_code=400)
    if outfit_image_count > len(prompt_list):
        return JSONResponse({"error": "服装图片数量不能超过服装数量"}, status_code=400)

    image_exts = {".jpg", ".jpeg", ".png", ".webp"}
    job_id = uuid.uuid4().hex[:12]
    job_dir = _PROJECT_ROOT / "output" / "magic_snaps" / job_id
    uploads_dir = job_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    saved_model: Path | None = None
    if model_image and model_image.filename:
        suffix = Path(model_image.filename).suffix.lower()
        if suffix not in image_exts:
            return JSONResponse({"error": "模特参考图仅支持 JPG、PNG、WEBP"}, status_code=400)
        saved_model = uploads_dir / f"model{suffix}"
        saved_model.write_bytes(await model_image.read())

    saved_outfits: list[Path] = []
    for index, outfit_image in enumerate(outfit_images, 1):
        if not outfit_image.filename:
            continue
        suffix = Path(outfit_image.filename).suffix.lower()
        if suffix not in image_exts:
            return JSONResponse({"error": "服装参考图仅支持 JPG、PNG、WEBP"}, status_code=400)
        path = uploads_dir / f"outfit_{index:02d}{suffix}"
        path.write_bytes(await outfit_image.read())
        saved_outfits.append(path)

    with _jobs_lock:
        _jobs[job_id] = {"id": job_id, "type": "magic_snap", "status": "running", "stage": "准备素材", "output_dir": str(job_dir)}
    threading.Thread(
        target=_run_magic_snap,
        args=(job_id, job_dir, saved_model, model_prompt.strip(), saved_outfits, prompt_list),
        daemon=True,
    ).start()
    return {"job_id": job_id, "status": "started"}

@app.post("/api/outfit-switch/reference-video")
async def api_reference_video_outfit(
    model_prompt: str = Form(""), outfit_prompts: str = Form(""),
    default_model: str = Form(""), default_outfits: str = Form(""),
    model_image: UploadFile | None = File(None),
):
    # Parse preset selections (the form sends default_outfits; the textarea may be empty).
    defaults = _load_outfit_defaults()
    model_items = {item["id"]: item for item in defaults.get("models", [])}
    outfit_items = {item["id"]: item for item in defaults.get("outfits", [])}
    selected_model = model_items.get(default_model)
    selected_ids = [v for v in default_outfits.split(",") if v]
    selected = [outfit_items[v] for v in selected_ids if v in outfit_items]
    if selected_ids and len(selected) != len(selected_ids):
        return JSONResponse({"error": "默认服装预设无效，请刷新页面后重试"}, status_code=400)
    if selected:
        outfit_prompts = "\n".join((it.get("garment") or it.get("prompt", "")) for it in selected)
    if selected_model:
        model_prompt = selected_model["prompt"]
    outfits = [line.strip() for line in outfit_prompts.splitlines() if line.strip()]
    if not 2 <= len(outfits) <= 6:
        return JSONResponse({"error": "参考视频换装模式支持 2–6 套服装"}, status_code=400)
    job_id = uuid.uuid4().hex[:12]; job_dir = _PROJECT_ROOT / "output" / "reference_video_outfits" / job_id; job_dir.mkdir(parents=True, exist_ok=True)
    saved: Path | None = None
    if model_image and model_image.filename:
        suffix = Path(model_image.filename).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}: return JSONResponse({"error": "模特图仅支持 JPG、PNG、WEBP"}, status_code=400)
        saved = job_dir / f"model{suffix}"; saved.write_bytes(await model_image.read())
    with _jobs_lock: _jobs[job_id] = {"id": job_id, "type": "reference_video_outfit", "status": "running", "stage": "准备 28.mp4 参考视频（将做人脸遮挡）", "output_dir": str(job_dir)}
    threading.Thread(target=_run_reference_video_outfit, args=(job_id, job_dir, saved, model_prompt.strip(), outfits), daemon=True).start()
    return {"job_id": job_id, "status": "started"}

@app.get("/project/{project_id}", response_class=HTMLResponse)
async def page_project(request: Request, project_id: str):
    project = project_mgr.get_project(project_id)
    if not project:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return templates.TemplateResponse("project.html", {
        "request": request,
        "project": project,
    })


# ═════════════════════════════════════════════════════════════════
# API 路由
# ═════════════════════════════════════════════════════════════════

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """上传文件(视频用于视频模式, 图片用于图片模式)"""
    if not file.filename:
        return JSONResponse({"error": "未选择文件"}, status_code=400)

    suffix = Path(file.filename).suffix.lower()
    video_exts = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv")
    image_exts = (".jpg", ".jpeg", ".png", ".webp")
    if suffix not in video_exts + image_exts:
        return JSONResponse({"error": f"不支持的文件格式(仅支持视频/图片): {suffix}"}, status_code=400)
    file_type = "image" if suffix in image_exts else "video"

    file_id = uuid.uuid4().hex[:8]
    save_path = UPLOAD_DIR / f"source_{file_id}{suffix}"
    content = await file.read()
    save_path.write_bytes(content)

    file_size_mb = len(content) / (1024 * 1024)
    logger.info("文件上传(%s): %s (%.1f MB) → %s", file_type, file.filename, file_size_mb, save_path)

    return {
        "path": str(save_path),
        "filename": file.filename,
        "size_mb": round(file_size_mb, 2),
        "file_type": file_type,
    }


@app.post("/api/replicate")
async def api_replicate(request: Request):
    """开始室内复刻任务（后台执行）"""
    data = await request.json()
    video_path = data.get("video_path", "")

    if not video_path or not Path(video_path).exists():
        return JSONResponse({"error": "视频文件不存在"}, status_code=400)

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "replicate",
            "video_path": video_path,
            "status": "running",
            "project_id": "",
        }

    t = threading.Thread(target=_run_clone, args=(job_id, video_path), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "started"}


@app.post("/api/replicate_image")
async def api_replicate_image(request: Request):
    """图片模式: 单张客厅图→同风格其他房间→4s视频(后台执行)"""
    data = await request.json()
    image_path = data.get("image_path", "")
    try:
        num_rooms = int(data.get("num_rooms", 7))
    except (TypeError, ValueError):
        num_rooms = 7

    if not image_path or not Path(image_path).exists():
        return JSONResponse({"error": "图片文件不存在"}, status_code=400)

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "replicate_image",
            "image_path": image_path,
            "status": "running",
            "project_id": "",
        }
    t = threading.Thread(target=_run_image_clone, args=(job_id, image_path, num_rooms), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "started"}


@app.post("/api/replica")
async def api_replica(request: Request):
    """完全复刻: 关键帧作首帧→运镜还原→4s片段→合并完整视频(后台执行)"""
    data = await request.json()
    video_path = data.get("video_path", "")

    if not video_path or not Path(video_path).exists():
        return JSONResponse({"error": "视频文件不存在"}, status_code=400)

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "replica",
            "video_path": video_path,
            "status": "running",
            "project_id": "",
        }

    t = threading.Thread(target=_run_replica, args=(job_id, video_path), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "started"}


@app.post("/api/projects/{project_id}/resume_replica")
async def api_resume_replica(project_id: str):
    """恢复完全复刻项目(从生成片段开始)"""
    project = project_mgr.get_project(project_id)
    if not project:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    if project.get("mode") != "replica":
        return JSONResponse({"error": "该项目不是完全复刻模式"}, status_code=400)

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "resume_replica",
            "status": "running",
            "project_id": project_id,
        }
    t = threading.Thread(target=_run_replica_resume, args=(job_id, project_id), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "started"}


@app.post("/api/projects/{project_id}/resume")
async def api_resume(project_id: str):
    """恢复已有项目(从生成素材开始)"""
    if not project_mgr.get_project(project_id):
        return JSONResponse({"error": "项目不存在"}, status_code=404)

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "resume",
            "status": "running",
            "project_id": project_id,
        }
    t = threading.Thread(target=_run_resume, args=(job_id, project_id), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "started"}


@app.post("/api/projects/{project_id}/retry_shot/{shot_id}")
async def api_retry_shot(project_id: str, shot_id: int):
    """单独重试失败的镜头素材"""
    project = project_mgr.get_project(project_id)
    if not project:
        return JSONResponse({"error": "项目不存在"}, status_code=404)

    is_replica = project.get("mode") == "replica"

    shot_plan = project.get("shot_plan", [])
    for shot in shot_plan:
        if shot.get("shot_id") == shot_id:
            shot["clip_status"] = "pending"
            shot["clip_path"] = ""
            # 如果图片也失败了，顺便把图片状态也重置（replica 模式无图片，跳过）
            if not is_replica and shot.get("image_status") == "failed":
                shot["image_status"] = "pending"
            break

    project_mgr.update_project(project_id, shot_plan=shot_plan, status="generating_clips")

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "resume_replica" if is_replica else "resume",
            "status": "running",
            "project_id": project_id,
        }
    if is_replica:
        t = threading.Thread(target=_run_replica_resume, args=(job_id, project_id), daemon=True)
    else:
        t = threading.Thread(target=_run_resume, args=(job_id, project_id), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "started"}


@app.post("/api/projects/{project_id}/quick_cut")
async def api_quick_cut(project_id: str, request: Request):
    """一键剪辑成片: 支持选择 before-after-cloud-editor, koubo, quick-cut, shenghuo 任意技能"""
    if not project_mgr.get_project(project_id):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
        
    data = await request.json()
    clip_paths = data.get("clip_paths", [])
    title = data.get("title", "")
    accent = data.get("accent", "")
    skill = data.get("skill", "quick-cut")
    
    if not clip_paths:
        return JSONResponse({"error": "未选择任何视频素材"}, status_code=400)
    if not title:
        return JSONResponse({"error": "未填写标题"}, status_code=400)
        
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "quick_cut",
            "skill": skill,
            "status": "running",
            "project_id": project_id,
        }
        
    t = threading.Thread(target=_run_quick_cut, args=(job_id, project_id, clip_paths, title, accent, skill), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "started"}

@app.get("/api/projects")
async def api_projects_list(
    status: str | None = Query(None),
    limit: int = Query(50),
):
    projects = project_mgr.list_projects(status=status, limit=limit)
    return list(reversed(projects))


@app.get("/api/projects/{project_id}")
async def api_project_detail(project_id: str):
    project = project_mgr.get_project(project_id)
    if not project:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return project


@app.delete("/api/projects/{project_id}")
async def api_project_delete(project_id: str):
    ok = project_mgr.delete_project(project_id)
    if ok:
        return {"ok": True}
    return JSONResponse({"error": "项目不存在"}, status_code=404)


@app.get("/api/jobs")
async def api_jobs():
    with _jobs_lock:
        return list(reversed(list(_jobs.values())))


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str):
    with _jobs_lock:
        return _jobs.get(job_id, {"status": "not_found"})


@app.get("/api/stats")
async def api_stats():
    return project_mgr.get_stats()


@app.get("/api/media")
async def api_media(path: str = Query(...)):
    """直接服务本地媒体文件(图片/视频)"""
    if not path:
        return JSONResponse({"error": "File not found"}, status_code=404)

    target_path = Path(path)
    if not target_path.exists():
        # 兼容跨机器/目录搬迁后的历史绝对路径
        normalized = path.replace("\\", "/")
        if "/output/" in normalized:
            rel = normalized.split("/output/", 1)[1]
            candidate = _PROJECT_ROOT / "output" / rel
            if candidate.exists():
                target_path = candidate
        elif normalized.startswith("output/"):
            rel = normalized.split("output/", 1)[1]
            candidate = _PROJECT_ROOT / "output" / rel
            if candidate.exists():
                target_path = candidate

    if not target_path.exists():
        return JSONResponse({"error": f"File not found: {path}"}, status_code=404)
    return FileResponse(str(target_path))
