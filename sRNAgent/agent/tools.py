"""Agent tool handlers wired to sRNAgent function + skill registries."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .execution import ExecutionBackend, execute_agent_code
from ..skill_registry import SkillRegistry


def search_functions(function_registry: Any, query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "Please provide a non-empty function search query."

    matches = function_registry.find(query)
    if not matches:
        return f"No functions found matching '{query}'. Try broader keywords."

    lines: List[str] = [f"Found {len(matches)} match(es) for '{query}':\n"]
    seen: set[str] = set()
    for entry in matches[:15]:
        full_name = entry.get("full_name", "")
        if full_name in seen:
            continue
        seen.add(full_name)
        sig = entry.get("signature", "")
        desc = (entry.get("description") or "")[:400]
        lines.append(f"  {full_name}{sig}")
        lines.append(f"    {desc}")
        examples = entry.get("examples") or []
        if examples:
            lines.append(f"    Example: {examples[0]}")
        lines.append("")
    return "\n".join(lines).strip()


def search_skills(skill_registry: Optional[SkillRegistry], query: str) -> str:
    if not skill_registry or not skill_registry.skill_metadata:
        return "No domain skills available."

    query_lower = query.lower()
    scored: List[tuple[Any, int]] = []
    for meta in skill_registry.skill_metadata.values():
        searchable = f"{meta.name} {meta.description} {meta.slug}".lower()
        score = sum(1 for word in query_lower.split() if word in searchable)
        if score > 0:
            scored.append((meta, score))

    scored.sort(key=lambda item: item[1], reverse=True)

    if not scored:
        slugs = ", ".join(m.slug for m in skill_registry.skill_metadata.values())
        return f"No skills matched '{query}'. Available skills: {slugs}"

    results: List[str] = []
    for meta, _ in scored[:2]:
        full_skill = skill_registry.load_full_skill(meta.slug)
        if full_skill:
            body = full_skill.prompt_instructions(max_chars=4000)
            results.append(f"=== {full_skill.name} ===\n{body}")

    if not results:
        return "Skills matched but content could not be loaded."
    return "\n\n".join(results)


def list_available_skills(skill_registry: Optional[SkillRegistry]) -> str:
    if not skill_registry or not skill_registry.skill_metadata:
        return "No skills registered."
    lines = ["Available skills:"]
    for meta in sorted(skill_registry.skill_metadata.values(), key=lambda m: m.slug):
        lines.append(f"  - {meta.slug}: {meta.description}")
    return "\n".join(lines)


def execute_code(
    code: str,
    project_root: Path,
    execution_backend: Optional[ExecutionBackend] = None,
    on_stream: Optional[Callable[[str, str], None]] = None,
) -> str:
    if execution_backend is None:
        from .execution import initialize_execution_backend

        execution_backend = initialize_execution_backend(project_root=project_root)
    return execute_agent_code(execution_backend, code, project_root, on_stream=on_stream)


AGENT_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_functions",
            "description": (
                "Search the sRNAgent function registry. Returns signatures, "
                "descriptions, and examples. Call before writing code."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_skills",
            "description": (
                "Search installed sRNA-seq workflow skills (SKILL.md guides). "
                "Use for multi-step pipelines like FASTQ download."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_code",
            "description": (
                "Execute Python in the active Jupyter kernel for the sRNAgent conda env. "
                "Namespace includes `import sRNAgent as sa`. "
                "Prefer sa.fastq.* functions discovered via search_functions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["code", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Send your final reply directly to the user in chat. "
                "Write as if talking to the user — not an internal status report."
            ),
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    },
]
