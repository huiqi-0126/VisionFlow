"""运镜理解器 - VLM 分析关键帧序列，为每个首帧推断 4 秒运镜

完全复刻模式专用：不做风格改造，直接拿关键帧当首帧，
关键是还原原片的镜头运动。本模块：

  - 接收按时间顺序排列的关键帧序列
  - 一次性发给多模态 LLM，对比相邻帧推断每段运镜
  - 输出每帧对应的 camera_move / motion_detail / video_prompt

与 shot_planner 的差异：
  - shot_planner 从注册表【轮询分配】运镜（室内复刻，追求多样性）
  - camera_analyzer 从源视频【理解还原】运镜（完全复刻，追求还原度）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.llm_client import LLMClient
from core.video_analyzer import KeyFrame

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).resolve().parent / "prompts" / "room_view_registry.json"


def _load_camera_templates() -> dict[str, str]:
    """从注册表加载 camera_move_id → 英文运镜模板"""
    try:
        registry = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
        return {c["id"]: c.get("en", "") for c in registry.get("camera_moves_4s", [])}
    except Exception as exc:
        logger.error("加载运镜注册表失败: %s", exc)
        return {}


_CAMERA_TEMPLATES = _load_camera_templates()


# ── VLM 分析 prompt ──────────────────────────────────────────

_CAMERA_SYSTEM = (
    "你是一位专业的电影摄影分析师和 AI 视频提示词工程师。\n"
    "下面给你一段视频的 {n} 个按时间顺序排列的关键帧（第 1 帧最早，第 {n} 帧最晚）。\n\n"
    "任务：为每个关键帧推断「从该帧开始的 4 秒片段」的运镜方式，"
    "以便用 AI 图生视频模型还原原片的镜头运动。\n\n"
    "分析方法：\n"
    "1. 对比相邻帧（第 i 帧与第 i+1 帧）的画面差异，推断镜头运动方向和类型\n"
    "2. 常见运镜：推近(push-in)、拉远(pull-out)、左右平摇(pan)、上下仰俯(tilt)、"
    "环绕弧线(orbit/arc)、静态(static)、升降(crane)、跟随(follow)\n"
    "3. 最后 1 帧没有后续帧对比时，用温和运镜（缓慢推近或静态微动）\n\n"
    "输出 JSON 数组（只返回 JSON，不要 markdown 代码块），恰好 {n} 个对象，顺序与输入帧一一对应：\n"
    "[\n"
    "  {\n"
    '    "shot_id": 1,\n'
    '    "camera_move": "slow_push_in",\n'
    '    "motion_detail_en": "smooth dolly forward, camera slowly advances into the space",\n'
    '    "scene_desc_cn": "客厅全景，暖色调，沙发居中",\n'
    '    "video_prompt": "[0s-4s]: smooth slow push-in (dolly forward) revealing the space. Photorealistic, cinematic, natural camera movement."\n'
    "  }\n"
    "]\n\n"
    "可选的 camera_move ID（选最接近的一个）：\n"
    "- slow_push_in: 推近/dolly forward\n"
    "- slow_pull_out: 拉远/dolly back\n"
    "- pan_left_to_right: 左到右平摇\n"
    "- orbital_arc: 环绕弧线\n"
    "- tilt_up: 仰拍上摇\n"
    "- static_shift_focus: 静态+变焦\n\n"
    "若实际运镜不在列表（如 tilt_down、pan_right_to_left、crane_down），"
    "选最接近的 ID，并在 motion_detail_en 中精确描述真实运动方向。\n\n"
    "video_prompt 规则：\n"
    "- 必须以 [0s-4s] 时间轴开头\n"
    "- 包含运镜描述 + 该帧可见的场景元素（用于视频模型理解画面）\n"
    "- 英文，60-100 词，自包含，可直接喂给视频生成模型\n"
    "- 不要出现人物描述（除非原帧确有人物且需保留）"
)

_CAMERA_USER = (
    "这是视频的 {n} 个连续关键帧。"
    "请为每帧推断 4 秒运镜，输出 {n} 个对象的 JSON 数组。"
)


# ── 数据结构 ──────────────────────────────────────────────────

@dataclass
class CameraSegment:
    """单帧的运镜分析结果（对应一个 4s 片段）"""
    shot_id: int
    camera_move: str = "slow_push_in"      # registry ID
    motion_detail_en: str = ""             # VLM 推断的运镜细节
    scene_desc_cn: str = ""                # 场景中文描述（展示用）
    video_prompt: str = ""                 # 直供视频生成的 prompt
    frame_path: str = ""                   # 对应的关键帧路径
    timestamp: float = 0.0                 # 在源视频中的时间戳

    def to_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "camera_move": self.camera_move,
            "motion_detail_en": self.motion_detail_en,
            "scene_desc_cn": self.scene_desc_cn,
            "video_prompt": self.video_prompt,
            "frame_path": self.frame_path,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CameraSegment":
        return cls(
            shot_id=int(d.get("shot_id", 0)),
            camera_move=d.get("camera_move", "slow_push_in"),
            motion_detail_en=d.get("motion_detail_en", ""),
            scene_desc_cn=d.get("scene_desc_cn", ""),
            video_prompt=d.get("video_prompt", ""),
            frame_path=d.get("frame_path", ""),
            timestamp=float(d.get("timestamp", 0.0)),
        )


# ── 分析器 ──────────────────────────────────────────────────

# 单次 VLM 调用最多分析的帧数（超过则分批，避免 token 超限）
_MAX_FRAMES_PER_CALL = 10


class CameraAnalyzer:
    """运镜理解器：VLM 分析关键帧序列，为每帧推断 4s 运镜"""

    def __init__(self, llm: LLMClient, clip_duration: int = 4) -> None:
        self.llm = llm
        self.clip_duration = clip_duration
        if not _CAMERA_TEMPLATES:
            logger.warning("运镜模板注册表为空，video_prompt 将使用兜底模板")

    def analyze(self, frames: list[KeyFrame]) -> list[CameraSegment]:
        """分析关键帧序列，返回与帧一一对应的 CameraSegment 列表

        对每个关键帧，推断从该帧开始的 4s 片段的运镜方式。
        帧数超过 _MAX_FRAMES_PER_CALL 时自动分批，每批内部保留时序上下文。
        """
        if not frames:
            logger.warning("无关键帧可分析运镜")
            return []

        total = len(frames)
        segments: list[CameraSegment] = []

        # 帧数少：一次调用
        if total <= _MAX_FRAMES_PER_CALL:
            segments = self._analyze_batch(frames, start_id=1)
        else:
            # 分批：每批最多 _MAX_FRAMES_PER_CALL 帧，相邻批重叠 1 帧（保留过渡上下文）
            i = 0
            shot_id = 1
            while i < total:
                batch = frames[i:i + _MAX_FRAMES_PER_CALL]
                batch_segs = self._analyze_batch(batch, start_id=shot_id)
                segments.extend(batch_segs)
                shot_id += len(batch_segs)
                i += _MAX_FRAMES_PER_CALL

        logger.info("运镜分析完成: %d 个片段, 运镜分布 %s",
                    len(segments), [s.camera_move for s in segments])
        return segments

    # ── 内部 ──────────────────────────────────────────────────

    def _analyze_batch(self, frames: list[KeyFrame], start_id: int) -> list[CameraSegment]:
        """单次 VLM 调用分析一批帧"""
        n = len(frames)
        logger.info("运镜分析: 发送 %d 帧给 VLM (shot_id %d~%d)", n, start_id, start_id + n - 1)

        try:
            raw = self.llm.chat_with_images(
                system_prompt=_CAMERA_SYSTEM.format(n=n),
                user_message=_CAMERA_USER.format(n=n),
                image_paths=[f.path for f in frames],
            )
            data_list = self.llm.parse_json_list(raw)
        except Exception as exc:
            logger.error("运镜分析 VLM 调用失败: %s", exc)
            data_list = []

        # VLM 返回的数量与帧数不一致时，兜底
        if len(data_list) != n:
            logger.warning("VLM 返回 %d 个片段，期望 %d，缺失的用兜底运镜填充",
                           len(data_list), n)

        segments: list[CameraSegment] = []
        for i, frame in enumerate(frames):
            sid = start_id + i
            data = data_list[i] if i < len(data_list) and isinstance(data_list[i], dict) else {}
            seg = CameraSegment(
                shot_id=sid,
                camera_move=data.get("camera_move", self._fallback_camera(sid)),
                motion_detail_en=data.get("motion_detail_en", ""),
                scene_desc_cn=data.get("scene_desc_cn", ""),
                video_prompt=data.get("video_prompt", ""),
                frame_path=frame.path,
                timestamp=frame.timestamp,
            )
            # video_prompt 为空时用注册表模板 + 运镜细节兜底
            if not seg.video_prompt.strip():
                seg.video_prompt = self._build_fallback_prompt(seg, frame)
            segments.append(seg)

        return segments

    def _fallback_camera(self, index: int) -> str:
        """兜底运镜：轮询分配（保证多样性）"""
        ids = list(_CAMERA_TEMPLATES.keys()) or ["slow_push_in"]
        return ids[(index - 1) % len(ids)]

    def _build_fallback_prompt(self, seg: CameraSegment, frame: KeyFrame) -> str:
        """video_prompt 缺失时，用注册表模板 + 运镜细节拼一个"""
        template = _CAMERA_TEMPLATES.get(
            seg.camera_move,
            f"[0s-{self.clip_duration}s]: smooth slow cinematic camera movement",
        )
        detail = seg.motion_detail_en or "gentle natural camera movement"
        return (
            f"{template}. {detail}. "
            "Photorealistic, cinematic, natural and smooth motion."
        )
