"""LangGraph ReAct-style tool loop for sRNAgent.

Replaces the hand-rolled ``for turn in range(max_turns)`` control flow with a
small state graph while keeping:

- existing LLM client (``_llm_complete_cancellable`` + AGENT_TOOL_SCHEMAS)
- ``dispatch_tool`` / Jupyter execution
- code approval, progress events, checkpoints, compaction, cancel
- finish / max-turns semantics

Toggle with ``SRNAGENT_USE_LC_TOOL_LOOP`` (default: on). Set to
``0`` / ``legacy`` to use the original ``_tool_loop`` body.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .context import bounded_tool_result, normalize_text_payload
from .hooks import get_default_registry
from .mcp_client import get_default_manager
from .tools import AGENT_TOOL_SCHEMAS

logger = logging.getLogger(__name__)

_ENV_FLAG = "SRNAGENT_USE_LC_TOOL_LOOP"


def lc_tool_loop_enabled() -> bool:
    raw = str(os.environ.get(_ENV_FLAG, "1")).strip().lower()
    return raw not in {"0", "false", "no", "off", "legacy"}


class ToolLoopState(TypedDict, total=False):
    messages: List[Dict[str, Any]]
    turn: int
    final_answer: str
    done: bool
    chat_id: str
    pending_tool_calls: List[Dict[str, Any]]
    assistant_content: str
    rounds_since_plan_update: int


def _summarize_tool_call(name: str, arguments: Dict[str, Any]) -> str:
    from .srn_agent import _summarize_tool_call as _sum

    return _sum(name, arguments)


def _truncate_result(text: str, limit: int = 600) -> str:
    from .srn_agent import _truncate_result as _tr

    return _tr(text, limit)


def _resolve_answer_text(completion: Any) -> str:
    from .srn_agent import _resolve_answer_text as _res

    return _res(completion)


def _audit_execute_code_policy(messages: List[Dict[str, Any]], arguments: Dict[str, Any]) -> str:
    from .srn_agent import _audit_execute_code_policy as _audit

    return _audit(messages, arguments) or ""


def _normalize_execute_code_args(arguments: Dict[str, Any]) -> Dict[str, Any]:
    raw_code = arguments.get("code")
    if isinstance(raw_code, dict):
        code = next(
            (
                value
                for key in ("$text", "text")
                if isinstance((value := raw_code.get(key)), str)
            ),
            "",
        )
    else:
        code = raw_code if isinstance(raw_code, str) else ""
    out = dict(arguments)
    if code:
        out["code"] = code
    return out


def run_lc_tool_loop(
    agent: Any,
    messages: List[Dict[str, Any]],
    *,
    on_progress: Optional[Any] = None,
    cancel_event: Optional[Any] = None,
    code_approval_callback: Optional[Any] = None,
    chat_id: str = "",
    checkpoint_extra: Optional[Dict[str, Any]] = None,
    goal_gate: Optional[Any] = None,
) -> str:
    """Run the LangGraph tool loop; return the final user-facing answer."""

    max_turns = int(getattr(agent, "max_turns", 200) or 200)
    hooks = get_default_registry()

    def _evaluate_stop(msgs: List[Dict[str, Any]], answer: str) -> Optional[str]:
        """Return non-None to force another turn (Stop hook or goal gate)."""
        # 1. User-registered Stop hooks
        force = hooks.trigger_stop(msgs, answer)
        if force is not None:
            return force
        # 2. Goal gate (s17 pattern)
        if goal_gate is not None and getattr(goal_gate, "active", False):
            llm_fn = getattr(agent, "_evaluator_complete", None)
            decision = goal_gate.evaluate(msgs, answer, llm_complete=llm_fn)
            if decision.action == "block":
                return goal_gate.block_message(decision)
            if decision.action == "impossible":
                return f"[Goal impossible]\n{decision.reason}\nExplain to the user and stop."
            if decision.action == "limit":
                return f"[Goal limit reached]\n{decision.reason}\nSummarize what was done and stop."
        # 3. Memory extraction (s09 pattern) — opt-in, only on genuine stop.
        if os.environ.get("SRNAGENT_MEMORY_EXTRACT", "").lower() in ("1", "true", "yes"):
            _extract_memories_on_stop(agent, msgs)
        return None

    def _extract_memories_on_stop(agent: Any, msgs: List[Dict[str, Any]]) -> None:
        """Extract durable memories at the stop boundary."""
        try:
            from .memory import extract_memories
            workspace = getattr(agent, "project_root", None) or Path.cwd()
            llm_fn = getattr(agent, "_evaluator_complete", None)
            extract_memories(msgs, Path(workspace), llm_complete=llm_fn)
        except Exception:  # noqa: BLE001
            pass

    def model_node(state: ToolLoopState) -> Dict[str, Any]:
        agent._check_cancelled(cancel_event)
        turn = int(state.get("turn") or 0)
        if turn >= max_turns:
            message = "Agent reached max turns without calling finish."
            agent._emit_progress(on_progress, "final", content=message)
            return {"done": True, "final_answer": message}

        msgs = agent._compact_context(
            list(state.get("messages") or []),
            on_progress=on_progress,
        )
        # Background task notification injection (s11 pattern).
        try:
            from .background import inject_background_notifications
            inject_background_notifications(msgs)
        except Exception:  # noqa: BLE001
            pass
        # Assemble dynamic tool pool: built-in + MCP (s14 pattern).
        mcp = get_default_manager()
        active_tools = AGENT_TOOL_SCHEMAS
        if mcp.list_servers():
            mcp_tools = []
            for server_name in mcp.list_servers():
                with mcp._lock:
                    client = mcp._servers.get(server_name)
                if client and client.connected:
                    mcp_tools.extend(client.tool_schemas())
            if mcp_tools:
                active_tools = list(AGENT_TOOL_SCHEMAS) + mcp_tools
        agent._emit_progress(on_progress, "status", message="正在请求 LLM…")
        completion = agent._llm_complete_cancellable(
            msgs,
            tools=active_tools,
            cancel_event=cancel_event,
            on_progress=on_progress,
            enable_thinking=False,
        )
        agent._check_cancelled(cancel_event)

        if not completion.tool_calls:
            answer = _resolve_answer_text(completion)
            if answer:
                answer = agent._ensure_user_facing_reply(
                    msgs,
                    answer,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                )
                # Stop hook / goal gate: force another turn if goal not met.
                force = _evaluate_stop(msgs, answer)
                if force is not None:
                    msgs.append({"role": "user", "content": force})
                    return {
                        "messages": msgs,
                        "turn": turn + 1,
                        "done": False,
                        "pending_tool_calls": [],
                        "rounds_since_plan_update": int(state.get("rounds_since_plan_update") or 0) + 1,
                    }
                agent._save_run_checkpoint(msgs, chat_id, checkpoint_extra)
                if checkpoint_extra is None:
                    agent._clear_run_checkpoint(chat_id)
                agent._emit_progress(
                    on_progress,
                    "final",
                    turn=turn + 1,
                    content=answer,
                )
                return {
                    "messages": msgs,
                    "turn": turn + 1,
                    "done": True,
                    "final_answer": answer,
                    "pending_tool_calls": [],
                }
            return {
                "messages": msgs,
                "turn": turn + 1,
                "done": True,
                "final_answer": "Agent stopped without a final response.",
                "pending_tool_calls": [],
            }

        thinking = str(completion.thinking or "").strip()
        visible = str(completion.content or "").strip()
        if thinking and thinking != visible:
            agent._emit_progress(
                on_progress,
                "thinking",
                turn=turn + 1,
                content=thinking,
            )

        pending: List[Dict[str, Any]] = []
        for call in completion.tool_calls:
            pending.append(
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": dict(call.arguments or {}),
                }
            )

        assistant_message: Dict[str, Any] = {
            "role": "assistant",
            "content": completion.content or None,
            "tool_calls": [
                {
                    "id": item["id"],
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "arguments": json.dumps(item["arguments"], ensure_ascii=False),
                    },
                }
                for item in pending
            ],
        }
        msgs.append(assistant_message)

        # finish short-circuits the loop (same as legacy tool-loop)
        for item in pending:
            if item["name"] != "finish":
                continue
            message = normalize_text_payload(item["arguments"].get("message")) or "Task completed."
            message = agent._ensure_user_facing_reply(
                msgs,
                message,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            # Goal gate: even on explicit finish, check if goal is met.
            force = _evaluate_stop(msgs, message)
            if force is not None:
                msgs.append({"role": "user", "content": force})
                agent._emit_progress(
                    on_progress,
                    "tool_call",
                    turn=turn + 1,
                    toolCallId=item["id"],
                    name="finish",
                    summary=_summarize_tool_call("finish", item["arguments"]),
                    arguments=item["arguments"],
                )
                return {
                    "messages": msgs,
                    "turn": turn + 1,
                    "done": False,
                    "pending_tool_calls": [],
                    "rounds_since_plan_update": int(state.get("rounds_since_plan_update") or 0) + 1,
                }
            agent._save_run_checkpoint(msgs, chat_id, checkpoint_extra)
            if checkpoint_extra is None:
                agent._clear_run_checkpoint(chat_id)
            agent._emit_progress(
                on_progress,
                "tool_call",
                turn=turn + 1,
                toolCallId=item["id"],
                name="finish",
                summary=_summarize_tool_call("finish", item["arguments"]),
                arguments=item["arguments"],
            )
            agent._emit_progress(
                on_progress,
                "final",
                turn=turn + 1,
                content=message,
            )
            return {
                "messages": msgs,
                "turn": turn + 1,
                "done": True,
                "final_answer": message,
                "pending_tool_calls": [],
            }

        return {
            "messages": msgs,
            "turn": turn + 1,
            "done": False,
            "pending_tool_calls": pending,
            "assistant_content": str(completion.content or ""),
        }

    def tools_node(state: ToolLoopState) -> Dict[str, Any]:
        msgs = list(state.get("messages") or [])
        turn = int(state.get("turn") or 1)
        pending = list(state.get("pending_tool_calls") or [])

        for item in pending:
            agent._check_cancelled(cancel_event)
            name = str(item.get("name") or "")
            arguments = dict(item.get("arguments") or {})
            call_id = str(item.get("id") or "")
            summary = _summarize_tool_call(name, arguments)
            agent._emit_progress(
                on_progress,
                "tool_call",
                turn=turn,
                toolCallId=call_id,
                name=name,
                summary=summary,
                arguments=arguments,
            )

            if name == "finish":
                # Should have been handled in model_node; treat as soft finish.
                message = normalize_text_payload(arguments.get("message")) or "Task completed."
                agent._emit_progress(on_progress, "final", turn=turn, content=message)
                return {
                    "messages": msgs,
                    "done": True,
                    "final_answer": message,
                    "pending_tool_calls": [],
                }

            # PreToolUse hook: non-None return blocks the tool (used as result).
            blocked = hooks.trigger_pre_tool_use(name, arguments, msgs)
            if blocked is not None:
                result = blocked
            elif name == "execute_code":
                tool_arguments = _normalize_execute_code_args(arguments)
                code = str(tool_arguments.get("code") or "")
                description = str(tool_arguments.get("description") or "")
                policy_violation = _audit_execute_code_policy(msgs, tool_arguments)
                if not code:
                    result = (
                        "TOOL_INPUT_ERROR: execute_code.code must be a non-empty string. "
                        "Call execute_code again with Python source in the code field."
                    )
                elif policy_violation:
                    result = policy_violation
                elif code_approval_callback is not None:
                    request_id = str(uuid.uuid4())
                    approved = code_approval_callback(request_id, code, description)
                    if not approved:
                        result = (
                            "User denied code execution. Explain what the code would do "
                            "and ask whether to try again."
                        )
                    elif on_progress is not None:
                        result = agent._run_execute_code_with_progress(
                            tool_arguments,
                            on_progress=on_progress,
                            cancel_event=cancel_event,
                            turn=turn,
                            summary=summary,
                            description=description,
                            tool_call_id=call_id,
                        )
                    else:
                        result = agent.dispatch_tool(name, tool_arguments)
                elif on_progress is not None:
                    result = agent._run_execute_code_with_progress(
                        tool_arguments,
                        on_progress=on_progress,
                        cancel_event=cancel_event,
                        turn=turn,
                        summary=summary,
                        description=description,
                        tool_call_id=call_id,
                    )
                else:
                    result = agent.dispatch_tool(name, tool_arguments)
            elif get_default_manager().is_mcp_tool(name):
                result = get_default_manager().call_mcp_tool(name, arguments)
                if result is None:
                    result = f"MCP tool '{name}' not found or server disconnected."
            else:
                result = agent.dispatch_tool(name, arguments)

            result = bounded_tool_result(result, agent.max_tool_result_chars)

            # PostToolUse hook (side-effects only: logging, output guards).
            hooks.trigger_post_tool_use(name, arguments, result, msgs)

            if name == "execute_code":
                agent._emit_progress(
                    on_progress,
                    "tool_result",
                    turn=turn,
                    toolCallId=call_id,
                    name=name,
                    summary=summary,
                    content=_truncate_result(result),
                    fullContent=result,
                )
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result,
                }
            )

        # Plan-step reminder: if 3+ turns passed without a plan update, nudge.
        rounds_since = int(state.get("rounds_since_plan_update") or 0) + 1
        if rounds_since >= 3:
            rounds_since = 0
            msgs.append({
                "role": "user",
                "content": (
                    "<reminder>You have not updated the plan in a few turns. "
                    "If you are working through a multi-step task, review your "
                    "current step status and update it before continuing.</reminder>"
                ),
            })

        agent._save_run_checkpoint(msgs, chat_id, checkpoint_extra)
        return {
            "messages": msgs,
            "pending_tool_calls": [],
            "done": False,
            "rounds_since_plan_update": rounds_since,
        }

    def after_model(state: ToolLoopState) -> str:
        if state.get("done"):
            return "end"
        if state.get("pending_tool_calls"):
            return "tools"
        return "end"

    def after_tools(state: ToolLoopState) -> str:
        if state.get("done"):
            return "end"
        return "model"

    graph = StateGraph(ToolLoopState)
    graph.add_node("model", model_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "model")
    graph.add_conditional_edges(
        "model",
        after_model,
        {"tools": "tools", "end": END},
    )
    graph.add_conditional_edges(
        "tools",
        after_tools,
        {"model": "model", "end": END},
    )
    app = graph.compile()

    final: ToolLoopState = app.invoke(
        {
            "messages": list(messages),
            "turn": 0,
            "done": False,
            "chat_id": chat_id,
            "pending_tool_calls": [],
            "final_answer": "",
            "rounds_since_plan_update": 0,
        }
    )
    answer = str(final.get("final_answer") or "").strip()
    if answer:
        return answer
    message = "Agent reached max turns without calling finish."
    agent._emit_progress(on_progress, "final", content=message)
    return message
