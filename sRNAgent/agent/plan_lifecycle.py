"""Deterministic lifecycle operations for persisted execution plans.

The LLM is allowed to propose plan content, but it does not own state
transitions.  This module is the single place where plans are made resumable,
dependencies are normalized, and stale approval states are repaired.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from .plan_state import (
    PLAN_STEP_STATUSES,
    PlanGraph,
    STEP_AWAITING_APPROVAL,
    STEP_DONE,
    STEP_PENDING,
    STEP_RUNNING,
)


class PlanLifecycle:
    """Own the persisted plan state machine independently of the LLM."""

    def __init__(self, plan: Dict[str, Any]) -> None:
        self.plan = plan

    @property
    def steps(self) -> List[Dict[str, Any]]:
        steps = self.plan.get("steps")
        return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []

    def normalize_structure(self) -> Dict[str, Any]:
        """Normalize legacy JSON without changing the user's requested scope."""
        if not isinstance(self.plan.get("steps"), list):
            self.plan["steps"] = []
        seen: set[str] = set()
        for index, step in enumerate(self.plan["steps"], start=1):
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("id") or index).strip()
            if not step_id or step_id in seen:
                step_id = str(index)
                while step_id in seen:
                    step_id = f"{index}-{len(seen)}"
            step["id"] = step_id
            seen.add(step_id)
            status = str(step.get("status") or STEP_PENDING).strip().lower()
            step["status"] = status if status in PLAN_STEP_STATUSES else STEP_PENDING
            dependencies = step.get("depends_on")
            if isinstance(dependencies, list):
                unique: List[str] = []
                for dependency in dependencies:
                    value = str(dependency).strip()
                    if value and value != step_id and value not in unique:
                        unique.append(value)
                step["depends_on"] = unique
        return self.plan

    def prepare_for_resume(self) -> Dict[str, Any]:
        """Reset interrupted work and repair impossible approval transitions."""
        self.normalize_structure()
        PlanGraph(self.plan).reset_interrupted(running=STEP_RUNNING, pending=STEP_PENDING)
        self.repair_completed_approval_dependencies()
        return self.plan

    def insert_prerequisite(self, gate: Dict[str, Any], prerequisite: Dict[str, Any], *, index: int) -> None:
        """Insert a prerequisite and make the gate depend on it exactly once."""
        steps = self.steps
        gate_id = str(gate.get("id") or index + 1).strip()
        prerequisite_id = str(prerequisite.get("id") or f"{gate_id}-prerequisite").strip()
        original_dependencies = PlanGraph.dependency_ids(gate)
        if any(str(step.get("id") or "") == prerequisite_id for step in steps):
            gate["depends_on"] = [
                prerequisite_id,
                *[dependency for dependency in original_dependencies if dependency != prerequisite_id],
            ]
            return
        if original_dependencies:
            prerequisite["depends_on"] = original_dependencies
        gate["depends_on"] = [prerequisite_id]
        self.plan["steps"].insert(index, prerequisite)

    def repair_completed_approval_dependencies(self) -> bool:
        """Close a stale gate only when a direct dependent already completed."""
        steps = self.steps
        repaired = False
        for gate in steps:
            if gate.get("status") != STEP_AWAITING_APPROVAL:
                continue
            gate_id = str(gate.get("id") or "").strip()
            if not gate_id:
                continue
            completed_dependent = next(
                (
                    step for step in steps
                    if gate_id in PlanGraph.dependency_ids(step)
                    and step.get("status") == STEP_DONE
                ),
                None,
            )
            if completed_dependent is None:
                continue
            approval = gate.setdefault("approval", {})
            if not isinstance(approval, dict):
                approval = {}
                gate["approval"] = approval
            gate["status"] = STEP_DONE
            gate["result"] = (
                "系统恢复：依赖此确认的后续步骤“"
                f"{completed_dependent.get('title') or completed_dependent.get('id')}”已完成，"
                "因此将该确认恢复为已完成。"
            )
            approval.setdefault("response", "recovered_from_completed_dependent")
            approval.pop("lastResponse", None)
            repaired = True
        return repaired

def clone_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Copy plan JSON before applying lifecycle transitions."""
    return deepcopy(plan)


__all__ = ["PlanLifecycle", "clone_plan"]
