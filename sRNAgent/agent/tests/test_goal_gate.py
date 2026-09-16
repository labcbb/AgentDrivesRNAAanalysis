"""Tests for the goal gate evaluator (s17 pattern)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.goal_gate import GoalGate, GoalDecision  # noqa: E402


def test_inactive_gate_always_achieved():
    gate = GoalGate("")
    assert not gate.active
    decision = gate.evaluate([], "done", llm_complete=None)
    assert decision.action == "achieved"


def test_no_evaluator_trusts_finish():
    gate = GoalGate("完成 SRP181693 质控")
    assert gate.active
    decision = gate.evaluate(
        [{"role": "user", "content": "do QC"}, {"role": "assistant", "content": "done"}],
        "done",
        llm_complete=None,
    )
    assert decision.action == "achieved"


def test_block_increments_counter():
    gate = GoalGate("完成质控", max_blocks=3)

    def evaluator(msgs, *, system=""):
        return "BLOCK"

    for i in range(3):
        decision = gate.evaluate([], "text", llm_complete=evaluator)
        assert decision.action == "block"
        assert str(i + 1) in decision.reason

    # 4th block hits the limit
    decision = gate.evaluate([], "text", llm_complete=evaluator)
    assert decision.action == "limit"


def test_achieved_when_evaluator_says_achieved():
    gate = GoalGate("完成质控")

    def evaluator(msgs, *, system=""):
        return "ACHIEVED"

    decision = gate.evaluate([], "done", llm_complete=evaluator)
    assert decision.action == "achieved"


def test_impossible_when_evaluator_says_impossible():
    gate = GoalGate("完成质控")

    def evaluator(msgs, *, system=""):
        return "IMPOSSIBLE — data missing"

    decision = gate.evaluate([], "text", llm_complete=evaluator)
    assert decision.action == "impossible"


def test_evaluator_exception_trusts_finish():
    gate = GoalGate("完成质控")

    def bad_evaluator(msgs, *, system=""):
        raise RuntimeError("LLM down")

    decision = gate.evaluate([], "text", llm_complete=bad_evaluator)
    assert decision.action == "achieved"


def test_block_message_contains_condition():
    gate = GoalGate("完成 SRP181693 质控")
    decision = GoalDecision("block", "Goal not yet achieved (block 1/8).")
    msg = gate.block_message(decision)
    assert "SRP181693" in msg
    assert "Continue working" in msg


def test_reset_clears_blocks():
    gate = GoalGate("goal", max_blocks=2)

    def evaluator(msgs, *, system=""):
        return "BLOCK"

    gate.evaluate([], "text", llm_complete=evaluator)
    gate.evaluate([], "text", llm_complete=evaluator)
    gate.reset()
    decision = gate.evaluate([], "text", llm_complete=evaluator)
    assert decision.action == "block"
    assert "1/2" in decision.reason
