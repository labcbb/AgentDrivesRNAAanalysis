"""Read-only skill access for the supervisor agent (smart approval gates)."""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

UI_ROOT = Path(__file__).resolve().parent
SRNAGENT_PROJECT = UI_ROOT.parent
SRNAGENT_SKILLS_ROOT = SRNAGENT_PROJECT / "sRNAgent" / "skills"

from work_space import get_work_space  # noqa: E402

logger = logging.getLogger(__name__)

_CACHE_LOCK = threading.RLock()
_SKILL_FILES: Optional[Dict[str, Path]] = None
_SKILL_FILES_SNAPSHOT: Tuple[Tuple[str, float], ...] = ()
_SKILL_BODY_CACHE: Dict[str, Tuple[float, str]] = {}
_DYNAMIC_TRIGGERS: List[Tuple[re.Pattern[str], str]] = []
_DYNAMIC_TRIGGERS_SNAPSHOT: Tuple[Tuple[str, float], ...] = ()

_API_PATTERN = re.compile(r"\bsa\.[a-z_]+\.[a-z_]+\b", re.I)

_CONFIRMATION_MARKERS = (
    "agent 行动要求",
    "必须先",
    "必须让用户确认",
    "必须提醒用户确认",
    "用户确认后再",
    "必须先问用户",
    "必须先让用户确认",
    "除非用户明确要求",
)


def _skill_roots() -> List[Path]:
    return [
        SRNAGENT_SKILLS_ROOT,
        get_work_space() / "skills",
        get_work_space() / ".claude" / "skills",
    ]


def _snapshot_skill_files() -> Tuple[Tuple[str, float], ...]:
    entries: List[Tuple[str, float]] = []
    for root in _skill_roots():
        if not root.exists():
            continue
        for skill_file in sorted(root.glob("*/SKILL.md")):
            try:
                mtime = skill_file.stat().st_mtime
            except OSError:
                continue
            entries.append((str(skill_file.resolve()), mtime))
    return tuple(sorted(entries))


def _discover_skill_files(*, force: bool = False) -> Dict[str, Path]:
    global _SKILL_FILES, _SKILL_FILES_SNAPSHOT

    snapshot = _snapshot_skill_files()
    if not force and _SKILL_FILES is not None and snapshot == _SKILL_FILES_SNAPSHOT:
        return _SKILL_FILES

    with _CACHE_LOCK:
        snapshot = _snapshot_skill_files()
        if not force and _SKILL_FILES is not None and snapshot == _SKILL_FILES_SNAPSHOT:
            return _SKILL_FILES

        discovered: Dict[str, Path] = {}
        for root in _skill_roots():
            if not root.exists():
                continue
            for skill_file in sorted(root.glob("*/SKILL.md")):
                slug = skill_file.parent.name.lower()
                discovered[slug] = skill_file
        _SKILL_FILES = discovered
        _SKILL_FILES_SNAPSHOT = snapshot
        return discovered


def _parse_skill_body(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            closing_index = lines.index("---", 1)
            return "\n".join(lines[closing_index + 1 :]).strip()
        except ValueError:
            return text
    return text


def _read_skill_body(slug: str, *, force: bool = False) -> str:
    key = slug.lower()
    skill_files = _discover_skill_files(force=force)
    skill_path = skill_files.get(key)
    if not skill_path:
        return ""

    try:
        mtime = skill_path.stat().st_mtime
    except OSError as exc:
        logger.warning("unable to stat skill %s: %s", skill_path, exc)
        return ""

    cached = _SKILL_BODY_CACHE.get(key)
    if not force and cached and cached[0] == mtime:
        return cached[1]

    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("unable to read skill %s: %s", skill_path, exc)
        return ""

    body = _parse_skill_body(text)
    _SKILL_BODY_CACHE[key] = (mtime, body)
    return body


def _skill_has_confirmation_gates(body: str) -> bool:
    lowered = (body or "").lower()
    return any(marker in lowered for marker in _CONFIRMATION_MARKERS)


def _extract_apis_from_skill(body: str) -> List[str]:
    apis = sorted({match.lower() for match in _API_PATTERN.findall(body or "")})
    return apis


def _rebuild_dynamic_triggers_if_needed(*, force: bool = False) -> List[Tuple[re.Pattern[str], str]]:
    global _DYNAMIC_TRIGGERS, _DYNAMIC_TRIGGERS_SNAPSHOT

    snapshot = _snapshot_skill_files()
    if not force and _DYNAMIC_TRIGGERS and snapshot == _DYNAMIC_TRIGGERS_SNAPSHOT:
        return _DYNAMIC_TRIGGERS

    with _CACHE_LOCK:
        snapshot = _snapshot_skill_files()
        if not force and _DYNAMIC_TRIGGERS and snapshot == _DYNAMIC_TRIGGERS_SNAPSHOT:
            return _DYNAMIC_TRIGGERS

        triggers: List[Tuple[re.Pattern[str], str]] = []
        seen: set[Tuple[str, str]] = set()
        for slug in _discover_skill_files(force=force):
            body = _read_skill_body(slug, force=force)
            if not body or not _skill_has_confirmation_gates(body):
                continue
            for api in _extract_apis_from_skill(body):
                key = (api, slug)
                if key in seen:
                    continue
                seen.add(key)
                triggers.append((re.compile(re.escape(api) + r"\b", re.I), slug))

        _DYNAMIC_TRIGGERS = triggers
        _DYNAMIC_TRIGGERS_SNAPSHOT = snapshot
        logger.debug("supervisor rebuilt %d skill approval triggers", len(triggers))
        return _DYNAMIC_TRIGGERS


def match_skill_slugs_for_code(code: str) -> List[str]:
    text = str(code or "")
    if not text.strip():
        return []

    matched: List[str] = []
    seen: set[str] = set()
    for pattern, slug in _rebuild_dynamic_triggers_if_needed():
        if not pattern.search(text):
            continue
        key = slug.lower()
        if key not in seen:
            seen.add(key)
            matched.append(slug)
    return matched


def _extract_confirmation_blocks(body: str, *, max_chars: int = 900) -> str:
    if not body:
        return ""
    chunks: List[str] = []
    for block in re.findall(r"(?:^|\n)(>\s*.+(?:\n>\s*.+)*)", body):
        normalized = re.sub(r"^>\s?", "", block, flags=re.M).strip()
        lowered = normalized.lower()
        if not any(marker in lowered for marker in _CONFIRMATION_MARKERS):
            continue
        if normalized not in chunks:
            chunks.append(normalized)
    text = "\n\n".join(chunks).strip()
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _load_skill_confirmation_excerpt(slug: str) -> str:
    return _extract_confirmation_blocks(_read_skill_body(slug))


def build_skill_gate_prompt(code: str) -> str:
    """Return skill confirmation rules relevant to the pending code snippet."""
    slugs = match_skill_slugs_for_code(code)
    if not slugs:
        return ""

    lines = ["## Skill 用户确认门槛（只读，智能审批必须遵守）"]
    for slug in slugs:
        excerpt = _load_skill_confirmation_excerpt(slug)
        if not excerpt:
            continue
        lines.append(f"### {slug}")
        lines.append(excerpt)
    if len(lines) <= 1:
        return ""
    lines.append(
        "若代码触发上述门槛，但对话/账本中未见用户已确认相关前提（adapter、建库方案、分组、"
        "novel miRNA 需求、QC 后继续等），必须 action=escalate，reason 说明缺失的确认项。"
    )
    return "\n".join(lines)


def assess_skill_confirmation_gates(code: str) -> Optional[Dict[str, str]]:
    """If code hits skills that require explicit user confirmation, return escalate info.

    This is a fast, deterministic check (no LLM). Returns None when no gated skill matches.
    """
    slugs = match_skill_slugs_for_code(code)
    if not slugs:
        return None

    gated: List[str] = []
    for slug in slugs:
        excerpt = _load_skill_confirmation_excerpt(slug)
        if excerpt:
            gated.append(slug)
    if not gated:
        return None

    label = "、".join(gated)
    return {
        "level": "medium",
        "action": "escalate",
        "reason": f"代码触发 Skill 强制确认门槛（{label}），需用户批准后再执行",
        "source": "skill_gate",
    }


def summarize_chat_for_confirmation(chat_id: str, *, max_messages: int = 14) -> str:
    """Recent user/assistant messages as evidence for whether prerequisites were confirmed."""
    if not chat_id:
        return ""
    try:
        from session_store import load_chat_record, sanitize_chat_id

        chat = load_chat_record(sanitize_chat_id(chat_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("chat summary unavailable for %s: %s", chat_id, exc)
        return ""
    if not chat:
        return ""
    messages = chat.get("messages") if isinstance(chat.get("messages"), list) else []
    if not messages:
        return ""
    lines = ["## 最近对话（判断用户是否已确认前提）"]
    for item in messages[-max_messages:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        label = "用户" if role == "user" else "助手"
        lines.append(f"- {label}: {content[:500]}")
    return "\n".join(lines)


def skill_registry_status() -> Dict[str, object]:
    files = _discover_skill_files()
    triggers = _rebuild_dynamic_triggers_if_needed()
    gated_slugs = sorted({slug for _, slug in triggers})
    return {
        "ok": bool(files),
        "count": len(files),
        "slugs": sorted(files.keys()),
        "gatedSlugs": gated_slugs,
        "triggerCount": len(triggers),
    }
