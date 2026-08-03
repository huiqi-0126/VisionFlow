"""镜头规划器 - 基于真实基底帧 + 统一目标风格，为每帧生成改造 prompt 和运镜

新流程（真实帧风格统一改造）下，每个 shot 对应一张真实视频截图
（去人/字后的干净基底），不再由 LLM 想象新房间。

本模块职责：
  - calc_num_shots: 按视频时长推导镜头数 (8~15)
  - build(frames, profile): 为每张基底帧生成
      * camera_move   — 从运镜注册表分配（保证多样性）
      * image_prompt  — "保持布局家具，统一改风格"的 image-edit prompt
      * video_prompt  — 运镜 + 风格的 4s 视频 prompt
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.style_analyzer import StyleProfile
from core.video_analyzer import KeyFrame

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).resolve().parent / "prompts" / "room_view_registry.json"


def _load_registry() -> dict[str, Any]:
    try:
        return json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("room_view_registry.json 加载失败: %s", exc)
        return {"camera_moves_4s": []}


_REGISTRY = _load_registry()
_CAMERA_MOVES: list[dict[str, Any]] = _REGISTRY.get("camera_moves_4s", [])


def calc_num_shots(duration: float, min_shots: int = 8, max_shots: int = 15) -> int:
    """根据源视频时长推导镜头数。

    规则: 基数 5 + 每 8 秒 1 个镜头, 再 clamp 到 [min_shots, max_shots]。

    Examples:
        10s -> 8 (下限)     40s -> 10      72s -> 14
        24s -> 8            56s -> 12      80s+ -> 15 (上限)
        32s -> 9            64s -> 13
    """
    if duration <= 0:
        return min_shots
    raw = round(duration / 8.0) + 5
    return max(min_shots, min(max_shots, raw))


class ShotPlanner:
    """基底帧镜头规划器：为每张真实帧分配运镜并生成改造/视频 prompt"""

    def __init__(self, clip_duration: int = 4) -> None:
        self.clip_duration = clip_duration
        if not _CAMERA_MOVES:
            logger.warning("运镜注册表为空，将使用兜底运镜")

    def build(
        self,
        frames: list[KeyFrame],
        profile: StyleProfile,
    ) -> list[dict[str, Any]]:
        """为每张基底帧构建一个 shot

        Args:
            frames: 真实关键帧（应已设置 clean_path，即去人/字后的基底图）
            profile: 统一目标风格画像

        Returns:
            list of shot dict:
              { shot_id, base_frame_path, base_frame_desc, clean_frame_path,
                camera_move, image_prompt, video_prompt, duration,
                image_status, image_path, clip_status, clip_path }
        """
        shots: list[dict[str, Any]] = []
        for i, frame in enumerate(frames, 1):
            camera = self._assign_camera(i)
            shots.append({
                "shot_id": i,
                "base_frame_path": frame.path,
                "base_frame_desc": frame.description or "",
                "clean_frame_path": frame.clean_path or frame.path,
                "camera_move": camera,
                "image_prompt": self._build_style_prompt(profile),
                "video_prompt": self._build_video_prompt(camera, profile, frame),
                "duration": self.clip_duration,
                "image_status": "pending",
                "image_path": "",
                "clip_status": "pending",
                "clip_path": "",
            })

        logger.info(
            "镜头规划完成: %d 个 shot, 运镜分配 %s",
            len(shots), [s["camera_move"] for s in shots],
        )
        return shots

    # ── 运镜分配 ──────────────────────────────────────────────

    def _assign_camera(self, index: int) -> str:
        """轮询分配运镜，保证相邻 shot 运镜不同"""
        if not _CAMERA_MOVES:
            return "slow_push_in"
        return _CAMERA_MOVES[(index - 1) % len(_CAMERA_MOVES)]["id"]

    # ── Prompt 生成 ───────────────────────────────────────────

    def _build_style_prompt(self, profile: StyleProfile) -> str:
        """统一风格改造 prompt（保持布局家具，只改风格）"""
        descriptor = profile.style_descriptor_en or (
            "modern minimalist interior, warm neutral tones, natural wood, clean lines, soft lighting"
        )
        return (
            f"Transform this interior photo into the following design style: {descriptor}.\n"
            "CRITICAL RULES (must follow exactly):\n"
            "- Keep the EXACT same room layout, camera angle, viewpoint, furniture arrangement "
            "and furniture pieces. Do NOT move, add, remove or replace any furniture, walls, "
            "windows, doors or large objects.\n"
            "- Only change the STYLE: wall colors, surface materials, finishes, flooring, "
            "lighting mood, and small decorative accents.\n"
            "- The composition and spatial geometry must remain identical to the input photo.\n"
            "- No people in the result.\n"
            "Photorealistic architectural rendering, high detail, professional interior photography."
        )

    def _build_video_prompt(
        self,
        camera_move_id: str,
        profile: StyleProfile,
        frame: KeyFrame,
    ) -> str:
        """4s 视频 prompt（运镜 + 风格）"""
        cam_template = next(
            (c.get("en", "") for c in _CAMERA_MOVES if c.get("id") == camera_move_id),
            f"[0s-{self.clip_duration}s]: smooth slow cinematic camera movement",
        )
        descriptor = profile.style_descriptor_en or "modern minimalist interior"
        return (
            f"{cam_template}. The space embodies this style: {descriptor}. "
            "Empty interior, no people, cinematic, photorealistic."
        )
