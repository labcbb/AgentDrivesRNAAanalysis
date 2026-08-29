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
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .context import bounded_tool_result, normalize_text_payload
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
) -> str:
    """Run the LangGraph tool loop; return the final user-facing answer."""

    max_turns = int(getattr(agent, "max_turns", 100) or 100)

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
        agent._emit_progress(on_progress, "status", message="正在请求 LLM…")
        completion = agent._llm_complete_cancellable(
            msgs,
            tools=AGENT_TOOL_SCHEMAS,
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

            if name == "execute_code":
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
            else:
                result = agent.dispatch_tool(name, arguments)

            result = bounded_tool_result(result, agent.max_tool_result_chars)
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

        agent._save_run_checkpoint(msgs, chat_id, checkpoint_extra)
        return {
            "messages": msgs,
            "pending_tool_calls": [],
            "done": False,
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
        }
    )
    answer = str(final.get("final_answer") or "").strip()
    if answer:
        return answer
    message = "Agent reached max turns without calling finish."
    agent._emit_progress(on_progress, "final", content=message)
    return message
