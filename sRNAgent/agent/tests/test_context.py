"""Tests for context estimation / compaction (runnable with pytest or directly)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.context import (  # noqa: E402
    _SUMMARY_PREFIX,
    bounded_tool_result,
    compact_messages,
    estimate_tokens,
    messages_tokens,
    normalize_text_payload,
    should_compact,
    truncate_text,
)
from sRNAgent.agent.srn_agent import _parse_progress_output  # noqa: E402


def test_estimate_tokens_basic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 4) >= 1
    ascii_only = estimate_tokens("hello world, this is a test")
    cjk_only = estimate_tokens("中文测试中文测试")
    assert cjk_only > ascii_only  # CJK is token-heavier per char
    assert estimate_tokens("a" * 400) < estimate_tokens("中" * 400)


def test_messages_tokens_includes_tool_calls():
    messages = [
        {"role": "user", "content": "你好" * 100},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "function": {
                        "name": "execute_code",
                        "arguments": '{"code": "' + "x" * 500 + '"}',
                    }
                }
            ],
        },
    ]
    total = messages_tokens(messages)
    assert total > estimate_tokens(messages[0]["content"]) + 10


def test_truncate_text_keeps_head_and_tail():
    text = "A" * 100 + "Z" * 100
    out = truncate_text(text, 60)
    assert len(out) < len(text)
    marker = "\n…[内容已截断]"
    # implementation: head = max_chars // 2, tail = max_chars - head - len(marker)
    assert out.startswith(text[:30])
    assert out.endswith(text[-(60 - 30 - len(marker)):])
    assert "截断" in out
    assert truncate_text("short", 100) == "short"


def test_bounded_tool_result():
    assert bounded_tool_result("", 100) == "(no output)"
    assert bounded_tool_result("ok", 100) == "ok"
    big = "head" * 100 + "\n" + "tail" * 100
    out = bounded_tool_result(big, 100)
    assert len(out) <= 100 + len("…[内容已截断]")
    assert "head" in out and "tail" in out


def test_bounded_tool_result_keeps_error_tail():
    """超长 traceback 截断后必须保留尾部的真实错误行（如 NameError）。"""
    head = "Traceback (most recent call last):\n  File \"<string>\", line 1\n"
    filler = "  context line\n" * 300
    tail = "NameError: name 'pd' is not defined"
    out = bounded_tool_result(head + filler + tail, 8000)
    assert len(out) <= 8000
    assert out.endswith(tail)


def test_normalize_text_payload_unwraps_provider_fragments_and_legacy_repr():
    wrapped = {"$text": "第一行\n", "SRR": {"$text": "第二行"}}

    assert normalize_text_payload(wrapped) == "第一行\nSRR第二行"
    assert normalize_text_payload(repr(wrapped)) == "第一行\nSRR第二行"


def test_progress_parser_preserves_fragomics_log_prefix():
    progress = _parse_progress_output(
        "[fragomics] sample=SRR15720393 phase=bam start FSC/RCD/BPM extraction"
    )

    assert progress["highlights"] == [
        "[fragomics] sample=SRR15720393 phase=bam start FSC/RCD/BPM extraction"
    ]


def test_should_compact_threshold():
    small = [{"role": "user", "content": "hi"}]
    assert not should_compact(small, 48000)
    big = [{"role": "user", "content": "中" * 60000}]  # ~60k tokens
    assert should_compact(big, 48000)
    assert not should_compact(big, 0)  # disabled


class _FakeLLM:
    def __init__(self, summary="【摘要】完成了下载和比对，结果在 aligned/"):
        self.summary = summary
        self.calls = 0

    def complete(self, messages, tools=None, enable_thinking=None):
        self.calls += 1
        from sRNAgent.agent.llm_client import ChatCompletion

        return ChatCompletion(content=self.summary)


def test_compact_messages_with_llm_summary():
    messages = [
        {"role": "system", "content": "system rule"},
    ] + [
        {"role": "user", "content": f"turn {i} 内容"}
        for i in range(20)
    ]
    llm = _FakeLLM()
    out = compact_messages(messages, llm=llm, max_tokens=48000, keep_recent=5)
    # system kept
    assert out[0]["role"] == "system"
    assert out[0]["content"] == "system rule"
    # summary inserted
    assert any("摘要" in str(m.get("content") or "") for m in out)
    # last 5 turns kept verbatim
    assert "turn 19 内容" in str(out[-1]["content"])
    assert "turn 15 内容" in str(out[-5]["content"])
    assert not any("turn 0 内容" in str(m.get("content") or "") for m in out)
    assert llm.calls == 1
    summary_messages = [m for m in out if _SUMMARY_PREFIX in str(m.get("content") or "")]
    assert len(summary_messages) == 1
    assert summary_messages[0]["role"] == "assistant"


def test_compact_messages_fallback_without_llm():
    messages = [{"role": "user", "content": f"turn {i} 内容"} for i in range(20)]
    out = compact_messages(messages, llm=None, max_tokens=48000, keep_recent=4)
    assert len(out) <= 4 + 1  # 4 recent (+ possible summary marker)
    assert "turn 19 内容" in str(out[-1]["content"])
    assert not any("turn 0 内容" in str(m.get("content") or "") for m in out)


def test_compact_messages_noop_when_short():
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    out = compact_messages(messages, llm=None, max_tokens=48000, keep_recent=12)
    assert out == messages


def test_compact_messages_replaces_previous_summary_block():
    messages = [
        {"role": "system", "content": "system rule"},
        {"role": "assistant", "content": f"{_SUMMARY_PREFIX}\n旧摘要"},
    ] + [
        {"role": "user", "content": f"turn {i} 内容"}
        for i in range(18)
    ]
    llm = _FakeLLM(summary="【新摘要】已完成下载、比对与结果保存，保留关键决策与输出路径。")
    out = compact_messages(messages, llm=llm, max_tokens=48000, keep_recent=4)
    summaries = [m for m in out if _SUMMARY_PREFIX in str(m.get("content") or "")]
    assert len(summaries) == 1
    assert "旧摘要" not in str(summaries[0].get("content") or "")
    assert "新摘要" in str(summaries[0].get("content") or "")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
