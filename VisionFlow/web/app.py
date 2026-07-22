"""VisionFlow Web - 视频复刻工具 Web 界面"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import sys

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import get_settings
from core.plan_manager import PlanManager
from core.project_manager import ProjectManager
from workflow.pipeline import ReplicationPipeline
from workflow.planner_pipeline import PlannerPipeline
from core.cos_client import COSClient

logger = logging.getLogger(__name__)

# ── 初始化 ──────────────────────────────────────────────────────

settings = get_settings()
pipeline = ReplicationPipeline(settings)
project_mgr = ProjectManager(settings.data_dir, settings.projects_dir)
plan_mgr = PlanManager(settings.data_dir, settings.projects_dir)

WEB_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = _PROJECT_ROOT / "output" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="VisionFlow - 视频复刻工具", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# 静态文件服务：让 output/projects 目录可访问
app.mount("/output", StaticFiles(directory=str(_PROJECT_ROOT / "output")), name="output")

# ── 后台任务追踪 ────────────────────────────────────────────────

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _run_replicate(job_id: str, video_path: str, style_hint: str = "", mode: str = "auto") -> None:
    try:
        result = pipeline.run(video_path, style_hint=style_hint, mode=mode)
        with _jobs_lock:
            _jobs[job_id]["status"] = result.get("status", "failed")
            _jobs[job_id]["project_id"] = result.get("project_id", "")
            _jobs[job_id]["result"] = result
    except Exception as exc:
        logger.error("Job %s failed: %s", job_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)


def _run_plan(job_id: str, persona_data: dict[str, Any], generate_videos: bool) -> None:
    """后台执行30天内容规划"""
    try:
        from core.content_planner import Persona
        planner_pipe = PlannerPipeline(settings, plan_mgr=plan_mgr)
        persona = Persona.from_dict(persona_data)
        result = planner_pipe.run(persona, generate_videos=generate_videos)
        with _jobs_lock:
            _jobs[job_id]["status"] = result.get("status", "failed")
            _jobs[job_id]["plan_id"] = result.get("plan_id", "")
    except Exception as exc:
        logger.error("Plan job %s failed: %s", job_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)


def _run_single_video(job_id: str, plan_id: str, day: int) -> None:
    """后台生成单天视频"""
    try:
        planner_pipe = PlannerPipeline(settings, plan_mgr=plan_mgr)
        result_script = planner_pipe.generate_video_for_day(plan_id, day)
        with _jobs_lock:
            _jobs[job_id]["status"] = result_script.get("video_status", "failed")
            _jobs[job_id]["video_file"] = result_script.get("video_file")
    except Exception as exc:
        logger.error("Video generation job %s failed: %s", job_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)

def _run_generate_images(job_id: str, plan_id: str, day: int) -> None:
    """后台生成单天配图（图文平台 reddit/Twitter/FB）"""
    try:
        planner_pipe = PlannerPipeline(settings, plan_mgr=plan_mgr)
        result_script = planner_pipe.generate_images_for_day(plan_id, day)
        # 统计配图完成情况：全部完成=done，部分=partial，全失败=failed
        frames = result_script.get("frames", [])
        done = sum(1 for f in frames if f.get("image_status") == "done")
        total = len(frames)
        if total > 0 and done == total:
            status = "done"
        elif done > 0:
            status = "partial"
        else:
            status = "failed"
        with _jobs_lock:
            _jobs[job_id]["status"] = status
            _jobs[job_id]["images_done"] = done
            _jobs[job_id]["images_total"] = total
    except Exception as exc:
        logger.error("Image generation job %s failed: %s", job_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)

def _run_regenerate_script(job_id: str, plan_id: str, day: int) -> None:
    """后台重新生成单天脚本"""
    try:
        planner_pipe = PlannerPipeline(settings, plan_mgr=plan_mgr)
        planner_pipe.regenerate_script_for_day(plan_id, day)
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
    except Exception as exc:
        logger.error("Script regeneration job %s failed: %s", job_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)

def _run_regenerate_all(job_id: str, plan_id: str, day: int) -> None:
    """后台完全重新生成单天日历和脚本"""
    try:
        planner_pipe = PlannerPipeline(settings, plan_mgr=plan_mgr)
        planner_pipe.regenerate_all_for_day(plan_id, day)
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
    except Exception as exc:
        logger.error("Complete regeneration job %s failed: %s", job_id, exc)
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)


def _run_regenerate_all_scripts(job_id: str, plan_id: str) -> None:
    """后台重新生成全部 30 天脚本(保留日历)"""
    try:
        planner_pipe = PlannerPipeline(settings, plan_mgr=plan_mgr)
        planner_pipe.regenerate_all_scripts(plan_id)
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
    except Exception as exc:
        logger.error("Regenerate-all-scripts job %s failed: %s", job_id, exc)
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


@app.get("/planner", response_class=HTMLResponse)
async def page_planner(request: Request):
    plans = plan_mgr.list_plans()
    return templates.TemplateResponse("planner.html", {
        "request": request,
        "plans": plans,
    })


@app.get("/plan/{plan_id}", response_class=HTMLResponse)
async def page_plan_detail(request: Request, plan_id: str):
    plan = plan_mgr.get_plan(plan_id)
    if not plan:
        return JSONResponse({"error": "规划项目不存在"}, status_code=404)
    return templates.TemplateResponse("plan_detail.html", {
        "request": request,
        "plan": plan,
    })


# ═════════════════════════════════════════════════════════════════
# API 路由
# ═════════════════════════════════════════════════════════════════

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    """上传视频文件"""
    if not file.filename:
        return JSONResponse({"error": "未选择文件"}, status_code=400)

    # 检查文件类型
    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv"):
        return JSONResponse({"error": f"不支持的视频格式: {suffix}"}, status_code=400)

    # 保存文件
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
    """开始视频复刻任务（后台执行）"""
    data = await request.json()
    video_path = data.get("video_path", "")
    style_hint = data.get("style", "")  # 用户选择的风格方向
    mode = data.get("mode", "auto")  # fixed 或 auto

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

    t = threading.Thread(
        target=_run_replicate,
        args=(job_id, video_path, style_hint, mode),
        daemon=True,
    )
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


# ═════════════════════════════════════════════════════════════════
# 内容规划 API
# ═════════════════════════════════════════════════════════════════

@app.post("/api/plans")
async def api_create_plan(request: Request):
    """创建并启动30天内容规划任务（后台执行）"""
    data = await request.json()
    persona_data = data.get("persona", {})
    generate_videos = data.get("generate_videos", False)

    if not persona_data.get("occupation"):
        return JSONResponse({"error": "请至少填写职业"}, status_code=400)

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "plan",
            "status": "running",
            "plan_id": "",
        }

    t = threading.Thread(
        target=_run_plan,
        args=(job_id, persona_data, generate_videos),
        daemon=True,
    )
    t.start()

    return {"job_id": job_id, "status": "started"}

@app.get("/api/media")
async def api_media(path: str = Query(...)):
    """直接服务本地媒体文件"""
    if not path or not Path(path).exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    from fastapi.responses import FileResponse
    return FileResponse(path)

@app.post("/api/plans/{plan_id}/day/{day}/generate_video")
async def api_plan_generate_video(plan_id: str, day: int):
    """为特定的一天生成视频（后台执行）"""
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "generate_single_video",
            "status": "running",
            "plan_id": plan_id,
            "day": day,
        }

    t = threading.Thread(
        target=_run_single_video,
        args=(job_id, plan_id, day),
        daemon=True,
    )
    t.start()

    return {"job_id": job_id, "status": "started"}

@app.post("/api/plans/{plan_id}/day/{day}/generate_images")
async def api_plan_generate_images(plan_id: str, day: int):
    """为特定的一天生成配图（图文平台 reddit/Twitter/FB，后台执行）

    图文平台的内容规划阶段不生成真实配图，由用户在 Web 端手动触发此接口。
    为该天脚本里的每个 frame 调用图片生成 API。
    """
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "generate_images",
            "status": "running",
            "plan_id": plan_id,
            "day": day,
        }

    t = threading.Thread(
        target=_run_generate_images,
        args=(job_id, plan_id, day),
        daemon=True,
    )
    t.start()

    return {"job_id": job_id, "status": "started"}

@app.post("/api/plans/{plan_id}/day/{day}/regenerate_script")
async def api_plan_regenerate_script(plan_id: str, day: int):
    """重新生成特定的一天脚本（后台执行）"""
    # 埋点（必须在启动后台任务前完成，否则 plan 里的旧 script 已被覆盖）：
    # 用户点"重新生成脚本" = 对当前视频效果不满意 = negative 样本
    plan_mgr.record_feedback(plan_id, day, "negative", "regenerate_script")

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "regenerate_script",
            "status": "running",
            "plan_id": plan_id,
            "day": day,
        }

    t = threading.Thread(
        target=_run_regenerate_script,
        args=(job_id, plan_id, day),
        daemon=True,
    )
    t.start()

    return {"job_id": job_id, "status": "started"}

@app.post("/api/plans/{plan_id}/day/{day}/regenerate_all")
async def api_plan_regenerate_all(plan_id: str, day: int):
    """完全重新生成特定的一天内容（后台执行）"""
    # 埋点：regenerate_all 比 regenerate_script 信号更强（连内容主题都不满意），
    # 但仍归为 negative。同样必须在后台任务前记录。
    plan_mgr.record_feedback(plan_id, day, "negative", "regenerate_all")

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "regenerate_all",
            "status": "running",
            "plan_id": plan_id,
            "day": day,
        }

    t = threading.Thread(
        target=_run_regenerate_all,
        args=(job_id, plan_id, day),
        daemon=True,
    )
    t.start()

    return {"job_id": job_id, "status": "started"}

@app.post("/api/plans/{plan_id}/regenerate_all_scripts")
async def api_plan_regenerate_all_scripts(plan_id: str):
    """重新生成全部 30 天脚本（保留日历不变，后台执行）"""
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "type": "regenerate_all_scripts",
            "status": "running",
            "plan_id": plan_id,
        }

    t = threading.Thread(
        target=_run_regenerate_all_scripts,
        args=(job_id, plan_id),
        daemon=True,
    )
    t.start()

    return {"job_id": job_id, "status": "started"}

@app.get("/api/devices")
async def api_get_devices():
    devices = set()
    
    # 1. Fetch from phoneRPA db (historical)
    try:
        import pymysql
        conn = pymysql.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_pass,
            database=settings.db_name,
            charset='utf8mb4'
        )
        with conn.cursor() as cursor:
            cursor.execute('SELECT DISTINCT device_id FROM rpa_post_plans WHERE device_id IS NOT NULL AND device_id != ""')
            for row in cursor.fetchall():
                devices.add(row[0])
        conn.close()
    except Exception as e:
        logger.error(f"Failed to fetch historical devices: {e}")
        
    # 2. Add MCP URL devices if any
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        import os
        mcp_url = settings.mcp_url or "https://rc.guokecs.com/mcp"
        mcp_key = settings.mcp_key
        headers = {"Authorization": f"Bearer {mcp_key}"}
        
        async with streamablehttp_client(url=mcp_url, headers=headers) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("list_devices", {})
                if result.content and hasattr(result.content[0], "text"):
                    text = result.content[0].text
                    import re
                    for block in text.split("---"):
                        block = block.strip()
                        if not block: continue
                        id_match = re.search(r"ID:\s*(\S+)", block)
                        if id_match:
                            devices.add(id_match.group(1).strip())
    except BaseException as e:
        logger.error(f"Failed to fetch MCP devices: {e}")
        
    return {"devices": sorted(list(devices))}

class ScheduleVideoRequest(BaseModel):
    device_id: str
    scheduled_time: str
    description: str
    tags: str
    platforms: list[str] = ["tiktok_post"]
    image_files: list[str] = []  # 图文:用户在弹窗里删除后保留的图片路径;空=取全部

@app.post("/api/plans/{plan_id}/day/{day}/schedule")
async def api_plan_schedule_video(plan_id: str, day: int, req: ScheduleVideoRequest):
    """将内容设为定时发布任务，插入 phoneRPA 数据库

    支持两种内容类型：
    - 视频 (TK/YT): 上传 video_file, task_type 用 req.platforms (tiktok_post/youtube_post)
    - 图文 (reddit/Twitter/FB): 上传所有配图, task_type 由 persona.platform 自动映射
      (reddit→reddit_post, Twitter→twitter_post, FB→fb_post)
    """
    plan = plan_mgr.get_plan(plan_id)
    if not plan:
        return JSONResponse({"error": "规划项目不存在"}, status_code=404)

    script = next((s for s in plan.get("scripts", []) if s.get("day") == day), None)
    if not script:
        return JSONResponse({"error": "找不到该天的脚本"}, status_code=404)

    is_image_text = script.get("content_type") == "image_text"
    frames = script.get("frames") or []

    # ── 媒体校验 ──
    image_files: list[str] = []
    if is_image_text:
        all_imgs = [f.get("image_file") for f in frames if f.get("image_file")]
        # 优先用前端传的(用户在弹窗里删除后保留的图),否则取全部
        image_files = req.image_files if req.image_files else all_imgs
        if not image_files:
            return JSONResponse(
                {"error": "该天尚未生成配图，请先点击\"生成配图\""}, status_code=400
            )
    else:
        if not script.get("video_file"):
            return JSONResponse({"error": "找不到该天的视频"}, status_code=404)

    try:
        local_scheduled = datetime.strptime(req.scheduled_time, "%Y-%m-%dT%H:%M")
    except ValueError:
        return JSONResponse({"error": "发布时间格式错误"}, status_code=400)

    # iPhoneRPA 要求 UTC 时间 (假设东八区)
    utc_scheduled = local_scheduled - timedelta(hours=8)

    # ── 平台: 图文由 persona.platform 决定 task_type; 视频用前端传入的 platforms ──
    if is_image_text:
        persona = plan.get("persona", {})
        pf = (persona.get("platform") or "").strip()
        _platform_map = {
            "reddit": "reddit_post",
            "Twitter": "twitter_post",
            "FB": "fb_post",
        }
        platforms = [_platform_map.get(pf, "reddit_post")]
    else:
        platforms = req.platforms

    # ── 上传媒体到腾讯云 COS ──
    # final_media_url: 主 URL (视频 URL 或首图 URL), 存 video_url 字段 (NOT NULL)
    # image_urls_json: 图文所有图片 URL 的 JSON 数组, 存 image_urls 字段
    final_media_url = ""
    cover_url: str | None = None
    image_urls_json: str | None = None
    description_text = req.description

    cos_configured = bool(settings.secret_id and settings.secret_key and settings.bucket)

    def _to_local_url(p: str) -> str:
        p = p.replace("\\", "/")
        if p.startswith("http") or p.startswith("file://"):
            return p
        return "file:///" + p.lstrip("/")

    if cos_configured:
        try:
            cos = COSClient(
                secret_id=settings.secret_id,
                secret_key=settings.secret_key,
                region=settings.region,
                bucket=settings.bucket,
                base_url=settings.cos_url,
            )
            if is_image_text:
                uploaded = []
                for f in image_files:
                    if not os.path.isabs(f):
                        f = os.path.abspath(f)
                    uploaded.append(cos.upload_file(f))
                final_media_url = ",".join(uploaded) if uploaded else ""
                cover_url = uploaded[0] if uploaded else ""
                image_urls_json = ",".join(uploaded)
                # 图文 description: 优先用前端传的(用户在 modal 看到/编辑过 captions);
                # 前端没传时回退到 frames caption 拼接
                captions = [fr.get("caption", "") for fr in frames if fr.get("image_file")]
                body = "\n\n".join(c for c in captions if c)
                description_text = req.description or body
            else:
                video_path = script["video_file"]
                if not os.path.isabs(video_path):
                    video_path = os.path.abspath(video_path)
                final_media_url = cos.upload_file(video_path)
        except Exception as e:
            logger.error(f"COS 上传失败: {e}")
            label = "配图" if is_image_text else "视频"
            return JSONResponse({"error": f"上传{label}到COS失败: {e}"}, status_code=500)
    else:
        # 未配置 COS, 退回本地路径 (仅开发调试用)
        if is_image_text:
            local_urls = [_to_local_url(f) for f in image_files]
            final_media_url = ",".join(local_urls) if local_urls else ""
            cover_url = local_urls[0] if local_urls else ""
            image_urls_json = ",".join(local_urls)
            captions = [fr.get("caption", "") for fr in frames if fr.get("image_file")]
            body = "\n\n".join(c for c in captions if c)
            description_text = req.description or body
        else:
            final_media_url = _to_local_url(script["video_file"])

    try:
        import pymysql
        conn = pymysql.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_pass,
            database=settings.db_name,
            charset='utf8mb4',
            autocommit=True,
        )
        with conn.cursor() as cursor:
            # 找到 admin 用户作为创建者
            cursor.execute("SELECT id FROM rpa_users WHERE username='admin' LIMIT 1")
            row = cursor.fetchone()
            admin_id = row[0] if row else "admin"

            import socket as _socket
            _hostname = _socket.gethostname().lower().replace('-', '_').replace('.', '_')
            rpa_env = os.environ.get('RPA_ENV', _hostname)

            created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            scheduled_at = utc_scheduled.strftime("%Y-%m-%d %H:%M:%S")

            # 图文: title 单独存(reddit 等平台标题/正文分开);视频无 title
            title_text = script.get("title_en", "") if is_image_text else None

            for platform in platforms:
                plan_record_id = uuid.uuid4().hex
                cursor.execute('''
                    INSERT INTO rpa_post_plans (
                        id, video_url, description, title, tags, task_type,
                        device_id, scheduled_at, status, created_by, created_at,
                        cover_url, image_urls
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (
                    plan_record_id,
                    final_media_url,
                    description_text,
                    title_text,
                    req.tags,
                    platform,
                    req.device_id,
                    scheduled_at,
                    "pending_ntx_dt_1xch74",
                    admin_id,
                    created_at,
                    cover_url,
                    image_urls_json,
                ))

        conn.close()

        # 埋点：用户点"定时发布" = 认可内容 = positive 样本
        plan_mgr.record_feedback(
            plan_id, day, "positive", "schedule",
            device_id=req.device_id,
        )

        return {"ok": True, "scheduled_at": local_scheduled.strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e:
        logger.error(f"Failed to schedule: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/plans")
async def api_plans_list():
    plans = plan_mgr.list_plans()
    return list(reversed(plans))


@app.get("/api/plans/{plan_id}")
async def api_plan_detail(plan_id: str):
    plan = plan_mgr.get_plan(plan_id)
    if not plan:
        return JSONResponse({"error": "规划项目不存在"}, status_code=404)
    return plan


@app.get("/api/plans/{plan_id}/export")
async def api_plan_export(plan_id: str):
    import json as _json
    plan = plan_mgr.get_plan(plan_id)
    if not plan:
        return JSONResponse({"error": "规划项目不存在"}, status_code=404)
    return JSONResponse(
        content=plan,
        headers={
            "Content-Disposition": f'attachment; filename="plan_{plan_id}.json"',
        },
    )


@app.delete("/api/plans/{plan_id}")
async def api_plan_delete(plan_id: str):
    ok = plan_mgr.delete_plan(plan_id)
    if ok:
        return {"ok": True}
    return JSONResponse({"error": "规划项目不存在"}, status_code=404)
