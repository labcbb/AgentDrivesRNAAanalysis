"""Session memory — medium-term context (steps, artifacts, workspace manifest)."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from session_errors import build_session_errors_context
from session_store import _read_json, _write_json, ensure_session_dir, sanitize_chat_id
from work_space import get_work_space

_MEMORY_FILE = "session_memory.json"
_LOCK = threading.RLock()
_MAX_STEPS = 48
_MAX_ARTIFACTS = 64

_ARTIFACT_RE = re.compile(
    r"(?:[\w./-]+/)?[\w.-]+\.(?:fastq(?:\.gz)?|fa(?:\.gz)?|gtf(?:\.gz)?|tsv|csv|bam|bai|dict)(?:\b|$)",
    re.IGNORECASE,
)
_RUN_RE = re.compile(r"\b(SRR|ERR|DRR|SRP|GSE|GSM)\d+\b")
_IMPORTANT_DIRS = (
    "srna_fastq",
    "ref",
    "metadata_srna",
    "metadata",
    "srp335685_pipeline",
)


def _memory_path(chat_id: str) -> Path:
    chat_id = sanitize_chat_id(chat_id)
    return ensure_session_dir(chat_id) / _MEMORY_FILE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_session_memory(chat_id: str) -> Dict[str, Any]:
    if not chat_id:
        return {"steps": [], "artifacts": [], "updatedAt": None}
    payload = _read_json(_memory_path(chat_id))
    if not payload:
        return {"steps": [], "artifacts": [], "updatedAt": None}
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    return {
        "steps": steps,
        "artifacts": [str(item) for item in artifacts if str(item).strip()],
        "updatedAt": payload.get("updatedAt"),
    }


def save_session_memory(chat_id: str, payload: Dict[str, Any]) -> None:
    if not chat_id:
        return
    chat_id = sanitize_chat_id(chat_id)
    body = {
        "chatId": chat_id,
        "steps": payload.get("steps") or [],
        "artifacts": payload.get("artifacts") or [],
        "updatedAt": _utc_now(),
    }
    with _LOCK:
        _write_json(_memory_path(chat_id), body)


def _extract_artifacts(text: str) -> List[str]:
    found: List[str] = []
    seen: set[str] = set()
    for match in _ARTIFACT_RE.finditer(text or ""):
        path = match.group(0).strip().strip("'").strip('"')
        if path and path not in seen:
            seen.add(path)
            found.append(path)
    for match in _RUN_RE.finditer(text or ""):
        token = match.group(0)
        if token.startswith(("SRR", "ERR", "DRR")):
            candidate = f"srna_fastq/{token}.fastq.gz"
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
    return found


def _append_step(chat_id: str, summary: str, *, tool: str = "", detail: str = "") -> None:
    summary = str(summary or "").strip()
    if not summary:
        return
    with _LOCK:
        memory = load_session_memory(chat_id)
        steps: List[Dict[str, str]] = list(memory.get("steps") or [])
        if steps and steps[-1].get("summary") == summary:
            return
        steps.append(
            {
                "tool": str(tool or ""),
                "summary": summary[:500],
                "detail": str(detail or "")[:800],
                "at": _utc_now(),
            }
        )
        memory["steps"] = steps[-_MAX_STEPS:]
        artifacts = list(memory.get("artifacts") or [])
        for item in _extract_artifacts(f"{summary}\n{detail}"):
            if item not in artifacts:
                artifacts.append(item)
        memory["artifacts"] = artifacts[-_MAX_ARTIFACTS:]
        save_session_memory(chat_id, memory)


def record_stream_event(chat_id: str, event: Dict[str, Any]) -> None:
    if not chat_id or not event:
        return
    event_type = str(event.get("type") or "")
    if event_type == "tool_call":
        name = str(event.get("name") or "")
        if name and name != "finish":
            _append_step(chat_id, str(event.get("summary") or name), tool=name)
        return
    if event_type == "tool_result":
        name = str(event.get("name") or "")
        summary = str(event.get("summary") or name or "tool_result")
        detail = str(event.get("content") or "")
        _append_step(chat_id, summary, tool=name, detail=detail)
        return
    if event_type == "final":
        content = str(event.get("content") or "").strip()
        if content:
            _append_step(chat_id, f"结论: {content[:240]}", tool="finish", detail=content)
        return
    if event_type == "done":
        text = str(event.get("text") or "").strip()
        if text:
            _append_step(chat_id, f"结论: {text[:240]}", tool="finish", detail=text)
        return
    if event_type in ("plan_created", "plan_revised", "plan_complete"):
        message = str(event.get("message") or event_type).strip()
        if message:
            _append_step(chat_id, message, tool="plan", detail=json.dumps(event.get("plan") or {}, ensure_ascii=False)[:800])
        return
    if event_type in ("plan_step_start", "plan_step_done", "plan_step_failed"):
        message = str(event.get("message") or event_type).strip()
        if message:
            _append_step(chat_id, message, tool="plan_step", detail=str(event.get("result") or "")[:800])


def _format_bytes(value: int) -> str:
    size = float(max(value, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def build_workspace_manifest(*, max_files: int = 36) -> str:
    root = get_work_space()
    if not root.is_dir():
        return ""

    entries: List[tuple[str, int]] = []
    seen: set[str] = set()

    def add_file(path: Path) -> None:
        rel = str(path.relative_to(root))
        if rel in seen or not path.is_file():
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        seen.add(rel)
        entries.append((rel, size))

    for dirname in _IMPORTANT_DIRS:
        base = root / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                add_file(path)

    for pattern in ("**/fastq-run-info.tsv", "**/*run-info*.tsv", "**/*.fa.gz"):
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                add_file(path)

    entries.sort(key=lambda item: item[0].lower())
    if not entries:
        return ""

    lines = []
    for rel, size in entries[:max_files]:
        lines.append(f"- {rel} ({_format_bytes(size)})")
    if len(entries) > max_files:
        lines.append(f"- … 另有 {len(entries) - max_files} 个文件")
    return "\n".join(lines)


def build_session_memory_context(chat_id: str) -> str:
    if not chat_id:
        return ""

    memory = load_session_memory(chat_id)
    steps = memory.get("steps") or []
    artifacts = memory.get("artifacts") or []
    manifest = build_workspace_manifest()
    errors_context = build_session_errors_context(chat_id)

    if not steps and not artifacts and not manifest and not errors_context:
        return ""

    lines = [
        "## Session Context（Cursor-style memory）",
        "以下是本会话的已知进度与产物。请在此基础上继续，不要重复已完成步骤或重复下载已有文件。",
    ]

    if errors_context:
        lines.append("")
        lines.append(errors_context)

    if steps:
        lines.append("")
        lines.append("### 已完成步骤")
        for step in steps[-15:]:
            summary = str(step.get("summary") or "").strip()
            if summary:
                lines.append(f"- {summary}")

    if artifacts:
        lines.append("")
        lines.append("### 已知产物路径")
        for item in artifacts[-20:]:
            lines.append(f"- {item}")

    if manifest:
        lines.append("")
        lines.append("### 工作区关键文件")
        lines.append(manifest)

    return "\n".join(lines).strip()
