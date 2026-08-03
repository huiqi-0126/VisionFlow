"""cloneVideo Web - 室内装修视频复刻 Web 界面"""

from __future__ import annotations

import logging
import sys
import threading
import uuid
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import get_settings
from core.project_manager import ProjectManager
from workflow.pipeline import CloneVideoPipeline
from workflow.image_pipeline import ImageClonePipeline

logger = logging.getLogger(__name__)

# ── 初始化 ──────────────────────────────────────────────────────

settings = get_settings()
pipeline = CloneVideoPipeline(settings)
image_pipeline = ImageClonePipeline(settings)
project_mgr = ProjectManager(settings.data_dir, settings.projects_dir)

WEB_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = _PROJECT_ROOT / "output" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="cloneVideo - 室内装修视频复刻", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
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


def _run_quick_cut(job_id: str, project_id: str, clip_paths: list[str], title: str, accent: str, skill: str = "quick-cut") -> None:
    try:
        output_path = _PROJECT_ROOT / "output" / "projects" / project_id / "quick_cut_output.mp4"
        ref_video = _PROJECT_ROOT / "example .mp4"
        
        import os
        import shutil
        env = os.environ.copy()
        ffmpeg_bin_path = r"H:\soft\ffmpeg-n7.1-latest-win64-gpl-7.1\bin"
        env["PATH"] = ffmpeg_bin_path + os.pathsep + env.get("PATH", "")
        ffmpeg_exe = shutil.which("ffmpeg", path=env["PATH"]) or os.path.join(ffmpeg_bin_path, "ffmpeg.exe")

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
        
    shot_plan = project.get("shot_plan", [])
    for shot in shot_plan:
        if shot.get("shot_id") == shot_id:
            shot["clip_status"] = "pending"
            shot["clip_path"] = ""
            # 如果图片也失败了，顺便把图片状态也重置
            if shot.get("image_status") == "failed":
                shot["image_status"] = "pending"
            break
            
    project_mgr.update_project(project_id, shot_plan=shot_plan, status="generating_clips")
    
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
    if not path or not Path(path).exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(path)
