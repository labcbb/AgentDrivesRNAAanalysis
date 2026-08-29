"""LangGraph state machine for PlanOrchestrator step execution.

Phase-3 design:
- Plan creation / restore / amend / scope repair stay in ``PlanOrchestrator.run``
- Only the step DAG loop (pick → approve/execute → succeed/fail/replan) moves here
- Domain helpers (``_execute_step``, ``_replan``, approval prompts) stay on the orchestrator

Toggle: ``SRNAGENT_USE_LC_PLAN_GRAPH`` (default on). Set to ``legacy`` to keep the
original ``while True`` loop inside ``PlanOrchestrator.run``.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .plan_state import (
    STEP_AWAITING_APPROVAL,
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
    PlanGraph,
)

logger = logging.getLogger(__name__)

_ENV_FLAG = "SRNAGENT_USE_LC_PLAN_GRAPH"


def lc_plan_graph_enabled() -> bool:
    raw = str(os.environ.get(_ENV_FLAG, "1")).strip().lower()
    return raw not in {"0", "false", "no", "off", "legacy"}


class PlanStepState(TypedDict, total=False):
    plan: Dict[str, Any]
    history: List[Dict[str, str]]
    user_query: str
    execution_user_query: str
    extra_context: str
    available_artifacts: List[str]
    resume: bool
    replan_attempts: int
    step_total: int
    current_step_id: str
    current_step_index: int
    step_result: str
    final_answer: str
    done: bool
    outcome: str  # continue | approve | complete | failed_terminal | blocked


def run_lc_plan_steps(
    orchestrator: Any,
    *,
    plan: Dict[str, Any],
    history: List[Dict[str, str]],
    user_query: str,
    execution_user_query: str,
    extra_context: str = "",
    available_artifacts: Optional[List[str]] = None,
    resume: bool = False,
    checkpoint: Optional[Dict[str, Any]] = None,
    on_progress: Optional[Any] = None,
    cancel_event: Optional[Any] = None,
    code_approval_callback: Optional[Any] = None,
) -> str:
    """Execute the prepared plan's step DAG via LangGraph."""
    # Local imports avoid circular import at module load and keep domain helpers
    # owned by plan_orchestrator.py.
    from .plan_orchestrator import (
        _build_approval_request,
        _build_final_summary,
        _step_failed,
    )

    max_replan = int(getattr(orchestrator, "max_replan_attempts", 8) or 8)
    steps_list = plan.get("steps") or []
    if not isinstance(steps_list, list) or not steps_list:
        message = "计划生成失败：未生成任何可执行步骤，任务尚未运行。"
        orchestrator._emit(on_progress, "plan_failed", plan=plan, message=message)
        raise ValueError(message)

    def pick_node(state: PlanStepState) -> Dict[str, Any]:
        orchestrator.agent._check_cancelled(cancel_event)
        current_plan = state["plan"]
        steps = current_plan.get("steps") or []
        step_total = len(steps) if isinstance(steps, list) else 0
        pending = orchestrator._next_pending_step(current_plan)

        if pending is None:
            graph = PlanGraph(current_plan)
            waiting = graph.first_with_status(STEP_AWAITING_APPROVAL)
            if waiting:
                prompt = _build_approval_request(
                    current_plan,
                    waiting,
                    history=history,
                    extra_context=extra_context,
                )
                orchestrator._persist_plan(current_plan)
                orchestrator._save_step_checkpoint(current_plan, None)
                orchestrator._emit(
                    on_progress,
                    "plan_approval_required",
                    plan=current_plan,
                    stepId=waiting.get("id"),
                    message=prompt,
                )
                orchestrator._emit(on_progress, "final", content=prompt)
                return {
                    "done": True,
                    "outcome": "approve",
                    "final_answer": prompt,
                    "step_total": step_total,
                }

            blocked = graph.blocked_pending(
                pending=STEP_PENDING,
                completed={STEP_DONE, STEP_SKIPPED},
            )
            if blocked:
                labels = "; ".join(
                    f"{step.get('title') or step.get('id') or '未命名步骤'}"
                    f" <- {', '.join(graph.unmet_dependencies(step, {STEP_DONE, STEP_SKIPPED})) or '未知依赖'}"
                    for step in blocked[:3]
                )
                message = f"计划被未完成或缺失的依赖阻塞，尚未执行：{labels}。"
                orchestrator._emit(on_progress, "plan_failed", plan=current_plan, message=message)
                orchestrator._emit(on_progress, "final", content=message)
                return {
                    "done": True,
                    "outcome": "blocked",
                    "final_answer": message,
                    "step_total": step_total,
                }

            summary = orchestrator._ensure_user_facing_reply(
                user_query,
                _build_final_summary(current_plan),
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            orchestrator.agent._clear_run_checkpoint(orchestrator.chat_id)
            terminal_event = (
                "plan_incomplete"
                if any(step.get("status") == STEP_FAILED for step in current_plan.get("steps") or [])
                else "plan_complete"
            )
            orchestrator._emit(on_progress, terminal_event, plan=current_plan, message=summary)
            orchestrator._emit(on_progress, "final", content=summary)
            return {
                "done": True,
                "outcome": "complete",
                "final_answer": summary,
                "step_total": step_total,
            }

        step_index = steps.index(pending) + 1
        if isinstance(pending.get("approval"), dict):
            pending["status"] = STEP_AWAITING_APPROVAL
            prompt = _build_approval_request(
                current_plan,
                pending,
                history=history,
                extra_context=extra_context,
            )
            orchestrator._persist_plan(current_plan)
            orchestrator._save_step_checkpoint(current_plan, None)
            orchestrator._emit(
                on_progress,
                "plan_approval_required",
                plan=current_plan,
                stepId=pending.get("id"),
                stepIndex=step_index,
                message=prompt,
            )
            orchestrator._emit(on_progress, "final", content=prompt)
            return {
                "plan": current_plan,
                "done": True,
                "outcome": "approve",
                "final_answer": prompt,
                "current_step_id": str(pending.get("id") or ""),
                "current_step_index": step_index,
                "step_total": step_total,
            }

        pending["status"] = STEP_RUNNING
        orchestrator._persist_plan(current_plan)
        orchestrator._emit(
            on_progress,
            "plan_step_start",
            plan=current_plan,
            stepId=pending.get("id"),
            stepIndex=step_index,
            stepTotal=step_total,
            title=pending.get("title"),
            message=f"正在执行步骤 {step_index}/{step_total}：{pending.get('title')}",
        )
        return {
            "plan": current_plan,
            "done": False,
            "outcome": "continue",
            "current_step_id": str(pending.get("id") or ""),
            "current_step_index": step_index,
            "step_total": step_total,
        }

    def execute_node(state: PlanStepState) -> Dict[str, Any]:
        current_plan = state["plan"]
        step_id = str(state.get("current_step_id") or "")
        step_index = int(state.get("current_step_index") or 0)
        step_total = int(state.get("step_total") or len(current_plan.get("steps") or []))
        pending = next(
            (
                step
                for step in (current_plan.get("steps") or [])
                if isinstance(step, dict) and str(step.get("id") or "") == step_id
            ),
            None,
        )
        if pending is None:
            return {
                "step_result": "STEP_EXECUTION_ERROR: pending step disappeared from plan",
                "outcome": "continue",
            }

        resume_messages: Optional[List[Dict[str, Any]]] = None
        if (
            resume
            and checkpoint
            and checkpoint.get("step_id") == pending.get("id")
            and isinstance(checkpoint.get("messages"), list)
            and checkpoint["messages"]
        ):
            resume_messages = checkpoint["messages"]

        try:
            result = orchestrator._execute_step(
                pending,
                step_index=step_index,
                step_total=step_total,
                plan_goal=str(current_plan.get("goal") or ""),
                user_query=execution_user_query,
                history=history,
                plan=current_plan,
                resume_messages=resume_messages,
                on_progress=on_progress,
                cancel_event=cancel_event,
                code_approval_callback=code_approval_callback,
            )
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "AgentCancelledError":
                raise
            result = f"STEP_EXECUTION_ERROR: {type(exc).__name__}: {exc}"

        return {"plan": current_plan, "step_result": result, "outcome": "continue"}

    def after_step_node(state: PlanStepState) -> Dict[str, Any]:
        current_plan = state["plan"]
        step_id = str(state.get("current_step_id") or "")
        step_index = int(state.get("current_step_index") or 0)
        step_total = int(state.get("step_total") or 0)
        result = str(state.get("step_result") or "")
        replan_attempts = int(state.get("replan_attempts") or 0)

        pending = next(
            (
                step
                for step in (current_plan.get("steps") or [])
                if isinstance(step, dict) and str(step.get("id") or "") == step_id
            ),
            None,
        )
        if pending is None:
            return {"done": False, "outcome": "continue", "plan": current_plan}

        if _step_failed(result):
            pending["status"] = STEP_FAILED
            pending["result"] = result
            orchestrator._persist_plan(current_plan)
            orchestrator._save_step_checkpoint(current_plan, None)
            orchestrator._emit(
                on_progress,
                "plan_step_failed",
                plan=current_plan,
                stepId=pending.get("id"),
                stepIndex=step_index,
                message=f"步骤 {step_index} 未在轮次上限内完成",
            )

            if replan_attempts >= max_replan:
                summary = orchestrator._ensure_user_facing_reply(
                    user_query,
                    _build_final_summary(current_plan),
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                )
                orchestrator._emit(on_progress, "plan_incomplete", plan=current_plan, message=summary)
                orchestrator._emit(on_progress, "final", content=summary)
                return {
                    "plan": current_plan,
                    "done": True,
                    "outcome": "failed_terminal",
                    "final_answer": summary,
                    "replan_attempts": replan_attempts,
                }

            replan_attempts += 1
            current_plan = orchestrator._replan(
                current_plan,
                user_query=user_query,
                extra_context=extra_context,
                available_artifacts=available_artifacts,
                failed_step=pending,
                failure_reason=result,
                history=history,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            orchestrator._persist_plan(current_plan)
            orchestrator._save_step_checkpoint(current_plan, None)
            orchestrator._emit(
                on_progress,
                "plan_revised",
                plan=current_plan,
                message=f"计划已更新（第 {current_plan.get('version')} 版）",
            )
            return {
                "plan": current_plan,
                "done": False,
                "outcome": "continue",
                "replan_attempts": replan_attempts,
                "step_total": len(current_plan.get("steps") or []),
                "current_step_id": "",
                "step_result": "",
            }

        pending["status"] = STEP_DONE
        pending["result"] = result
        orchestrator._persist_plan(current_plan)
        orchestrator._save_step_checkpoint(current_plan, None)
        orchestrator._emit(
            on_progress,
            "plan_step_done",
            plan=current_plan,
            stepId=pending.get("id"),
            stepIndex=step_index,
            result=result[:600] if result else "",
            message=f"步骤 {step_index}/{step_total} 完成",
        )
        return {
            "plan": current_plan,
            "done": False,
            "outcome": "continue",
            "current_step_id": "",
            "step_result": "",
        }

    def after_pick(state: PlanStepState) -> str:
        if state.get("done"):
            return "end"
        return "execute"

    def after_result(state: PlanStepState) -> str:
        if state.get("done"):
            return "end"
        return "pick"

    graph = StateGraph(PlanStepState)
    graph.add_node("pick", pick_node)
    graph.add_node("execute", execute_node)
    graph.add_node("after_step", after_step_node)
    graph.add_edge(START, "pick")
    graph.add_conditional_edges("pick", after_pick, {"execute": "execute", "end": END})
    graph.add_edge("execute", "after_step")
    graph.add_conditional_edges("after_step", after_result, {"pick": "pick", "end": END})
    app = graph.compile()

    if orchestrator._materialize_approval_followup(plan):
        orchestrator._persist_plan(plan)
        orchestrator._save_step_checkpoint(plan, None)

    final: PlanStepState = app.invoke(
        {
            "plan": plan,
            "history": list(history or []),
            "user_query": user_query,
            "execution_user_query": execution_user_query,
            "extra_context": extra_context,
            "available_artifacts": list(available_artifacts or []),
            "resume": resume,
            "replan_attempts": 0,
            "step_total": len(plan.get("steps") or []),
            "done": False,
            "outcome": "continue",
            "final_answer": "",
            "current_step_id": "",
            "step_result": "",
        }
    )
    return str(final.get("final_answer") or "")
