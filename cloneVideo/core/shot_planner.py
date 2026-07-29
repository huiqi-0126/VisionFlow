"""镜头规划器 - 基于风格画像生成 N 个镜头清单(数量由 calc_num_shots 按视频时长推导)

每个 shot = (房间类型, 视角, 4秒运镜) 的组合，配 image_prompt / video_prompt。
风格画像的 style_descriptor_en（风格锁）会被注入到每个 prompt 中，
确保不同房间/视角下生成的是同一套装修风格。

注册表 room_view_registry.json 提供：room_types / viewpoints / camera_moves_4s /
shot_planning_prompt 模板。LLM 负责多样性地挑选组合并撰写英文 prompt。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.llm_client import LLMClient
from core.style_analyzer import StyleProfile

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).resolve().parent / "prompts" / "room_view_registry.json"


def _load_registry() -> dict[str, Any]:
    try:
        return json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("room_view_registry.json 加载失败: %s", exc)
        return {"room_types": [], "viewpoints": [], "camera_moves_4s": [], "shot_planning_prompt": ""}


_REGISTRY = _load_registry()

_ROOM_TYPES = _REGISTRY.get("room_types", [])
_VIEWPOINTS = _REGISTRY.get("viewpoints", [])
_CAMERA_MOVES = _REGISTRY.get("camera_moves_4s", [])
_PLANNING_PROMPT = _REGISTRY.get("shot_planning_prompt", "")


def _format_options(items: list[dict[str, Any]]) -> str:
    """把注册表项格式化成 LLM 可读的选项清单"""
    lines = []
    for it in items:
        lines.append(f'- {it["id"]}: {it.get("cn", "")} / {it.get("en", "")}')
    return "\n".join(lines)


_ROOM_OPTIONS_TEXT = _format_options(_ROOM_TYPES)
_VIEWPOINT_OPTIONS_TEXT = _format_options(_VIEWPOINTS)
# camera_moves 的 en 是完整运镜描述，直接用
_CAMERA_OPTIONS_TEXT = "\n".join(f'- {c["id"]}: {c.get("en", "")}' for c in _CAMERA_MOVES)


def calc_num_shots(duration: float, min_shots: int = 4, max_shots: int = 15) -> int:
    """根据源视频时长推导镜头数。

    规则: 基数 2 + 每 6 秒 1 个镜头, 再 clamp 到 [min_shots, max_shots]。
    装修视频内容密度适中, 6 秒/镜头是经验值; 起步基数保证短视频也有足够素材。

    Examples:
        3s  -> 4 (下限)
        12s -> 4
        24s -> 6
        36s -> 8
        60s -> 12
        90s -> 15 (上限)
    """
    if duration <= 0:
        return min_shots
    raw = round(duration / 6.0) + 2
    return max(min_shots, min(max_shots, raw))


class ShotPlanner:
    """镜头清单规划器"""

    def __init__(
        self,
        llm: LLMClient,
        num_shots: int = 8,
        clip_duration: int = 4,
    ) -> None:
        self.llm = llm
        self.num_shots = num_shots
        self.clip_duration = clip_duration

        if not _ROOM_TYPES or not _VIEWPOINTS or not _CAMERA_MOVES:
            logger.warning("注册表数据不完整，镜头规划可能受限")

    def plan(self, profile: StyleProfile) -> list[dict[str, Any]]:
        """基于风格画像生成 num_shots 个镜头

        Returns:
            list of shot dict:
              { shot_id, room_type, viewpoint, camera_move,
                image_prompt, video_prompt, duration,
                image_status, image_path, clip_status, clip_path }
        """
        if not _PLANNING_PROMPT:
            logger.error("shot_planning_prompt 为空，无法规划")
            return self._fallback_plan(profile)

        descriptor = profile.style_descriptor_en or "modern minimalist interior"
        # 用 replace 而非 .format()：prompt 里的 JSON 示例含 { } 会被 format 误解析
        prompt = _PLANNING_PROMPT
        prompt = prompt.replace("{num_shots}", str(self.num_shots))
        prompt = prompt.replace("{style_descriptor_en}", descriptor)
        prompt = prompt.replace("{room_options}", _ROOM_OPTIONS_TEXT)
        prompt = prompt.replace("{viewpoint_options}", _VIEWPOINT_OPTIONS_TEXT)
        prompt = prompt.replace("{camera_options}", _CAMERA_OPTIONS_TEXT)

        try:
            raw = self.llm.chat(prompt, f"请规划 {self.num_shots} 个室内装修镜头。")
            shots = self._parse_shots(raw, profile)
        except Exception as exc:
            logger.error("镜头规划 LLM 调用失败: %s", exc)
            shots = []

        if not shots:
            shots = self._fallback_plan(profile)

        # 补齐结构字段
        for s in shots:
            s.setdefault("duration", self.clip_duration)
            s.setdefault("image_status", "pending")
            s.setdefault("image_path", "")
            s.setdefault("clip_status", "pending")
            s.setdefault("clip_path", "")

        logger.info(
            "镜头规划完成: %d 个 | 房间 %s | 视角 %s",
            len(shots),
            sorted({s.get("room_type", "?") for s in shots}),
            sorted({s.get("viewpoint", "?") for s in shots}),
        )
        return shots

    # ── 解析 ──────────────────────────────────────────────────

    def _parse_shots(self, raw: str, profile: StyleProfile) -> list[dict[str, Any]]:
        """解析 LLM 返回的 JSON 数组，并做基本校验与去重"""
        shots_raw = self.llm.parse_json_list(raw)
        if not shots_raw:
            return []

        valid_room_ids = {r["id"] for r in _ROOM_TYPES}
        valid_vp_ids = {v["id"] for v in _VIEWPOINTS}
        valid_cam_ids = {c["id"] for c in _CAMERA_MOVES}

        shots: list[dict[str, Any]] = []
        seen_combos: set[tuple[str, str, str]] = set()

        for i, item in enumerate(shots_raw, 1):
            if not isinstance(item, dict):
                continue
            if len(shots) >= self.num_shots:
                break

            room = item.get("room_type", "")
            viewpoint = item.get("viewpoint", "")
            camera = item.get("camera_move", "")

            # 校验 id 有效性（无效不致命，但记录）
            if room and room not in valid_room_ids:
                logger.debug("未知 room_type: %s（保留）", room)
            if viewpoint and viewpoint not in valid_vp_ids:
                logger.debug("未知 viewpoint: %s（保留）", viewpoint)
            if camera and camera not in valid_cam_ids:
                logger.debug("未知 camera_move: %s（保留）", camera)

            combo = (room, viewpoint, camera)
            if combo in seen_combos:
                logger.debug("重复组合已跳过: %s", combo)
                continue
            seen_combos.add(combo)

            image_prompt = (item.get("image_prompt") or "").strip()
            video_prompt = (item.get("video_prompt") or "").strip()
            # 若 LLM 漏写 prompt，注入风格锁兜底
            if not image_prompt:
                image_prompt = self._build_fallback_image_prompt(room, viewpoint, profile)
            if not video_prompt:
                video_prompt = self._build_fallback_video_prompt(camera, profile)

            shots.append({
                "shot_id": len(shots) + 1,
                "room_type": room,
                "viewpoint": viewpoint,
                "camera_move": camera,
                "image_prompt": image_prompt,
                "video_prompt": video_prompt,
            })

        return shots

    # ── 兜底（LLM 完全失败时用规则生成）──────────────────────

    def _fallback_plan(self, profile: StyleProfile) -> list[dict[str, Any]]:
        """LLM 失败时的规则兜底：在 room×view×camera 里轮流取 num_shots 个"""
        descriptor = profile.style_descriptor_en or "modern minimalist interior"
        shots: list[dict[str, Any]] = []
        rooms = _ROOM_TYPES or [{"id": "living_room", "en": "living room"}]
        views = _VIEWPOINTS or [{"id": "wide_corner", "en": "wide-angle corner view"}]
        cams = _CAMERA_MOVES or [{"id": "slow_push_in", "en": "slow push-in"}]

        for i in range(self.num_shots):
            room = rooms[i % len(rooms)]
            view = views[i % len(views)]
            cam = cams[i % len(cams)]
            shots.append({
                "shot_id": i + 1,
                "room_type": room["id"],
                "viewpoint": view["id"],
                "camera_move": cam["id"],
                "image_prompt": self._build_fallback_image_prompt(room["id"], view["id"], profile),
                "video_prompt": self._build_fallback_video_prompt(cam["id"], profile),
            })
        return shots

    def _build_fallback_image_prompt(self, room_id: str, viewpoint_id: str, profile: StyleProfile) -> str:
        descriptor = profile.style_descriptor_en or "modern minimalist interior"
        room_en = next((r.get("en", room_id) for r in _ROOM_TYPES if r["id"] == room_id), room_id)
        view_en = next((v.get("en", viewpoint_id) for v in _VIEWPOINTS if v["id"] == viewpoint_id), viewpoint_id)
        return (
            f"{descriptor}. A {room_en} of the same home project, {view_en}. "
            f"Empty interior, no people. Architectural photography, "
            f"professional lighting, high detail, 4K."
        )

    def _build_fallback_video_prompt(self, camera_id: str, profile: StyleProfile) -> str:
        descriptor = profile.style_descriptor_en or "modern minimalist interior"
        cam_en = next((c.get("en", camera_id) for c in _CAMERA_MOVES if c["id"] == camera_id), camera_id)
        return f"{cam_en}. The space embodies: {descriptor}. Empty interior, no people, cinematic."
