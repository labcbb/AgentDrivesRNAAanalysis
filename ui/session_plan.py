"""Session plan persistence — Plan-and-Execute state under sessions/{chatId}/plan.json."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from session_store import _read_json, _write_json, ensure_session_dir, sanitize_chat_id

_PLAN_FILE = "plan.json"
_LOCK = threading.RLock()

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"


def _plan_path(chat_id: str) -> Path:
    chat_id = sanitize_chat_id(chat_id)
    return ensure_session_dir(chat_id) / _PLAN_FILE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty_plan(goal: str = "") -> Dict[str, Any]:
    return {
        "goal": goal,
        "steps": [],
        "version": 1,
        "createdAt": _utc_now(),
        "updatedAt": _utc_now(),
    }


def load_plan(chat_id: str) -> Optional[Dict[str, Any]]:
    if not chat_id:
        return None
    payload = _read_json(_plan_path(chat_id))
    if not payload or not isinstance(payload.get("steps"), list):
        return None
    return payload


def save_plan(chat_id: str, plan: Dict[str, Any]) -> None:
    if not chat_id:
        return
    chat_id = sanitize_chat_id(chat_id)
    body = dict(plan)
    body["chatId"] = chat_id
    body["updatedAt"] = _utc_now()
    if not body.get("createdAt"):
        body["createdAt"] = body["updatedAt"]
    with _LOCK:
        _write_json(_plan_path(chat_id), body)


def clear_plan(chat_id: str) -> None:
    if not chat_id:
        return
    path = _plan_path(chat_id)
    if path.exists():
        path.unlink(missing_ok=True)


def normalize_plan(raw: Dict[str, Any], *, goal: str = "") -> Dict[str, Any]:
    steps_in = raw.get("steps") if isinstance(raw.get("steps"), list) else []
    steps: List[Dict[str, Any]] = []
    for index, item in enumerate(steps_in, start=1):
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("id") or index)
        title = str(item.get("title") or f"步骤 {index}").strip()
        step_goal = str(item.get("goal") or title).strip()
        skill = str(item.get("skill") or "").strip()
        status = str(item.get("status") or STEP_PENDING)
        if status not in {STEP_PENDING, STEP_RUNNING, STEP_DONE, STEP_FAILED, STEP_SKIPPED}:
            status = STEP_PENDING
        steps.append(
            {
                "id": step_id,
                "title": title,
                "goal": step_goal,
                "skill": skill,
                "status": status,
                "result": str(item.get("result") or "").strip(),
            }
        )
    plan_goal = str(raw.get("goal") or goal or "").strip()
    return {
        "goal": plan_goal,
        "steps": steps,
        "version": int(raw.get("version") or 1),
        "createdAt": raw.get("createdAt") or _utc_now(),
        "updatedAt": _utc_now(),
    }


def plan_progress_summary(plan: Dict[str, Any]) -> str:
    steps = plan.get("steps") or []
    if not steps:
        return "计划为空"
    done = sum(1 for s in steps if s.get("status") == STEP_DONE)
    total = len(steps)
    running = next((s for s in steps if s.get("status") == STEP_RUNNING), None)
    if running:
        return f"步骤 {done + 1}/{total}：{running.get('title') or '执行中'}"
    if done == total:
        return f"全部 {total} 个步骤已完成"
    return f"进度 {done}/{total}"


def format_plan_for_planner(plan: Dict[str, Any]) -> str:
    lines = [f"Goal: {plan.get('goal') or '(未指定)'}", "Steps:"]
    for step in plan.get("steps") or []:
        status = step.get("status") or STEP_PENDING
        title = step.get("title") or step.get("goal") or step.get("id")
        result = str(step.get("result") or "").strip()
        line = f"  - [{status}] {step.get('id')}: {title}"
        if step.get("skill"):
            line += f" (skill: {step['skill']})"
        if result:
            preview = result[:300] + ("…" if len(result) > 300 else "")
            line += f"\n    result: {preview}"
        lines.append(line)
    return "\n".join(lines)
