"""Tests for the plan state machine, independent from LLM orchestration."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.plan_state import PlanGraph  # noqa: E402


def test_graph_only_returns_pending_steps_with_completed_dependencies():
    plan = {
        "steps": [
            {"id": "download", "status": "done"},
            {"id": "adapter", "status": "awaiting_approval", "depends_on": ["download"]},
            {"id": "fastqc", "status": "pending", "depends_on": ["adapter"]},
        ]
    }
    graph = PlanGraph(plan)

    assert graph.next_runnable_pending(pending="pending", completed={"done", "skipped"}) is None
    assert graph.blocked_pending(pending="pending", completed={"done", "skipped"}) == [plan["steps"][2]]


def test_graph_treats_unknown_dependencies_as_blocking_and_resets_interrupted_steps():
    plan = {"steps": [{"id": "qc", "status": "running", "depends_on": ["missing"]}]}
    graph = PlanGraph(plan)

    graph.reset_interrupted(running="running", pending="pending")

    assert plan["steps"][0]["status"] == "pending"
    assert graph.next_runnable_pending(pending="pending", completed={"done"}) is None
    assert graph.unmet_dependencies(plan["steps"][0], {"done"}) == ["missing"]
