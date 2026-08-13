"""State and dependency operations for persisted execution plans.

This module deliberately owns no LLM, tool, or UI behavior.  It makes the
state-machine rules testable independently from :mod:`plan_orchestrator`.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_AWAITING_APPROVAL = "awaiting_approval"
PLAN_STEP_STATUSES = frozenset({
    STEP_PENDING,
    STEP_RUNNING,
    STEP_DONE,
    STEP_FAILED,
    STEP_SKIPPED,
    STEP_AWAITING_APPROVAL,
})


class PlanGraph:
    """Read and transition a plan's ordered dependency graph."""

    def __init__(self, plan: Dict[str, Any]) -> None:
        self.plan = plan
        self.steps = [step for step in (plan.get("steps") or []) if isinstance(step, dict)]
        self.by_id = {
            str(step.get("id") or "").strip(): step
            for step in self.steps
            if str(step.get("id") or "").strip()
        }

    @staticmethod
    def dependency_ids(step: Dict[str, Any]) -> List[str]:
        dependencies = step.get("depends_on")
        if not isinstance(dependencies, list):
            return []
        return [str(value).strip() for value in dependencies if str(value).strip()]

    def dependencies_satisfied(self, step: Dict[str, Any], completed: Iterable[str]) -> bool:
        completed_statuses: Set[str] = set(completed)
        return all(
            str(self.by_id.get(dependency_id, {}).get("status") or "") in completed_statuses
            for dependency_id in self.dependency_ids(step)
        )

    def unmet_dependencies(self, step: Dict[str, Any], completed: Iterable[str]) -> List[str]:
        completed_statuses: Set[str] = set(completed)
        return [
            dependency_id
            for dependency_id in self.dependency_ids(step)
            if str(self.by_id.get(dependency_id, {}).get("status") or "") not in completed_statuses
        ]

    def next_runnable_pending(self, *, pending: str, completed: Iterable[str]) -> Optional[Dict[str, Any]]:
        for step in self.steps:
            if step.get("status") != pending:
                continue
            if self.dependencies_satisfied(step, completed):
                return step
        return None

    def first_with_status(self, status: str) -> Optional[Dict[str, Any]]:
        return next((step for step in self.steps if step.get("status") == status), None)

    def blocked_pending(self, *, pending: str, completed: Iterable[str]) -> List[Dict[str, Any]]:
        return [
            step for step in self.steps
            if step.get("status") == pending and not self.dependencies_satisfied(step, completed)
        ]

    def reset_interrupted(self, *, running: str, pending: str) -> None:
        for step in self.steps:
            if step.get("status") == running:
                step["status"] = pending
