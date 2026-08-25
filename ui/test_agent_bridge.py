"""Routing tests for plan approval gates."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_bridge  # noqa: E402


def test_latest_user_message_prefers_current_messages_over_legacy_fields():
    assert agent_bridge._latest_user_message({
        "messages": [
            {"role": "assistant", "content": "确认参数"},
            {"role": "user", "content": "可以"},
        ],
        "query": "stale query",
    }) == "可以"


def test_question_at_approval_gate_does_not_resume_or_clear_plan(monkeypatch):
    monkeypatch.setattr(
        agent_bridge,
        "load_plan",
        lambda _: {"steps": [{"status": "awaiting_approval"}]},
    )

    assert agent_bridge._should_answer_without_resuming_plan(
        "chat-1", "enrichr为什么需要在线API，我不是用的本地的吗"
    )


def test_soft_chinese_question_bypasses_planning_without_an_approval_gate(monkeypatch):
    monkeypatch.setattr(agent_bridge, "load_plan", lambda _: {"steps": [{"status": "pending"}]})

    message = "这个isomir的序列，是真实的不同于miRNA的mature序列的吧"
    resume, side_question = agent_bridge._resolve_plan_routing(
        {"messages": [{"role": "user", "content": message}]}, "chat-1", message,
    )

    assert resume is False
    assert side_question is True


def test_approval_reply_and_new_workflow_are_not_treated_as_side_questions(monkeypatch):
    monkeypatch.setattr(
        agent_bridge,
        "load_plan",
        lambda _: {"steps": [{"status": "awaiting_approval"}]},
    )

    assert not agent_bridge._should_answer_without_resuming_plan("chat-1", "可以，继续")
    assert not agent_bridge._should_answer_without_resuming_plan("chat-1", "重新生成 isomiR 结果")


def test_continuation_question_resumes_instead_of_becoming_a_side_question(monkeypatch):
    plan = {"steps": [{"status": "pending"}]}
    monkeypatch.setattr(agent_bridge, "load_plan", lambda _: plan)

    resume, side_question = agent_bridge._resolve_plan_routing(
        {"messages": [{"role": "user", "content": "怎么不继续了"}]},
        "chat-1",
        "怎么不继续了",
    )

    assert resume is True
    assert side_question is False


def test_reset_interrupted_plan_returns_running_step_to_pending(monkeypatch):
    plan = {
        "steps": [
            {"id": "1", "status": "done"},
            {"id": "2", "status": "running"},
        ]
    }
    saved = []
    monkeypatch.setattr(agent_bridge, "load_plan", lambda _: plan)
    monkeypatch.setattr(agent_bridge, "save_plan", lambda chat_id, payload: saved.append((chat_id, payload)))

    assert agent_bridge._reset_interrupted_plan("chat-1") is True
    assert plan["steps"][1]["status"] == "pending"
    assert saved == [("chat-1", plan)]
