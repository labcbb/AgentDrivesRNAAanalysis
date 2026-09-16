"""Smoke test for LangGraph plan step loop without LLM."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class FakeOrchestrator:
    def __init__(self):
        self.chat_id = "test"
        self.max_replan_attempts = 2
        self.events: List[Dict[str, Any]] = []
        self.agent = self
        self._cleared = False

    def _check_cancelled(self, cancel_event):
        return None

    def _emit(self, on_progress, typ, **kwargs):
        self.events.append({"type": typ, **kwargs})
        if on_progress:
            on_progress({"type": typ, **kwargs})

    def _persist_plan(self, plan):
        return None

    def _save_step_checkpoint(self, plan, step):
        return None

    def _materialize_approval_followup(self, plan):
        return False

    def _clear_run_checkpoint(self, chat_id):
        self._cleared = True

    def _next_pending_step(self, plan):
        from sRNAgent.agent.plan_state import PlanGraph, STEP_DONE, STEP_PENDING, STEP_SKIPPED

        return PlanGraph(plan).next_runnable_pending(
            pending=STEP_PENDING,
            completed={STEP_DONE, STEP_SKIPPED},
        )

    def _ensure_user_facing_reply(self, user_query, message, **kwargs):
        return message

    def _execute_step(self, step, **kwargs):
        return f"done:{step.get('id')}"

    def _execute_step_resilient(self, step, **kwargs):
        return self._execute_step(step, **kwargs)

    def _load_checkpoint(self):
        return None

    def _replan(self, plan, **kwargs):
        return plan


def test_two_steps_complete():
    from sRNAgent.agent.lc_plan_graph import run_lc_plan_steps
    from sRNAgent.agent.plan_state import STEP_PENDING

    orch = FakeOrchestrator()
    plan = {
        "goal": "demo",
        "version": 1,
        "steps": [
            {"id": "1", "title": "A", "status": STEP_PENDING, "depends_on": []},
            {"id": "2", "title": "B", "status": STEP_PENDING, "depends_on": ["1"]},
        ],
    }
    answer = run_lc_plan_steps(
        orch,
        plan=plan,
        history=[{"role": "user", "content": "跑一下"}],
        user_query="跑一下",
        execution_user_query="demo",
    )
    assert plan["steps"][0]["status"] == "done"
    assert plan["steps"][1]["status"] == "done"
    assert orch._cleared
    assert any(e["type"] == "plan_complete" for e in orch.events)
    assert answer


def test_approval_gate():
    from sRNAgent.agent.lc_plan_graph import run_lc_plan_steps
    from sRNAgent.agent.plan_state import STEP_PENDING

    orch = FakeOrchestrator()
    plan = {
        "goal": "demo",
        "steps": [
            {
                "id": "1",
                "title": "确认分组",
                "status": STEP_PENDING,
                "depends_on": [],
                "approval": {"kind": "group_design", "prompt": "请确认分组"},
            }
        ],
    }
    answer = run_lc_plan_steps(
        orch,
        plan=plan,
        history=[{"role": "user", "content": "差异分析"}],
        user_query="差异分析",
        execution_user_query="demo",
    )
    assert plan["steps"][0]["status"] == "awaiting_approval"
    assert any(e["type"] == "plan_approval_required" for e in orch.events)
    assert answer


if __name__ == "__main__":
    test_two_steps_complete()
    test_approval_gate()
    print("lc_plan_graph smoke OK")
