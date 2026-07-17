"""Per-chat run ledger — structured event log for the supervisor agent."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from session_store import _read_json, _write_json, ensure_session_dir, sanitize_chat_id

_LEDGER_FILE = "run_ledger.json"
_LOCK = threading.RLock()
_MAX_EVENTS = 400


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ledger_path(chat_id: str):
    return ensure_session_dir(chat_id) / _LEDGER_FILE


def load_run_ledger(chat_id: str) -> Dict[str, Any]:
    if not chat_id:
        return {"chatId": "", "events": [], "updatedAt": None}
    try:
        chat_id = sanitize_chat_id(chat_id)
    except Exception:
        return {"chatId": chat_id, "events": [], "updatedAt": None}
    payload = _read_json(_ledger_path(chat_id))
    if not payload:
        return {"chatId": chat_id, "events": [], "updatedAt": None}
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    return {
        "chatId": chat_id,
        "events": events,
        "updatedAt": payload.get("updatedAt"),
        "runId": payload.get("runId") or "",
    }


def clear_run_ledger(chat_id: str, *, run_id: str = "") -> None:
    if not chat_id:
        return
    chat_id = sanitize_chat_id(chat_id)
    body = {
        "chatId": chat_id,
        "runId": str(run_id or ""),
        "events": [],
        "updatedAt": _utc_now(),
    }
    with _LOCK:
        _write_json(_ledger_path(chat_id), body)


def append_ledger_event(
    chat_id: str,
    event: Dict[str, Any],
    *,
    run_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Normalize and append a stream/progress event into the persistent ledger."""
    if not chat_id or not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "").strip()
    if not event_type or event_type in {"heartbeat"}:
        return None

    chat_id = sanitize_chat_id(chat_id)
    entry: Dict[str, Any] = {
        "at": _utc_now(),
        "type": event_type,
        "runId": str(run_id or event.get("runId") or ""),
    }

    # Keep a compact, supervisor-friendly projection.
    for key in (
        "message",
        "name",
        "summary",
        "requestId",
        "stepId",
        "stepIndex",
        "description",
        "stage",
        "kind",
    ):
        if event.get(key) not in (None, ""):
            entry[key] = event.get(key)

    if event_type in {"code_approval_required", "code_execution_started", "code_execution_progress"}:
        code = str(event.get("code") or "")
        if code:
            entry["codePreview"] = code[:1200]
            entry["codeChars"] = len(code)
    if event_type in {"tool_result", "error", "plan_step_failed", "plan_step_done"}:
        content = str(event.get("content") or event.get("result") or event.get("message") or "")
        if content:
            entry["detailPreview"] = content[:1500]
    if event_type in {"plan_created", "plan_revised", "plan_complete", "plan_step_start", "plan_step_done", "plan_step_failed"}:
        plan = event.get("plan")
        if isinstance(plan, dict):
            steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
            entry["planGoal"] = plan.get("goal") or ""
            entry["planStepStatuses"] = [
                {
                    "id": s.get("id"),
                    "title": s.get("title") or s.get("goal"),
                    "status": s.get("status"),
                }
                for s in steps
                if isinstance(s, dict)
            ]
    if event_type in {"supervisor_approval", "approval_decision"}:
        for key in ("action", "level", "reason", "mode"):
            if event.get(key) not in (None, ""):
                entry[key] = event.get(key)
    if event_type == "done":
        text = str(event.get("text") or "")
        if text:
            entry["detailPreview"] = text[:800]

    with _LOCK:
        current = load_run_ledger(chat_id)
        events: List[Dict[str, Any]] = list(current.get("events") or [])
        events.append(entry)
        if len(events) > _MAX_EVENTS:
            events = events[-_MAX_EVENTS:]
        body = {
            "chatId": chat_id,
            "runId": str(run_id or current.get("runId") or entry.get("runId") or ""),
            "events": events,
            "updatedAt": _utc_now(),
        }
        _write_json(_ledger_path(chat_id), body)
    return entry


def summarize_ledger_for_prompt(chat_id: str, *, max_events: int = 40) -> str:
    ledger = load_run_ledger(chat_id)
    events = list(ledger.get("events") or [])[-max_events:]
    if not events:
        return "（运行账本为空）"
    lines = ["## Run ledger (recent)"]
    for item in events:
        kind = item.get("type")
        msg = item.get("message") or item.get("summary") or item.get("name") or ""
        extra = item.get("detailPreview") or item.get("codePreview") or ""
        line = f"- [{item.get('at')}] {kind}: {msg}".rstrip()
        statuses = item.get("planStepStatuses")
        if isinstance(statuses, list) and statuses:
            bits = []
            for s in statuses:
                if not isinstance(s, dict):
                    continue
                bits.append(f"{s.get('title') or s.get('id')}={s.get('status')}")
            if bits:
                line += " | plan=[" + ", ".join(bits) + "]"
        if extra:
            line += f" | {str(extra)[:180]}"
        lines.append(line)
    return "\n".join(lines)
