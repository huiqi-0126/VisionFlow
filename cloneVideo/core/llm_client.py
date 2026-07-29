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
        """底层 API 调用，带重试"""
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
            payload["thinking"] = {"type": "disabled"}
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
        """尝试修复被截断的 JSON"""
        text = text.strip()
        if not text:
            return text

        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass

        starts_with_bracket = text.lstrip().startswith("[")

        if starts_with_bracket:
            last_brace = text.rfind("}")
            if last_brace >= 0:
                repaired = text[:last_brace + 1] + "]"
                try:
                    json.loads(repaired)
                    return repaired
                except json.JSONDecodeError:
                    pass
        else:
            last_valid = text.rstrip()
            last_comma = last_valid.rfind(",")
            last_brace = last_valid.rfind("}")
            if last_comma > last_brace:
                repaired = last_valid[:last_brace + 1] + "}"
                try:
                    json.loads(repaired)
                    return repaired
                except json.JSONDecodeError:
                    pass
            repaired = last_valid + "}"
            try:
                json.loads(repaired)
                return repaired
            except json.JSONDecodeError:
                pass

        return text

    def parse_json_list(self, text: str) -> list[Any]:
        """从 LLM 回复中提取 JSON 数组"""
        text = text.strip()
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                result = json.loads(text[start:end])
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass

        repaired = self._repair_truncated_json(text)
        if repaired != text:
            try:
                result = json.loads(repaired)
                if isinstance(result, list):
                    logger.info("截断 JSON 修复成功")
                    return result
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
