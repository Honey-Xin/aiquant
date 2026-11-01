"""LLM 客户端（标签域专用）。

规则：
  * 只返回模型输出的原始文本，解析与校验在 ``schema`` 层做。
  * 必须有超时；任何失败抛 ``LLMError``，由上层按 fail-open 兜底。
  * 不重试、不写日志中的请求体（prompt 可能包含行情，但绝不含凭证）。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def complete_json(self, system_prompt: str, user_prompt: str) -> str: ...


class HttpLLMClient:
    """OpenAI / DeepSeek 兼容 chat/completions；Anthropic 走 messages。"""

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        timeout_sec: float = 30.0,
        max_tokens: int = 900,
        temperature: float = 0.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._transport = transport

    # ---------------------------------------------------------------- 公开
    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise LLMError("LLM_API_KEY 未配置")
        if self.provider == "anthropic":
            return self._anthropic(system_prompt, user_prompt)
        return self._openai_compatible(system_prompt, user_prompt)

    # ---------------------------------------------------------------- 实现
    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout_sec,
            transport=self._transport,
            headers={"content-type": "application/json"},
        )

    def _openai_compatible(self, system_prompt: str, user_prompt: str) -> str:
        url = f"{self.base_url}/v1/chat/completions"
        if self.base_url.endswith("/v1"):
            url = f"{self.base_url}/chat/completions"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {"authorization": f"Bearer {self.api_key}"}
        try:
            with self._client() as client:
                resp = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"LLM 返回 {resp.status_code}")
        try:
            data = resp.json()
            return self._extract_openai_text(data)
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"LLM 响应结构异常: {exc}") from exc

    def _anthropic(self, system_prompt: str, user_prompt: str) -> str:
        url = f"{self.base_url}/v1/messages"
        body = {
            "model": self.model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        try:
            with self._client() as client:
                resp = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 请求失败: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"LLM 返回 {resp.status_code}")
        try:
            data = resp.json()
            blocks = data["content"]
            return "".join(b["text"] for b in blocks if b.get("type") == "text")
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError(f"LLM 响应结构异常: {exc}") from exc

    @staticmethod
    def _extract_openai_text(data: dict[str, Any]) -> str:
        choices = data["choices"]
        message = choices[0]["message"]
        content = message.get("content")
        if isinstance(content, list):
            return "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            raise KeyError("message.content 缺失")
        return content


class StaticLLMClient:
    """测试/回放用：按顺序返回预置文本。"""

    def __init__(self, responses: list[str] | list[Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if not self._responses:
            raise LLMError("没有更多预置响应")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
