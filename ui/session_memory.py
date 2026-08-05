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
_WORK_LOG_FILE = "work_log.jsonl"
_LOCK = threading.RLock()
_MAX_STEPS = 48
_MAX_ARTIFACTS = 64
_MAX_FACTS = 32
_MAX_WORK_LOG_LINES = 200

_ARTIFACT_RE = re.compile(
    r"(?:[\w./-]+/)?[\w.-]+\.(?:"
    r"fastq(?:\.gz)?|fq(?:\.gz)?|fa(?:\.gz)?|fasta(?:\.gz)?|"
    r"gtf(?:\.gz)?|gff(?:\.gz)?|bed|tsv|csv|json|yaml|yml|"
    r"bam|bai|sam|dict|html|pdf|png|jpg|jpeg|svg|"
    r"h5ad|h5mu|ipynb|xlsx|xls|txt"
    r")(?:\b|$)",
    re.IGNORECASE,
)
_RUN_RE = re.compile(r"\b(SRR|ERR|DRR|SRP|GSE|GSM)\d+\b")
_IMPORTANT_DIRS = (
    "srna_fastq",
    "ref",
    "metadata_srna",
    "metadata",
    "pipeline",
    "results",
    "report",
    "reports",
    "figures",
    "plots",
    "aligned",
    "counts",
    "qc",
    "trimmed",
)
_FACT_PATTERNS = (
    re.compile(r"(adapter|接头).{0,40}(确认|使用|设为|设置为|为)\s*[:：]?\s*([^\n，。;；]{1,120})", re.I),
    re.compile(r"(分组|group).{0,20}(确认|使用|设为|设置为)\s*[:：]?\s*([^\n，。;；]{1,120})", re.I),
    re.compile(r"(novel miRNA).{0,20}(关闭|禁用|启用|开启|false|true)", re.I),
    re.compile(r"(链特异性|strand(?:ed)?).{0,20}(确认|为|设为|设置为)\s*[:：]?\s*([^\n，。;；]{1,120})", re.I),
    re.compile(r"\b(jobs\s*=\s*\d+)\b", re.I),
    re.compile(r"\b(force\s*=\s*(?:True|False))\b", re.I),
)


def _memory_path(chat_id: str) -> Path:
    chat_id = sanitize_chat_id(chat_id)
    return ensure_session_dir(chat_id) / _MEMORY_FILE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_session_memory(chat_id: str) -> Dict[str, Any]:
    if not chat_id:
        return {"steps": [], "artifacts": [], "facts": [], "updatedAt": None}
    payload = _read_json(_memory_path(chat_id))
    if not payload:
        return {"steps": [], "artifacts": [], "facts": [], "updatedAt": None}
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
    return {
        "steps": steps,
        "artifacts": [str(item) for item in artifacts if str(item).strip()],
        "facts": [str(item) for item in facts if str(item).strip()],
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
        "facts": payload.get("facts") or [],
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


def _extract_facts(text: str) -> List[str]:
    found: List[str] = []
    seen: set[str] = set()
    raw = str(text or "")
    if not raw:
        return found
    for pattern in _FACT_PATTERNS:
        for match in pattern.finditer(raw):
            fact = " ".join(part.strip() for part in match.groups() if part and str(part).strip())
            fact = re.sub(r"\s+", " ", fact).strip(" :：-")
            if fact and fact not in seen:
                seen.add(fact)
                found.append(fact[:180])
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
        facts = list(memory.get("facts") or [])
        for item in _extract_facts(f"{summary}\n{detail}"):
            if item not in facts:
                facts.append(item)
        memory["facts"] = facts[-_MAX_FACTS:]
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
        if rel.startswith("sessions/") or "/sessions/" in rel:
            return
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

    for pattern in (
        "**/fastq-run-info.tsv",
        "**/*run-info*.tsv",
        "**/*.fa.gz",
        "**/*.h5ad",
        "**/*.html",
        "**/*.pdf",
        "**/*.png",
        "**/*.svg",
        "**/*.json",
        "**/*.ipynb",
        "**/*.xlsx",
        "**/*counts*.csv",
        "**/*de_results*.csv",
    ):
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
    facts = memory.get("facts") or []
    manifest = build_workspace_manifest()
    errors_context = build_session_errors_context(chat_id)

    if not steps and not artifacts and not facts and not manifest and not errors_context:
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

    if facts:
        lines.append("")
        lines.append("### 已确认的关键事实")
        for item in facts[-12:]:
            lines.append(f"- {item}")

    if artifacts:
        lines.append("")
        lines.append("### 已知产物路径")
        for item in artifacts[-20:]:
            lines.append(f"- {item}")

    if manifest:
        lines.append("")
        lines.append("### 工作区关键文件")
        lines.append(manifest)

    work_log_text = _format_work_log(read_work_log(chat_id, limit=15))
    if work_log_text:
        lines.append("")
        lines.append("### 最近工作日志（重启服务后 agent 会从这里读）")
        lines.append(work_log_text)

    return "\n".join(lines).strip()


def _work_log_path(chat_id: str) -> Path:
    chat_id = sanitize_chat_id(chat_id)
    return ensure_session_dir(chat_id) / _WORK_LOG_FILE


def append_work_log(
    chat_id: str,
    *,
    kind: str,
    label: str = "",
    paths: Optional[List[str]] = None,
    note: str = "",
    run_id: str = "",
) -> None:
    """Append a structured event to the per-chat work log.

    The log is JSONL (`{chat_id}/work_log.jsonl`) and is read back into the
    agent's system prompt on the next run via ``build_session_memory_context``,
    so after a service restart the next agent invocation can see exactly what
    was done in previous runs and pick up without re-asking.
    """
    if not chat_id:
        return
    entry = {
        "at": _utc_now(),
        "kind": str(kind or "event"),
        "label": str(label or "")[:240],
        "paths": [str(p) for p in (paths or [])][:10],
        "note": str(note or "")[:400],
        "runId": str(run_id or ""),
    }
    path = _work_log_path(chat_id)
    with _LOCK:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _trim_work_log(path)
        except OSError:
            pass


def read_work_log(chat_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    if not chat_id:
        return []
    path = _work_log_path(chat_id)
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries[-limit:]


def _trim_work_log(path: Path) -> None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        if len(lines) <= _MAX_WORK_LOG_LINES:
            return
        kept = lines[-_MAX_WORK_LOG_LINES:]
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(kept)
    except OSError:
        pass


def _format_work_log(entries: List[Dict[str, Any]]) -> str:
    if not entries:
        return ""
    lines = []
    for entry in entries:
        ts = str(entry.get("at") or "")
        kind = str(entry.get("kind") or "event")
        label = str(entry.get("label") or "").strip()
        paths = entry.get("paths") or []
        if ts and len(ts) > 19:
            ts = ts[:19]
        bits = [f"- {ts} {kind}"]
        if label:
            bits.append(label)
        line = " — ".join(bits)
        if paths:
            line += " | " + ", ".join(str(p) for p in paths[:3])
        lines.append(line)
    return "\n".join(lines)
