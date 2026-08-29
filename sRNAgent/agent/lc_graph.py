"""LangGraph orchestrator for sRNAgent.

Phase-1 design (keep domain logic, swap control flow):
- IntentRouter (existing, rule-based) decides the branch.
- ANSWER  → legacy SRNAgent.run_with_history (tool-loop + approvals intact)
- NEW / CONTINUE / AMEND → legacy PlanOrchestrator via run_planned
- Function registry + skill registry stay untouched
- UI / Jupyter / session store stay outside this graph

The graph is the single routing surface that UI should call. Heavy planning
and step execution remain in plan_orchestrator.py on purpose.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .intent_router import IntentRouter, RouteDecision, RouteIntent

logger = logging.getLogger(__name__)

# Default on: UI goes through LangGraph routing. Set to 0/false/legacy to bypass.
_ENV_FLAG = "SRNAGENT_USE_LANGGRAPH"


def langgraph_orchestrator_enabled() -> bool:
    raw = str(os.environ.get(_ENV_FLAG, "1")).strip().lower()
    return raw not in {"0", "false", "no", "off", "legacy"}


class OrchestratorState(TypedDict, total=False):
    """Mutable graph state for one user turn."""

    history: List[Dict[str, str]]
    chat_id: str
    user_message: str
    extra_context: str
    available_artifacts: List[str]
    resume: bool
    route_intent: str
    route_reason: str
    use_plan_mode: bool
    answer_without_plan: bool
    answer: str
    error: str


def _latest_user_text(history: List[Dict[str, str]], fallback: str = "") -> str:
    for item in reversed(history or []):
        if str(item.get("role") or "") == "user":
            text = str(item.get("content") or "").strip()
            if text:
                return text
    return str(fallback or "").strip()


def build_srnagent_graph(agent: Any, *, callbacks: Optional[Dict[str, Any]] = None):
    """Compile the top-level LangGraph app bound to one SRNAgent instance.

    ``callbacks`` may include:
      on_progress, cancel_event, code_approval_callback, save_plan, load_plan
    """
    callbacks = callbacks or {}

    def route_node(state: OrchestratorState) -> Dict[str, Any]:
        history = list(state.get("history") or [])
        message = _latest_user_text(history, state.get("user_message") or "")
        active_plan = None
        load_plan = callbacks.get("load_plan")
        chat_id = str(state.get("chat_id") or "")
        if callable(load_plan) and chat_id:
            try:
                active_plan = load_plan(chat_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("load_plan failed in route_node: %s", exc)

        decision: RouteDecision = IntentRouter.route(
            message,
            active_plan=active_plan,
            explicit_resume=bool(state.get("resume")),
        )
        use_plan_mode = bool(state.get("use_plan_mode", True))
        answer_without_plan = decision.intent == RouteIntent.ANSWER
        # When plan mode is disabled, always take the answer tool-loop.
        if not use_plan_mode:
            answer_without_plan = True
        return {
            "user_message": message,
            "route_intent": decision.intent.value,
            "route_reason": decision.reason,
            "answer_without_plan": answer_without_plan,
        }

    def answer_node(state: OrchestratorState) -> Dict[str, Any]:
        text = agent.run_with_history(
            list(state.get("history") or []),
            on_progress=callbacks.get("on_progress"),
            cancel_event=callbacks.get("cancel_event"),
            code_approval_callback=callbacks.get("code_approval_callback"),
            chat_id=str(state.get("chat_id") or ""),
            resume=bool(state.get("resume")),
            extra_context=str(state.get("extra_context") or ""),
        )
        return {"answer": text}

    def plan_node(state: OrchestratorState) -> Dict[str, Any]:
        text = agent.run_planned(
            list(state.get("history") or []),
            extra_context=str(state.get("extra_context") or ""),
            available_artifacts=list(state.get("available_artifacts") or []),
            chat_id=str(state.get("chat_id") or ""),
            save_plan=callbacks.get("save_plan"),
            load_plan=callbacks.get("load_plan"),
            resume=bool(state.get("resume"))
            or state.get("route_intent")
            in {RouteIntent.CONTINUE.value, RouteIntent.AMEND_PLAN.value},
            route_intent=str(state.get("route_intent") or ""),
            on_progress=callbacks.get("on_progress"),
            cancel_event=callbacks.get("cancel_event"),
            code_approval_callback=callbacks.get("code_approval_callback"),
        )
        return {"answer": text}

    def select_branch(state: OrchestratorState) -> str:
        if state.get("answer_without_plan"):
            return "answer"
        return "plan"

    graph = StateGraph(OrchestratorState)
    graph.add_node("route", route_node)
    graph.add_node("answer", answer_node)
    graph.add_node("plan", plan_node)
    graph.add_edge(START, "route")
    graph.add_conditional_edges(
        "route",
        select_branch,
        {"answer": "answer", "plan": "plan"},
    )
    graph.add_edge("answer", END)
    graph.add_edge("plan", END)
    return graph.compile()


def run_langgraph_turn(
    agent: Any,
    history: List[Dict[str, str]],
    *,
    chat_id: str = "",
    extra_context: str = "",
    available_artifacts: Optional[List[str]] = None,
    resume: bool = False,
    use_plan_mode: bool = True,
    on_progress: Any = None,
    cancel_event: Any = None,
    code_approval_callback: Any = None,
    save_plan: Any = None,
    load_plan: Any = None,
) -> Dict[str, Any]:
    """Execute one user turn through the LangGraph orchestrator.

    Returns dict with keys: answer, route_intent, route_reason, backend.
    """
    app = build_srnagent_graph(
        agent,
        callbacks={
            "on_progress": on_progress,
            "cancel_event": cancel_event,
            "code_approval_callback": code_approval_callback,
            "save_plan": save_plan,
            "load_plan": load_plan,
        },
    )
    final: OrchestratorState = app.invoke(
        {
            "history": list(history or []),
            "chat_id": chat_id,
            "extra_context": extra_context,
            "available_artifacts": list(available_artifacts or []),
            "resume": resume,
            "use_plan_mode": use_plan_mode,
        }
    )
    return {
        "answer": str(final.get("answer") or ""),
        "route_intent": str(final.get("route_intent") or ""),
        "route_reason": str(final.get("route_reason") or ""),
        "backend": "langgraph",
    }
