"""LLM client for sRNAgent — OpenAI-compatible + Anthropic Messages APIs."""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

# Transient gateway / rate-limit / overload responses worth retrying.
_TRANSIENT_HTTP_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_LLM_MAX_RETRIES = 3
_LLM_RETRY_BACKOFF_SEC = (1.0, 2.0, 4.0)
# Socket timeout for the request body phase.
_LLM_TIMEOUT_SEC = 120
# The first attempt is capped short: a healthy TLS handshake takes about a
# second, so a longer cap only delays the failover to an alternate host.
_LLM_FIRST_ATTEMPT_TIMEOUT = 12
# Ceiling for the whole attempt sequence. A dead route can hold a socket for
# a minute at a time, and retrying it several times would otherwise turn one
# failed request into a multi-minute stall.
_LLM_TOTAL_BUDGET_SEC = 45


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


def _safe_url(url: str) -> str:
    """Endpoint without credentials or query string, safe to show in errors."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _is_minimax_host(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in _MINIMAX_HOSTS


# MiniMax answers a request whose key it read but rejected with "invalid api
# key", and a request whose key it did not recognise at all with "Please carry
# the API secret key in the 'X-Api-Key' field".  The two need different
# answers, and the CN and Global platforms run separate accounts whose keys do
# not work on each other's host -- the usual cause of the first.
_MINIMAX_KEY_REJECTED_HINT = (
    "Key 已被 MiniMax 读到但被拒绝。国内版 (platform.minimaxi.com) 与国际版 "
    "(platform.minimax.io) 是两套独立账号，Key 不能互换：国内版 Key 请配 "
    "https://api.minimaxi.com/anthropic，国际版 Key 请配 "
    "https://api.minimax.io/anthropic；并确认 Key 未过期/未撤销。"
)
_MINIMAX_KEY_UNRECOGNISED_HINT = (
    "MiniMax 没认出这个 Key：可能格式不对、粘贴时被截断或带入了多余字符，"
    "也可能请求头没带上。请在 Config 页面重新完整粘贴 Key 后再测。"
)


def _minimax_auth_hint(detail: str) -> str:
    """Pick the hint that matches what MiniMax said about the key."""
    if "invalid api key" in detail.lower():
        return _MINIMAX_KEY_REJECTED_HINT
    return _MINIMAX_KEY_UNRECOGNISED_HINT


# MiniMax serves OpenAI-compatible calls at /v1 and Anthropic-compatible calls
# at /anthropic — the same host, two different path prefixes.  The list covers
# every host MiniMax serves the API on, because a host that is missing from it
# silently skips the path rewrite and lands on the plain-text "404 page not
# found" body instead.
_MINIMAX_HOSTS = frozenset({
    "api.minimaxi.com", "api.minimax.io", "api.minimax.cn", "api.minimax.chat",
})
# Alternate hosts tried when the configured one cannot be reached at all
# (TLS handshake timeout on the international route). The path is preserved,
# so the request still hits the same API surface. A host that answers with
# 401/403 is skipped rather than reported: it belongs to another platform.
_MINIMAX_FALLBACK_HOSTS: Dict[str, Tuple[str, ...]] = {
    "api.minimax.io": ("api.minimax.chat", "api.minimax.cn"),
    "api.minimaxi.com": ("api.minimax.cn", "api.minimax.chat"),
    "api.minimax.cn": ("api.minimax.chat", "api.minimaxi.com"),
    "api.minimax.chat": ("api.minimax.cn", "api.minimaxi.com"),
}
_MINIMAX_PATH_SUFFIXES = (
    "/v1/messages",
    "/v1/chat/completions",
    "/anthropic",
    "/chat/completions",
    "/messages",
    "/v1",
)


def _minimax_base(base_url: str, protocol: str) -> str:
    """Point a MiniMax base URL at the path prefix the given protocol needs."""
    parts = urlsplit(base_url)
    if (parts.hostname or "").lower() not in _MINIMAX_HOSTS:
        return base_url
    path = parts.path.rstrip("/")
    while True:
        for suffix in _MINIMAX_PATH_SUFFIXES:
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        else:
            break
    prefix = "/anthropic" if protocol == "anthropic-messages" else "/v1"
    return urlunsplit((parts.scheme, parts.netloc, f"{path}{prefix}", "", ""))


def _is_dns_failure(reason: Any) -> bool:
    """True when the failure was a name-resolution failure, not a network fault.

    ``socket.gaierror`` means the host could not be resolved. The important
    distinction is *which* failure: ``EAI_NONAME`` means the name does not
    exist, so retrying cannot help and the caller should report the bad URL.
    ``EAI_AGAIN`` means the resolver itself was unavailable, which is
    transient and must keep the retry.
    """
    if isinstance(reason, socket.gaierror):
        return reason.errno == socket.EAI_NONAME
    if isinstance(reason, OSError) and reason.errno == socket.EAI_NONAME:
        return True
    text = str(reason).lower()
    return "name or service not known" in text or "nodename nor servname" in text


def _request_url_candidates(url: str) -> List[str]:
    """Return the URLs to try for a request, primary first.

    Only MiniMax URLs get alternates: the international route to
    ``api.minimax.io`` degrades often enough that the TLS handshake can time
    out, and a sibling host serving the same API surface is worth a try. The
    path prefix is preserved so the request keeps hitting the same endpoint.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    alternates = _MINIMAX_FALLBACK_HOSTS.get(host)
    if not alternates:
        return [url]
    candidates = [url]
    for alternate in alternates:
        if alternate == host:
            continue
        candidates.append(urlunsplit((parts.scheme, alternate, parts.path, parts.query, "")))
    return candidates


def _anthropic_url(base_url: str) -> str:
    base = _minimax_base(base_url.rstrip("/"), "anthropic-messages").rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    if base.endswith("/anthropic"):
        return f"{base}/v1/messages"
    return f"{base}/v1/messages"


def _openai_url(base_url: str) -> str:
    base = _minimax_base(base_url.rstrip("/"), "openai-completions").rstrip("/")
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

    @staticmethod
    def _transient_http(code: int) -> bool:
        return int(code) in _TRANSIENT_HTTP_CODES

    @staticmethod
    def _retry_sleep(attempt: int) -> None:
        delay = _LLM_RETRY_BACKOFF_SEC[min(attempt, len(_LLM_RETRY_BACKOFF_SEC) - 1)]
        time.sleep(delay)

    def _request(self, url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        last_error: Optional[BaseException] = None
        # The route to a given MiniMax host can degrade (the international
        # path to api.minimax.io times out at the TLS handshake). When that
        # happens the request never reaches the API, so an alternate host for
        # the same API surface is tried before giving up.
        candidates = _request_url_candidates(url)
        primary = candidates[0]
        attempts = [primary, *candidates[1:], *([primary] * _LLM_MAX_RETRIES)]
        deadline = time.monotonic() + _LLM_TOTAL_BUDGET_SEC
        for attempt, target in enumerate(attempts):
            if attempt > 0 and time.monotonic() >= deadline:
                # The budget is spent; report the original failure rather than
                # keeping the caller waiting on a route that is clearly down.
                break
            request = urllib.request.Request(target, data=body, method="POST", headers=headers)
            # A healthy route completes its TLS handshake in about a second.
            # Capping the first attempt keeps a dead route from stalling the
            # whole request for the full timeout before the alternates are tried.
            timeout = _LLM_FIRST_ATTEMPT_TIMEOUT if attempt == 0 else _LLM_TIMEOUT_SEC
            # Never let an attempt run past the overall budget.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            timeout = max(1.0, min(float(timeout), remaining))
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read().decode("utf-8")
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError as exc:
                        last_error = RuntimeError(f"LLM connection error: invalid JSON ({exc})")
                        if attempt < len(attempts) - 1:
                            self._retry_sleep(attempt)
                            continue
                        raise last_error from exc
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                message = f"LLM HTTP {exc.code}: {detail}"
                if exc.code in (401, 403) and _is_minimax_host(target):
                    message = f"{message} — {_minimax_auth_hint(detail)}"
                last_error = RuntimeError(
                    f"{message} [url={_safe_url(target)}]"
                )
                if target != primary and exc.code in (401, 403):
                    # The alternate host rejected the key: it belongs to a
                    # different platform. Keep the original failure and try the
                    # next candidate instead of reporting a misleading 401.
                    continue
                if self._transient_http(exc.code) and attempt < len(attempts) - 1:
                    self._retry_sleep(attempt)
                    continue
                raise last_error from exc
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                # A name that does not resolve cannot be fixed by retrying: the
                # request never leaves the host. Fail fast with a message that
                # points at the base URL instead of burning the retry budget.
                if _is_dns_failure(reason):
                    raise RuntimeError(
                        f"LLM connection error: {reason} [url={_safe_url(target)}] — "
                        "域名无法解析，请检查 Config 中的 Base URL"
                    ) from exc
                last_error = RuntimeError(
                    f"LLM connection error: {reason} [url={_safe_url(target)}]"
                )
                if attempt < len(attempts) - 1:
                    self._retry_sleep(attempt)
                    continue
                raise last_error from exc
            except TimeoutError as exc:
                last_error = RuntimeError(
                    f"LLM connection error: timed out ({exc}) [url={_safe_url(target)}]"
                )
                if attempt < len(attempts) - 1:
                    self._retry_sleep(attempt)
                    continue
                raise last_error from exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("LLM request failed")

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
