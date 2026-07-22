"""分镜制作器 - 基于风格注册表，独立构建脚本，生成分镜表

6 大风格类别，每类 2-4 个子风格，每个子风格有专门的 prompt 模板：
  1. Drama & Story (剧情叙事) — 微短剧、反转段子、喜剧段子
  2. Visual & Aesthetic (视觉质感) — 电影氛围、治愈ASMR、Corecore混剪、港风
  3. Education (知识教育) — 迷你教程、辟谣冷知识、数据可视化
  4. Commerce (带货转化) — 产品展示、开箱评测、Before/After、UGC见证
  5. Engagement (互动参与) — 挑战接力、Vlog日常、BTS幕后
  6. Emotional (情感共鸣) — 怀旧回忆、励志故事、Aspirational Lifestyle

流程：
  1. 提取人物特征 → 锁定英文外貌描述
  2. 分析原视频风格 → 从注册表匹配类别+子类型
  3. 加载对应风格的 script_prompt → 独立构建脚本
  4. 为每个分镜生成 image_prompt + video_prompt（注入风格 modifier）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.llm_client import LLMClient

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────

CLIP_DURATION = 15   # 默认每个分镜 15 秒（fixed 模式）
NUM_SCENES = 3       # 默认 3 个分镜（fixed 模式）

# auto 模式参数
AUTO_MIN_SCENES = 2
AUTO_MAX_SCENES = 5
AUTO_MIN_DURATION = 4
AUTO_MAX_DURATION = 15

# ── 风格注册表加载 ──────────────────────────────────────────────────

_REGISTRY_PATH = Path(__file__).resolve().parent / "prompts" / "style_registry.json"


def _load_registry() -> dict[str, Any]:
    """加载风格注册表 JSON"""
    try:
        return json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("风格注册表加载失败: %s", exc)
        return {"categories": [], "style_analysis_prompt": ""}


_REGISTRY = _load_registry()

# 按 id 索引的类别查找表
_CATEGORIES_BY_ID: dict[str, dict[str, Any]] = {}
_SUBTYPES_BY_ID: dict[str, dict[str, Any]] = {}  # "category_id::subtype_id" -> subtype

for _cat in _REGISTRY.get("categories", []):
    _CATEGORIES_BY_ID[_cat["id"]] = _cat
    for _sub in _cat.get("subtypes", []):
        _SUBTYPES_BY_ID[f"{_cat['id']}::{_sub['id']}"] = _sub


def get_all_categories() -> list[dict[str, Any]]:
    """返回所有风格类别（含子类型）"""
    return _REGISTRY.get("categories", [])


def find_category(category_id: str) -> dict[str, Any] | None:
    """按 id 查找风格类别"""
    return _CATEGORIES_BY_ID.get(category_id)


def find_subtype(category_id: str, subtype_id: str) -> dict[str, Any] | None:
    """按 id 查找子类型"""
    return _SUBTYPES_BY_ID.get(f"{category_id}::{subtype_id}")


# ── 通用 Prompt（人物提取/翻译）──────────────────────────────────

_CHARACTER_EXTRACT_SYSTEM = (
    "你是一个专业的视频内容分析师。根据视频关键帧的描述，提取视频中出现的主要人物的外貌特征。\n\n"
    "重要原则：\n"
    "- 只描述最清晰、最常出现的那一套外貌信息\n"
    "- 如果不同帧中人物的穿着不同，只选出现次数最多的那一套\n"
    "- 绝对不要出现'部分画面''有的''有时'这种模糊描述\n"
    "- 每个属性只能有一个确定的描述\n\n"
    "需要提取的属性（每项必须确定、唯一）：\n"
    "1. 人种（如：东亚裔、白人、黑人等）\n"
    "2. 性别\n"
    "3. 年龄（精确数字）\n"
    "4. 肤色（如：浅米色、小麦色等）\n"
    "5. 发型发色（如：深色马尾辫、金色波浪长发）\n"
    "6. 上装（款式+颜色，如：红色无袖棉质上衣）\n"
    "7. 下装（款式+颜色，如：米色工装裤）\n"
    "8. 鞋子（款式+颜色，如：白色平底运动鞋，看不清则填\"未看清\"）\n"
    "9. 帽子（有则描述，无则填\"无\"）\n"
    "10. 配饰（有则描述，无则填\"无\"）\n\n"
    "请返回 JSON 数组，每个人物一个对象：\n"
    '[{"name": "主角", "race": "...", "gender": "...", "age": ..., '
    '"skin_tone": "...", "hair": "...", '
    '"top": "...", "bottom": "...", "shoes": "...", '
    '"hat": "...", "accessories": "..."}]\n\n'
    "只返回 JSON，不要其他文字。每项必须是确定的单一描述。"
)

_CHARACTER_TO_ENGLISH_SYSTEM = (
    "你是一个英文 prompt 工程师。将中文人物描述翻译为一段简洁的英文人物外貌描述。\n\n"
    "要求：\n"
    "- 翻译以下信息：人种+年龄+性别、发型发色、上装、下装、鞋子\n"
    "- 帽子和配饰如果原文有就加上\n"
    "- 描述要具体但不要过长，2-3 句话即可\n"
    "- 例如：'A 30-year-old Caucasian woman with dark hair in a ponytail, "
    "wearing a red sleeveless cotton top, beige cargo pants and white sneakers'\n"
    "- 不要出现 'some frames' 'sometimes' 'occasionally' 等模糊词，每项只给一个确定描述\n"
    "- 输出一段纯英文，不要前缀"
)

_SCRIPT_OUTPUT_FORMAT = """

OUTPUT FORMAT — return a JSON array with exactly {num_scenes} objects:
[
  {{
    "scene_id": 1,
    "title": "Scene title (English)",
    "script": "Detailed scene description: character actions, expressions, dialogue cues, environment details (English, 100-200 words)",
    "visual": "Shot composition, lighting, color palette, lens choice (English)",
    "camera": "Camera movement: dolly/tilt/handheld/static, angle, speed (English)",
    "emotion": "Emotional atmosphere label (English, e.g., 'tense anticipation', 'warm nostalgia')",
    "description": "Comprehensive scene description in Chinese (200 chars max, for internal reference)"
  }}
]

Return ONLY the JSON array. No markdown, no explanation."""


class StoryboardMaker:
    """基于风格注册表的分镜制作器

    7 大风格类别，每类有专门的 prompt 模板。

    两种模式：
      - "fixed": 固定 3 个分镜 × 15 秒（默认，保持向后兼容）
      - "auto":  分析原视频后自动决定分镜数量(2-5)和每个分镜时长(4-15s)

    人物特征跨分镜严格一致。
    """

    def __init__(
        self,
        llm: LLMClient,
        max_scenes: int = NUM_SCENES,
        clip_duration: int = CLIP_DURATION,
        mode: str = "auto",
    ) -> None:
        self.llm = llm
        self.max_scenes = max_scenes
        self.clip_duration = clip_duration
        self.mode = mode

    def create_storyboard(
        self,
        frames: list[dict[str, Any]],
        video_duration: float,
        style_hint: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """完整分镜制作流程
        """
        # 0. 提取高信息量帧
        high_value_frames = self._filter_low_info_frames(frames)
        
        # 0. 提取人物特征
        character_description = self._extract_character_description(high_value_frames)

        # 1. 分析原视频风格 → 得到 category_id + subtype_id
        category_id, subtype_id, style_info = self._analyze_style(high_value_frames, style_hint)

        # 2. 提取原视频主线内容（用于保持脚本与原视频的相关性）
        content_storyline = self._extract_storyline(high_value_frames)

        # 3. 加载对应风格的 prompt 模板
        category = find_category(category_id)
        subtype = find_subtype(category_id, subtype_id) if subtype_id else None

        # 确定分镜方案（数量 + 每个时长）
        if self.mode == "auto":
            # auto 模式：LLM 分析原视频后自动决定
            scene_durations = self._analyze_scene_plan(content_storyline, high_value_frames, video_duration)
        else:
            # fixed 模式：固定数量 + 固定时长
            import math
            if video_duration > 0:
                num_scenes = max(1, int(video_duration // self.clip_duration))
            else:
                num_scenes = self.max_scenes
            max_possible_scenes = max(1, len(high_value_frames) - 1)
            num_scenes = min(num_scenes, max_possible_scenes, self.max_scenes)
            scene_durations = [self.clip_duration] * num_scenes

        num_scenes = len(scene_durations)

        # 4. 构建脚本（基于原视频主线 + 风格模板）
        visual_summary = self._build_visual_summary(high_value_frames)
        scenes = self._build_script(
            character_description=character_description,
            style_info=style_info,
            visual_summary=visual_summary,
            content_storyline=content_storyline,
            category=category,
            subtype=subtype,
            num_scenes=num_scenes,
            scene_durations=scene_durations,
        )
        if not scenes:
            scenes = self._fallback_script(character_description, num_scenes, scene_durations)

        # 5. 为每个场景匹配首尾帧 (从高信息量帧中截取首尾)
        num_boundaries = len(scenes) + 1
        total_high = len(high_value_frames)
        indices = [int(i * (total_high - 1) / (num_boundaries - 1)) for i in range(num_boundaries)]
        boundary_frames = [high_value_frames[i] for i in indices]

        for i, scene in enumerate(scenes):
            scene["start_frame"] = boundary_frames[i].get("path", "")
            scene["start_frame_desc"] = boundary_frames[i].get("description", "")
            scene["end_frame"] = boundary_frames[i + 1].get("path", "")
            scene["end_frame_desc"] = boundary_frames[i + 1].get("description", "")
            scene["style_category"] = category_id
            scene["style_subtype"] = subtype_id or ""

        # 6. 为每个场景生成 prompt（注入风格 modifier）
        prompt_modifiers = (category or {}).get("prompt_modifiers", {})
        for scene in scenes:
            prompts = self._generate_scene_prompts(scene, character_description, prompt_modifiers, subtype)
            scene.update(prompts)

        # 7. 连贯性审查：检查场景间是否有逻辑矛盾或物理不合理
        scenes = self._review_continuity(scenes)

        # 8. 补全结构字段
        for scene in scenes:
            scene.setdefault("clip_status", "pending")
            scene.setdefault("generated_image", "")
            scene.setdefault("generated_clip", "")

        total_dur = sum(s.get("duration", self.clip_duration) for s in scenes)
        logger.info(
            "脚本构建完成: %d 个分镜 (模式: %s), 总时长 %ds, 风格: %s/%s",
            len(scenes), self.mode, total_dur, category_id, subtype_id or "auto",
        )

        metadata = {
            "character_description": character_description,
            "style_category": category_id,
            "style_subtype": subtype_id or "",
            "style_info": style_info,
            "mode": self.mode,
            "clip_duration": self.clip_duration,
            "total_duration": total_dur,
        }

        return scenes, metadata


    def _filter_low_info_frames(self, frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """利用 LLM 剔除低信息量的关键帧（如模糊、无主体的开头结尾）"""
        if len(frames) <= 2:
            return frames

        descs = "\n".join(f"帧 {f['index']}: {f.get('description', '')}" for f in frames)
        system_prompt = (
            "你是一个专业的视频剪辑师。请根据以下关键帧的画面描述，剔除其中信息量低（例如：纯黑/纯白画面、只有模糊背景、无明确主体动作、或者与主线内容无关的过渡帧）的帧。\n"
            "通常视频的开头和结尾可能有此类帧。\n"
            "请以 JSON 格式返回保留下来的高价值关键帧的序号列表（index）。\n"
            "要求：\n"
            "- 保留能反映视频核心动作和主要人物的帧\n"
            "- 至少保留两帧，以保证有起止参考\n"
            '返回格式示例: {"kept_indices": [1, 2, 4, 5]}'
        )
        try:
            raw = self.llm.chat(system_prompt, descs)
            result = self.llm.parse_json_object(raw)
            kept_indices = result.get("kept_indices", [])
            if not isinstance(kept_indices, list) or not kept_indices:
                raise ValueError("未返回有效的 indices 列表")
            
            high_value_frames = [f for f in frames if f.get("index") in kept_indices]
            if len(high_value_frames) < 2:
                # logger.warning("LLM 保留的帧少于 2，回退到原始帧")
                return frames
            # logger.info("剔除了 %d 个低信息量帧，保留: %s", len(frames) - len(high_value_frames), kept_indices)
            return high_value_frames
        except Exception as exc:
            # logger.warning("剔除低信息量帧失败: %s", exc)
            return frames

    # ── 人物特征锁定 ──────────────────────────────────────────────

    def _extract_character_description(self, frames: list[dict[str, Any]]) -> str:
        """从关键帧描述中提取统一的英文人物外貌描述"""
        frame_descs = [f.get("description", "") for f in frames if f.get("description")]
        if not frame_descs:
            return ""

        all_desc = "\n".join(f"帧 {i+1}: {d}" for i, d in enumerate(frame_descs))

        try:
            characters_raw = self.llm.chat(_CHARACTER_EXTRACT_SYSTEM, all_desc)
            characters = self.llm.parse_json_list(characters_raw)
            if not characters:
                obj = self.llm.parse_json_object(characters_raw)
                if obj:
                    characters = [obj]
        except Exception as exc:
            logger.warning("人物特征提取失败: %s", exc)
            characters = []

        if not characters:
            return ""

        char_text = json.dumps(characters, ensure_ascii=False, indent=2)
        try:
            english_desc = self.llm.chat(_CHARACTER_TO_ENGLISH_SYSTEM, char_text).strip()
        except Exception as exc:
            logger.warning("人物描述翻译失败: %s", exc)
            english_desc = ""

        logger.info("人物特征已锁定: %s", english_desc[:100])
        return english_desc

    # ── 风格分析 ──────────────────────────────────────────────────

    def _analyze_style(
        self, frames: list[dict[str, Any]], style_hint: str,
    ) -> tuple[str, str, str]:
        """分析原视频风格，返回 (category_id, subtype_id, style_info_text)

        style_hint 格式:
          - "" → 自动识别
          - "drama" → 指定类别，自动选子类型
          - "drama::plot_twist" → 指定类别+子类型
          - "剧情叙事" / "剧情叙事::反转段子" → 中文也兼容
        """
        # 解析用户指定
        if style_hint:
            return self._resolve_style_hint(style_hint)

        # 自动识别
        return self._auto_detect_style(frames)

    def _resolve_style_hint(self, hint: str) -> tuple[str, str, str]:
        """解析用户输入的风格提示，支持多种格式"""
        # 尝试 "::" 分隔
        parts = hint.split("::", 1)
        cat_input = parts[0].strip()
        sub_input = parts[1].strip() if len(parts) > 1 else ""

        category_id = self._match_category_id(cat_input)
        category = find_category(category_id)

        if not category:
            return "drama", "", f"用户指定: {hint} (未匹配，回退 drama)"

        subtype_id = ""
        subtype_label = ""

        if sub_input:
            # 尝试匹配子类型
            for sub in category.get("subtypes", []):
                if sub_input in (sub["id"], sub["label_cn"], sub["label_en"]):
                    subtype_id = sub["id"]
                    subtype_label = sub["label_cn"]
                    break

        if not subtype_id and category.get("subtypes"):
            # 默认取第一个子类型
            subtype_id = category["subtypes"][0]["id"]
            subtype_label = category["subtypes"][0]["label_cn"]

        info = f"{category['label_cn']} / {subtype_label}"
        return category_id, subtype_id, info

    def _match_category_id(self, text: str) -> str:
        """模糊匹配类别 id，支持英文 id、中文标签、英文标签"""
        text_lower = text.lower().strip()
        for cat in _REGISTRY.get("categories", []):
            if text_lower in (cat["id"], cat["label_cn"].lower(), cat["label_en"].lower()):
                return cat["id"]
        # 关键词模糊匹配
        _KEYWORD_MAP = {
            "drama": "drama", "剧情": "drama", "故事": "drama", "叙事": "drama",
            "visual": "visual", "质感": "visual", "视觉": "visual", "美学": "visual", "氛围": "visual",
            "education": "education", "知识": "education", "教育": "education", "教程": "education",
            "commerce": "commerce", "带货": "commerce", "产品": "commerce", "电商": "commerce",
            "engagement": "engagement", "互动": "engagement", "参与": "engagement", "vlog": "engagement",
            "emotional": "emotional", "情感": "emotional", "共鸣": "emotional", "怀旧": "emotional", "励志": "emotional",
        }
        return _KEYWORD_MAP.get(text_lower, "")

    def _auto_detect_style(self, frames: list[dict[str, Any]]) -> tuple[str, str, str]:
        """LLM 自动识别视频风格"""
        analysis_prompt = _REGISTRY.get("style_analysis_prompt", "")
        if not analysis_prompt:
            return "drama", "", "默认风格: 剧情叙事"

        frame_descs = [f.get("description", "") for f in frames if f.get("description")]
        if not frame_descs:
            return "drama", "", "默认风格: 剧情叙事（无帧描述）"

        all_desc = "\n".join(f"帧 {i+1}: {d}" for i, d in enumerate(frame_descs))

        try:
            raw = self.llm.chat(analysis_prompt, all_desc)
            result = self.llm.parse_json_object(raw)
            cat_id = result.get("category_id", "drama")
            sub_id = result.get("subtype_id", "")
            reason = result.get("reason", "")

            # 校验 category_id 有效
            if not find_category(cat_id):
                cat_id = "drama"

            category = find_category(cat_id)
            cat_label = category["label_cn"] if category else cat_id

            sub_label = ""
            if sub_id:
                sub = find_subtype(cat_id, sub_id)
                if sub:
                    sub_label = sub["label_cn"]
                else:
                    sub_id = ""

            info = f"{cat_label}"
            if sub_label:
                info += f" / {sub_label}"
            if reason:
                info += f" ({reason})"

            return cat_id, sub_id, info
        except Exception as exc:
            logger.warning("风格识别失败: %s", exc)
            return "drama", "", "默认风格: 剧情叙事（识别失败）"

    # ── 自动分镜规划 (auto 模式) ────────────────────────────────

    def _analyze_scene_plan(
        self,
        storyline: str,
        frames: list[dict[str, Any]],
        video_duration: float,
    ) -> list[int]:
        """让 LLM 根据原视频内容自动规划分镜方案。

        返回每个分镜的时长列表，例如 [8, 10, 6] 表示 3 个分镜，分别 8/10/6 秒。

        规则：
        - 分镜数量: 2~5 个
        - 每个分镜时长: 4~15 秒
        - 根据原视频的内容密度和节奏来决定
        """
        if not storyline:
            storyline = "无法提取主线内容，请根据关键帧描述自行规划。"

        frame_info = ""
        if frames:
            frame_info = f"\n\n原视频关键帧数量: {len(frames)}"

        system_prompt = (
            "你是一个专业的短视频分镜规划师。根据原视频的主线内容和节奏，规划最优的分镜方案。\n\n"
            f"规则：\n"
            f"- 原视频时长约为 {video_duration:.1f} 秒，你规划的【分镜总时长】必须与原视频时长相近（允许 ±3 秒偏差），绝对不能差别太大。\n"
            f"- 分镜数量: {AUTO_MIN_SCENES}~{AUTO_MAX_SCENES} 个\n"
            f"- 每个分镜时长: {AUTO_MIN_DURATION}~{AUTO_MAX_DURATION} 秒\n"
            f"- 分镜总时长不宜超过 45 秒\n\n"
            "规划原则：\n"
            "- 内容密度高的步骤（如精细操作、关键转折）应该分配更长时长\n"
            "- 简单的过渡或铺垫可以短一些\n"
            "- 每个分镜应该是视频中的一个完整动作或步骤\n"
            "- 不要强行凑够 5 个分镜，内容少就少分几个\n"
            "- 每个分镜的时长应该是整数\n\n"
            f"原视频主线内容：\n{storyline}{frame_info}\n\n"
            "返回 JSON 格式：\n"
            '{"num_scenes": 3, "durations": [10, 8, 12], "reason": "简要说明规划理由"}\n'
            "只返回 JSON，不要其他文字。"
        )

        try:
            raw = self.llm.chat(system_prompt, "请规划分镜方案。")
            result = self.llm.parse_json_object(raw)
            durations = result.get("durations", [])

            if not isinstance(durations, list) or not durations:
                logger.warning("分镜规划返回无效，回退到默认 3×15s")
                return [self.clip_duration] * min(NUM_SCENES, max(1, len(frames) - 1))

            # 校验并裁剪
            validated = []
            for d in durations:
                d_int = int(d)
                d_int = max(AUTO_MIN_DURATION, min(AUTO_MAX_DURATION, d_int))
                validated.append(d_int)

            # 限制数量
            validated = validated[:AUTO_MAX_SCENES]
            if len(validated) < AUTO_MIN_SCENES:
                validated = validated + [AUTO_MIN_DURATION] * (AUTO_MIN_SCENES - len(validated))

            # 限制帧数（需要 len+1 个边界帧）
            max_from_frames = max(1, len(frames) - 1)
            if len(validated) > max_from_frames:
                validated = validated[:max_from_frames]

            logger.info(
                "分镜规划: %d 个分镜, 时长 %s, 总计 %ds — %s",
                len(validated), validated, sum(validated),
                result.get("reason", ""),
            )
            return validated

        except Exception as exc:
            logger.warning("分镜规划失败: %s, 回退到默认方案", exc)
            return [self.clip_duration] * min(NUM_SCENES, max(1, len(frames) - 1))

    # ── 脚本构建 ──────────────────────────────────────────────────

    def _extract_storyline(self, frames: list[dict[str, Any]]) -> str:
        """从关键帧描述中提取原视频的主线内容。

        让 LLM 分析所有关键帧描述，提炼出：
        - 原视频在做什么（主线任务）
        - 具体步骤和先后顺序
        - 环境和道具
        - 开头/结尾的状态对比

        返回结构化的中文主线描述，用于指导脚本生成。
        """
        descs = [f.get("description", "") for f in frames if f.get("description")]
        if not descs:
            return ""

        all_desc = "\n".join(f"帧{i+1}: {d}" for i, d in enumerate(descs))

        system_prompt = (
            "你是一个视频内容分析师。根据以下关键帧描述，提炼原视频的主线内容。\n\n"
            "请输出：\n"
            "1. 主线任务：这个视频在做什么事（一句话概括）\n"
            "2. 具体步骤：按时间顺序列出实际发生的事情（3-5步）\n"
            "3. 环境场景：在哪里，周围是什么样的\n"
            "4. 使用的工具/材料：视频中出现了哪些工具和材料\n"
            "5. 起始状态 → 最终状态：开始时什么样，结束时什么样\n\n"
            "要求：\n"
            "- 只描述关键帧中**实际出现**的内容，不要编造\n"
            "- 保持客观，不要添加主观评价\n"
            "- 用中文输出，简洁清晰\n"
            "- 不要输出 JSON，直接输出文本"
        )

        try:
            storyline = self.llm.chat(system_prompt, all_desc).strip()
            logger.info("原视频主线提取完成:\n%s", storyline[:300])
            return storyline
        except Exception as exc:
            logger.warning("主线提取失败: %s", exc)
            return ""

    def _build_visual_summary(self, frames: list[dict[str, Any]]) -> str:
        """从关键帧描述中提炼视觉特征摘要"""
        descs = [f.get("description", "") for f in frames if f.get("description")]
        if not descs:
            return "Unknown visual style"
        return "\n".join(descs[:5])

    def _build_script(
        self,
        character_description: str,
        style_info: str,
        visual_summary: str,
        content_storyline: str,
        category: dict[str, Any] | None,
        subtype: dict[str, Any] | None,
        num_scenes: int,
        scene_durations: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """LLM 构建脚本，基于原视频主线内容 + 风格模板"""
        if scene_durations and len(scene_durations) == num_scenes:
            total_dur = sum(scene_durations)
            durations_str = ", ".join(f"Scene {i+1}={d}s" for i, d in enumerate(scene_durations))
        else:
            scene_durations = [self.clip_duration] * num_scenes
            total_dur = num_scenes * self.clip_duration
            durations_str = f"each {self.clip_duration}s"

        subtype_label = (subtype or {}).get("label_en", "General")
        char_desc = character_description if character_description else "infer from visual style"

        # 加载风格专用 prompt
        if category and category.get("script_prompt"):
            template = category["script_prompt"]
        else:
            template = self._default_script_prompt()

        system_prompt = template.format(
            num_scenes=num_scenes,
            total_dur=total_dur,
            clip_dur=self.clip_duration,
            subtype_label=subtype_label,
            character_desc=char_desc,
        )

        # 添加输出格式说明
        system_prompt += _SCRIPT_OUTPUT_FORMAT.format(num_scenes=num_scenes)

        # 构建用户消息：如果有主线内容，作为核心约束传入
        if content_storyline:
            storyline_instruction = (
                "CRITICAL — ORIGINAL VIDEO CONTENT (you MUST follow this storyline):\n"
                f"{content_storyline}\n\n"
                "Your script MUST recreate the SAME task/process shown above. "
                "Use the same tools, materials, environment, and sequence of actions. "
                "Do NOT invent different activities or settings.\n"
            )
        else:
            storyline_instruction = ""

        user_message = (
            f"Create a {num_scenes}-scene script (total {total_dur}s).\n"
            f"Scene durations: {durations_str}\n"
            f"Style: {style_info}\n\n"
            f"{storyline_instruction}"
            f"Visual reference from original video:\n{visual_summary[:600]}\n\n"
            f"Character appearance (MUST be consistent across ALL scenes): {char_desc}"
        )

        try:
            raw = self.llm.chat(system_prompt, user_message)
            return self._parse_script(raw, num_scenes, scene_durations)
        except Exception as exc:
            logger.error("脚本构建失败: %s", exc)
            return []

    def _default_script_prompt(self) -> str:
        """默认脚本 prompt（注册表加载失败时的兜底）"""
        return (
            "You are a master short-form video screenwriter.\n\n"
            "Create a {num_scenes}-scene script for a {total_dur}-second vertical short video ({clip_dur}s per scene).\n\n"
            "STYLE: General storytelling\n\n"
            "Each scene must have: concrete character actions, visual description, camera movement, emotional tone.\n"
            "3-act structure: Scene 1 hooks, Scene 2 escalates, Scene 3 resolves.\n\n"
            "CHARACTER LOCK: {character_desc}"
        )

    def _fallback_script(
        self,
        character_description: str,
        num_scenes: int,
        scene_durations: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """LLM 失败时的兜底分镜方案"""
        if not scene_durations:
            scene_durations = [self.clip_duration] * num_scenes

        char_note = f" ({character_description[:60]})" if character_description else ""
        time_cursor = 0.0
        scenes = []
        for i in range(1, num_scenes + 1):
            dur = float(scene_durations[i - 1] if i <= len(scene_durations) else self.clip_duration)
            scenes.append({
                "scene_id": i,
                "time_start": time_cursor,
                "time_end": time_cursor + dur,
                "duration": dur,
                "title": f"Scene {i}",
                "script": f"Scene {i}{char_note}",
                "visual": "natural lighting, vertical 9:16 composition",
                "camera": "static, eye level",
                "emotion": "neutral",
                "description": f"分镜 {i}{char_note}",
            })
            time_cursor += dur
        return scenes

    def _parse_script(
        self,
        raw_text: str,
        num_scenes: int,
        scene_durations: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """解析 LLM 返回的脚本 JSON"""
        text = raw_text.strip()

        for parse_fn in (self._try_direct_parse, self._try_extract_json):
            result = parse_fn(text)
            if result:
                return self._finalize_scenes(result, num_scenes, scene_durations)

        logger.error("无法解析脚本 JSON: %s", text[:300])
        return []

    def _try_direct_parse(self, text: str, num_scenes: int = 0) -> list[dict] | None:
        try:
            result = json.loads(text)
            if isinstance(result, list) and result:
                return result
        except json.JSONDecodeError:
            pass
        return None

    def _try_extract_json(self, text: str, num_scenes: int = 0) -> list[dict] | None:
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                result = json.loads(text[start:end])
                if isinstance(result, list) and result:
                    return result
            except json.JSONDecodeError:
                pass
        return None

    def _finalize_scenes(
        self,
        scenes: list[dict],
        num_scenes: int,
        scene_durations: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """统一场景结构，支持每个场景不同时长"""
        if not scene_durations:
            scene_durations = [self.clip_duration] * num_scenes

        final = []
        time_cursor = 0.0
        for i, s in enumerate(scenes, 1):
            if i > num_scenes:
                break
            dur = float(scene_durations[i - 1] if i <= len(scene_durations) else self.clip_duration)
            final.append({
                "scene_id": i,
                "time_start": time_cursor,
                "time_end": time_cursor + dur,
                "duration": dur,
                "title": s.get("title", f"Scene {i}"),
                "script": s.get("script", ""),
                "visual": s.get("visual", ""),
                "camera": s.get("camera", ""),
                "emotion": s.get("emotion", ""),
                "description": s.get("description", s.get("script", f"Scene {i}")),
            })
            time_cursor += dur
        return final

    # ── Prompt 生成（注入风格 modifier）──────────────────────────

    def _generate_scene_prompts(
        self,
        scene: dict[str, Any],
        character_description: str,
        prompt_modifiers: dict[str, str],
        subtype: dict[str, Any] | None,
    ) -> dict[str, str]:
        """为单个场景生成 prompt

        将人物外貌描述作为参考提供给 LLM，由 LLM 在 prompt 中自然融入。
        LLM 负责生成完整的 prompt（包括人物外貌、场景环境、动作、镜头等）。
        """
        subtype_label = (subtype or {}).get("label_en", "General")

        # 使用场景自身的时长（auto 模式下每个场景可能不同）
        scene_dur = int(scene.get("duration", self.clip_duration))

        # 风格 modifier
        img_modifier = prompt_modifiers.get("image", "Cinematic, 4K, vertical 9:16")
        vid_modifier = prompt_modifiers.get("video", f"Cinematic {scene_dur}s, 4K, vertical 9:16")
        img_modifier = img_modifier.replace("{subtype_label}", subtype_label).replace("{clip_dur}", str(scene_dur))
        vid_modifier = vid_modifier.replace("{subtype_label}", subtype_label).replace("{clip_dur}", str(scene_dur))

        # LLM 的 system prompt：提供人物描述作为参考，LLM 自然融入
        char_context = ""
        if character_description:
            char_context = (
                f"\n\nCHARACTER REFERENCE (use this as the basis for describing the character's appearance in the prompt):\n"
                f"{character_description}\n"
                f"Keep the character's core identity (age, ethnicity, hair) consistent, "
                f"but you may naturally adapt clothing details to fit the scene context."
            )

        shot_guidance = (
            "SHOT COMPOSITION for this scene: Use a MEDIUM shot, WIDE shot, or BACKGROUND-focused shot. "
            "Show the FULL environment and setting. Emphasize the space, the activity, and the surroundings. "
            "CRITICAL: Avoid using close-ups (CU) or extreme close-ups (ECU) as they are prone to generation issues. "
            "Feel free to incorporate creative camera angles such as overhead (bird's-eye view), low-angle, or shooting from behind the character, if it suits the action."
        )

        system_prompt = (
            "You are a professional AI video prompt engineer.\n\n"
            "TASK: Generate an English video prompt for this scene."
            f"{char_context}\n\n"
            f"{shot_guidance}\n\n"
            "RULES:\n"
            f"1. video_prompt: describe a {scene_dur}s dynamic shot — motion, camera movement, "
            f"action progression, atmosphere. Style: {vid_modifier}\n"
            "   CRITICAL for video_prompt: This video will be generated using keyframe interpolation (a start frame and an end frame). "
            "   You MUST describe a smooth, natural camera movement (e.g., 'slow pull back from a close-up detail to reveal the full scene', 'smooth pan') "
            "   or a logical, continuous action progression that perfectly bridges the start and end states. Avoid abrupt jumps or unnatural morphing.\n"
            f"   TIMING FORMAT RULE FOR video_prompt: You MUST format the prompt with two explicit timestamps to control pacing. The narrative must flow continuously between them, but the final ~30% of the video must feature a fast-forward effect.\n"
            "   Format exactly like this:\n"
            f"   '[0s-{scene_dur * 7 // 10}s]: <Detailed description of the character actions and camera movement at normal speed, building up the scene.>\n"
            f"    [{scene_dur * 7 // 10}s-{scene_dur}s]: <Continue the same narrative detailing the ongoing actions, but explicitly state that the scene accelerates into a dynamic fast-forward motion, smoothly decelerating to a crisp freeze-frame on the exact final state.>'\n"
            "2. Make the script FAST-PACED and HIGHLY INFORMATIVE. Use dynamic camera movements (e.g., orbital dolly, arc, push-in) to create tension and reveal details.\n"
            "3. The prompt should be vivid, specific, and visually descriptive.\n\n"
            "REALISM RULES (CRITICAL):\n"
            "- All actions must be physically possible and natural for a real person\n"
            "- Do NOT describe exaggerated, cartoonish, or superhuman actions\n"
            "- Objects must obey physics: things fall down, liquids flow naturally, materials behave realistically\n"
            "- Environment must be internally consistent: same location, same lighting direction, same weather\n"
            "- Object Permanence: Objects, people, or animals must NOT suddenly pop into existence or disappear mid-shot. Ensure strict continuity and logical presence.\n"
            "- The scene must look like something you could photograph with a real camera\n"
            "- Avoid sudden scene changes within a single prompt — one continuous shot, one location\n"
            "- If the scene involves work/craft/gardening, describe realistic tools, techniques and body mechanics\n\n"
            "PROMPT STYLE EXAMPLES (Follow the GOOD style!):\n"
            "❌ BAD (Too dramatic/exaggerated, subjective, overly emotional):\n"
            "Dramatic cinematic 15s shot, film grain, emotional performance, smooth camera movement, vertical 9:16. The scene opens with a handheld medium close-up of a 30-year-old Caucasian woman with long brown hair, wearing a red sleeveless top and dark shorts, standing in her suburban British backyard. Natural overcast daylight bathes the scene in soft, even light. She gazes at an overgrown patch of grass and weeds beside her red brick terrace house, hands on hips, expression shifting slowly from wistful longing to quiet frustration; she kicks gently at a tuft of weeds. The camera holds a slight push-in toward her face as her eyes suddenly catch something off-frame—her brow lifts, head turns with growing curiosity toward her neighbor's driveway. The camera executes a smooth whip pan to follow her gaze, revealing old paving slabs piled for disposal. Quick cut back to her face as it transforms: a mischievous, inspired grin spreading across her features, eyes bright with sudden vision. She points toward the slabs, then mimes carrying something heavy with both hands, shoulders engaged, already living the transformation. The camera pushes in closer to her face, capturing the hopeful determination glowing in her expression, warm earth tones and the red of her top vivid against green grass and brick. The shot resolves on her confident, dreaming smile—transformation already begun in her mind.\n\n"
            "✅ GOOD (Fast-paced, highly informative, dynamic camera movement, continuous narrative with timestamps):\n"
            "[0s-2s]: A deep navy blue fabric armchair in dynamic split-state against a pure white cyclorama studio backdrop. The backrest component is lifted and held approximately 12 inches above the seat body by an invisible rig or clear acrylic support arm, both components positioned to appear suspended with wooden cone legs remaining attached to the base. A blue-white floral patterned cushion rests on the elevated backrest. The camera begins a slow orbital dolly movement from a medium-wide shot, capturing the full assembly at eye level with the product centered slightly below frame middle. Clean shadowless even lighting from large softboxes illuminates the scene. The components slowly rotate on a turntable to reveal internal assembly structure. On-screen text appears in lower right: a blue-green rectangular label with white text 'FEATURING OUR' above smaller 'Patented Comfort Flex™' — partially blurred to create intrigue. Natural oak wood legs, deep navy fabric texture, and crisp white background dominate the color palette. 35mm equivalent lens with shallow depth of field keeps the product sharp. Smooth motorized camera movement begins a 15° arc around the furniture, slight push-in amplifying tension.\n"
            "[2s-4s]: The orbital dolly continues around the elevated components, now revealing the underside and internal frame structure of both pieces as they maintain their stable positions on the hidden support rig. The blue-white floral cushion remains undisturbed on the backrest. The blue-green text label stays visible in lower right, still intentionally blurred. The scene accelerates into dynamic fast-forward motion — the orbital speed increases, the turntable rotation of components becomes rapid and energetic, creating motion blur on the edges while the central product mass stays relatively readable. The fast-forward effect smoothly decelerates to a crisp freeze-frame on the exact final state: the camera holds still on a three-quarter angle showing the full separation gap between backrest and seat body, wooden legs anchored to the base, floral cushion in perfect position, and the blurred text label still present in lower right corner against the pure white cyclorama.\n\n"
            'Return JSON: {"video_prompt": "..."}\n'
            "Return ONLY JSON."
        )

        user_message = (
            f"Scene title: {scene.get('title', '')}\n"
            f"Script: {scene.get('script', '')}\n"
            f"Visual direction: {scene.get('visual', '')}\n"
            f"Camera: {scene.get('camera', '')}\n"
            f"Emotion: {scene.get('emotion', '')}\n"
            f"Duration: {self.clip_duration}s\n\n"
            "Generate the video prompt."
        )

        try:
            raw = self.llm.chat(system_prompt, user_message)
            result = self.llm.parse_json_object(raw)
            if result.get("video_prompt"):
                return {
                    "video_prompt": result["video_prompt"],
                }
        except Exception as exc:
            logger.warning("Prompt 生成失败 (scene %s): %s", scene.get("scene_id"), exc)

        # 兜底
        desc = scene.get("description", "")
        char_prefix = character_description + ". " if character_description else ""
        return {
            "image_prompt": f"{char_prefix}{img_modifier}: {desc}",
            "video_prompt": f"{char_prefix}{vid_modifier}: {desc}",
        }

    # ── 连贯性审查 ──────────────────────────────────────────────

    def _review_continuity(self, scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """审查所有场景 prompt 的连贯性和物理合理性。

        让 LLM 检查所有场景的 image_prompt 和 video_prompt，发现：
        - 物理上不可能的描述
        - 场景间环境/光线不一致
        - 动作不合理或过于夸张
        - 物品/道具凭空出现或消失

        如果发现问题，让 LLM 直接修正 prompt。
        """
        if len(scenes) < 2:
            return scenes

        # 收集所有 prompt 供审查
        review_input = ""
        for s in scenes:
            review_input += (
                f"\n--- Scene {s['scene_id']} ---\n"
                f"image_prompt: {s.get('image_prompt', '')}\n"
                f"video_prompt: {s.get('video_prompt', '')}\n"
            )

        system_prompt = (
            "You are a CONTINUITY and REALISM reviewer for AI-generated video prompts.\n\n"
            "Review ALL scenes below for these issues:\n"
            "1. PHYSICS VIOLATIONS: anything physically impossible, cartoonish, or exaggerated\n"
            "2. CONTINUITY ERRORS: environment, lighting, weather, time-of-day changing between scenes without reason\n"
            "3. IMPOSSIBLE ACTIONS: body positions or movements that a real human cannot do\n"
            "4. MAGIC PROPS: objects appearing/disappearing between scenes\n"
            "5. UNREALISTIC MATERIALS: materials behaving wrong (water flowing uphill, dirt floating, etc.)\n\n"
            "If you find NO issues, respond with exactly: {\"issues\": false}\n\n"
            "If you find issues, fix ONLY the problematic prompts. Return JSON:\n"
            "{\n"
            "  \"issues\": true,\n"
            "  \"fixes\": {\n"
            "    \"1\": {\"image_prompt\": \"fixed prompt\", \"video_prompt\": \"fixed prompt\"},\n"
            "    \"2\": {\"image_prompt\": \"fixed prompt\", \"video_prompt\": \"fixed prompt\"}\n"
            "  }\n"
            "}\n\n"
            "Only include scenes that need fixing. If a scene is fine, don't include it in fixes.\n"
            "When fixing: keep the same overall intent but make the description physically plausible.\n"
            "Return ONLY JSON."
        )

        try:
            raw = self.llm.chat(system_prompt, review_input)
            result = self.llm.parse_json_object(raw)

            if not result or not result.get("issues"):
                logger.info("连贯性审查通过，无需修正")
                return scenes

            fixes = result.get("fixes", {})
            if not fixes:
                return scenes

            for scene_id_str, fix in fixes.items():
                scene_id = int(scene_id_str)
                for s in scenes:
                    if s["scene_id"] == scene_id:
                        if fix.get("image_prompt"):
                            logger.info("修正 scene_%d image_prompt", scene_id)
                            s["image_prompt"] = fix["image_prompt"]
                        if fix.get("video_prompt"):
                            logger.info("修正 scene_%d video_prompt", scene_id)
                            s["video_prompt"] = fix["video_prompt"]

            logger.info("连贯性审查完成，修正了 %d 个场景", len(fixes))

        except Exception as exc:
            logger.warning("连贯性审查失败（不影响流程）: %s", exc)

        return scenes

    # ── 辅助 ──────────────────────────────────────────────────────

    def _find_best_reference_frame(
        self,
        scene: dict[str, Any],
        frames: list[dict[str, Any]],
    ) -> str:
        """根据场景描述，从原视频中找视觉上最接近的参考帧"""
        if not frames:
            return ""

        for f in frames:
            desc = f.get("description", "").lower()
            if any(kw in desc for kw in ["人", "脸", "女性", "男性", "woman", "man", "person", "girl", "boy"]):
                return f.get("path", "")

        return frames[0].get("path", "") if frames else ""
