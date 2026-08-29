"""LangChain Tool adapters over the existing function + skill registries.

The Tools/* APIs and skills/*/SKILL.md layers are unchanged. These wrappers only
expose the agent-facing capabilities that the legacy tool-loop already uses:
search_functions, search_skills, execute_code, manage_visualization, finish.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, List, Optional, TYPE_CHECKING

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .tools import execute_code, manage_visualization, search_functions, search_skills

if TYPE_CHECKING:
    from .execution import ExecutionBackend
    from .srn_agent import SRNAgent
    from .task_supervisor import TaskProgressSupervisor
    from ..skill_registry import SkillRegistry


class _SearchQuery(BaseModel):
    query: str = Field(..., description="Natural-language search query")


class _ExecuteCodeArgs(BaseModel):
    code: str = Field(..., description="Python source to run in the sRNAgent session")
    description: str = Field(..., description="Short description of what the code does")


class _ManageVisualizationArgs(BaseModel):
    action: str = Field(..., description="show | hide | list | clear_hidden")
    paths: List[str] = Field(
        default_factory=list,
        description="Workspace plot paths or filenames under results/plots/",
    )


class _FinishArgs(BaseModel):
    message: str = Field(..., description="Final user-facing reply")


def build_langchain_tools(
    *,
    function_registry: Any,
    skill_registry: Optional["SkillRegistry"],
    project_root: Path,
    execution_backend: Optional["ExecutionBackend"] = None,
    agent: Optional["SRNAgent"] = None,
    on_stream: Optional[Callable[[str, str], None]] = None,
    supervisor: Optional["TaskProgressSupervisor"] = None,
) -> List[StructuredTool]:
    """Build LangChain tools that delegate to the existing registries / executor."""

    def _search_functions(query: str) -> str:
        if agent is not None:
            return agent.dispatch_tool("search_functions", {"query": query})
        return search_functions(function_registry, query)

    def _search_skills(query: str) -> str:
        if agent is not None:
            return agent.dispatch_tool("search_skills", {"query": query})
        return search_skills(skill_registry, query)

    def _execute_code(code: str, description: str) -> str:
        # description is part of the schema for the LLM; execution ignores it.
        _ = description
        if agent is not None:
            return agent.dispatch_tool(
                "execute_code",
                {"code": code, "description": description},
                on_stream=on_stream,
                supervisor=supervisor,
            )
        return execute_code(
            code,
            project_root,
            execution_backend=execution_backend,
            on_stream=on_stream,
            supervisor=supervisor,
        )

    def _manage_visualization(action: str, paths: Optional[List[str]] = None) -> str:
        path_list = paths or []
        if agent is not None:
            return agent.dispatch_tool(
                "manage_visualization",
                {"action": action, "paths": path_list},
            )
        return manage_visualization(
            action,
            path_list,
            chat_id=str(getattr(agent, "active_chat_id", "") or ""),
        )

    def _finish(message: str) -> str:
        if agent is not None:
            return agent.dispatch_tool("finish", {"message": message})
        return message

    return [
        StructuredTool.from_function(
            name="search_functions",
            description=(
                "Search the sRNAgent function registry. Returns signatures, "
                "descriptions, and examples. Call before writing code."
            ),
            func=_search_functions,
            args_schema=_SearchQuery,
        ),
        StructuredTool.from_function(
            name="search_skills",
            description=(
                "Search installed sRNA-seq workflow skills (SKILL.md guides). "
                "Use for multi-step pipelines like FASTQ download."
            ),
            func=_search_skills,
            args_schema=_SearchQuery,
        ),
        StructuredTool.from_function(
            name="execute_code",
            description=(
                "Execute Python in the active sRNAgent execution session. "
                "Namespace includes `import sRNAgent as sa`. "
                "Prefer sa.fastq.* functions discovered via search_functions."
            ),
            func=_execute_code,
            args_schema=_ExecuteCodeArgs,
        ),
        StructuredTool.from_function(
            name="manage_visualization",
            description=(
                "Control the UI Visualization board. action=show|hide|list|clear_hidden. "
                "Use paths under results/plots/ when the user asks to display or close plots."
            ),
            func=_manage_visualization,
            args_schema=_ManageVisualizationArgs,
        ),
        StructuredTool.from_function(
            name="finish",
            description=(
                "Send your final reply directly to the user in chat. "
                "Write as if talking to the user — not an internal status report."
            ),
            func=_finish,
            args_schema=_FinishArgs,
        ),
    ]
