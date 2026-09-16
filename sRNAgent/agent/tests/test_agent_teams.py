"""Tests for the agent teams / sub-agent spawning system (s13 pattern)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.agent_teams import (  # noqa: E402
    _filter_tool_schemas,
    _SUBAGENT_TOOLS,
    spawn_subagent,
)


class _FakeToolCall:
    def __init__(self, name, arguments, call_id="c1"):
        self.id = call_id
        self.name = name
        self.arguments = arguments


class _FakeCompletion:
    def __init__(self, tool_calls=None, content=""):
        self.tool_calls = tool_calls
        self.content = content
        self.thinking = ""


class _FakeLLM:
    """Replays a scripted list of completions."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def complete(self, messages, tools=None, enable_thinking=None):
        self.calls += 1
        if self.calls <= len(self.script):
            return self.script[self.calls - 1]
        # Default: finish with empty message
        return _FakeCompletion(tool_calls=[_FakeToolCall("finish", {"message": "done"})])


def _make_parent(llm, dispatch=None):
    parent = MagicMock()
    parent.llm = llm
    parent.max_tool_result_chars = 8000
    parent._subagent_skill_overview = "alignment-srna, fastq-dl-srna"
    parent.dispatch_tool = dispatch or (lambda name, args: f"dispatched {name}")
    return parent


def test_filter_tool_schemas_excludes_spawn_subagent():
    for agent_type, names in _SUBAGENT_TOOLS.items():
        schemas = _filter_tool_schemas(names)
        tool_names = [s["function"]["name"] for s in schemas]
        assert "spawn_subagent" not in tool_names
        assert "finish" in tool_names


def test_filter_tool_schemas_explore_no_manage_visualization():
    schemas = _filter_tool_schemas(_SUBAGENT_TOOLS["explore"])
    names = [s["function"]["name"] for s in schemas]
    assert "manage_visualization" not in names
    assert "execute_code" in names


def test_filter_tool_schemas_code_has_manage_visualization():
    schemas = _filter_tool_schemas(_SUBAGENT_TOOLS["code"])
    names = [s["function"]["name"] for s in schemas]
    assert "manage_visualization" in names


def test_subagent_finishes_immediately_with_text():
    llm = _FakeLLM([_FakeCompletion(content="The results are in results/DE/")])
    parent = _make_parent(llm)
    result = spawn_subagent(parent, "Where are the DE results?", agent_type="explore")
    assert "results are in" in result
    assert llm.calls == 1


def test_subagent_runs_tool_then_finishes():
    # Turn 1: call search_functions, Turn 2: call finish
    llm = _FakeLLM([
        _FakeCompletion(tool_calls=[_FakeToolCall("search_functions", {"query": "fastq_dl"})]),
        _FakeCompletion(tool_calls=[_FakeToolCall("finish", {"message": "Found fastq_dl"})]),
    ])
    dispatched = []

    def fake_dispatch(name, args):
        dispatched.append((name, args))
        return f"result of {name}"

    parent = _make_parent(llm, dispatch=fake_dispatch)
    result = spawn_subagent(parent, "Find the fastq download function", agent_type="explore")
    assert "Found fastq_dl" in result
    assert llm.calls == 2
    assert dispatched == [("search_functions", {"query": "fastq_dl"})]


def test_subagent_unknown_type_defaults_to_explore():
    llm = _FakeLLM([_FakeCompletion(content="ok")])
    parent = _make_parent(llm)
    result = spawn_subagent(parent, "test", agent_type="nonexistent")
    assert "ok" in result


def test_subagent_max_turns_reached():
    # Always call search_functions, never finish — should hit max turns.
    llm = _FakeLLM([
        _FakeCompletion(tool_calls=[_FakeToolCall("search_functions", {"query": "x"}, call_id=f"c{i}")])
        for i in range(20)
    ])
    parent = _make_parent(llm)
    result = spawn_subagent(parent, "loop", agent_type="explore", max_turns=3)
    assert "max turns" in result
    assert llm.calls == 3


def test_subagent_dispatch_error_is_captured():
    llm = _FakeLLM([
        _FakeCompletion(tool_calls=[_FakeToolCall("execute_code", {"code": "boom"})]),
        _FakeCompletion(tool_calls=[_FakeToolCall("finish", {"message": "handled"})]),
    ])

    def boom_dispatch(name, args):
        raise RuntimeError("kernel exploded")

    parent = _make_parent(llm, dispatch=boom_dispatch)
    result = spawn_subagent(parent, "run code", agent_type="code")
    assert "handled" in result


def test_subagent_result_is_bounded():
    long_text = "x" * 50000
    llm = _FakeLLM([_FakeCompletion(content=long_text)])
    parent = _make_parent(llm)
    parent.max_tool_result_chars = 200
    result = spawn_subagent(parent, "test", agent_type="explore")
    assert len(result) <= 250  # bounded + truncation marker
