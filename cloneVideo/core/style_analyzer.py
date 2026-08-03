"""风格决策器 - AI 分析原视频后，推荐一个统一的目标装修风格

新流程（真实帧风格统一改造）下，这个模块的职责从"分析原风格去复刻"变为：
  分析原视频当前风格 → 推荐一个统一的、美观的目标风格，
  用于把所有真实房间截图统一改造成同一风格。

输出的 style_descriptor_en 是"目标风格锁"，会被注入到每张图的
image-edit prompt 中（保持布局家具，只改风格），确保所有图风格统一。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.llm_client import LLMClient
from core.video_analyzer import KeyFrame

logger = logging.getLogger(__name__)


# ── 统一目标风格决策 prompt ──────────────────────────────────

_STYLE_DECIDE_SYSTEM = (
    "你是一位顶尖的室内设计总监。下面给你一组同一套房子的装修视频关键帧。\n"
    "请完成两件事：\n\n"
    "【第一步】判断这套房子当前的整体装修风格（original_style）。\n\n"
    "【第二步】推荐一个统一的、美观的、有高级感的目标装修风格（target_style），"
    "用于把这套房子所有房间统一改造成这个风格。推荐时请考虑：\n"
    "  - 保留原风格中好的元素（如材质、色调的优点）\n"
    "  - 当前流行的、适合大面积落地的设计趋势（modern minimalist / Japandi / wabi-sabi / "
    "modern luxury / contemporary / scandinavian / neo-classic 等）\n"
    "  - 风格要统一、协调、有辨识度\n\n"
    "输出 JSON（只返回 JSON，不要任何解释或 markdown）：\n"
    "{\n"
    '  "original_style_cn": "原风格中文名",\n'
    '  "original_style_en": "original style in english",\n'
    '  "target_style_cn": "目标风格中文名",\n'
    '  "target_style_en": "target style in english",\n'
    '  "color_palette": ["#AABBCC", "#DDDDDD", "..."],\n'
    '  "materials_cn": ["目标风格主要材质", "..."],\n'
    '  "materials_en": ["main materials in english", "..."],\n'
    '  "furniture_style_cn": "目标风格家具款式(1句)",\n'
    '  "furniture_style_en": "target furniture style in english",\n'
    '  "lighting_cn": "目标风格灯光设计(1句)",\n'
    '  "lighting_en": "target lighting design in english",\n'
    '  "decorative_elements_cn": ["装饰元素", "..."],\n'
    '  "decorative_elements_en": ["decorative elements", "..."],\n'
    '  "vibe_en": "calm, warm, sophisticated",\n'
    '  "style_descriptor_en": "80-150词完整英文目标风格描述，可直接注入 AI 图像改造 prompt。'
    '必须含：材质、色板、家具线条、灯光色温、整体氛围。自包含、信息密度高。这段会被原样拼进每张图的改造 prompt。"\n'
    "}\n"
    "所有字段必须确定、唯一，不能出现'可能/有的'等模糊词。"
)

_USER_MSG = (
    "这是 {n} 张同一套房子的装修视频关键帧。"
    "请先判断原风格，再推荐一个统一的目标装修风格，输出 JSON。"
)


@dataclass
class StyleProfile:
    """风格画像（original=原视频风格, overall/target=AI推荐的统一目标风格）"""
    original_style_cn: str = ""
    original_style_en: str = ""
    overall_style_cn: str = ""           # 目标风格中文
    overall_style_en: str = ""           # 目标风格英文
    color_palette: list[str] = field(default_factory=list)
    materials_cn: list[str] = field(default_factory=list)
    materials_en: list[str] = field(default_factory=list)
    furniture_style_cn: str = ""
    furniture_style_en: str = ""
    lighting_cn: str = ""
    lighting_en: str = ""
    decorative_elements_cn: list[str] = field(default_factory=list)
    decorative_elements_en: list[str] = field(default_factory=list)
    vibe_en: str = ""
    style_descriptor_en: str = ""        # ★目标风格锁★，注入每次风格改造 prompt

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_style_cn": self.original_style_cn,
            "original_style_en": self.original_style_en,
            "overall_style_cn": self.overall_style_cn,
            "overall_style_en": self.overall_style_en,
            "color_palette": self.color_palette,
            "materials_cn": self.materials_cn,
            "materials_en": self.materials_en,
            "furniture_style_cn": self.furniture_style_cn,
            "furniture_style_en": self.furniture_style_en,
            "lighting_cn": self.lighting_cn,
            "lighting_en": self.lighting_en,
            "decorative_elements_cn": self.decorative_elements_cn,
            "decorative_elements_en": self.decorative_elements_en,
            "vibe_en": self.vibe_en,
            "style_descriptor_en": self.style_descriptor_en,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StyleProfile":
        return cls(
            original_style_cn=d.get("original_style_cn", ""),
            original_style_en=d.get("original_style_en", ""),
            overall_style_cn=d.get("target_style_cn") or d.get("overall_style_cn", ""),
            overall_style_en=d.get("target_style_en") or d.get("overall_style_en", ""),
            color_palette=list(d.get("color_palette", [])),
            materials_cn=list(d.get("materials_cn", [])),
            materials_en=list(d.get("materials_en", [])),
            furniture_style_cn=d.get("furniture_style_cn", ""),
            furniture_style_en=d.get("furniture_style_en", ""),
            lighting_cn=d.get("lighting_cn", ""),
            lighting_en=d.get("lighting_en", ""),
            decorative_elements_cn=list(d.get("decorative_elements_cn", [])),
            decorative_elements_en=list(d.get("decorative_elements_en", [])),
            vibe_en=d.get("vibe_en", ""),
            style_descriptor_en=d.get("style_descriptor_en", ""),
        )


class StyleAnalyzer:
    """统一目标风格决策器（AI 推荐）"""

    def __init__(self, llm: LLMClient, max_frames_for_analysis: int = 8) -> None:
        self.llm = llm
        self.max_frames_for_analysis = max_frames_for_analysis

    def decide(self, frames: list[KeyFrame]) -> StyleProfile:
        """分析关键帧，AI 推荐一个统一的目标装修风格

        Returns:
            StyleProfile，其中 style_descriptor_en 是目标风格锁。
        """
        if not frames:
            logger.warning("无关键帧可分析，返回空风格画像")
            return StyleProfile()

        sampled = self._sample_frames(frames, self.max_frames_for_analysis)
        n = len(sampled)

        logger.info("风格决策: 发送 %d 张代表帧给 VLM (原始 %d 帧)", n, len(frames))
        try:
            raw = self.llm.chat_with_images(
                system_prompt=_STYLE_DECIDE_SYSTEM,
                user_message=_USER_MSG.format(n=n),
                image_paths=[f.path for f in sampled],
            )
            data = self.llm.parse_json_object(raw)
        except Exception as exc:
            logger.error("风格决策 VLM 调用失败: %s", exc)
            data = {}

        profile = StyleProfile.from_dict(data)

        # 兜底：style_descriptor_en 为空时用其他字段拼一个
        if not profile.style_descriptor_en.strip():
            profile.style_descriptor_en = self._fallback_descriptor(profile)
            logger.warning("style_descriptor_en 为空，使用兜底描述")

        logger.info(
            "风格决策完成: 原风格 %s → 目标风格 %s / %s | 风格锁 %d 字符",
            profile.original_style_cn, profile.overall_style_cn,
            profile.overall_style_en, len(profile.style_descriptor_en),
        )
        return profile

    # ── 内部 ──────────────────────────────────────────────────

    @staticmethod
    def _sample_frames(frames: list[KeyFrame], max_n: int) -> list[KeyFrame]:
        """均匀采样最多 max_n 帧（保留首尾，中间均匀取）"""
        if len(frames) <= max_n:
            return list(frames)
        step = (len(frames) - 1) / (max_n - 1)
        indices = [round(i * step) for i in range(max_n)]
        seen = set()
        sampled = []
        for idx in indices:
            if idx not in seen and 0 <= idx < len(frames):
                seen.add(idx)
                sampled.append(frames[idx])
        return sampled or list(frames[:max_n])

    @staticmethod
    def _fallback_descriptor(profile: StyleProfile) -> str:
        """style_descriptor_en 缺失时，用其他字段拼一个最小可用的目标风格锁"""
        parts = []
        if profile.overall_style_en:
            parts.append(f"{profile.overall_style_en} interior")
        if profile.materials_en:
            parts.append("featuring " + ", ".join(profile.materials_en[:4]))
        if profile.color_palette:
            parts.append("with a palette of " + ", ".join(profile.color_palette[:4]))
        if profile.furniture_style_en:
            parts.append(profile.furniture_style_en)
        if profile.lighting_en:
            parts.append(profile.lighting_en)
        if profile.vibe_en:
            parts.append(f"Overall vibe: {profile.vibe_en}")
        desc = ". ".join(p for p in parts if p).strip()
        return desc or (
            "Modern minimalist interior, warm neutral tones, natural wood and stone, "
            "clean lines, soft warm ambient lighting, calm sophisticated atmosphere."
        )
