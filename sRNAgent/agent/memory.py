"""Durable memory pipeline — extract / consolidate / select (Claude Code s09).

Three subsystems that give the agent cross-session memory:

- **Selection**: at the start of a turn, pick the most relevant stored
  memories for the current user query and inject them into the system prompt.
- **Extraction**: at the end of a turn (stop boundary), ask the LLM to
  extract durable knowledge from the conversation (file paths, parameters,
  user decisions, analysis results).
- **Consolidation**: when the memory store exceeds a threshold, ask the LLM
  to merge and de-duplicate memories into a compact set.

Memories are stored as individual ``.md`` files under ``.memory/`` in the
workspace root, following the Claude Code pattern.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_MEMORIES = 30
_SELECT_TOP_K = 5
_MEMORY_DIR_NAME = ".memory"
_EXTRACTION_SYSTEM = (
    "You are a memory extractor. Read the conversation and extract durable, "
    "reusable knowledge that would help a future session: file paths, analysis "
    "parameters, user preferences, confirmed results, dataset accessions, "
    "adapter sequences, group assignments. Do NOT extract ephemeral status. "
    "Output one memory per line, each starting with a category tag:\n"
    "  [path] <description>\n"
    "  [param] <description>\n"
    "  [decision] <description>\n"
    "  [result] <description>\n"
    "  [preference] <description>\n"
    "If nothing durable, output exactly: NONE"
)
_CONSOLIDATION_SYSTEM = (
    "You are a memory consolidator. Given a list of memories, merge duplicates "
    "and remove outdated or contradictory entries. Keep at most 30 entries. "
    "Preserve the category tags. Output the consolidated list, one per line."
)


def memory_dir(workspace: Path) -> Path:
    return Path(workspace) / _MEMORY_DIR_NAME


def load_all_memories(workspace: Path) -> List[Dict[str, str]]:
    """Load all memories as a list of {file, content} dicts."""
    mdir = memory_dir(workspace)
    if not mdir.is_dir():
        return []
    memories = []
    for path in sorted(mdir.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8").strip()
            if content:
                memories.append({"file": path.name, "content": content})
        except OSError:
            continue
    return memories


def select_relevant_memories(
    query: str,
    workspace: Path,
    *,
    llm_complete: Optional[Any] = None,
    top_k: int = _SELECT_TOP_K,
) -> str:
    """Select up to ``top_k`` memories relevant to ``query``.

    Uses LLM selection if available, falls back to keyword matching.
    Returns a formatted block for system prompt injection (empty if none).
    """
    all_memories = load_all_memories(workspace)
    if not all_memories:
        return ""

    # LLM-based selection
    if llm_complete is not None:
        if len(all_memories) <= top_k:
            return _format_memories(all_memories)
        try:
            catalog = "\n".join(
                f"{i}: {m['content'][:200]}" for i, m in enumerate(all_memories)
            )
            system = (
                f"Select up to {top_k} memory indices most relevant to this query. "
                f"Output only comma-separated indices.\n\nMemories:\n{catalog}"
            )
            raw = llm_complete(
                [{"role": "user", "content": f"Query: {query}"}],
                system=system,
            )
            indices = _parse_indices(raw, len(all_memories))
            if indices:
                selected = [all_memories[i] for i in indices[:top_k]]
                return _format_memories(selected)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Memory LLM selection failed: %s", exc)

    # Keyword fallback (whole-word matching to avoid substring false positives)
    query_lower = str(query or "").lower()
    query_words = set(re.findall(r"\w+", query_lower))
    scored = []
    for mem in all_memories:
        content_lower = mem["content"].lower()
        content_words = set(re.findall(r"\w+", content_lower))
        score = len(query_words & content_words)
        scored.append((score, mem))
    scored.sort(key=lambda x: -x[0])
    selected = [m for s, m in scored[:top_k] if s > 0]
    return _format_memories(selected) if selected else ""


def extract_memories(
    messages: List[Dict[str, Any]],
    workspace: Path,
    *,
    llm_complete: Optional[Any] = None,
) -> int:
    """Extract durable memories from a conversation. Returns count saved."""
    if llm_complete is None:
        return 0
    mdir = memory_dir(workspace)
    mdir.mkdir(parents=True, exist_ok=True)

    transcript = _build_transcript(messages, max_chars=8000)
    if not transcript.strip():
        return 0

    try:
        raw = llm_complete(
            [{"role": "user", "content": transcript}],
            system=_EXTRACTION_SYSTEM,
        )
        lines = str(raw or "").strip().splitlines()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Memory extraction failed: %s", exc)
        return 0

    if not lines or lines[0].strip().upper() == "NONE":
        return 0

    saved = 0
    import time
    for line in lines:
        line = line.strip()
        if not line or line.upper() == "NONE":
            continue
        # Parse category tag
        match = re.match(r"^\[(\w+)\]\s*(.+)", line)
        category = match.group(1) if match else "general"
        content = match.group(2) if match else line
        if not content:
            continue
        filename = f"{int(time.time() * 1000)}_{category}_{saved:03d}.md"
        (mdir / filename).write_text(f"[{category}] {content}\n", encoding="utf-8")
        saved += 1

    if saved > 0:
        consolidate_memories(workspace, llm_complete=llm_complete)
    return saved


def consolidate_memories(
    workspace: Path,
    *,
    llm_complete: Optional[Any] = None,
    max_memories: int = _MAX_MEMORIES,
) -> int:
    """Merge and de-duplicate memories when count exceeds threshold."""
    all_memories = load_all_memories(workspace)
    if len(all_memories) <= max_memories:
        return len(all_memories)

    if llm_complete is None:
        # Fallback: keep the most recent max_memories
        mdir = memory_dir(workspace)
        for mem in all_memories[:-max_memories]:
            try:
                (mdir / mem["file"]).unlink()
            except OSError:
                pass
        return max_memories

    catalog = "\n".join(m["content"] for m in all_memories)
    try:
        raw = llm_complete(
            [{"role": "user", "content": catalog}],
            system=_CONSOLIDATION_SYSTEM,
        )
        lines = [l.strip() for l in str(raw or "").splitlines() if l.strip()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Memory consolidation failed: %s", exc)
        return len(all_memories)

    # Replace all files with consolidated set
    mdir = memory_dir(workspace)
    for mem in all_memories:
        try:
            (mdir / mem["file"]).unlink()
        except OSError:
            pass

    import time
    for i, line in enumerate(lines[:max_memories]):
        match = re.match(r"^\[(\w+)\]\s*(.+)", line)
        category = match.group(1) if match else "general"
        content = match.group(2) if match else line
        filename = f"{int(time.time() * 1000)}_{category}_{i:03d}.md"
        (mdir / filename).write_text(f"[{category}] {content}\n", encoding="utf-8")

    return min(len(lines), max_memories)


def _format_memories(memories: List[Dict[str, str]]) -> str:
    if not memories:
        return ""
    lines = ["## Relevant memories from previous sessions"]
    for mem in memories:
        lines.append(f"- {mem['content']}")
    return "\n".join(lines)


def _parse_indices(raw: str, max_index: int) -> List[int]:
    result = []
    for token in re.findall(r"\d+", str(raw or "")):
        idx = int(token)
        if 0 <= idx < max_index:
            result.append(idx)
    return result


def _build_transcript(messages: List[Dict[str, Any]], *, max_chars: int = 8000) -> str:
    lines: List[str] = []
    total = 0
    for msg in messages:
        role = str(msg.get("role") or "").strip()
        if role not in {"user", "assistant", "tool"}:
            continue
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
            content = " ".join(parts)
        else:
            content = str(content or "")
        content = content.strip()
        if not content:
            continue
        line = f"{role}: {content[:500]}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)
