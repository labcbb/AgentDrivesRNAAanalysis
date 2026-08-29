"""Facade: run one agent turn via LangGraph (default) or the legacy if/else path."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .lc_graph import langgraph_orchestrator_enabled, run_langgraph_turn


def run_agent_turn(
    agent: Any,
    history: List[Dict[str, str]],
    *,
    chat_id: str = "",
    extra_context: str = "",
    available_artifacts: Optional[List[str]] = None,
    resume: bool = False,
    use_plan_mode: bool = True,
    answer_without_resuming_plan: bool = False,
    route_intent: str = "",
    on_progress: Any = None,
    cancel_event: Any = None,
    code_approval_callback: Any = None,
    save_plan: Any = None,
    load_plan: Any = None,
) -> Dict[str, Any]:
    """Unified entry used by the UI bridge.

    When LangGraph is enabled (default), routing lives in ``lc_graph``.
    When disabled, preserve the previous answer-vs-plan branching.
    """
    if langgraph_orchestrator_enabled():
        # Mirror legacy "answer without plan" by forcing plan mode off for this turn.
        effective_plan_mode = bool(use_plan_mode) and not bool(answer_without_resuming_plan)
        return run_langgraph_turn(
            agent,
            history,
            chat_id=chat_id,
            extra_context=extra_context,
            available_artifacts=available_artifacts,
            resume=resume,
            use_plan_mode=effective_plan_mode,
            on_progress=on_progress,
            cancel_event=cancel_event,
            code_approval_callback=code_approval_callback,
            save_plan=save_plan,
            load_plan=load_plan,
        )

    if use_plan_mode and not answer_without_resuming_plan:
        text = agent.run_planned(
            history,
            extra_context=extra_context,
            available_artifacts=available_artifacts,
            chat_id=chat_id,
            save_plan=save_plan,
            load_plan=load_plan,
            resume=resume,
            route_intent=route_intent,
            on_progress=on_progress,
            cancel_event=cancel_event,
            code_approval_callback=code_approval_callback,
        )
        backend = "legacy_plan"
    else:
        text = agent.run_with_history(
            history,
            on_progress=on_progress,
            cancel_event=cancel_event,
            code_approval_callback=code_approval_callback,
            chat_id=chat_id,
            resume=resume,
            extra_context=extra_context,
        )
        backend = "legacy_answer"
    return {
        "answer": text,
        "route_intent": route_intent,
        "route_reason": "",
        "backend": backend,
    }
