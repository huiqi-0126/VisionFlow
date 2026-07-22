"""VisionFlow - 视频复刻工具 全局配置"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")


@dataclass
class Settings:
    """所有运行时配置，来源于 .env 文件 + 合理默认值"""

    # --- LLM ---
    qwen_api_key: str = ""
    qwen_baseurl: str = ""
    qwen_model: str = "qwen-plus"
    al_api_key: str = ""
    al_baseurl: str = ""
    al_model: str = "kimi-k2.7"

    # --- 改稿模型(anthropic 兼容接口,用于去 AI 味/真人化改写)---
    humanize_api_key: str = ""
    humanize_base_url: str = ""
    humanize_model: str = "MiniMax-M3"

    # --- 腾讯云 COS ---
    cos_url: str = ""
    secret_id: str = ""
    secret_key: str = ""
    bucket: str = ""
    region: str = ""

    # --- 视频/图片生成 API ---
    gkapi_baseurl: str = ""
    gkapi_key: str = ""

    # --- 数据库 (phoneRPA) ---
    db_host: str = "192.168.5.64"
    db_port: int = 3306
    db_user: str = "root"
    db_pass: str = "vC5vL7rA0jH1"
    db_name: str = "phoneRPA"
    
    # --- MCP 接口 ---
    mcp_url: str = ""
    mcp_key: str = ""

    # --- 生成默认参数 ---
    default_video_model: str = "seedance-2.0-fast"
    default_image_model: str = "gemini-3.0"  # gkapi id,displayName=Nano Banana Pro
    default_size: str = "720p"
    default_duration: str = "15"
    default_aspect_ratio: str = "9:16"

    # --- 轮询参数 ---
    poll_interval: int = 5
    max_poll_attempts: int = 120

    # --- 视频分析参数 ---
    keyframe_interval: int = 2       # 关键帧提取间隔（秒）
    ffmpeg_path: str = "ffmpeg"      # ffmpeg 可执行文件路径

    # --- 路径 ---
    project_root: Path = field(default_factory=lambda: _PROJECT_ROOT)
    data_dir: Path = field(init=False)
    output_dir: Path = field(init=False)
    projects_dir: Path = field(init=False)
    logs_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.qwen_api_key = os.getenv("qwen_api_key", "")
        self.qwen_baseurl = os.getenv("qwen_baseurl", "")
        self.default_video_model = os.getenv("MODEL", "")
        self.default_image_model = os.getenv("IMAGE_MODEL", "gemini-3.0")
        self.al_api_key = os.getenv("AL_API_KEY", "")
        self.al_baseurl = os.getenv("AL_BASE_URL", "")
        self.al_model = os.getenv("AL_MODEL", "glm-5v-turbo")
        self.humanize_api_key = os.getenv("HUMANIZE_API_KEY", "")
        self.humanize_base_url = os.getenv("HUMANIZE_BASE_URL", "")
        self.humanize_model = os.getenv("HUMANIZE_MODEL", "MiniMax-M3")
        self.secret_id = os.getenv("SECRETID", "")
        self.secret_key = os.getenv("SECRETKEY", "")
        self.bucket = os.getenv("BUCKET", "")
        self.region = os.getenv("REGION", "")
        self.cos_url = os.getenv("COC_URL", "")
        self.gkapi_baseurl = os.getenv("gkapi_baseurl", "")
        self.gkapi_key = os.getenv("gkapi_key", "")
        self.ffmpeg_path = os.getenv("FFMPEG_PATH", "ffmpeg")

        self.db_host = os.getenv("DB_HOST", "192.168.5.64")
        self.db_port = int(os.getenv("DB_PORT", "3306"))
        self.db_user = os.getenv("DB_USER", "root")
        self.db_pass = os.getenv("DB_PASS", "vC5vL7rA0jH1")
        self.db_name = os.getenv("DB_NAME", "phoneRPA")
        
        self.mcp_url = os.getenv("MCP_URL", "https://rc.guokecs.com/mcp")
        self.mcp_key = os.getenv("MCP_API_KEY", "")

        self.data_dir = self.project_root / "data"
        self.output_dir = self.project_root / "output"
        self.projects_dir = self.output_dir / "projects"
        self.logs_dir = self.project_root / "logs"

        for d in (self.data_dir, self.projects_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    """获取全局单例配置"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
