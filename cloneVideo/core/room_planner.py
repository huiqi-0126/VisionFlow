"""房间规划器 - 图片模式：基于一张客厅参考图，规划"同风格其他房间"清单

与 shot_planner（视频模式，基于真实帧改造）不同：
  这里输入是【一张客厅参考图】，要生成【其他房间】（卧室/厨房/卫生间等）。
  风格一致性靠两层锚定：
    1. 生成时把客厅参考图作为 pic 传给 image-edit 模型（Nano Banana 风格保持）
    2. image_prompt 里明确"与参考图同风格"并注入 style_descriptor_en
  用【规则模板】生成 prompt（不用 LLM 想象），严格控制不发散。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.style_analyzer import StyleProfile

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).resolve().parent / "prompts" / "room_view_registry.json"


def _load_registry() -> dict[str, Any]:
    try:
        return json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("room_view_registry.json 加载失败: %s", exc)
        return {"room_types": [], "viewpoints": [], "camera_moves_4s": []}


_REGISTRY = _load_registry()
_ROOM_TYPES: list[dict[str, Any]] = _REGISTRY.get("room_types", [])
_VIEWPOINTS: list[dict[str, Any]] = _REGISTRY.get("viewpoints", [])
_CAMERA_MOVES: list[dict[str, Any]] = _REGISTRY.get("camera_moves_4s", [])

# 图片模式默认生成的"其他房间"（排除客厅本身），按优先级排序
_DEFAULT_OTHER_ROOMS = [
    "master_bedroom",   # 主卧
    "second_bedroom",   # 次卧
    "kitchen",          # 厨房
    "bathroom",         # 卫生间
    "dining_area",      # 餐厅
    "study",            # 书房
    "entryway",         # 玄关
    "balcony",          # 阳台
    "walkin_closet",    # 衣帽间
]

# 兜底房间英文描述（注册表缺失时用）
_FALLBACK_ROOM_EN = {
    "master_bedroom": "master bedroom",
    "second_bedroom": "secondary bedroom",
    "kitchen": "modern kitchen",
    "bathroom": "bathroom",
    "dining_area": "dining area",
    "study": "home study",
    "entryway": "entryway foyer",
    "balcony": "balcony",
    "walkin_closet": "walk-in closet",
}


class RoomPlanner:
    """图片模式房间规划器：同风格其他房间清单"""

    def __init__(self, clip_duration: int = 4) -> None:
        self.clip_duration = clip_duration
        if not _ROOM_TYPES:
            logger.warning("room_types 注册表为空，将用兜底房间描述")

    def plan(
        self,
        profile: StyleProfile,
        source_room_id: str = "living_room",
        num_rooms: int = 7,
        room_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """规划"同风格其他房间"镜头清单

        Args:
            profile: 由客厅参考图分析出的风格画像
            source_room_id: 参考图的房间类型(默认客厅), 会被排除
            num_rooms: 生成几个其他房间(默认7)
            room_ids: 显式指定房间清单(优先于 num_rooms/默认清单)

        Returns:
            list of shot dict:
              { shot_id, room_type, viewpoint, camera_move,
                image_prompt, video_prompt, duration,
                image_status, image_path, clip_status, clip_path }
        """
        # 确定房间清单
        if room_ids:
            chosen = [r for r in room_ids if r != source_room_id]
        else:
            chosen = [r for r in _DEFAULT_OTHER_ROOMS if r != source_room_id][:num_rooms]

        shots: list[dict[str, Any]] = []
        for i, room_id in enumerate(chosen, 1):
            viewpoint = self._assign_viewpoint(i)
            camera = self._assign_camera(i)
            room_en = self._room_en(room_id)
            view_en = self._view_en(viewpoint)
            shots.append({
                "shot_id": i,
                "room_type": room_id,
                "viewpoint": viewpoint,
                "camera_move": camera,
                "image_prompt": self._build_image_prompt(room_en, view_en, profile),
                "video_prompt": self._build_video_prompt(camera, room_en, profile),
                "duration": self.clip_duration,
                "image_status": "pending",
                "image_path": "",
                "clip_status": "pending",
                "clip_path": "",
            })

        logger.info(
            "房间规划完成: %d 个其他房间 (源: %s) | %s",
            len(shots), source_room_id, [s["room_type"] for s in shots],
        )
        return shots

    # ── 分配 ──────────────────────────────────────────────────

    def _assign_viewpoint(self, index: int) -> str:
        """轮询分配视角（优先全景，再多样化）"""
        if not _VIEWPOINTS:
            return "wide_corner"
        # 第一个房间用全景(wide_corner)，其余轮询其他视角，保证素材丰富
        if index == 1 and any(v["id"] == "wide_corner" for v in _VIEWPOINTS):
            return "wide_corner"
        return _VIEWPOINTS[(index - 1) % len(_VIEWPOINTS)]["id"]

    def _assign_camera(self, index: int) -> str:
        """轮询分配运镜，保证相邻不同"""
        if not _CAMERA_MOVES:
            return "slow_push_in"
        return _CAMERA_MOVES[(index - 1) % len(_CAMERA_MOVES)]["id"]

    def _room_en(self, room_id: str) -> str:
        for r in _ROOM_TYPES:
            if r["id"] == room_id:
                return r.get("en", room_id)
        return _FALLBACK_ROOM_EN.get(room_id, room_id.replace("_", " "))

    def _view_en(self, viewpoint_id: str) -> str:
        for v in _VIEWPOINTS:
            if v["id"] == viewpoint_id:
                return v.get("en", viewpoint_id)
        return "wide-angle view"

    # ── Prompt 生成（规则模板，严格锁风格）────────────────────

    def _build_image_prompt(self, room_en: str, view_en: str, profile: StyleProfile) -> str:
        descriptor = profile.style_descriptor_en or (
            "modern minimalist interior, warm neutral tones, natural wood, clean lines, soft lighting"
        )
        return (
            f"Using the provided reference image's interior design style, generate a {room_en} "
            f"({view_en}) that belongs to the SAME home / SAME project.\n"
            "CRITICAL STYLE CONSISTENCY RULES (must follow exactly):\n"
            f"- Match the reference image's design style EXACTLY: {descriptor}.\n"
            "- Use the SAME color palette, SAME materials, SAME furniture design language, "
            "SAME lighting mood and SAME level of finish as the reference.\n"
            "- This is a DIFFERENT room of the same house — only the room type and its layout "
            "are different. Everything about the STYLE stays identical.\n"
            "- Empty interior, no people, no text, no watermark.\n"
            "Photorealistic architectural photography, high detail, professional interior shot."
        )

    def _build_video_prompt(self, camera_move_id: str, room_en: str, profile: StyleProfile) -> str:
        cam_template = next(
            (c.get("en", "") for c in _CAMERA_MOVES if c.get("id") == camera_move_id),
            f"[0s-{self.clip_duration}s]: smooth slow cinematic camera movement",
        )
        descriptor = profile.style_descriptor_en or "modern minimalist interior"
        return (
            f"{cam_template}. A {room_en} in this exact style: {descriptor}. "
            "Empty interior, no people, cinematic, photorealistic."
        )
