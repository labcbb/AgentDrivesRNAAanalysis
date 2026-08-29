"""LLM client for sRNAgent — OpenAI-compatible + Anthropic Messages APIs."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ChatCompletion:
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    thinking: str = ""


@dataclass
class LLMConfig:
    api_key: str
    base_url: str = "https://api.minimaxi.com/v1"
    model: str = "MiniMax-M2.5-highspeed"
    protocol: str = "openai-completions"
    temperature: float = 0.3
    max_tokens: int = 4096
    top_p: float = 1.0

    @classmethod
    def from_env(cls) -> "LLMConfig":
        api_key = (
            os.environ.get("SRNAGENT_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("MINIMAX_API_KEY")
            or ""
        ).strip()
        if not api_key:
            raise ValueError(
                "Missing API key. Set SRNAGENT_API_KEY, OPENAI_API_KEY, or MINIMAX_API_KEY."
            )
        return cls(
            api_key=api_key,
            base_url=(
                os.environ.get("SRNAGENT_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or "https://api.minimaxi.com/v1"
            ).rstrip("/"),
            model=os.environ.get("SRNAGENT_MODEL") or "MiniMax-M2.5-highspeed",
            protocol=os.environ.get("SRNAGENT_PROTOCOL", "openai-completions"),
        )

    @classmethod
    def from_ui_payload(
        cls,
        account: Dict[str, Any],
        vendor: Optional[Dict[str, Any]] = None,
        agent: Optional[Dict[str, Any]] = None,
    ) -> "LLMConfig":
        vendor = vendor or {}
        agent = agent or {}
        auth_mode = str(account.get("authMode") or "api_key")
        api_key = str(account.get("apiKey") or "").strip()
        if auth_mode == "local":
            if not api_key:
                api_key = "local"
        elif not api_key:
            raise ValueError("API Key 未配置。请在 Config 页面填写 Key 并保存。")

        base_url = str(
            account.get("baseUrl") or vendor.get("defaultBaseUrl") or "https://api.minimaxi.com/v1"
        ).strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(
                f"API Base URL 无效（{base_url!r}）。请在 Config 页面填写完整地址，例如 https://api.minimaxi.com/v1"
            )

        return cls(
            api_key=api_key,
            base_url=base_url,
            model=str(account.get("model") or vendor.get("defaultModel") or "MiniMax-M2.5-highspeed"),
            protocol=str(account.get("apiProtocol") or vendor.get("apiProtocol") or "openai-completions"),
            temperature=float(agent.get("temperature") or 0.3),
            max_tokens=int(agent.get("maxTokens") or 4096),
            top_p=float(agent.get("topP") or 1.0),
        )


def _uses_bearer_auth(config: LLMConfig) -> bool:
    if config.protocol == "anthropic-messages":
        return False
    return "minimax" in config.base_url.lower() or "minimax" in config.model.lower()


def _anthropic_header_variants(config: LLMConfig) -> List[Dict[str, str]]:
    base: Dict[str, str] = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    is_minimax = "minimax" in config.base_url.lower() or "minimax" in config.model.lower()
    if is_minimax:
        # MiniMax CN 官方要求 X-Api-Key；其余格式作为 fallback
        return [
            {**base, "X-Api-Key": config.api_key},
            {**base, "x-api-key": config.api_key},
            {**base, "Authorization": f"Bearer {config.api_key}"},
        ]
    return [
        {**base, "x-api-key": config.api_key},
        {**base, "Authorization": f"Bearer {config.api_key}"},
    ]


def _anthropic_headers(config: LLMConfig) -> Dict[str, str]:
    return _anthropic_header_variants(config)[0]


def _anthropic_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    if base.endswith("/anthropic"):
        return f"{base}/v1/messages"
    return f"{base}/v1/messages"


def _openai_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _to_anthropic_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") or {}
        converted.append(
            {
                "name": fn.get("name"),
                "description": fn.get("description"),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def _should_enable_thinking(config: LLMConfig) -> bool:
    model = config.model.lower()
    return "m3" in model or "m2" in model or "minimax" in model


def _extract_embedded_thinking(text: str) -> tuple[str, str]:
    """Split ``<think>`` blocks from visible answer content."""
    import re

    raw = (text or "").strip()
    if not raw:
        return "", ""

    pattern = re.compile(
        r"<\s*(?:redacted_)?thinking\s*>(.*?)<\s*/\s*(?:redacted_)?thinking\s*>",
        re.IGNORECASE | re.DOTALL,
    )
    thinking_parts = [match.group(1).strip() for match in pattern.finditer(raw) if match.group(1).strip()]
    visible = pattern.sub("", raw).strip()
    return "\n\n".join(thinking_parts).strip(), visible


def _collect_reasoning_from_message(message: Dict[str, Any]) -> str:
    parts: List[str] = []
    reasoning = str(message.get("reasoning_content") or "").strip()
    if reasoning:
        parts.append(reasoning)

    details = message.get("reasoning_details")
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                parts.append(text)

    content = str(message.get("content") or "").strip()
    embedded, _visible = _extract_embedded_thinking(content)
    if embedded:
        parts.append(embedded)

    seen: set[str] = set()
    merged: List[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            merged.append(part)
    return "\n\n".join(merged).strip()


class ChatClient:
    def __init__(self, config: LLMConfig):
        self.config = config

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        enable_thinking: Optional[bool] = None,
    ) -> ChatCompletion:
        if self.config.protocol == "anthropic-messages":
            return self._complete_anthropic(messages, tools, enable_thinking=enable_thinking)
        return self._complete_openai(messages, tools, enable_thinking=enable_thinking)

    def _request(self, url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc

    def _request_with_auth_retry(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        last_error: Optional[RuntimeError] = None
        for headers in _anthropic_header_variants(self.config):
            try:
                return self._request(url, headers, payload)
            except RuntimeError as exc:
                message = str(exc)
                if "HTTP 401" in message or "HTTP 403" in message:
                    last_error = exc
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("LLM 认证失败，请检查 Config 中的 API Key")

    def _complete_openai(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        *,
        enable_thinking: Optional[bool] = None,
    ) -> ChatCompletion:
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "top_p": self.config.top_p,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if (enable_thinking if enable_thinking is not None else True) and _should_enable_thinking(self.config):
            payload["reasoning_split"] = True

        data = self._request(
            _openai_url(self.config.base_url),
            {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            payload,
        )
        message = (data.get("choices") or [{}])[0].get("message") or {}
        tool_calls: List[ToolCall] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(
                    id=str(call.get("id") or fn.get("name") or "tool"),
                    name=str(fn.get("name") or ""),
                    arguments=args if isinstance(args, dict) else {},
                )
            )
        thinking = _collect_reasoning_from_message(message)
        content_raw = str(message.get("content") or "").strip()
        _, visible_content = _extract_embedded_thinking(content_raw)
        if not visible_content:
            visible_content = content_raw
        return ChatCompletion(
            content=visible_content,
            tool_calls=tool_calls,
            thinking=thinking,
        )

    def _complete_anthropic(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        *,
        enable_thinking: Optional[bool] = None,
    ) -> ChatCompletion:
        system_parts: List[str] = []
        anthropic_messages: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role")
            if role == "system":
                system_parts.append(str(msg.get("content") or ""))
                continue
            if role == "tool":
                anthropic_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id"),
                                "content": str(msg.get("content") or ""),
                            }
                        ],
                    }
                )
                continue
            if role == "assistant" and msg.get("tool_calls"):
                content_blocks: List[Dict[str, Any]] = []
                if msg.get("content"):
                    content_blocks.append({"type": "text", "text": str(msg.get("content"))})
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        args = {}
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id"),
                            "name": fn.get("name"),
                            "input": args if isinstance(args, dict) else {},
                        }
                    )
                anthropic_messages.append({"role": "assistant", "content": content_blocks})
                continue
            if role in ("user", "assistant"):
                anthropic_messages.append({"role": role, "content": str(msg.get("content") or "")})

        payload: Dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "messages": anthropic_messages,
        }
        if system_parts:
            payload["system"] = "\n\n".join(part for part in system_parts if part)
        if tools:
            payload["tools"] = _to_anthropic_tools(tools)
        if (enable_thinking if enable_thinking is not None else True) and _should_enable_thinking(self.config):
            payload["thinking"] = {"type": "adaptive"}

        headers = _anthropic_headers(self.config)

        data = self._request_with_auth_retry(_anthropic_url(self.config.base_url), payload)

        text_parts: List[str] = []
        thinking_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "thinking":
                thinking_text = str(block.get("thinking") or block.get("text") or "").strip()
                if thinking_text:
                    thinking_parts.append(thinking_text)
            elif block_type == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=str(block.get("id") or block.get("name") or "tool"),
                        name=str(block.get("name") or ""),
                        arguments=block.get("input") if isinstance(block.get("input"), dict) else {},
                    )
                )
        visible = "\n".join(text_parts).strip()
        embedded_thinking, stripped_visible = _extract_embedded_thinking(visible)
        if stripped_visible:
            visible = stripped_visible
        thinking = "\n\n".join(part for part in [*thinking_parts, embedded_thinking] if part).strip()
        return ChatCompletion(content=visible, tool_calls=tool_calls, thinking=thinking)
