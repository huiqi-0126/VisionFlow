"""风格分析器 - 从关键帧提取室内装修风格画像，并自动挑选风格代表性参考帧

输出 StyleProfile（结构化 JSON），其中 style_descriptor_en 是"风格锁"，
会被注入到每次图片/视频生成 prompt 中，确保不同房间/视角保持同一套风格。

同时自动挑出 1-2 张最能代表风格的帧作为参考帧（reference_frames），
上传到 TOS 后作为 gkapi 图片生成的 pic 参数，双重锁定风格。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.llm_client import LLMClient
from core.video_analyzer import KeyFrame

logger = logging.getLogger(__name__)


# ── 风格画像提取 prompt ──────────────────────────────────────

_STYLE_PROFILE_SYSTEM = (
    "你是一位顶尖的室内设计风格分析师。下面给你一组室内装修视频的关键帧截图。\n"
    "请综合分析这套装修的【整体风格】，并输出结构化 JSON，用于驱动 AI 在不同房间/视角下"
    "复刻同一套风格。\n\n"
    "必须输出的字段：\n"
    "1. overall_style_cn: 整体风格中文名（如：现代简约 / 北欧 / 侘寂 / 轻奢 / 日式 / 工业 / 美式 / 法式 / 新中式）\n"
    "2. overall_style_en: 对应英文（如：modern minimalist / Japandi / wabi-sabi / modern luxury）\n"
    "3. color_palette: 主色调十六进制数组（4-6 个，按占比从大到小，如 [\"#E8DCC8\", \"#3A3A3A\"]）\n"
    "4. materials_cn: 主要材质中文数组（3-6 个，如 [\"胡桃木\", \"微水泥\", \"哑光黑五金\"]）\n"
    "5. materials_en: 对应英文数组\n"
    "6. furniture_style_cn: 家具款式中文（1 句，如\"低矮线性家具，无主灯设计\"）\n"
    "7. furniture_style_en: 对应英文\n"
    "8. lighting_cn: 灯光设计中英文（1 句，如\"暖色间接光，3000K，大面积柔光\"）\n"
    "9. lighting_en: 对应英文\n"
    "10. decorative_elements_cn: 装饰元素中文数组（2-4 个，如 [\"绿植\", \"抽象挂画\"]）\n"
    "11. decorative_elements_en: 对应英文数组\n"
    "12. vibe_en: 整体氛围英文关键词（3-5 个，如 \"calm, warm, sophisticated\"）\n"
    "13. style_descriptor_en: ★最关键★ 一段 80-150 词的完整英文描述，把以上所有要素串成一段"
    "可直接注入 AI 图像/视频 prompt 的\"风格锁\"。要求：具体到材质、配色、家具线条、灯光色温、"
    "氛围。这段会被原样拼进每次生成的 prompt，必须自包含、信息密度高。\n"
    "14. reference_indices: 从给你看的这组帧里，挑出 1-2 张【最能代表这套风格整体】的帧序号（按我"
    "给的顺序，从 1 开始）。这些会作为参考图，用于风格保持。优先选：构图完整、风格特征鲜明、"
    "画质清晰的帧。\n\n"
    "只返回 JSON 对象，不要任何解释或 markdown。所有文本字段必须确定、唯一，不能出现\"可能/有的\"等模糊词。"
)

_USER_MSG_TEMPLATE = (
    "这是 {n} 张室内装修视频的关键帧（按顺序编号 1~{n}）。"
    "请综合分析这套装修的整体风格，输出结构化 JSON，并挑出 1-2 张代表性参考帧。"
)


@dataclass
class StyleProfile:
    """风格画像（结构化）"""
    overall_style_cn: str = ""
    overall_style_en: str = ""
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
    style_descriptor_en: str = ""   # ★风格锁★，注入每次生成
    reference_indices: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
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
            "reference_indices": self.reference_indices,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StyleProfile":
        return cls(
            overall_style_cn=d.get("overall_style_cn", ""),
            overall_style_en=d.get("overall_style_en", ""),
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
            reference_indices=[int(i) for i in d.get("reference_indices", []) if str(i).strip().lstrip("-").isdigit()],
        )


class StyleAnalyzer:
    """室内风格画像分析器"""

    def __init__(
        self,
        llm: LLMClient,
        max_frames_for_analysis: int = 8,
        max_reference_frames: int = 2,
    ) -> None:
        self.llm = llm
        self.max_frames_for_analysis = max_frames_for_analysis
        self.max_reference_frames = max_reference_frames

    def analyze(
        self,
        frames: list[KeyFrame],
    ) -> tuple[StyleProfile, list[KeyFrame]]:
        """分析关键帧，返回 (风格画像, 参考帧列表)

        流程：
          1. 帧数过多时均匀采样代表帧（最多 max_frames_for_analysis 张）
          2. 一次 VLM 调用：发采样帧 + prompt → 输出风格画像 JSON + 参考帧序号
          3. 按序号取回参考帧；解析失败/序号无效时回退到中间帧
        """
        if not frames:
            logger.warning("无关键帧可分析，返回空风格画像")
            return StyleProfile(), []

        # 1. 采样：帧太多时均匀抽取，避免一次发太多图
        sampled = self._sample_frames(frames, self.max_frames_for_analysis)
        n = len(sampled)

        logger.info("风格分析: 发送 %d 张代表帧给 VLM (原始 %d 帧)", n, len(frames))
        try:
            raw = self.llm.chat_with_images(
                system_prompt=_STYLE_PROFILE_SYSTEM,
                user_message=_USER_MSG_TEMPLATE.format(n=n),
                image_paths=[f.path for f in sampled],
            )
            data = self.llm.parse_json_object(raw)
        except Exception as exc:
            logger.error("风格画像 VLM 调用失败: %s", exc)
            data = {}

        profile = StyleProfile.from_dict(data)

        # 兜底：style_descriptor_en 为空时用 materials/style 拼一个
        if not profile.style_descriptor_en.strip():
            profile.style_descriptor_en = self._fallback_descriptor(profile)
            logger.warning("style_descriptor_en 为空，使用兜底描述")

        # 2. 取回参考帧（reference_indices 是相对 sampled 的 1-based 序号）
        ref_frames = self._resolve_reference_frames(profile.reference_indices, sampled, frames)

        logger.info(
            "风格画像完成: %s / %s | 参考帧 %d 张 | 风格锁 %d 字符",
            profile.overall_style_cn, profile.overall_style_en,
            len(ref_frames), len(profile.style_descriptor_en),
        )
        return profile, ref_frames

    # ── 内部 ──────────────────────────────────────────────────

    @staticmethod
    def _sample_frames(frames: list[KeyFrame], max_n: int) -> list[KeyFrame]:
        """均匀采样最多 max_n 帧（保留首尾，中间均匀取）"""
        if len(frames) <= max_n:
            return list(frames)
        # 均匀采样：含首尾
        step = (len(frames) - 1) / (max_n - 1)
        indices = [round(i * step) for i in range(max_n)]
        # 去重保序
        seen = set()
        sampled = []
        for idx in indices:
            if idx not in seen and 0 <= idx < len(frames):
                seen.add(idx)
                sampled.append(frames[idx])
        return sampled or list(frames[:max_n])

    def _resolve_reference_frames(
        self,
        indices: list[int],
        sampled: list[KeyFrame],
        all_frames: list[KeyFrame],
    ) -> list[KeyFrame]:
        """根据 VLM 返回的参考帧序号取回 KeyFrame，最多 max_reference_frames 张"""
        refs: list[KeyFrame] = []
        for i in indices:
            # reference_indices 是 1-based，相对 sampled
            if isinstance(i, int) and 1 <= i <= len(sampled):
                refs.append(sampled[i - 1])
            if len(refs) >= self.max_reference_frames:
                break

        # 兜底：没拿到有效参考帧 → 用中间帧
        if not refs:
            mid = len(all_frames) // 2
            refs = [all_frames[mid]] if all_frames else []

        return refs[: self.max_reference_frames]

    @staticmethod
    def _fallback_descriptor(profile: StyleProfile) -> str:
        """style_descriptor_en 缺失时，用其他字段拼一个最小可用的风格锁"""
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
        return desc or "Modern minimalist interior, warm neutral tones, natural materials, clean lines, soft natural lighting."
