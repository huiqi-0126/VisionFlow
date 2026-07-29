"""cloneVideo - 室内装修视频复刻 全局配置"""

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

    # --- LLM (OpenAI 兼容, 支持多模态) ---
    qwen_api_key: str = ""
    qwen_baseurl: str = ""
    qwen_model: str = "qwen-plus"
    al_api_key: str = ""
    al_baseurl: str = ""
    al_model: str = "glm-5v-turbo"

    # --- 火山云 TOS ---
    cos_url: str = ""
    secret_id: str = ""
    secret_key: str = ""
    bucket: str = ""
    region: str = ""

    # --- 媒体生成 API (gkapi) ---
    gkapi_baseurl: str = ""
    gkapi_key: str = ""

    # --- 生成默认参数 ---
    # 尺寸跟随源视频: aspect_ratio 在 pipeline 运行时根据源视频分辨率动态设置
    default_video_model: str = "seedance-2.0-fast"
    default_image_model: str = "gemini-3.0"
    default_size: str = "1080p"
    default_duration: str = "4"            # 室内素材固定 4 秒
    default_aspect_ratio: str = "16:9"     # 兜底，运行时会被源视频实际比例覆盖

    # --- 轮询参数 ---
    poll_interval: int = 5
    max_poll_attempts: int = 120

    # --- 视频分析参数 ---
    keyframe_interval: int = 2            # 关键帧提取间隔（秒）
    ffmpeg_path: str = "ffmpeg"

    # --- 镜头规划参数 ---
    # 镜头数按源视频时长智能推导 (见 shot_planner.calc_num_shots), 这里只设上下限
    min_shots: int = 4                    # 最短视频也至少给 4 个镜头
    max_shots: int = 15                   # 无论视频多长, 最多 15 个镜头
    clip_duration: int = 4                # 每个素材片段固定 4 秒

    # --- 路径 ---
    project_root: Path = field(default_factory=lambda: _PROJECT_ROOT)
    data_dir: Path = field(init=False)
    output_dir: Path = field(init=False)
    projects_dir: Path = field(init=False)
    logs_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.al_api_key = os.getenv("AL_API_KEY", "") or os.getenv("qwen_api_key", "")
        self.al_baseurl = os.getenv("AL_BASE_URL", "") or os.getenv("qwen_baseurl", "")
        self.al_model = os.getenv("AL_MODEL", "glm-5v-turbo")
        self.qwen_api_key = os.getenv("qwen_api_key", "")
        self.qwen_baseurl = os.getenv("qwen_baseurl", "")
        self.qwen_model = os.getenv("qwen_model", "qwen-plus")

        self.secret_id = os.getenv("SECRETID", "")
        self.secret_key = os.getenv("SECRETKEY", "")
        self.bucket = os.getenv("BUCKET", "")
        self.region = os.getenv("REGION", "")
        self.cos_url = os.getenv("COC_URL", "")

        self.gkapi_baseurl = os.getenv("gkapi_baseurl", "")
        self.gkapi_key = os.getenv("gkapi_key", "")

        self.default_video_model = os.getenv("MODEL", "seedance-2.0-fast")
        self.default_image_model = os.getenv("IMAGE_MODEL", "gemini-3.0")
        self.ffmpeg_path = os.getenv("FFMPEG_PATH", "ffmpeg")

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
