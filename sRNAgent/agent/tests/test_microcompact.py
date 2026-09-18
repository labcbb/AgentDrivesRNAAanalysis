"""Tests for microcompaction of tool results (Claude Code s15 pattern)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.context import (  # noqa: E402
    _looks_like_error,
    bounded_tool_result,
    microcompact_tool_result,
)


class _FakeLLM:
    def __init__(self, output="compacted summary"):
        self.output = output
        self.calls = 0

    def complete(self, messages, tools=None, enable_thinking=None):
        self.calls += 1
        comp = MagicMock()
        comp.content = self.output
        return comp


def test_small_result_not_compacted():
    llm = _FakeLLM()
    result = microcompact_tool_result("short output", llm=llm, max_chars=8000)
    assert result == "short output"
    assert llm.calls == 0  # no LLM call for small results


def test_empty_result():
    assert microcompact_tool_result("", llm=_FakeLLM()) == "(no output)"


def test_large_result_compacted_with_llm():
    big = "line of data\n" * 1000  # ~14000 chars
    llm = _FakeLLM(output="summarised: 1000 lines of data")
    result = microcompact_tool_result(big, llm=llm, max_chars=8000, threshold=4000)
    assert "summarised" in result
    assert "已由 LLM 摘要压缩" in result
    assert llm.calls == 1
    assert len(result) < len(big)


def test_large_result_no_llm_falls_back_to_truncation():
    big = "x" * 10000
    result = microcompact_tool_result(big, llm=None, max_chars=8000, threshold=4000)
    # head+tail truncation
    assert "内容已截断" in result
    assert len(result) <= 8000


def test_error_output_not_compacted_kept_verbatim():
    # Make the error larger than max_chars so head+tail truncation kicks in.
    err = "Traceback (most recent call last):\n  File ...\nValueError: boom\n" + "x" * 20000
    llm = _FakeLLM()
    result = microcompact_tool_result(err, llm=llm, max_chars=8000, threshold=4000)
    assert llm.calls == 0  # errors skip microcompaction
    assert "Traceback" in result  # head preserved
    assert "内容已截断" in result  # truncated since > max_chars
    assert len(result) <= 8000


def test_error_detected_by_error_colon():
    assert _looks_like_error("some output\nError: something failed\n" + "x" * 5000) is True
    assert _looks_like_error("just normal output\n" * 100) is False


def test_empty_llm_summary_falls_back():
    big = "x" * 10000
    llm = _FakeLLM(output="")  # empty summary
    result = microcompact_tool_result(big, llm=llm, max_chars=8000, threshold=4000)
    assert "内容已截断" in result  # fallback to head+tail


def test_summary_longer_than_original_falls_back():
    big = "x" * 5000
    llm = _FakeLLM(output="y" * 6000)  # summary longer than original
    result = microcompact_tool_result(big, llm=llm, max_chars=8000, threshold=4000)
    # Summary rejected (longer than original) → fallback returns original verbatim
    # (5000 chars fits within max_chars=8000, so no truncation marker).
    assert "已由 LLM 摘要压缩" not in result  # LLM summary not used
    assert result == big  # original kept verbatim
    assert llm.calls == 1  # LLM was tried but rejected


def test_threshold_respected():
    # Result just under threshold: no compaction even with LLM
    result = microcompact_tool_result("x" * 3999, llm=_FakeLLM(), max_chars=8000, threshold=4000)
    assert result == "x" * 3999


def test_compacted_result_still_capped_at_max_chars():
    big = "x" * 50000
    llm = _FakeLLM(output="y" * 9000)  # summary itself exceeds max_chars
    result = microcompact_tool_result(big, llm=llm, max_chars=8000, threshold=4000)
    assert len(result) <= 8000
