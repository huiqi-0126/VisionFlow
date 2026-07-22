"""LLM 客户端 - 调用智谱 GLM / Qwen (OpenAI 兼容) 接口

支持:
  - 文本对话 (chat)
  - 多模态分析 (chat_with_images) - 发送图片+文本给多模态 LLM
  - JSON 解析辅助
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)


class LLMClient:
    """LLM 客户端 (OpenAI 兼容格式，支持智谱 GLM / Qwen)"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "glm-5v-turbo",
        max_retries: int = 3,
        retry_base_delay: float = 2.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay

    def chat(self, system_prompt: str, user_message: str) -> str:
        """调用 ChatCompletion 接口，纯文本对话"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        return self._call(messages)

    def chat_with_images(
        self,
        system_prompt: str,
        user_message: str,
        image_paths: list[str | Path],
    ) -> str:
        """多模态对话：发送图片 + 文本给多模态 LLM (GLM-4V / Qwen-VL)

        图片以 base64 内联方式发送，兼容智谱 GLM 和 Qwen 的 OpenAI 兼容接口。
        """
        content_parts: list[dict[str, Any]] = []

        # 添加文本部分
        if user_message:
            content_parts.append({"type": "text", "text": user_message})

        # 添加图片部分
        for img_path in image_paths:
            img_path = Path(img_path)
            if not img_path.exists():
                logger.warning("图片不存在，跳过: %s", img_path)
                continue
            suffix = img_path.suffix.lower()
            mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
            mime_type = mime_map.get(suffix, "image/jpeg")
            b64 = base64.b64encode(img_path.read_bytes()).decode("utf-8")
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64}"},
            })

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_parts},
        ]
        return self._call(messages)

    # ── 内部调用 ──────────────────────────────────────────────

    def _call(self, messages: list[dict[str, Any]]) -> str:
        """底层 API 调用，带重试

        关键配置（对齐 iphoneRPA 已验证可用的调用方式）：
          - kimi-k2.x 是思考模型，默认开启深度思考，响应极慢 → 几乎必然超时。
            通过 thinking={"type":"disabled"} 关闭思考模式（等价于 OpenAI SDK 的
            extra_body={"thinking":...}，requests 方式直接放进请求体顶层即可）。
          - kimi 不传 max_tokens（等价于 SDK 的 NOT_GIVEN），避免思考模式下被
            额度截断；其他模型仍保留 8192。
          - timeout 从 120s 提到 300s（与 iphoneRPA LLM_CALL_TIMEOUT 一致）。
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        is_kimi = "kimi-" in self.model.lower()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if is_kimi:
            # 关闭 kimi 思考模式：响应速度大幅提升，避免超时。
            # 注意：thinking=disabled 模式下 API 强制限制 temperature 只能为 0.6，
            # 传其他值(1.0/0.7 等)会返回 400 "invalid temperature: only 0.6 is allowed
            # for this model"。不传 temperature，让服务端用默认。
            payload["thinking"] = {"type": "disabled"}
            # 显式传大 max_tokens：默认值会截断长输出(图文脚本 8-10 frame 需要长 JSON)，
            # 实测 max_tokens=16384 在 thinking=disabled 模式下可用(200 + finish=stop)。
            payload["max_tokens"] = 16384
        else:
            payload["temperature"] = 0.7
            payload["max_tokens"] = 8192

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=300)
                resp.raise_for_status()
                data = resp.json()
                message = data["choices"][0]["message"]
                content = message.get("content", "")
                if not content.strip():
                    content = message.get("reasoning_content", "")
                # 清理 markdown 代码块包裹
                content = re.sub(r"^```(?:json)?\s*\n?", "", content.strip())
                content = re.sub(r"\n?```\s*$", "", content.strip())
                return content.strip()
            except Exception as exc:
                last_error = exc
                delay = self.retry_base_delay * (2 ** (attempt - 1))
                logger.warning("LLM 请求失败 (第 %d 次): %s，%.1fs 后重试", attempt, exc, delay)
                time.sleep(delay)

        raise RuntimeError(f"LLM 请求连续 {self.max_retries} 次失败: {last_error}")

    # ── JSON 解析辅助 ──────────────────────────────────────────

    @staticmethod
    def _repair_truncated_json(text: str) -> str:
        """尝试修复被截断的 JSON（LLM 输出因 max_tokens 被切断的情况）

        策略：
        1. 找到最后一个完整的 `}` （即一个完整的对象）
        2. 用 `]` 闭合数组（如果是 JSON 数组）
        3. 用 `}` 闭合对象（如果是 JSON 对象）
        """
        text = text.strip()
        if not text:
            return text

        # 去掉 markdown 代码块包裹
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

        # 先尝试直接解析（可能没被截断）
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass

        # 判断是数组还是对象
        starts_with_bracket = text.lstrip().startswith("[")

        if starts_with_bracket:
            # JSON 数组被截断：找最后一个完整的 `}`，然后补 `]`
            last_brace = text.rfind("}")
            if last_brace >= 0:
                repaired = text[:last_brace + 1] + "]"
                try:
                    json.loads(repaired)
                    return repaired
                except json.JSONDecodeError:
                    pass
        else:
            # JSON 对象被截断：尝试补 `}`
            last_valid = text.rstrip()
            # 去掉末尾的不完整内容（如截断在字符串值中间）
            # 找到最后一个 `"` 后的 `:` 或 `,` 或 `}`
            # 简单策略：去掉最后一个不完整的 key-value 对
            last_comma = last_valid.rfind(",")
            last_brace = last_valid.rfind("}")
            if last_comma > last_brace:
                # 截断在一个不完整的 key-value 对中，去掉它
                repaired = last_valid[:last_brace + 1] + "}"
                try:
                    json.loads(repaired)
                    return repaired
                except json.JSONDecodeError:
                    pass
            # 直接补 }
            repaired = last_valid + "}"
            try:
                json.loads(repaired)
                return repaired
            except json.JSONDecodeError:
                pass

        return text  # 修复失败，返回原文

    def parse_json_list(self, text: str) -> list[str]:
        """从 LLM 回复中提取 JSON 数组"""
        text = text.strip()
        # 先尝试直接解析
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return [str(item) for item in result]
        except json.JSONDecodeError:
            pass

        # 尝试提取 [...] 子串
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                result = json.loads(text[start:end])
                if isinstance(result, list):
                    return [str(item) for item in result]
            except json.JSONDecodeError:
                pass

        # 最后尝试修复截断的 JSON
        repaired = self._repair_truncated_json(text)
        if repaired != text:
            try:
                result = json.loads(repaired)
                if isinstance(result, list):
                    logger.info("截断 JSON 修复成功")
                    return [str(item) for item in result]
            except json.JSONDecodeError:
                pass

        logger.error("无法解析 LLM 返回的 JSON: %s", text[:200])
        return []

    def parse_json_object(self, text: str) -> dict[str, Any]:
        """从 LLM 回复中提取 JSON 对象"""
        text = text.strip()
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                result = json.loads(text[start:end])
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        # 最后尝试修复截断的 JSON
        repaired = self._repair_truncated_json(text)
        if repaired != text:
            try:
                result = json.loads(repaired)
                if isinstance(result, dict):
                    logger.info("截断 JSON 修复成功")
                    return result
            except json.JSONDecodeError:
                pass

        logger.error("无法解析 LLM 返回的 JSON 对象: %s", text[:200])
        return {}


class HumanizerClient:
    """真人化改稿客户端(anthropic messages 兼容格式,用于 minimax 等去 AI 味)

    与 LLMClient(OpenAI 兼容格式)不同,本客户端走 anthropic /v1/messages
    协议,认证用 x-api-key + anthropic-version。用途:把 kimi 生成的结构化
    图文草稿改写成真人发帖的口吻,降低 AI 味。

    未配置(api_key/base_url 为空)时 is_configured() 返回 False,调用方
    应跳过改稿、回退到原始草稿(不影响主流程)。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "MiniMax-M3",
        max_retries: int = 2,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries

    def is_configured(self) -> bool:
        """是否配置了改稿服务"""
        return bool(self.api_key and self.base_url)

    def chat(self, system_prompt: str, user_message: str) -> str:
        """调用 anthropic /v1/messages,返回纯文本

        anthropic 响应的 content 是 block 数组,合并所有 text block。
        (minimax 的 M3 可能返回 thinking block,会被跳过,只取 text。)
        """
        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=300)
                resp.raise_for_status()
                data = resp.json()
                # anthropic 格式:content 是 block 数组,合并所有 text block
                parts: list[str] = []
                for block in data.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                return "".join(parts).strip()
            except Exception as exc:
                last_error = exc
                delay = 2.0 * (2 ** (attempt - 1))
                logger.warning("改稿请求失败 (第 %d 次): %s，%.1fs 后重试", attempt, exc, delay)
                time.sleep(delay)

        raise RuntimeError(f"改稿请求连续 {self.max_retries} 次失败: {last_error}")
