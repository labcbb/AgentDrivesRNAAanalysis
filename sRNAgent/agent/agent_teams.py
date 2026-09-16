"""Agent teams — sub-agent spawning for scoped sub-tasks (Claude Code s13).

The main agent can delegate a self-contained sub-task to a *sub-agent*: a
lightweight agent run that gets its own message list, a scoped system prompt,
a restricted tool set, and a tight turn budget.  The sub-agent runs its own
tool loop to completion and returns a single text summary back to the parent,
exactly like Claude Code's ``Task`` tool.

Design rules (mirroring Claude Code):

- Sub-agents share the parent's LLM client and execution backend (same kernel
  namespace), so code state is reusable but the conversation is isolated.
- Sub-agents CANNOT spawn further sub-agents — the ``spawn_subagent`` tool is
  withheld from their tool pool, preventing unbounded recursion.
- Sub-agents have a small turn budget (default 12) and a compact system prompt
  focused on the sub-task type.
- The sub-agent's final answer is bounded by ``bounded_tool_result`` before
  being returned to the parent as a normal tool result.

Sub-agent types:

- ``explore``  — read-only reconnaissance: search_functions, search_skills,
  execute_code (inspection only, no heavy compute).  Best for "find where the
  results are stored" or "what does function X do".
- ``analyze``  — read-only deeper analysis: same tools, more turns, for
  summarising existing adata / result files without rerunning anything.
- ``code``     — can run code (execute_code) to perform a concrete sub-task
  such as "trim adapters for SRR8479188 and report the path".
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .context import bounded_tool_result
from .tools import AGENT_TOOL_SCHEMAS

logger = logging.getLogger(__name__)

# Tool sets per sub-agent type.  ``spawn_subagent`` is never included.
_SUBAGENT_TOOLS = {
    "explore": ["search_functions", "search_skills", "execute_code", "finish"],
    "analyze": ["search_functions", "search_skills", "execute_code", "finish"],
    "code": ["search_functions", "search_skills", "execute_code", "manage_visualization", "finish"],
}

_SUBAGENT_MAX_TURNS = {
    "explore": 8,
    "analyze": 12,
    "code": 15,
}

_SUBAGENT_SYSTEM = {
    "explore": (
        "You are a read-only exploration sub-agent for sRNA-seq analysis. "
        "Investigate the workspace to answer the given question. "
        "Use search_functions / search_skills to find APIs, and execute_code "
        "ONLY to inspect existing state (list files, read adata.uns, check "
        "manifests). Do NOT run expensive analysis or modify anything. "
        "When done, call finish with a concise factual summary."
    ),
    "analyze": (
        "You are a read-only analysis sub-agent for sRNA-seq data. "
        "Inspect existing results (adata objects, h5ad files, result manifests, "
        "plots) and summarise what is present: sample names, group assignments, "
        "which analyses are complete, key numbers. Do NOT rerun any analysis. "
        "Call finish with a structured summary when done."
    ),
    "code": (
        "You are a code-execution sub-agent for sRNA-seq analysis. "
        "Perform the given concrete sub-task using execute_code with the "
        "sRNAgent namespace (`import sRNAgent as sa`). Prefer registered sa.* "
        "APIs discovered via search_functions. Persist outputs and report the "
        "exact file paths in your finish message."
    ),
}


def _filter_tool_schemas(allowed_names: List[str]) -> List[Dict[str, Any]]:
    """Return only the built-in tool schemas whose name is in ``allowed_names``."""
    return [
        schema
        for schema in AGENT_TOOL_SCHEMAS
        if schema.get("function", {}).get("name") in allowed_names
    ]


def spawn_subagent(
    parent_agent: Any,
    prompt: str,
    *,
    agent_type: str = "explore",
    max_turns: Optional[int] = None,
) -> str:
    """Run a sub-agent for a scoped sub-task; return its summary text.

    Parameters
    ----------
    parent_agent : SRNAgent
        The parent agent whose LLM client / execution backend are reused.
    prompt : str
        The self-contained sub-task description.
    agent_type : str
        One of ``explore``, ``analyze``, ``code``.
    max_turns : int, optional
        Override the default turn budget for this type.
    """
    agent_type = agent_type if agent_type in _SUBAGENT_TOOLS else "explore"
    system_text = _SUBAGENT_SYSTEM[agent_type]
    budget = int(max_turns or _SUBAGENT_MAX_TURNS[agent_type])

    # Build a scoped system prompt: sub-agent base + parent skill overview.
    skill_overview = getattr(parent_agent, "_subagent_skill_overview", "") or ""
    if skill_overview:
        system_content = f"{system_text}\n\n## Available skills\n{skill_overview}"
    else:
        system_content = system_text

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": prompt},
    ]

    allowed_tools = _filter_tool_schemas(_SUBAGENT_TOOLS[agent_type])
    summary = _run_subagent_loop(parent_agent, messages, allowed_tools, budget)
    return bounded_tool_result(summary, getattr(parent_agent, "max_tool_result_chars", 8000))


def spawn_subagents(
    parent_agent: Any,
    tasks: List[Dict[str, Any]],
) -> str:
    """Run multiple sub-agents CONCURRENTLY; return all summaries.

    Each task is ``{"prompt": str, "agent_type": str}``.  Sub-agents run in a
    thread pool and share the parent's execution backend, so code-execution
    tasks should be read-only or operate on disjoint state to avoid kernel
    races.  For independent reconnaissance this is safe and fast.

    Returns a single string with each sub-agent's summary under a numbered
    header, suitable to use as one tool result.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not tasks:
        return "[spawn_subagents] no tasks provided"

    def _run_one(index: int, task: Dict[str, Any]) -> tuple[int, str]:
        prompt = str(task.get("prompt") or "")
        agent_type = str(task.get("agent_type") or "explore")
        try:
            summary = spawn_subagent(parent_agent, prompt, agent_type=agent_type)
        except Exception as exc:  # noqa: BLE001
            summary = f"[sub-agent {index} error] {exc}"
        return index, summary

    results: Dict[int, str] = {}
    # Cap concurrency to avoid overwhelming the shared LLM/kernel.
    max_workers = min(len(tasks), 4)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_run_one, i, t) for i, t in enumerate(tasks)]
        for fut in as_completed(futures):
            idx, summary = fut.result()
            results[idx] = summary

    parts = []
    for idx in sorted(results):
        parts.append(f"### Sub-agent {idx}\n{results[idx]}")
    combined = "\n\n".join(parts)
    return bounded_tool_result(combined, getattr(parent_agent, "max_tool_result_chars", 8000))


def _run_subagent_loop(
    parent_agent: Any,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    max_turns: int,
) -> str:
    """A minimal ReAct loop reusing the parent's LLM + dispatch, no sub-spawning."""
    for turn in range(max_turns):
        try:
            completion = parent_agent.llm.complete(
                messages, tools=tools, enable_thinking=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sub-agent LLM call failed: %s", exc)
            return f"[sub-agent error] {exc}"

        if not completion.tool_calls:
            text = str(completion.content or "").strip()
            if text:
                return text
            # No tool calls and no text — treat as finish.
            return "[sub-agent returned no output]"

        # Record assistant message with tool calls.
        messages.append({
            "role": "assistant",
            "content": completion.content or None,
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in completion.tool_calls
            ],
        })

        for call in completion.tool_calls:
            name = str(call.name or "")
            try:
                import json
                arguments = json.loads(call.arguments) if isinstance(call.arguments, str) else dict(call.arguments or {})
            except (json.JSONDecodeError, TypeError):
                arguments = {}

            if name == "finish":
                return str(arguments.get("message") or "[sub-agent finished with no message]")

            # Dispatch via parent — shares execution backend / registries.
            try:
                result = parent_agent.dispatch_tool(name, arguments)
            except Exception as exc:  # noqa: BLE001
                result = f"[tool error] {exc}"

            result = bounded_tool_result(result, getattr(parent_agent, "max_tool_result_chars", 8000))
            messages.append({
                "role": "tool",
                "tool_call_id": str(call.id),
                "name": name,
                "content": result,
            })

    return "[sub-agent reached max turns without finishing]"
