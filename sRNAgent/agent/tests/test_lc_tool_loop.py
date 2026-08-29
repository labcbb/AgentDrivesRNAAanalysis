"""Smoke tests for LangGraph tool-loop without calling a real LLM."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List


class _Call:
    def __init__(self, id: str, name: str, arguments: Dict[str, Any]):
        self.id = id
        self.name = name
        self.arguments = arguments


class _Completion:
    def __init__(self, content: str = "", tool_calls=None, thinking: str = ""):
        self.content = content
        self.tool_calls = tool_calls or []
        self.thinking = thinking


class FakeAgent:
    def __init__(self):
        self.max_turns = 5
        self.max_tool_result_chars = 8000
        self.events: List[Dict[str, Any]] = []
        self._n = 0

    def _check_cancelled(self, cancel_event):
        return None

    def _compact_context(self, messages, on_progress=None):
        return messages

    def _emit_progress(self, on_progress, typ, **kwargs):
        self.events.append({"type": typ, **kwargs})
        if on_progress:
            on_progress({"type": typ, **kwargs})

    def _llm_complete_cancellable(self, messages, **kwargs):
        self._n += 1
        if self._n == 1:
            return _Completion(
                content="",
                tool_calls=[
                    _Call("1", "search_skills", {"query": "fastq"}),
                ],
            )
        return _Completion(
            content="",
            tool_calls=[
                _Call("2", "finish", {"message": "找到了相关 skill。"}),
            ],
        )

    def _ensure_user_facing_reply(self, messages, message, **kwargs):
        return message

    def _save_run_checkpoint(self, *args, **kwargs):
        return None

    def _clear_run_checkpoint(self, *args, **kwargs):
        return None

    def dispatch_tool(self, name, arguments, **kwargs):
        if name == "search_skills":
            return "skill: fastq-dl-srna"
        return f"ok:{name}"


def test_lc_tool_loop_finish():
    from sRNAgent.agent.lc_tool_loop import run_lc_tool_loop

    agent = FakeAgent()
    answer = run_lc_tool_loop(
        agent,
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "查一下 fastq skill"},
        ],
    )
    assert "找到了相关 skill" in answer
    assert any(e.get("type") == "tool_call" and e.get("name") == "search_skills" for e in agent.events)
    assert any(e.get("type") == "final" for e in agent.events)


def test_lc_tool_loop_plain_answer():
    from sRNAgent.agent.lc_tool_loop import run_lc_tool_loop

    agent = FakeAgent()

    def llm(messages, **kwargs):
        return _Completion(content="这是直接回答。", tool_calls=[])

    agent._llm_complete_cancellable = llm  # type: ignore
    answer = run_lc_tool_loop(
        agent,
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
    )
    assert "直接回答" in answer


if __name__ == "__main__":
    test_lc_tool_loop_finish()
    test_lc_tool_loop_plain_answer()
    print("lc_tool_loop smoke OK")
