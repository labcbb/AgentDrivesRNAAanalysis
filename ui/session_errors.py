"""Session interruption/error log — persisted under sessions/{chatId}/session_errors.json."""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from session_plan import load_plan, plan_progress_summary
from session_store import _read_json, _write_json, ensure_session_dir, sanitize_chat_id

_ERRORS_FILE = "session_errors.json"
_LOCK = threading.RLock()
_MAX_EVENTS = 48
_CONTEXT_MAX = 1200
_DETAIL_MAX = 2400
_SUMMARY_MAX = 500
_DEDUP_SEC = 8

# Per-chat snapshot of the in-flight agent run (updated from SSE progress events).
_run_context: Dict[str, Dict[str, Any]] = {}


def _errors_path(chat_id: str):
    chat_id = sanitize_chat_id(chat_id)
    return ensure_session_dir(chat_id) / _ERRORS_FILE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _truncate(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def load_session_errors(chat_id: str) -> Dict[str, Any]:
    if not chat_id:
        return {"events": [], "updatedAt": None}
    payload = _read_json(_errors_path(chat_id))
    if not payload:
        return {"events": [], "updatedAt": None}
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    return {
        "events": events,
        "updatedAt": payload.get("updatedAt"),
    }


def save_session_errors(chat_id: str, payload: Dict[str, Any]) -> None:
    if not chat_id:
        return
    chat_id = sanitize_chat_id(chat_id)
    body = {
        "chatId": chat_id,
        "events": payload.get("events") or [],
        "updatedAt": _utc_now(),
    }
    with _LOCK:
        _write_json(_errors_path(chat_id), body)


def clear_run_context(chat_id: str) -> None:
    if not chat_id:
        return
    with _LOCK:
        _run_context.pop(sanitize_chat_id(chat_id), None)


def update_run_context(chat_id: str, event: Dict[str, Any]) -> None:
    """Track the latest tool/code/plan activity for richer interruption records."""
    if not chat_id or not event:
        return
    chat_id = sanitize_chat_id(chat_id)
    event_type = str(event.get("type") or "")
    ctx = dict(_run_context.get(chat_id) or {})
    ctx["updatedAt"] = _utc_now()

    if event_type == "run_start":
        ctx["runId"] = str(event.get("runId") or ctx.get("runId") or "")
    elif event_type in ("plan_created", "plan_revised", "plan_step_start", "plan_step_done", "plan_step_failed"):
        plan = event.get("plan") if isinstance(event.get("plan"), dict) else load_plan(chat_id)
        if plan:
            ctx["planGoal"] = str(plan.get("goal") or "")
            ctx["planSummary"] = plan_progress_summary(plan)
            running = next(
                (s for s in (plan.get("steps") or []) if str(s.get("status") or "") == "running"),
                None,
            )
            if running:
                ctx["planStepId"] = str(running.get("id") or "")
                ctx["planStepTitle"] = str(running.get("title") or running.get("goal") or "")
        if event.get("message"):
            ctx["lastMessage"] = str(event.get("message") or "")
    elif event_type == "tool_call":
        name = str(event.get("name") or "")
        if name and name != "finish":
            ctx["lastTool"] = name
            ctx["lastToolSummary"] = str(event.get("summary") or name)
    elif event_type == "tool_result":
        name = str(event.get("name") or "")
        ctx["lastTool"] = name or ctx.get("lastTool", "")
        ctx["lastToolSummary"] = str(event.get("summary") or name or "")
        content = str(event.get("content") or "")
        if content:
            ctx["lastToolResultPreview"] = _truncate(content, 600)
    elif event_type in ("code_execution_started", "code_execution_progress"):
        ctx["lastTool"] = "execute_code"
        ctx["lastToolSummary"] = str(event.get("summary") or event.get("description") or "execute_code")
        if event.get("code"):
            ctx["lastCodePreview"] = _truncate(str(event.get("code") or ""), 800)
        if event.get("stage"):
            ctx["lastCodeStage"] = str(event.get("stage") or "")
    elif event_type == "status" and event.get("message"):
        ctx["lastMessage"] = str(event.get("message") or "")

    with _LOCK:
        _run_context[chat_id] = ctx


def snapshot_run_context(chat_id: str) -> Dict[str, Any]:
    if not chat_id:
        return {}
    chat_id = sanitize_chat_id(chat_id)
    with _LOCK:
        ctx = dict(_run_context.get(chat_id) or {})
    plan = load_plan(chat_id)
    if plan and not ctx.get("planSummary"):
        ctx["planSummary"] = plan_progress_summary(plan)
    if plan and not ctx.get("planGoal"):
        ctx["planGoal"] = str(plan.get("goal") or "")
    return ctx


def _is_duplicate(events: List[Dict[str, Any]], kind: str, summary: str) -> bool:
    if not events:
        return False
    last = events[-1]
    if str(last.get("kind") or "") != kind:
        return False
    if str(last.get("summary") or "") != summary:
        return False
    try:
        last_at = datetime.fromisoformat(str(last.get("at") or ""))
        now = datetime.now(timezone.utc)
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        return (now - last_at).total_seconds() < _DEDUP_SEC
    except Exception:
        return False


def record_session_error(
    chat_id: str,
    *,
    kind: str,
    summary: str,
    detail: str = "",
    run_id: str = "",
    source: str = "",
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Append an interruption/error event for later agent context."""
    if not chat_id:
        return
    chat_id = sanitize_chat_id(chat_id)
    summary = _truncate(summary, _SUMMARY_MAX)
    if not summary:
        return

    merged_context = snapshot_run_context(chat_id)
    if context:
        merged_context.update(context)
    if run_id:
        merged_context["runId"] = run_id

    event = {
        "id": str(uuid.uuid4()),
        "at": _utc_now(),
        "kind": str(kind or "unknown"),
        "source": str(source or ""),
        "summary": summary,
        "detail": _truncate(detail, _DETAIL_MAX),
        "context": {
            key: _truncate(str(value), _CONTEXT_MAX) if isinstance(value, str) else value
            for key, value in merged_context.items()
            if value not in (None, "", [], {})
        },
    }

    with _LOCK:
        store = load_session_errors(chat_id)
        events: List[Dict[str, Any]] = list(store.get("events") or [])
        if _is_duplicate(events, event["kind"], event["summary"]):
            return
        events.append(event)
        store["events"] = events[-_MAX_EVENTS:]
        save_session_errors(chat_id, store)


def record_stream_event_error(chat_id: str, event: Dict[str, Any]) -> None:
    """Record error-like SSE events into session_errors.json."""
    if not chat_id or not event:
        return
    event_type = str(event.get("type") or "")

    if event_type == "error":
        record_session_error(
            chat_id,
            kind="stream_error",
            summary=str(event.get("message") or "Agent 流式执行失败"),
            source="sse",
        )
        return

    if event_type == "plan_step_failed":
        record_session_error(
            chat_id,
            kind="plan_step_failed",
            summary=str(event.get("message") or "计划步骤失败"),
            detail=str(event.get("result") or ""),
            source="plan_orchestrator",
        )
        return

    if event_type == "tool_result":
        name = str(event.get("name") or "")
        content = str(event.get("content") or "")
        if name == "execute_code" and _looks_like_execution_error(content):
            record_session_error(
                chat_id,
                kind="code_error",
                summary=str(event.get("summary") or "代码执行失败"),
                detail=content,
                source="execute_code",
            )


def _looks_like_execution_error(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    if "Traceback (most recent call last)" in text:
        return True
    lowered = text.lower()
    if any(token in lowered for token in ("exception:", "syntaxerror:", "nameerror:", "runtimeerror:", "keyboardinterrupt")):
        return True
    if lowered.startswith("error:") or "\nerror:" in lowered:
        return True
    if "non-zero exit" in lowered or "command failed" in lowered:
        return True
    return False


def record_user_cancellation(
    chat_id: str,
    *,
    run_id: str = "",
    interrupted: bool = False,
    source: str = "cancel_api",
) -> None:
    summary = "用户手动停止 Agent 运行"
    if interrupted:
        summary += "，并已请求中断 Jupyter 内核中的代码"
    record_session_error(
        chat_id,
        kind="user_cancelled",
        summary=summary,
        run_id=run_id,
        source=source,
        context={"kernelInterrupted": interrupted},
    )


def record_sse_disconnect(chat_id: str, *, run_id: str = "") -> None:
    record_session_error(
        chat_id,
        kind="sse_disconnect",
        summary="前端流式连接断开，Agent LLM 循环已停止（内核中的代码可能仍在运行）",
        run_id=run_id,
        source="serve_sse",
    )


def build_session_errors_context(chat_id: str, *, max_events: int = 10) -> str:
    if not chat_id:
        return ""
    store = load_session_errors(chat_id)
    events = store.get("events") or []
    if not events:
        return ""

    lines = [
        "## 中断与错误记录（session_errors.json）",
        "以下是本会话中发生过的人工停止、连接中断或执行失败。继续任务前请先阅读，"
        "在此基础上判断该重试、跳过、清理残留进程还是更换策略，不要假装这些事没发生过。",
    ]
    for item in events[-max_events:]:
        at = str(item.get("at") or "")
        kind = str(item.get("kind") or "unknown")
        summary = str(item.get("summary") or "").strip()
        detail = str(item.get("detail") or "").strip()
        ctx = item.get("context") if isinstance(item.get("context"), dict) else {}
        line = f"- [{at}] {kind}: {summary}" if at else f"- {kind}: {summary}"
        lines.append(line)
        if ctx.get("planSummary"):
            lines.append(f"  - 计划进度: {ctx['planSummary']}")
        if ctx.get("planStepTitle"):
            lines.append(f"  - 进行中步骤: {ctx['planStepTitle']}")
        if ctx.get("lastToolSummary"):
            lines.append(f"  - 最近工具: {ctx['lastToolSummary']}")
        if ctx.get("lastCodeStage"):
            lines.append(f"  - 代码阶段: {ctx['lastCodeStage']}")
        if detail:
            preview = _truncate(detail.replace("\n", " "), 280)
            lines.append(f"  - 详情: {preview}")
    return "\n".join(lines).strip()
