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

logger = logging.getLogger(__name__)

# ── 初始化 ──────────────────────────────────────────────────────

settings = get_settings()
pipeline = CloneVideoPipeline(settings)
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


def _run_quick_cut(job_id: str, project_id: str, clip_paths: list[str], title: str, accent: str) -> None:
    try:
        output_path = _PROJECT_ROOT / "output" / "projects" / project_id / "quick_cut_output.mp4"
        ref_video = _PROJECT_ROOT / "example .mp4"
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
        
        import os
        env = os.environ.copy()
        ffmpeg_bin_path = r"H:\soft\ffmpeg-n7.1-latest-win64-gpl-7.1\bin"
        env["PATH"] = ffmpeg_bin_path + os.pathsep + env.get("PATH", "")
        
        logger.info(f"Running quick_cut for {project_id}: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=env)
        
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["output_path"] = str(output_path)
    except subprocess.CalledProcessError as exc:
        logger.error(f"Quick cut job {job_id} failed: {exc.stderr}")
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = exc.stderr
    except Exception as exc:
        logger.error(f"Quick cut job {job_id} failed: {exc}")
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
    """上传视频文件"""
    if not file.filename:
        return JSONResponse({"error": "未选择文件"}, status_code=400)

    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv"):
        return JSONResponse({"error": f"不支持的视频格式: {suffix}"}, status_code=400)

    file_id = uuid.uuid4().hex[:8]
    save_path = UPLOAD_DIR / f"source_{file_id}{suffix}"
    content = await file.read()
    save_path.write_bytes(content)

    file_size_mb = len(content) / (1024 * 1024)
    logger.info("视频上传: %s (%.1f MB) → %s", file.filename, file_size_mb, save_path)

    return {
        "path": str(save_path),
        "filename": file.filename,
        "size_mb": round(file_size_mb, 2),
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


@app.post("/api/projects/{project_id}/quick_cut")
async def api_quick_cut(project_id: str, request: Request):
    """智能剪辑: 基于选择的素材和 quick-cut 脚本生成混剪视频"""
    if not project_mgr.get_project(project_id):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
        
    data = await request.json()
    clip_paths = data.get("clip_paths", [])
    title = data.get("title", "")
    accent = data.get("accent", "")
    
    if not clip_paths:
        return JSONResponse({"error": "未选择任何视频素材"}, status_code=400)
    if not title:
        return JSONResponse({"error": "未填写标题"}, status_code=400)
        
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "quick_cut",
            "status": "running",
            "project_id": project_id,
        }
        
    t = threading.Thread(target=_run_quick_cut, args=(job_id, project_id, clip_paths, title, accent), daemon=True)
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
