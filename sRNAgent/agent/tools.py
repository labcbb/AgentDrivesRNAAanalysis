"""Agent tool handlers wired to sRNAgent function + skill registries."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .execution import ExecutionBackend, execute_agent_code
from ..skill_registry import SkillDefinition, SkillMetadata, SkillRegistry

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)


def _tokenize(text: str) -> List[str]:
    return [token.lower() for token in _TOKEN_RE.findall(str(text or "")) if token.strip()]


def rank_skill_matches(
    skill_registry: Optional[SkillRegistry],
    query: str,
) -> List[Tuple[SkillMetadata, int]]:
    if not skill_registry or not skill_registry.skill_metadata:
        return []
    raw_query = str(query or "").strip()
    if not raw_query:
        return []

    query_lower = raw_query.lower()
    query_tokens = _tokenize(raw_query)
    scored: List[Tuple[SkillMetadata, int]] = []

    for meta in skill_registry.skill_metadata.values():
        score = 0
        slug = meta.slug.lower()
        name = meta.name.lower()
        description = meta.description.lower()
        searchable = f"{slug} {name} {description}"
        meta_tokens = set(_tokenize(searchable))

        if query_lower == slug:
            score += 120
        if query_lower == name:
            score += 100
        if slug.startswith(query_lower):
            score += 60
        if query_lower and query_lower in searchable:
            score += 35

        for token in query_tokens:
            if token == slug:
                score += 40
            elif token in meta_tokens:
                score += 18
            elif token in searchable:
                score += 10

        if score > 0:
            scored.append((meta, score))

    scored.sort(key=lambda item: (-item[1], len(item[0].slug), item[0].slug))
    return scored


def resolve_skill_query(
    skill_registry: Optional[SkillRegistry],
    query: str,
) -> Optional[SkillDefinition]:
    matches = rank_skill_matches(skill_registry, query)
    if not matches:
        return None
    return skill_registry.load_full_skill(matches[0][0].slug) if skill_registry else None


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

    scored = rank_skill_matches(skill_registry, query)

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
                "Execute Python in the active sRNAgent execution session. "
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
