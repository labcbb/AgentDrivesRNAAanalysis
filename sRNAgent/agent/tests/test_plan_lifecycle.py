"""Tests for deterministic persisted-plan lifecycle transitions."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.plan_lifecycle import PlanLifecycle  # noqa: E402


def test_lifecycle_normalizes_statuses_and_dependencies_without_losing_plan_scope():
    plan = {
        "goal": "keep this goal",
        "steps": [
            {"id": "1", "status": "not-a-state", "depends_on": ["1", "2", "2", ""]},
            {"id": "2", "status": "done"},
        ],
    }

    PlanLifecycle(plan).normalize_structure()

    assert plan["goal"] == "keep this goal"
    assert plan["steps"][0]["status"] == "pending"
    assert plan["steps"][0]["depends_on"] == ["2"]


def test_lifecycle_inserts_prerequisite_and_repairs_completed_dependent_gate():
    gate = {"id": "gate", "status": "awaiting_approval", "approval": {"id": "x"}}
    plan = {
        "steps": [
            gate,
            {"id": "work", "status": "done", "depends_on": ["gate"]},
        ]
    }
    lifecycle = PlanLifecycle(plan)

    lifecycle.insert_prerequisite(gate, {"id": "preflight", "status": "pending"}, index=0)
    assert gate["depends_on"] == ["preflight"]
    assert plan["steps"][0]["id"] == "preflight"

    # The direct completed dependent remains evidence that the gate had already
    # been passed before a stale checkpoint was written.
    assert lifecycle.repair_completed_approval_dependencies() is True
    assert gate["status"] == "done"
    assert gate["approval"]["response"] == "recovered_from_completed_dependent"
