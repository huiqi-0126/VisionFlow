"""项目管理器 - JSON 文件持久化的复刻项目管理

每个项目记录一次室内装修视频复刻的全部状态:
  关键帧 / 风格画像 / 参考帧 / 镜头清单 / 生成的图片和视频素材
"""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROJECTS_FILE = "projects.json"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_projects(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("读取项目文件失败: %s，初始化为空", exc)
        return []


def _save_projects(path: Path, projects: list[dict[str, Any]]) -> None:
    """原子写入：先写临时文件再重命名，防止写入中断导致数据丢失"""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(projects, ensure_ascii=False, indent=2)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with open(fd, "w", encoding="utf-8") as f:
            f.write(content)
        tmp_path = Path(tmp)
        tmp_path.replace(path)  # 原子操作
    except OSError:
        # fallback: 直接写入
        path.write_text(content, encoding="utf-8")


class ProjectManager:
    """基于 JSON 文件的复刻项目管理"""

    def __init__(self, data_dir: str | Path, projects_output_dir: str | Path) -> None:
        self._path = Path(data_dir) / _PROJECTS_FILE
        self._output_dir = Path(projects_output_dir)
        self._projects: list[dict[str, Any]] = _load_projects(self._path)

    def _flush(self) -> None:
        _save_projects(self._path, self._projects)

    # ── CRUD ──────────────────────────────────────────────────

    def create_project(
        self,
        source_video: str,
        source_resolution: tuple[int, int] = (0, 0),
        aspect_ratio: str = "16:9",
    ) -> dict[str, Any]:
        """创建新的复刻项目

        Args:
            source_resolution: 源视频分辨率 (width, height)，用于让生成尺寸跟随源视频
            aspect_ratio: 由源视频分辨率推导的 "W:H" 字符串
        """
        project_id = uuid.uuid4().hex[:8]

        project_dir = self._output_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)

        project: dict[str, Any] = {
            "project_id": project_id,
            "status": "uploaded",
            "source_video": source_video,
            "source_resolution": {"width": source_resolution[0], "height": source_resolution[1]},
            "aspect_ratio": aspect_ratio,
            "video_duration": 0.0,
            "output_dir": str(project_dir),
            "keyframes": [],            # 抽取的关键帧
            "reference_frames": [],     # 风格代表性参考帧路径（本地）
            "style_profile": {},        # 风格画像 JSON
            "shot_plan": [],            # 镜头清单（每条含 room/viewpoint/camera/prompt）
            "images": [],               # 生成的素材图片路径
            "clips": [],                # 生成的素材视频路径
            "error": "",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }

        self._projects.append(project)
        self._flush()
        logger.info("项目已创建: %s", project_id)
        return project

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        for p in self._projects:
            if p["project_id"] == project_id:
                return p
        return None

    def update_project(self, project_id: str, **kwargs: Any) -> dict[str, Any]:
        for p in self._projects:
            if p["project_id"] == project_id:
                p.update(kwargs)
                p["updated_at"] = _now_iso()
                self._flush()
                return p
        logger.warning("更新项目未找到: %s", project_id)
        return {}

    def update_shot(
        self,
        project_id: str,
        shot_id: int,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """更新镜头清单中某个 shot 的字段"""
        project = self.get_project(project_id)
        if not project:
            return None
        for shot in project.get("shot_plan", []):
            if shot.get("shot_id") == shot_id:
                shot.update(kwargs)
                self._flush()
                return shot
        return None

    def list_projects(
        self,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        result = self._projects
        if status:
            result = [p for p in result if p.get("status") == status]
        return result[-limit:]

    def delete_project(self, project_id: str) -> bool:
        before = len(self._projects)
        self._projects = [p for p in self._projects if p["project_id"] != project_id]
        if len(self._projects) < before:
            self._flush()
            logger.info("项目已删除: %s", project_id)
            return True
        return False

    def get_stats(self) -> dict[str, Any]:
        total = len(self._projects)
        by_status: dict[str, int] = {}
        for p in self._projects:
            s = p.get("status", "unknown")
            by_status[s] = by_status.get(s, 0) + 1
        return {"total": total, "by_status": by_status}
