"""内容规划引擎 - 基于自定义人设，自动规划30天全套短视频脚本。

核心流程：
  1. 用户定义人设（性别、年龄、种族、职业、兴趣、价值观、用语风格、肖像等）
  2. LLM 根据人设生成30天内容日历（4大板块均衡分配，7天小周期结构）
  3. 为每一天生成标准化视频脚本（标题/封面文案/镜头脚本/台词/品牌推荐）
  4. 汇总爆款TOP10 + 高频品牌清单
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from core.llm_client import LLMClient

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────


@dataclass
class Persona:
    """自定义人设"""

    # 基本信息
    age: int = 28
    gender: str = "female"
    ethnicity: str = "Second-generation Indian American"
    location: str = "California, USA"
    language: str = "English"
    accent: str = "slight Indian accent"
    platform: str = "TK"  # 平台: TK, reddit, Twitter, YT, FB

    # 职业与身份
    occupation: str = "Full-time high-end real estate agent (single-family & essential housing)"
    experience_years: int = 4
    personal_tags: str = "Expert in US real estate, home renovation specialist, high ROI remodeling, American light luxury / Indian fusion decor, down-to-earth real estate blogger"  # 个人标签

    # 受众
    target_audience: str = "US homebuyers aged 25-45, new homeowners, old house renovators, budget-friendly remodeling seekers"

    # 个性与风格
    personality: str = "Enthusiastic, straightforward, loves sharing, loves showing off clients' home transformations"  # 性格特点
    content_style: str = "Blends Indian aesthetics with American decor, shares highly cost-effective renovation plans and mistake-avoidance guides"  # 内容风格

    # 肖像描述（用于生成图片时的外貌参考）
    portrait_description: str = "28-year-old Indian American woman, confident and approachable, professional yet stylish real estate agent attire, warm smile"

    # 额外信息
    extra_info: str = ""

    def to_prompt_text(self) -> str:
        """生成供 LLM 使用的人设描述文本"""
        lines = [
            f"Age: {self.age}",
            f"Gender: {self.gender}",
            f"Ethnicity: {self.ethnicity}",
            f"Location: {self.location}",
            f"Language: {self.language} (accent: {self.accent})",
            f"Occupation: {self.occupation} ({self.experience_years} years experience)",
        ]
        if self.personal_tags:
            lines.append(f"Personal tags: {self.personal_tags}")
        if self.target_audience:
            lines.append(f"Target audience: {self.target_audience}")
        if self.personality:
            lines.append(f"Personality: {self.personality}")
        if self.content_style:
            lines.append(f"Content style: {self.content_style}")
        if self.portrait_description:
            lines.append(f"Portrait/Appearance: {self.portrait_description}")
        if self.extra_info:
            lines.append(f"Additional info: {self.extra_info}")
        if self.platform:
            platform_hint = f"Platform: {self.platform}"
            if self.platform in ["TK", "YT"]:
                platform_hint += " (Focus on vertical short videos, fast-paced, information-dense)"
            elif self.platform in ["reddit", "Twitter", "FB"]:
                platform_hint += " (Focus on image/text posts. "
                if self.platform == "reddit":
                    platform_hint += "Reddit communities are highly sensitive to AI-generated content, so strictly reduce 'AI flavor', be highly authentic, and design content to spark discussions and choices. "
                elif self.platform == "Twitter":
                    platform_hint += "Keep it concise, engaging, and suitable for threads. "
                elif self.platform == "FB":
                    platform_hint += "Focus on community engagement and shareability. "
                platform_hint += "Adapt 'video' references in your output to mean 'image/text carousel or post'.)"
            lines.append(platform_hint)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "age": self.age,
            "gender": self.gender,
            "ethnicity": self.ethnicity,
            "location": self.location,
            "language": self.language,
            "accent": self.accent,
            "platform": self.platform,
            "occupation": self.occupation,
            "experience_years": self.experience_years,
            "personal_tags": self.personal_tags,
            "target_audience": self.target_audience,
            "personality": self.personality,
            "content_style": self.content_style,
            "portrait_description": self.portrait_description,
            "extra_info": self.extra_info,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Persona:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── Prompt 模板 ──────────────────────────────────────────────────

_CALENDAR_SYSTEM_PROMPT = """You are a professional overseas social media content strategist. \
Based on the given persona, you will plan ONE WEEK of daily content.

HARD REQUIREMENTS:
1. Format: Tailor the content format (video or image/text) based on the target Platform specified in the PERSONA.
2. Language: All titles in English

CONTENT TRACKS (4 fixed tracks, balanced across 30 days):
- Track A: Core expertise content — professional knowledge, tips, industry insights
- Track B: Practical guides — how-to, transformation, before/after
- Track C: Brand/Product spotlight — reviews, recommendations, tiered pricing
- Track D: Daily vlog — behind the scenes, daily routine, client work

WEEKLY CYCLE STRUCTURE (each 7-day week must follow this):
- 1 viral/hook video (broad appeal, designed to go viral)
- 4 niche/vertical content videos
- 1 brand review video
- 1 daily vlog

RULES:
- NO duplicate topics with previous weeks (I will tell you what was already used)
- Difficulty and depth should progress gradually week by week
- Each video must have a clear hook in the first 3 seconds
- Topics must be culturally relevant to the US market
- The last 2 days of the month (Day 29-30) should be a grand finale/recap

OUTPUT FORMAT — return a JSON ARRAY of 7 objects (or fewer if told):
[
  {{
    "day": 1,
    "week": 1,
    "track": "A",
    "track_name": "Core expertise",
    "role": "viral",
    "title_en": "5 Things Your Real Estate Agent Won't Tell You",
    "title_cn": "房产中介不会告诉你的5件事",
    "hook": "The hook description (first 3 seconds or opening sentence)",
    "topic_summary": "Brief summary of what this content covers",
    "pain_point": "What audience pain point this addresses",
    "estimated_viral_score": 8
  }}
]

Return ONLY the JSON array. No markdown, no explanation.
"""

_SCRIPT_SYSTEM_PROMPT = """You are a professional social media content creator specializing in authentic, down-to-earth vlogs and realistic content.

Generate a detailed, grounded, and realistic content script/plan for a single piece of content (video or image/text post, depending on the platform).

The script MUST include ALL of the following sections:

1. CONTENT META:
   - title_en: English title optimized for the platform
   - title_cn: Chinese title for reference
   - day: Day number (1-30)

2. COVER TEXT / MAIN HEADLINE:
   - main_text: Bold main headline (5-8 words, punchy)
   - sub_text: Smaller subtitle (context/additional info)

3. CONTENT INFO:
   - core_topic: The single core topic of this content
   - pain_point: The audience pain point being addressed

4. CONTENT BREAKDOWN / SHOTS (adapt to platform):
   If Video (TK, YT): Split into 2-4 continuous segments (15s total).
   If Image/Text (Reddit, Twitter, FB): Split into 2-4 image/text frames or paragraphs.
   For EACH segment provide:
   - shot_id: sequential number
   - duration: seconds (if video, e.g., "5s") or "image/text"
   - visual: REALISTIC and GROUNDED visual descriptions. If Reddit/Twitter, describe the image or text focus. Focus on authentic everyday settings.
   - dialogue: Spoken dialogue (for video) or main text/caption (for image/text).
   - location: indoor/outdoor/studio

5. SUPPLEMENTARY:
   - recommendations: Product/brand recommendations if applicable (with tier: budget/mid/high-end)
   - hashtags: 5-8 relevant hashtags
   - notes: Any production notes (e.g., if Reddit, add notes on how to reduce AI-flavor and trigger discussion)

STYLE RULES:
- AUTHENTIC, REALISTIC, and RELATABLE. Focus heavily on real-world scenes.
- If Reddit: strictly reduce 'AI flavor', be authentic, design content to spark discussions and choices. Do not sound promotional.
- POV & PERSONA RULE: Usually First-Person Point of View (POV). The author typically doesn't appear on screen.
- ACTION REALISM (if video): Explicitly describe OTHER hired workers performing the action. Do not describe objects moving by themselves. Ensure strict physical realism.
- LANGUAGE: The final JSON output MUST be entirely in Chinese (简体中文), EXCEPT for `title_en`, `hashtags`, and any English product names. The visual descriptions (`visual`) and dialogue (`dialogue`) MUST be in Chinese.

OUTPUT FORMAT — return JSON:
{{
  "day": 1,
  "title_en": "...",
  "title_cn": "...",
  "cover": {{
    "main_text": "...",
    "sub_text": "..."
  }},
  "core_topic": "...",
  "pain_point": "...",
  "shots": [
    {{
      "shot_id": 1,
      "duration": "5s",
      "visual": "...",
      "dialogue": "...",
      "location": "indoor"
    }},
    {{
      "shot_id": 2,
      "duration": "5s",
      "visual": "...",
      "dialogue": "...",
      "location": "indoor"
    }},
    {{
      "shot_id": 3,
      "duration": "5s",
      "visual": "...",
      "dialogue": "...",
      "location": "indoor"
    }}
  ],
  "recommendations": ["brand1 (mid-tier) - brief reason", ...],
  "hashtags": ["#tag1", "#tag2", ...],
  "notes": "..."
}}

Return ONLY the JSON. No markdown, no explanation.
"""

_VIDEO_PROMPT_SYSTEM = """You are an AI video generation prompt engineer specializing in renovation / home-construction vertical short videos.

Convert the provided 15-second video script into a single, comprehensive video generation prompt suitable for the seedance-2.0-fast model.

CORE RULES:
1. SEGMENT COUNT MUST MATCH THE SCRIPT. The script provides N shots (2 to 4). Your output MUST contain exactly N timestamped segments, one per shot. Do NOT invent extra cuts. Do NOT merge shots. Each [start-end] timestamp in your output must correspond exactly to that shot's duration in the script.
2. POV & PERSONA RULE: The video is shot entirely from a First-Person Point of View (POV) by the author holding a smartphone. The author NEVER appears on screen. DO NOT include any circular talking-head avatar overlays, the author's face, or the author's body.
3. CAMERA: Strictly AVOID close-ups and full-body shots. Use medium shots, wide shots, or environmental POV. Describe realistic, smooth, natural camera movements (smartphone gimbal feel). No hyper-cinematic crane/orbital shots.
4. LIGHTING/STYLE: Grounded, everyday lighting only. No cinematic hyper-stylization, no film grain, no neon. Vertical 9:16 only.
5. NO TEXT ON SCREEN: Absolutely no text, subtitles, captions, watermarks, logos, or UI overlays. If the script mentions text, remove it or replace it with a pure environment element (e.g., a real label on a paint can).

REALISM RULES (CRITICAL — any violation makes the video unusable):
- PHYSICS: All actions must be physically possible for a real person. Objects obey gravity, liquids flow down, materials behave realistically (paint drips, dust settles, tiles clink, wood splinters).
- OBJECT PERMANENCE: Tools, materials, furniture, and people must NOT suddenly appear or disappear between segments. If a hammer is set down in segment 1, it must still be on the same surface in segment 2 unless a worker is shown moving it. This is the #1 cause of "无中生有" hallucinations — enforce it strictly.
- ENVIRONMENT CONTINUITY: Same room, same wall color, same furniture layout, same lighting direction, same weather and time-of-day across ALL segments. Treat the 15 seconds as ONE continuous take with natural camera cuts, NOT as separate scenes.
- CONTINUITY OF MOTION: Each segment must start where the previous one ended. Body position, tool placement, and progress state must flow naturally across the cut. No teleportation, no jump cuts.
- ACTION REALISM: If depicting construction, renovation, or any physical work, you MUST explicitly describe OTHER hired workers (hands, arms, or back of a contractor dressed in typical workwear) performing the action. Objects must NEVER move by themselves. Workers shown must clearly be laborers distinct from the author. Do NOT show their faces.
- NO EXAGGERATION: No cartoonish, superhuman, or overly dramatic actions. The final video must look like something a real person could film with a smartphone on a real job site.

TIMING FORMAT RULE:
Format the output as N timestamped segments, where N = the number of shots in the script (2 to 4). Each segment's [start-end] must match the corresponding shot's duration exactly. The narrative must flow continuously across segment boundaries — never start a new "scene".

Format example for a 3-shot script (5s + 5s + 5s):
'[0s-5s]: <Detailed description of camera movement, setting, lighting, and the worker actions in this segment. Grounded and realistic.>\n\n[5s-10s]: <Continue the narrative smoothly — same room, same workers, action progresses naturally from where segment 1 ended.>\n\n[10s-15s]: <Final segment, smoothly concluding the action. The last frame must be a clean, stable end state.>'

Return ONLY the prompt text, using the timestamp format exactly. Do not use JSON or markdown code blocks.
"""


# ── 图文平台（reddit / Twitter / FB）专用 Prompt ────────────────
#
# 这些平台不产出视频，而是产出「图文帖子」：每条内容由若干 frame 组成，
# 每个 frame = 一段文案 (caption) + 一张配图的画面描述 (image_description)。
# 配图的真实生成在 web 端由用户手动触发（见 planner_pipeline.generate_images_for_day），
# 规划阶段只预生成 image_prompt，不调用图片 API。

_TEXT_PLATFORMS = ("reddit", "Twitter", "FB")

# AI 创作人设关键词: 命中则跳过"去AI味"管线(保留精致 AI 艺术效果)
_AI_ART_KEYWORDS = (
    "ai art", "ai artist", "ai generated", "ai-generated", "stable diffusion",
    "flux", "midjourney", "concept art", "ai character", "ai photographer",
    "ai photography", "ai creator",
)


_CALENDAR_SYSTEM_PROMPT_TEXT = """You are a professional overseas social media content strategist. \
Based on the given persona, you will plan ONE WEEK of daily IMAGE/TEXT content (NOT video).

HARD REQUIREMENTS:
1. Format: IMAGE/TEXT posts only (carousels, threads, or single-image posts). NO video content.
2. Language: Write ALL fields (title_en, hook, topic_summary, pain_point) in the language specified in PERSONA.language (default: English). `title_cn` is a Chinese translation for reference only.

CONTENT TRACKS (4 fixed tracks, balanced across 30 days):
- Track A: Core expertise content — professional knowledge, tips, industry insights
- Track B: Practical guides — how-to, transformation, before/after
- Track C: Brand/Product spotlight — reviews, recommendations, tiered pricing
- Track D: Daily life — behind the scenes, daily routine, client work

WEEKLY CYCLE STRUCTURE (each 7-day week must follow this):
- 1 viral/hook post (broad appeal, designed to go viral)
- 4 niche/vertical content posts
- 1 brand review post
- 1 daily life post

RULES:
- NO duplicate topics with previous weeks (I will tell you what was already used)
- Difficulty and depth should progress gradually week by week
- Each post must have a strong opening hook in the first sentence/headline
- Topics must be culturally relevant to the US market
- The last 2 days of the month (Day 29-30) should be a grand finale/recap
- If Reddit: design titles that spark discussion and choices, strictly avoid promotional/AI flavor
- If Twitter: keep titles punchy and thread-friendly

OUTPUT FORMAT — return a JSON ARRAY of 7 objects (or fewer if told):
[
  {
    "day": 1,
    "week": 1,
    "track": "A",
    "track_name": "Core expertise",
    "role": "viral",
    "title_en": "5 Things Your Real Estate Agent Won't Tell You",
    "title_cn": "房产中介不会告诉你的5件事",
    "hook": "The opening sentence / headline hook",
    "topic_summary": "Brief summary of what this post covers",
    "pain_point": "What audience pain point this addresses",
    "estimated_viral_score": 8
  }
]

Return ONLY the JSON array. No markdown, no explanation.
"""


_SCRIPT_SYSTEM_PROMPT_TEXT = """You are a professional social media content creator specializing in authentic, down-to-earth IMAGE/TEXT posts (NOT video).

Generate a detailed, grounded, and realistic content plan for a single IMAGE/TEXT post.

The script MUST include ALL of the following sections:

1. CONTENT META:
   - title_en: English title optimized for the platform
   - title_cn: Chinese title for reference
   - day: Day number (1-30)

2. COVER TEXT / MAIN HEADLINE:
   - main_text: Bold main headline (5-8 words, punchy)
   - sub_text: Smaller subtitle (context/additional info)

3. CONTENT INFO:
   - core_topic: The single core topic of this post
   - pain_point: The audience pain point being addressed

4. CONTENT BREAKDOWN / FRAMES (4-6 frames, each = one image + caption). Generate 4 to 6 frames — fewer frames with less info reads MORE real (8-10 frames crammed with detail is a top "obviously AI" giveaway). All frames MUST depict ONE continuous scene/moment (see SCENE CONTINUITY below):
   For EACH frame provide:
   - frame_id: sequential number (1, 2, 3, ...)
   - caption: The text/copy for this frame (the post body paragraph or slide text). Write in PERSONA.language (default: English).
   - image_description: A REALISTIC and GROUNDED description of what the image should show. Write in PERSONA.language (default: English). Focus on authentic everyday settings, real objects, real scenes. NO cartoon, NO hyper-stylized, NO text overlays. FRAMING (CRITICAL): NEVER describe a person or pet facing the camera, looking at/off camera, making eye contact, in a face close-up, OR in a selfie/handheld/arm's-length shot. NEVER mention facial details (eyes, smile, expression) — even "off-camera" or "turned away" versions are banned. Always describe subjects from BEHIND or as a SMALL figure in a WIDE environmental shot (distant, surroundings dominate). Focus on hands, objects, food, the environment — NOT faces. Example good: "wide shot from across the street, a small figure of a person and a dog walk away down the sidewalk"; Example BAD (forbidden): "selfie-style, the guy looks off-camera with a tired smile".

5. SUPPLEMENTARY:
   - recommendations: Product/brand recommendations if applicable (with tier: budget/mid/high-end)
   - hashtags: 5-8 relevant hashtags
   - notes: Any production notes (e.g., if Reddit, add notes on how to reduce AI-flavor and trigger discussion)

STYLE RULES:
- AUTHENTIC, REALISTIC, and RELATABLE. Focus heavily on real-world scenes.
- NATURALNESS (reduce AI flavor at generation time): Vary caption length drastically between frames — some can be 5 words, others 80+. It's GOOD if 1-2 frames have very little text or a vague caption (real posts aren't uniformly detailed). Use conversational, everyday phrasing, NOT formal/encyclopedic tone. Avoid marketing buzzwords ("game changer", "must-know", "pro tips", "elevate", "transform"). A little messiness (fragments, trailing thoughts) is fine — real posts aren't perfectly edited. Less info per frame reads MORE real.
- SCENE CONTINUITY (CRITICAL — prevents the #1 "obviously AI" giveaway: frames contradicting each other): All frames MUST depict ONE continuous scene/moment — same time of day, same location, same lighting throughout. They should read like photos someone snapped in sequence during ONE real activity (e.g. one cooking session: prep -> chop -> cook -> plate, all in the same kitchen, same light). FORBIDDEN: jumping between locations, flipping day/night, swapping settings, or contradictory states (food raw in frame 2 but fully plated in frame 3 with no cooking step between). Think "a person grabbed their phone and snapped a few quick photos while doing one thing" — NOT "a curated set of different scenes".
- LANGUAGE: Write ALL content (caption, image_description, core_topic, pain_point, notes, title_en, main_text, sub_text) in the language specified in PERSONA.language (default: English). `title_cn` is a Chinese translation for internal reference only.
- REDDIT TONE (critical when platform=reddit): Write captions like a REAL redditor — first-person, conversational, slightly informal, sharing genuine experience or asking for input. Absolutely NO marketing speak, NO promotional tone, NO "AI flavor", NO salesy language. Frame the post as a personal story / insight / question, not an ad. Use natural Reddit phrasing (e.g. "Here's what I learned after doing this 3 times...", "Am I the only one who...", "Took me a while to figure this out..."). The goal is to spark discussion and relatability, not to sell.
- TWITTER TONE (when platform=Twitter): Punchy, thread-friendly, strong hook in the first line. Each frame's caption should work as a tweet or thread segment.
- CONTENT TYPE ADAPTATION (CRITICAL for food/cooking personas): If the persona is FOOD/COOKING related (occupation or tags mention cooking, food, recipe, chef, kitchen, meals, cuisine), generate a STEP-BY-STEP COOKING POST, not a story:
  * Pick ONE specific dish. Each frame = one cooking step (ingredients laid out → prep/chopping → in the pan → seasoning → final plate).
  * Focus on the FOOD and the process — hands, ingredients, pans, steam, the dish itself. People do NOT need to appear in every frame (most frames should be just food/kitchen, a person is optional and only as hands or distant back).
  * Give it a clear angle: a comparison ("mom's way vs the internet way"), a tradition ("how my family makes X"), a technique ("the one step everyone gets wrong"), or a specific recipe. Example good direction: "My Asian Mom's 'No Recipe' Fried Rice vs. Every TikTok Hack" — one dish, a clear angle, shown through cooking steps.
  * AVOID long personal narratives, "today I tried...", emotional storytelling. Keep it ABOUT THE DISH and the cooking. Captions should describe what's happening in that step (short, practical), not a life story.
- AI ART ADAPTATION (CRITICAL for AI artist personas — occupation/tags mention AI art, Stable Diffusion, Flux, Midjourney, concept art, AI generated): The content is OPENLY AI-generated art for the AI art community. Apply DIFFERENT rules than the "fake real person" personas:
  * DO NOT apply "candid phone snapshot / anti-AI / imperfection" rules. These are showcase posts — images can be polished, detailed, artistic, even surreal or fantastical. No need for blur, grain, or "messy" framing.
  * DO NOT forbid faces or frontal views — AI art showcases WANT striking, detailed subjects.
  * Captions focus on the CREATIVE PROCESS: the prompt used, the model/settings (e.g. "Flux.1-dev + Realism LoRA, 30 steps"), the concept or inspiration, techniques. AI art communities value prompt/workflow sharing — be generous with details.
  * CONSISTENCY within a post comes from a unified VISUAL STYLE (same art style, color palette, lighting) or the SAME recurring character — the persona's portrait_description defines this anchor (a world, a character, or a style). Describe it explicitly and identically in EVERY frame so the series reads as a cohesive set.
  * FRAMING for AI art: compositions can be cinematic, dramatic, detailed — whatever serves the art. The "wide shot from behind / no faces" rule does NOT apply here.
- Each frame's image should look like a real photo someone took with a phone, NOT a stock photo or AI-generated-looking image. (NOTE: this rule applies to "fake real person" personas ONLY — AI art personas are exempt, their images are openly AI.)

OUTPUT FORMAT — return JSON:
{
  "day": 1,
  "title_en": "...",
  "title_cn": "...",
  "cover": {
    "main_text": "...",
    "sub_text": "..."
  },
  "core_topic": "...",
  "pain_point": "...",
  "frames": [
    {
      "frame_id": 1,
      "caption": "...",
      "image_description": "..."
    },
    {
      "frame_id": 2,
      "caption": "...",
      "image_description": "..."
    },
    {
      "frame_id": 3,
      "caption": "...",
      "image_description": "..."
    }
  ],
  "recommendations": ["brand1 (mid-tier) - brief reason", ...],
  "hashtags": ["#tag1", "#tag2", ...],
  "notes": "..."
}

NOTE: The example above shows only 3 frames for brevity — you MUST generate 4 to 6 frames per section 4.

Return ONLY the JSON. No markdown, no explanation.
"""


_IMAGE_PROMPT_SYSTEM = """You are an AI image generation prompt engineer specializing in authentic, realistic social media photos.

You will receive a list of frames, each with an `image_description` (in Chinese). Convert each one into a single, high-quality image generation prompt in ENGLISH, suitable for the Nano Banana Pro (gemini-3.0) image model.

CORE RULES:
1. AUTHENTICITY: Every image must look like a REAL photo taken by a real person with a smartphone. Absolutely NO cartoon, NO illustration, NO 3D render, NO hyper-stylized AI look.
2. REALISM: Real lighting (natural window light, overhead kitchen light, etc.), real textures, real objects. Grounded everyday settings only.
3. NO TEXT IN IMAGE: No text, no watermarks, no logos, no captions, no UI overlays in the generated image.
4. CONSISTENCY (CRITICAL — maximize this): Keep the SAME subjects across ALL frames of one post: the same person (matching the persona portrait — age, ethnicity, hair, build, clothing style), the same pet if any (same species, breed, color, markings, size — e.g. always describe it identically like "a scruffy tan medium-sized rescue mutt", NEVER switch breed/color between frames), and the same environment (same room layout, wall color, furniture, lighting direction, time of day). Treat all frames of one post as photos taken in the same home on the same day.
5. STYLE: Bright, clean, eye-catching but natural. American social media aesthetic.
6. PEOPLE & PETS FRAMING (CRITICAL): NEVER show a person's OR animal's face directly/frontally or in a close-up portrait. Apply the SAME framing rule to pets (dogs, cats, etc.) as to people: show them ONLY from the side (profile), from the back, or as a distant wide shot where the face/eyes are not the focus. Prefer shots focused on hands, objects, food, the environment, over-the-shoulder angles, or wide environmental shots with the subject small in frame. This avoids the uncanny "AI portrait / hero shot" look for both humans and animals, and keeps photos feeling candid and real.
   HARD SUB-RULES (image models weight specific words heavily, so these are mandatory):
   a. Every image_prompt MUST begin with a WIDE framing phrase: "Wide environmental shot of...", "Distant wide shot of...", or "Wide shot from behind of...". Do NOT lead with "side profile" or "over-the-shoulder" — they still tend to render faces. The person/pet should be SMALL in the frame, with the environment (street, room, objects) as the main subject.
   b. FORBIDDEN words/phrases (they trigger faces or a handheld/selfie look in the image model): "selfie", "selfie-style", "arm's length", "arm length", "handheld", "holding phone", "looking at camera", "looking off-camera", "looking away", "facing the camera", "making eye contact", "close-up", "portrait", "headshot", "gazing", "smile", "smiling", "tired eyes", "eyes", "expression", "face", "faint smile". If the source image_description mentions ANY of these, you MUST reframe entirely to a wide environmental shot from behind.
   c. When a person or pet appears, describe ONLY their back, full silhouette, or a small distant figure. Add "back to camera, face completely hidden" or "small figure in the distance, no face visible". NEVER mention eyes, smile, or expression — even "off-camera" or "turned away" versions of these are banned.
   d. Prefer compositions where people/pets are INCIDENTAL: the environment (street, room, food, objects, weather) is the main subject, and any person/animal is a small background element. Think "photo of a street that happens to have a person walking away in it", not "photo of a person".

REALISM RULES (CRITICAL):
- Objects must obey physics and look real (paint drips, dust settles, items have weight).
- No floating objects, no impossible geometry, no extra fingers.
- Settings must look like real homes, real offices, real streets — NOT studio sets.

OUTPUT FORMAT — return a JSON ARRAY with exactly one entry per input frame, preserving frame_id:
[
  {
    "frame_id": 1,
    "image_prompt": "<English image generation prompt, detailed, grounded, realistic>"
  },
  {
    "frame_id": 2,
    "image_prompt": "..."
  }
]

Return ONLY the JSON array. No markdown, no explanation.
"""


# ── image_prompt 简化 Prompt(去 AI 味,让图片更像真人随手拍)─────────
#
# kimi 生成的 image_prompt 往往"太满太完美"(每个元素都画进去、光线构图讲究),
# 这是 AI 图最容易被一眼识破的特征。这段 prompt 让 minimax 做"减法 + 加瑕疵":
# 只保留 1-2 个核心元素,删掉其余,并注入真实手机照片的瑕疵(噪点/虚焦/杂乱)。

_SIMPLIFY_IMAGE_PROMPT = """You are an expert at making AI image prompts produce photos that look like REAL casual snapshots, NOT AI-generated images.

You will receive JSON containing image prompts (one per frame). AI image prompts tend to be too detailed, too perfect, too "filled" — which is the #1 giveaway that an image is AI-generated. Your job: SIMPLIFY each prompt and ADD IMPERFECTIONS so the result looks like a quick candid phone snap.

HARD RULES:
1. SUBTRACTION (most important): Cut each prompt down to 1-2 core subjects maximum. Remove extra objects, decorative elements, "also visible in the background" clutter. A real snap has ONE main thing; AI images cram everything in. If the prompt lists 4+ distinct objects/elements, delete all but the 1-2 most essential.
2. ADD IMPERFECTIONS: Append 1-2 of these flaws to EVERY prompt (vary which ones): "slightly blurry", "mild phone camera grain", "uneven/exposed lighting", "cluttered messy background", "slightly off-center composition", "soft focus on edges", "casual imperfect framing". Never let a prompt sound "clean".
3. KILL PERFECTION WORDS: Remove or never add: "perfect", "beautiful", "stunning", "gorgeous", "well-lit", "perfectly composed", "professional", "crisp", "pristine", "flawless", "vibrant", "polished".
4. CASUAL TONE: Frame as a quick candid snap someone grabbed fast — "quick snap of...", "grabbed a shot of...", "casual phone photo". NOT a planned/staged photo.
5. KEEP FRAMING: Preserve any "wide shot / from behind / distant / face not visible" framing — do NOT change the camera angle or reintroduce faces.
6. KEEP IT SHORT: The simplified prompt should be noticeably shorter and sparser than the input. Less detail = more real.
7. PRESERVE PERSON IDENTITY (CRITICAL): When simplifying, NEVER remove or change a person's gender, hair length/style, or clothing. These MUST stay IDENTICAL across all frames — if frame 1 has "a young woman with long dark hair in a cozy sweater", every frame with a person keeps those exact features. Removing/altering them causes the #1 AI giveaway: the person looking different in every photo.

OUTPUT — return ONLY a JSON array, one object per input frame, preserving frame_id:
[
  {"frame_id": 1, "image_prompt": "<simplified, sparse, imperfect version>"},
  {"frame_id": 2, "image_prompt": "..."}
]

Return ONLY the JSON. No markdown, no explanation.
"""


# ── 改稿 Prompt(真人化,降低 AI 味)──────────────────────────────
#
# kimi 生成的图文草稿结构完整但"AI 味"重(太工整、太有用、太营销)。
# 这段 prompt 让 minimax 把 caption 改写成真人发帖口吻。
# 配合 _SCRIPT_SYSTEM_PROMPT_TEXT 里的"不完美"规则,三管齐下去 AI 味。

_HUMANIZE_SYSTEM_PROMPT = """You are an expert at making AI-generated social media content sound like a REAL person wrote it.

You will receive JSON containing captions for an image/text post. These were written by AI and sound too polished, too useful, too "marketing-like". Rewrite each caption to sound like a real user posting casually on the platform.

HARD RULES:
1. Talk like you're messaging a friend, NOT writing an article or ad. Use spoken, casual phrasing.
2. Strip ALL marketing / tutorial / sales tone. Forbidden phrases: "must-know", "pro tips", "game changer", "highly recommend", "you won't believe", "the ultimate", "top 5 ways". No listicle energy.
3. Use contractions and casual spelling where natural (gonna, kinda, tbh, ngl, bc, ive, wanna). Lowercase sentence starts are fine.
4. Inject real emotion and subjectivity — be excited, annoyed, uncertain, skeptical, hyped. Real people have feelings, not just information to deliver.
5. Vary length drastically between frames. Some captions can be 4 words, others 120 words. Do NOT make them uniform length or structure.
6. Imperfection is GOOD: fragments, run-ons, trailing thoughts, a slight tangent are all fine. Do NOT over-edit into clean prose.
7. Keep the core fact/info but reframe it as a personal observation or experience, not a tutorial.
8. At most 1-2 emojis per caption, or none. NEVER emoji-spam.
9. NO hashtags in the captions (those are handled separately).
10. Write in the LANGUAGE specified by the user.

OUTPUT — return ONLY a JSON array, one object per input frame, preserving frame_id:
[
  {"frame_id": 1, "caption": "..."},
  {"frame_id": 2, "caption": "..."}
]

Return ONLY the JSON. No markdown, no explanation.
"""


# ── 核心引擎 ──────────────────────────────────────────────────────


class ContentPlanner:
    """30天内容规划引擎"""

    def __init__(
        self,
        llm: LLMClient,
        feedback_mgr: Any = None,
        humanizer: Any = None,
    ) -> None:
        """
        Args:
            llm: LLM 客户端
            feedback_mgr: 可选，传入 PlanManager 实例以启用 few-shot 注入。
                当 video_feedback 表里 positive 样本 >=3 条时，下次生成 video_prompt
                会把同 track 的 top-K 历史成功 prompt 作为示例注入。
                传 None 时该机制自动禁用（向后兼容）。
            humanizer: 可选,真人化改稿客户端(HumanizerClient)。配置后,图文脚本
                生成后会自动调改稿模型把 caption 改写成真人口吻。传 None 或未配置
                时自动跳过(向后兼容)。
        """
        self.llm = llm
        self.feedback_mgr = feedback_mgr
        self.humanizer = humanizer

    @staticmethod
    def _is_text_platform(persona: Persona) -> bool:
        """判断是否为图文平台（reddit / Twitter / FB）—— 不生成视频，只生成图文帖子"""
        return (persona.platform or "").strip() in _TEXT_PLATFORMS

    @staticmethod
    def _is_ai_art_persona(persona: Persona) -> bool:
        """判断是否为 AI 创作人设(公开 AI 身份的 AI artist)

        AI 创作人设的内容是 openly AI-generated,不应走"去AI味/真人化"管线
        (simplify_image_prompts / humanize_captions),否则会破坏精致的 AI 艺术效果。
        判据:occupation / personal_tags / content_style 含 AI art 相关关键词。
        """
        text = " ".join([
            persona.occupation or "",
            persona.personal_tags or "",
            persona.content_style or "",
        ]).lower()
        return any(kw in text for kw in _AI_ART_KEYWORDS)

    @staticmethod
    def _parse_json_array(text: str) -> list[dict[str, Any]]:
        """从 LLM 回复中解析 JSON 数组（返回 dict 列表，不像 parse_json_list 会转成 str）"""
        import json
        import re

        text = text.strip()
        # 去掉 markdown 代码块
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

        # 直接解析
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return [item for item in result if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass

        # 提取 [...] 子串
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                result = json.loads(text[start:end])
                if isinstance(result, list):
                    return [item for item in result if isinstance(item, dict)]
            except json.JSONDecodeError:
                pass

        # 尝试修复截断
        repaired = LLMClient._repair_truncated_json(text)
        if repaired != text:
            try:
                result = json.loads(repaired)
                if isinstance(result, list):
                    return [item for item in result if isinstance(item, dict)]
            except json.JSONDecodeError:
                pass

        logger.warning("无法解析 JSON 数组(已跳过,不影响整体流程): %s", text[:200])
        return []

    def generate_calendar(
        self,
        persona: Persona,
        content_tracks: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """生成30天内容日历（按周分批，避免单次 LLM 调用超时）

        Args:
            persona: 完整人设
            content_tracks: 自定义内容板块，key 为 A/B/C/D，value 为板块描述

        Returns:
            包含 calendar 和 summary 的字典
        """
        if content_tracks is None:
            content_tracks = {
                "A": "Core professional expertise, tips, and industry insights",
                "B": "Practical how-to guides, transformations, before/after",
                "C": "Brand and product spotlight, reviews, recommendations",
                "D": "Daily vlog, behind the scenes, client work",
            }

        tracks_text = "\n".join(
            f"  Track {k}: {v}" for k, v in content_tracks.items()
        )

        all_entries: list[dict[str, Any]] = []
        used_titles: list[str] = []

        # 按 7 天为一组: Week1-4 各7天, Week5 = 2天
        batches = [(1, 7), (8, 14), (15, 21), (22, 28), (29, 30)]

        for start_day, end_day in batches:
            week_num = (start_day - 1) // 7 + 1
            num_days = end_day - start_day + 1

            used_text = ""
            if used_titles:
                used_text = (
                    f"\n\nTOPICS ALREADY USED (do NOT repeat):\n"
                    + "; ".join(used_titles[:20])
                )

            is_final = start_day >= 29
            finale_note = ""
            if is_final:
                finale_note = (
                    "\n\nIMPORTANT: Day 29-30 is the GRAND FINALE. "
                    "Day 29 should be a viral recap/best-of compilation. "
                    "Day 30 should be a 'what's next / month transformation' video."
                )

            user_message = (
                f"PERSONA:\n{persona.to_prompt_text()}\n\n"
                f"CONTENT TRACKS:\n{tracks_text}\n\n"
                f"Generate content for Day {start_day} to Day {end_day} ({num_days} videos)."
                f" This is Week {week_num} of a 30-day plan.{used_text}{finale_note}"
            )

            try:
                calendar_prompt = (
                    _CALENDAR_SYSTEM_PROMPT_TEXT
                    if self._is_text_platform(persona)
                    else _CALENDAR_SYSTEM_PROMPT
                )
                raw = self.llm.chat(calendar_prompt, user_message)
                week_entries = self._parse_json_array(raw)

                # parse_json_list 可能返回 dict 列表，也可能包装在对象里
                if not week_entries:
                    obj = self.llm.parse_json_object(raw)
                    if isinstance(obj, dict):
                        # 可能是 {"calendar": [...]}
                        for v in obj.values():
                            if isinstance(v, list):
                                week_entries = v
                                break

                if not week_entries:
                    logger.error("Week %d 日历解析失败: %s", week_num, raw[:200])
                    continue

                # 确保 day 和 week 字段正确
                for entry in week_entries:
                    if isinstance(entry, dict):
                        if "day" not in entry:
                            entry["day"] = start_day + len(all_entries)
                        if "week" not in entry:
                            entry["week"] = week_num
                        all_entries.append(entry)
                        title = entry.get("title_en", "")
                        if title:
                            used_titles.append(title)

                logger.info(
                    "Week %d 日历完成: %d 条 (Day %d-%d)",
                    week_num, len(week_entries), start_day, end_day,
                )
            except Exception as exc:
                logger.error("Week %d 日历生成失败: %s", week_num, exc)

        # 统计汇总
        track_counts: dict[str, int] = {}
        viral_count = 0
        for e in all_entries:
            t = e.get("track", "?")
            track_counts[t] = track_counts.get(t, 0) + 1
            if e.get("role") == "viral":
                viral_count += 1

        summary = {
            "total_videos": len(all_entries),
            "track_a": track_counts.get("A", 0),
            "track_b": track_counts.get("B", 0),
            "track_c": track_counts.get("C", 0),
            "track_d": track_counts.get("D", 0),
            "viral_videos": viral_count,
            "top_topics": used_titles[:10],
        }

        logger.info("30天日历生成完成: %d 条视频", len(all_entries))
        return {"calendar": all_entries, "summary": summary}

    def generate_single_calendar_entry(
        self,
        persona: Persona,
        day: int,
        existing_calendar: list[dict[str, Any]],
        content_tracks: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """为特定的一天重新生成单条日历记录，避免与现有内容重复"""
        if content_tracks is None:
            content_tracks = {
                "A": "Core professional expertise, tips, and industry insights",
                "B": "Practical how-to guides, transformations, before/after",
                "C": "Brand and product spotlight, reviews, recommendations",
                "D": "Daily vlog, behind the scenes, client work",
            }

        tracks_text = "\n".join(
            f"  Track {k}: {v}" for k, v in content_tracks.items()
        )

        used_titles = [e.get("title_en", "") for e in existing_calendar if e.get("title_en")]
        used_text = ""
        if used_titles:
            import random
            # 随机抽一些用过的标题避免太多超长
            sampled_titles = random.sample(used_titles, min(len(used_titles), 20))
            used_text = (
                f"\n\nTOPICS ALREADY USED (do NOT repeat):\n"
                + "; ".join(sampled_titles)
            )

        week_num = (day - 1) // 7 + 1
        
        user_message = (
            f"PERSONA:\n{persona.to_prompt_text()}\n\n"
            f"CONTENT TRACKS:\n{tracks_text}\n\n"
            f"Generate ONLY ONE video content entry for Day {day}. "
            f"This is Week {week_num} of a 30-day plan. "
            f"Make sure it's completely new and fresh.{used_text}\n\n"
            "Return a JSON ARRAY containing EXACTLY ONE object."
        )

        try:
            calendar_prompt = (
                _CALENDAR_SYSTEM_PROMPT_TEXT
                if self._is_text_platform(persona)
                else _CALENDAR_SYSTEM_PROMPT
            )
            raw = self.llm.chat(calendar_prompt, user_message)
            entries = self._parse_json_array(raw)
            if entries and len(entries) > 0:
                entry = entries[0]
                entry["day"] = day
                entry["week"] = week_num
                return entry
        except Exception as exc:
            logger.error("单条日历生成失败 (Day %d): %s", day, exc)
            
        return None

    def generate_script(
        self,
        persona: Persona,
        calendar_entry: dict[str, Any],
    ) -> dict[str, Any]:
        """为日历中的单条生成详细脚本

        Args:
            persona: 完整人设
            calendar_entry: 日历中的一条（来自 generate_calendar 的输出）

        Returns:
            完整的脚本字典
        """
        user_message = (
            f"PERSONA:\n{persona.to_prompt_text()}\n\n"
            f"VIDEO INFO:\n"
            f"Day: {calendar_entry.get('day', '?')}\n"
            f"Track: {calendar_entry.get('track', '')} - {calendar_entry.get('track_name', '')}\n"
            f"Role: {calendar_entry.get('role', '')}\n"
            f"Title: {calendar_entry.get('title_en', '')}\n"
            f"Hook: {calendar_entry.get('hook', '')}\n"
            f"Topic: {calendar_entry.get('topic_summary', '')}\n"
            f"Pain point: {calendar_entry.get('pain_point', '')}\n\n"
            "Generate the complete production-ready script."
        )

        try:
            is_text = self._is_text_platform(persona)
            script_prompt = (
                _SCRIPT_SYSTEM_PROMPT_TEXT if is_text else _SCRIPT_SYSTEM_PROMPT
            )
            raw = self.llm.chat(script_prompt, user_message)
            script = self.llm.parse_json_object(raw)
            if script:
                # 保留日历中的元数据
                script.setdefault("day", calendar_entry.get("day"))
                script.setdefault("track", calendar_entry.get("track"))
                script.setdefault("track_name", calendar_entry.get("track_name"))
                script.setdefault("role", calendar_entry.get("role"))

                if is_text:
                    # 图文平台：标记内容类型，预生成每个 frame 的配图 prompt
                    # （只生成 prompt 文本，不调用图片 API —— 真实配图由 web 端手动触发）
                    script.setdefault("content_type", "image_text")
                    self.generate_image_prompts(persona, script)
                    # AI 创作人设(公开AI身份): 保留精致AI风格, 不去AI味
                    # 真人伪装人设: simplify(减法+瑕疵) + humanize(真人化) 降低AI味
                    if not self._is_ai_art_persona(persona):
                        self.simplify_image_prompts(persona, script)
                        self.humanize_captions(persona, script)
                else:
                    # 视频平台：提前生成视频提示词，以便前端展示
                    video_prompt = self.generate_video_prompt(persona, script)
                    if video_prompt:
                        script["video_prompt"] = video_prompt

                logger.info("脚本 Day %d 生成完成", script.get("day", 0))
                return script
        except Exception as exc:
            logger.error("脚本生成失败 (Day %s): %s", calendar_entry.get("day"), exc)

        return {}

    def humanize_captions(self, persona: Persona, script: dict[str, Any]) -> None:
        """用改稿模型(minimax)把 caption 改写成真人口吻,降低 AI 味

        就地修改 script:对 script["frames"] 的每个 caption 做真人化改写。
        前提:humanizer 已配置,且 script 是图文(content_type=image_text)。
        未配置 / 失败时静默跳过(保留 kimi 原始 caption,不影响主流程)。
        """
        if self.humanizer is None or not self.humanizer.is_configured():
            logger.info("改稿未配置,跳过真人化 (Day %s)", script.get("day"))
            return

        frames = script.get("frames") or []
        if not frames:
            return

        user_message = (
            f"PLATFORM: {persona.platform}\n"
            f"LANGUAGE: {persona.language or 'English'}\n"
            f"PERSONA CONTEXT: {persona.occupation}; {persona.personality}\n\n"
            f"CAPTIONS TO REWRITE (JSON):\n"
            f"{json.dumps([{'frame_id': f.get('frame_id', i + 1), 'caption': f.get('caption', '')} for i, f in enumerate(frames)], ensure_ascii=False, indent=2)}"
        )

        try:
            raw = self.humanizer.chat(_HUMANIZE_SYSTEM_PROMPT, user_message)
            items = self._parse_json_array(raw)
            if not items:
                logger.warning("改稿解析失败 (Day %s): %s", script.get("day"), raw[:200])
                return

            cap_map = {
                it.get("frame_id"): it.get("caption", "")
                for it in items
                if isinstance(it, dict)
            }
            changed = 0
            for i, f in enumerate(frames):
                fid = f.get("frame_id", i + 1)
                new_cap = cap_map.get(fid, "")
                if new_cap:
                    # 保留原始 caption 作为 caption_original(审计/对比用)
                    f.setdefault("caption_original", f.get("caption", ""))
                    f["caption"] = new_cap
                    changed += 1

            logger.info(
                "Day %s 改稿完成: %d/%d caption 已真人化",
                script.get("day"), changed, len(frames),
            )
        except Exception as exc:
            # 改稿失败不能阻塞主流程:保留 kimi 原始 caption
            logger.warning("改稿失败 (Day %s),保留原始 caption: %s", script.get("day"), exc)

    def generate_image_prompts(
        self,
        persona: Persona,
        script: dict[str, Any],
    ) -> None:
        """为图文脚本的每个 frame 预生成配图 prompt（不调用图片 API）

        就地修改 script：为 script["frames"] 中每个 frame 填入 image_prompt 字段。
        真实配图生成由 web 端手动触发（见 planner_pipeline.generate_images_for_day）。

        失败时只 log warning，不影响主流程（frame 仍保留 caption + image_description）。
        """
        frames = script.get("frames") or []
        if not frames:
            logger.warning(
                "generate_image_prompts: 无 frames，跳过 (Day %s)", script.get("day")
            )
            return

        # 构造输入：只取 frame_id + image_description
        frames_input = [
            {
                "frame_id": f.get("frame_id", idx + 1),
                "image_description": f.get("image_description", ""),
            }
            for idx, f in enumerate(frames)
        ]

        portrait = persona.portrait_description or "infer from persona"
        user_message = (
            f"PERSONA PORTRAIT (MANDATORY — any person appearing in ANY frame MUST match this EXACTLY):\n{portrait}\n\n"
            f"PERSON CONSISTENCY RULE (CRITICAL — violating this is the #1 AI giveaway):\n"
            f"- If a frame includes a person (even just their back, hand, or silhouette), you MUST describe them using the persona's FIXED features: gender, hair length/style/color, clothing.\n"
            f"- Use the EXACT SAME wording in every frame. If frame 1 says 'a young woman with long dark hair in a cozy sweater', frame 5 MUST say the same — NEVER 'a man with short hair'.\n"
            f"- NEVER write generic terms like 'a person' or 'someone' — ALWAYS specify gender + hair + clothing matching the persona.\n"
            f"- Varying hair length, gender, or clothing between frames is STRICTLY FORBIDDEN.\n\n"
            f"PLATFORM: {persona.platform}\n\n"
            f"FRAMES (convert each image_description into an English image_prompt):\n"
            f"{json.dumps(frames_input, ensure_ascii=False, indent=2)}"
        )

        try:
            raw = self.llm.chat(_IMAGE_PROMPT_SYSTEM, user_message)
            prompts = self._parse_json_array(raw)
            if not prompts:
                logger.warning(
                    "image_prompt 解析失败 (Day %s): %s",
                    script.get("day"), raw[:200],
                )
                return

            # 建立 frame_id -> image_prompt 映射
            prompt_map = {
                p.get("frame_id"): p.get("image_prompt", "")
                for p in prompts
                if isinstance(p, dict)
            }

            filled = 0
            for idx, f in enumerate(frames):
                fid = f.get("frame_id", idx + 1)
                prompt = prompt_map.get(fid, "")
                if prompt:
                    f["image_prompt"] = prompt
                    # 初始化配图生成状态（供 web 端手动生成时追踪）
                    f.setdefault("image_status", "pending")
                    filled += 1

            logger.info(
                "Day %s image_prompt 生成完成: %d/%d frame 已填充",
                script.get("day"), filled, len(frames),
            )
        except Exception as exc:
            # 失败不能阻塞主流程：frames 仍有 caption + image_description 可用
            logger.warning("image_prompt 生成失败 (Day %s): %s", script.get("day"), exc)

    def simplify_image_prompts(self, persona: Persona, script: dict[str, Any]) -> None:
        """用改稿模型(minimax)简化 image_prompt,降低 AI 味

        kimi 生成的 image_prompt 往往太满太完美 → 图片一眼假。
        本方法做"减法 + 加瑕疵":只保留 1-2 个核心元素,注入真实手机照片的瑕疵。
        就地修改 script["frames"][i]["image_prompt"]。
        未配置 humanizer / 失败时静默跳过(保留 kimi 原始 prompt)。
        """
        if self.humanizer is None or not self.humanizer.is_configured():
            return

        frames = script.get("frames") or []
        items = [
            {"frame_id": f.get("frame_id", i + 1), "image_prompt": f.get("image_prompt", "")}
            for i, f in enumerate(frames)
            if f.get("image_prompt")
        ]
        if not items:
            return

        user_message = (
            f"PLATFORM: {persona.platform}\n\n"
            f"IMAGE PROMPTS TO SIMPLIFY (JSON):\n"
            f"{json.dumps(items, ensure_ascii=False, indent=2)}"
        )

        try:
            raw = self.humanizer.chat(_SIMPLIFY_IMAGE_PROMPT, user_message)
            simplified = self._parse_json_array(raw)
            if not simplified:
                logger.warning("image_prompt 简化解析失败 (Day %s): %s", script.get("day"), raw[:200])
                return

            prompt_map = {
                it.get("frame_id"): it.get("image_prompt", "")
                for it in simplified
                if isinstance(it, dict)
            }
            changed = 0
            for i, f in enumerate(frames):
                fid = f.get("frame_id", i + 1)
                new_prompt = prompt_map.get(fid, "")
                if new_prompt and new_prompt != f.get("image_prompt"):
                    # 保留原始作 image_prompt_original(审计)
                    f.setdefault("image_prompt_original", f.get("image_prompt", ""))
                    f["image_prompt"] = new_prompt
                    changed += 1

            logger.info(
                "Day %s image_prompt 简化完成: %d/%d 已去AI味",
                script.get("day"), changed, len(items),
            )
        except Exception as exc:
            logger.warning("image_prompt 简化失败 (Day %s),保留原始: %s", script.get("day"), exc)

    def generate_video_prompt(
        self,
        persona: Persona,
        script: dict[str, Any],
    ) -> str:
        """将脚本转换为视频生成 prompt（用于 seedance-2.0-fast）

        如果 feedback_mgr 已注入且 video_feedback 表里有 >=3 条同 track 的 positive
        样本，会把最近的几条作为 few-shot 示例注入，让生成结果向"用户认可的风格"靠拢。
        样本不足时该机制自动跳过，不影响原有流程。
        """
        char_desc = persona.portrait_description or "infer from persona"

        shots_text = ""
        for shot in script.get("shots", []):
            shots_text += (
                f"[{shot.get('duration', '3s')}] "
                f"{shot.get('visual', '')} "
                f"Dialogue: \"{shot.get('dialogue', '')}\". "
            )

        # ── few-shot 注入：从用户历史"认可"的样本里拉同 track 的成功 prompt ──
        # 这是"自动优化"闭环的关键 —— 用户点过 schedule 的视频 prompt 会成为
        # 下次生成的参考。样本 <3 条时 get_positive_examples 返回空，跳过注入。
        examples_block = ""
        track = script.get("track")
        if self.feedback_mgr is not None and track:
            try:
                examples = self.feedback_mgr.get_positive_examples(track=track, k=3)
                if examples:
                    lines = []
                    for i, ex in enumerate(examples, 1):
                        lines.append(
                            f"EXAMPLE {i} (a previously SCHEDULED/PUBLISHED video "
                            f"that the user approved — title: {ex.get('title_cn', '?')}):\n"
                            f"{ex.get('video_prompt', '')}"
                        )
                    examples_block = (
                        "\n\n--- REFERENCE EXAMPLES (learn the style/structure/level of "
                        f"detail from these user-approved prompts, but do NOT copy content) ---\n"
                        + "\n\n".join(lines)
                        + "\n--- END REFERENCE EXAMPLES ---"
                    )
                    logger.info(
                        "注入 %d 条 positive few-shot 样本 (track=%s)",
                        len(examples), track,
                    )
            except Exception as exc:
                # few-shot 失败不能影响主流程
                logger.warning("few-shot 注入失败（已跳过）: %s", exc)

        user_message = (
            f"CHARACTER: {char_desc}\n\n"
            f"VIDEO TITLE: {script.get('title_en', '')}\n"
            f"SETTING: {script.get('core_topic', '')}\n\n"
            f"SHOT BREAKDOWN:\n{shots_text}\n"
            f"{examples_block}\n\n"
            f"Generate the video prompt for seedance-2.0-fast."
        )

        try:
            prompt = self.llm.chat(_VIDEO_PROMPT_SYSTEM, user_message).strip()
            return prompt
        except Exception as exc:
            logger.error("视频 prompt 生成失败: %s", exc)
            return ""

    def generate_summary(
        self,
        calendar: list[dict[str, Any]],
        scripts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """汇总30天规划的摘要信息"""
        # 统计高频品牌
        brand_count: dict[str, int] = {}
        for s in scripts:
            for rec in s.get("recommendations", []):
                # 提取品牌名（括号前的部分）
                brand = rec.split("(")[0].split("-")[0].strip()
                if brand:
                    brand_count[brand] = brand_count.get(brand, 0) + 1

        top_brands = sorted(brand_count.items(), key=lambda x: x[1], reverse=True)[:15]

        # 选出爆款 TOP10（按 estimated_viral_score 排序）
        viral_entries = sorted(
            calendar,
            key=lambda x: x.get("estimated_viral_score", 0),
            reverse=True,
        )
        top10 = [
            {
                "day": e.get("day"),
                "title_en": e.get("title_en"),
                "title_cn": e.get("title_cn"),
                "score": e.get("estimated_viral_score"),
            }
            for e in viral_entries[:10]
        ]

        return {
            "top10_viral": top10,
            "frequent_brands": [{"brand": b, "count": c} for b, c in top_brands],
            "total_scripts": len(scripts),
            "total_videos": len(calendar),
        }
