"""Context size estimation and compaction for long agent sessions.

Long-running sessions accumulate full chat history and full tool outputs,
which eventually overflow the LLM context window. This module provides:

- ``estimate_tokens`` / ``messages_tokens``: cheap token heuristics used to
  decide when a session is getting too large.
- ``bounded_tool_result``: cap tool outputs (head+tail preserved) at append
  time so a single huge result cannot blow the budget.
- ``compact_messages``: fold older turns into one LLM-written summary while
  keeping the system prompt and the most recent turns verbatim. If the LLM
  summarizer is unavailable or fails, falls back to dropping the oldest turns.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

_TRUNCATED_MARKER = "\n…[内容已截断]"
_SUMMARY_ROLE = "system"
_SUMMARY_PREFIX = "[会话摘要（早期对话已压缩，保留关键决策、路径、参数与结果）]"


def estimate_tokens(text: str) -> int:
    """Heuristic token count: CJK chars ~1 token each, other text ~4 chars/token."""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + other // 4 + 1


def messages_tokens(messages: List[Dict[str, Any]]) -> int:
    """Total estimated tokens for a message list, including tool-call payloads."""
    total = 0
    for msg in messages:
        total += estimate_tokens(str(msg.get("content") or ""))
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            total += estimate_tokens(str(fn.get("name") or ""))
            total += estimate_tokens(str(fn.get("arguments") or ""))
    return total


def truncate_text(text: str, max_chars: int) -> str:
    """Keep head+tail of a long string around a truncation marker."""
    if not text or len(text) <= max_chars:
        return text
    marker = _TRUNCATED_MARKER
    if max_chars <= len(marker):
        return text[:max_chars]
    head = max_chars // 2
    tail = max_chars - head - len(marker)
    return f"{text[:head]}{marker}{text[-tail:]}"


def bounded_tool_result(result: str, max_chars: int = 8000) -> str:
    """Cap a tool result before it is sent back to the LLM.

    Head+tail are kept so that error traces (tail) and leading context (head)
    survive truncation; a marker notes the cut.
    """
    if not result:
        return "(no output)"
    if len(result) <= max_chars:
        return result
    return truncate_text(result, max_chars)


def should_compact(messages: List[Dict[str, Any]], max_tokens: int) -> bool:
    return max_tokens > 0 and messages_tokens(messages) > max_tokens


def _split_system(messages: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    systems = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    return systems, rest


def _llm_summarize(
    llm: Any,
    old_messages: List[Dict[str, Any]],
) -> str:
    """One-shot LLM summary of the old turns. Returns "" on failure."""
    try:
        transcript = json.dumps(
            [
                {
                    "role": m.get("role"),
                    "content": str(m.get("content") or "")[:4000],
                    "tool": [
                        (c.get("function") or {}).get("name", "")
                        for c in (m.get("tool_calls") or [])
                    ],
                }
                for m in old_messages
            ],
            ensure_ascii=False,
        )
        completion = llm.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You are sRNAgent's session summarizer. Summarize the earlier "
                        "conversation below into one compact block that preserves: "
                        "completed steps, decisions, file paths, parameters, adata state, "
                        "and any remaining work. Keep the same language as the conversation. "
                        "Output only the summary, no preamble."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
            tools=None,
            enable_thinking=False,
        )
        text = str(getattr(completion, "content", "") or "").strip()
        return text if len(text) >= 20 else ""
    except Exception:  # noqa: BLE001 — summarization is best-effort
        return ""


def _drop_oldest_until_fit(
    systems: List[Dict[str, Any]],
    rest: List[Dict[str, Any]],
    max_tokens: int,
    keep_recent: int,
) -> List[Dict[str, Any]]:
    """Fallback compaction: drop oldest turns until the budget fits."""
    kept = rest[-keep_recent:]
    while kept and messages_tokens(systems + kept) > max_tokens and len(kept) > 1:
        kept = kept[1:]
    return systems + kept


def compact_messages(
    messages: List[Dict[str, Any]],
    *,
    llm: Optional[Any] = None,
    max_tokens: int,
    keep_recent: int = 12,
    on_compact: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """Compress early turns into a summary; keep system + recent turns verbatim.

    ``llm`` may be ``None`` (or fail) — then a lossy drop-oldest fallback is
    used so the caller never hits an unhandled error. ``on_compact`` receives
    the summary text (or "" for the fallback) for progress reporting.
    """
    if len(messages) <= 1:
        return messages

    systems, rest = _split_system(messages)
    if len(rest) <= keep_recent:
        return messages

    to_summarize = rest[:-keep_recent]
    to_keep = rest[-keep_recent:]

    summary = _llm_summarize(llm, to_summarize) if llm is not None else ""
    if summary:
        if on_compact:
            on_compact(summary)
        return systems + [{"role": _SUMMARY_ROLE, "content": f"{_SUMMARY_PREFIX}\n{summary}"}] + to_keep

    if on_compact:
        on_compact("")
    return _drop_oldest_until_fit(systems, rest, max_tokens, keep_recent)
