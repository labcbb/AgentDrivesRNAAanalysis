"""The LangGraph plan engine must replay a transiently failed step.

The LangGraph engine (``lc_plan_graph``) is the default execution path.
When it ignored transient failures, a step that died on a dropped TLS
handshake stayed ``failed`` and every dependant reported "计划被未完成或缺失
的依赖阻塞" -- the plan could never continue.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.lc_plan_graph import run_lc_plan_steps  # noqa: E402


class _FakeAgent:
    def _check_cancelled(self, cancel_event):
        return None

    def _clear_run_checkpoint(self, chat_id):
        return None


class _FakeOrchestrator:
    """Minimal orchestrator stand-in that records what the engine does."""

    def __init__(self, results):
        self.results = list(results)
        self.attempts = []
        self.emitted = []
        self.chat_id = "c"
        self.max_replan_attempts = 0  # no replanning: isolate the replay path
        self.agent = _FakeAgent()

    # -- surface used by the engine -------------------------------------
    def _emit(self, on_progress, kind, **payload):
        self.emitted.append((kind, payload))

    def _persist_plan(self, plan):
        pass

    def _save_step_checkpoint(self, plan, step_id):
        pass

    def _materialize_approval_followup(self, plan):
        return False

    def _auto_complete_target_candidate_gates(self, plan):
        return False

    def _ensure_user_facing_reply(self, user_query, summary, **kwargs):
        return summary

    def _next_pending_step(self, plan):
        from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

        return PlanOrchestrator._next_pending_step(plan)

    def _next_retryable_failed_step(self, plan):
        from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

        return PlanOrchestrator._next_retryable_failed_step(plan)

    def _can_retry_step(self, step):
        from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

        return PlanOrchestrator._can_retry_step(step)

    def _execute_step_resilient(self, step, **kwargs):
        self.attempts.append(step.get("id"))
        if self.results:
            return self.results.pop(0)
        return "ok"

    def _replan(self, plan, **kwargs):
        raise AssertionError("a transient failure must be replayed, not replanned")


def _plan():
    return {
        "goal": "g",
        "steps": [
            {
                "id": "preflight",
                "title": "读取现有 FASTQ 修剪配置",
                "skill": "fastq-qc",
                "status": "failed",
                "result": (
                    "STEP_EXECUTION_ERROR: RuntimeError: LLM connection error: "
                    "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation "
                    "of protocol (_ssl.c:1032)"
                ),
                "depends_on": [],
            },
            {
                "id": "approve",
                "title": "确认 sRNA-seq 3' adapter",
                "skill": "fastq-qc",
                "status": "pending",
                "depends_on": ["preflight"],
            },
        ],
    }


def test_transient_failure_is_replayed_and_the_plan_continues():
    orch = _FakeOrchestrator(["ok"])
    plan = _plan()

    result = run_lc_plan_steps(
        orch,
        plan=plan,
        history=[{"role": "user", "content": "q"}],
        user_query="q",
        execution_user_query="q",
    )

    assert orch.attempts == ["preflight", "approve"], (
        "the failed step must be executed again, and the plan must continue "
        "past it instead of stopping"
    )
    statuses = {s["id"]: s.get("status") for s in plan["steps"]}
    assert statuses["preflight"] == "done", "the replayed step must succeed"
    assert "依赖阻塞" not in result, (
        f"the plan must not report a blocked dependency: {result}"
    )


def test_replay_is_used_instead_of_replanning():
    """Replanning would call the same flaky endpoint and fail the same way."""
    orch = _FakeOrchestrator(["ok"])
    plan = _plan()
    run_lc_plan_steps(
        orch,
        plan=plan,
        history=[{"role": "user", "content": "q"}],
        user_query="q",
        execution_user_query="q",
    )
    assert orch.attempts.count("preflight") == 1
    kinds = [kind for kind, _ in orch.emitted]
    assert "plan_revised" not in kinds, "a transient failure must not trigger a replan"


def test_a_permanent_failure_is_not_replayed_forever():
    """A genuine bug must reach the replan/terminal path, not loop."""
    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    step = {"id": "s", "status": "failed"}
    assert PlanOrchestrator._can_retry_step(step) is True
    assert PlanOrchestrator._can_retry_step(step) is True
    # The bound stops the replay after the allowed number of attempts.
    assert PlanOrchestrator._can_retry_step(step) is False
