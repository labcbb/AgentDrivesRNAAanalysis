"""Background task notification queue (Claude Code s11 pattern).

Slow operations (long-running CLI, file downloads, reference builds) can be
kicked off in background threads.  When they finish, their results are queued
as XML-style notifications and injected into the agent's ``messages[]`` at the
top of the next model turn — exactly like s15's integrated harness does.

Usage from agent code::

    from sRNAgent.agent.background import start_background, collect_notifications

    # In a tool handler:
    start_background("bg_0001", "fastq-dl SRP181693", lambda: sa.fastq.fastq_dl(...))

    # In the model node, before calling the LLM:
    for note in collect_notifications():
        messages.append({"role": "user", "content": note})
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_notifications: List[str] = []
_active: Dict[str, Dict[str, Any]] = {}


def start_background(
    task_id: str,
    description: str,
    fn: Callable[[], Any],
) -> str:
    """Run ``fn`` in a daemon thread; queue its result as a notification."""
    with _lock:
        _active[task_id] = {"description": description, "status": "running"}

    def _worker() -> None:
        try:
            result = fn()
            note = (
                f"<task_notification task_id=\"{task_id}\" status=\"completed\">\n"
                f"Description: {description}\n"
                f"Result: {str(result)[:2000]}\n"
                f"</task_notification>"
            )
        except Exception as exc:  # noqa: BLE001
            note = (
                f"<task_notification task_id=\"{task_id}\" status=\"failed\">\n"
                f"Description: {description}\n"
                f"Error: {exc}\n"
                f"</task_notification>"
            )
        finally:
            with _lock:
                _active.pop(task_id, None)
                _notifications.append(note)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return task_id


def collect_notifications() -> List[str]:
    """Drain all pending background notifications."""
    with _lock:
        pending = list(_notifications)
        _notifications.clear()
    return pending


def has_pending() -> bool:
    with _lock:
        return bool(_notifications)


def active_count() -> int:
    with _lock:
        return len(_active)


def inject_background_notifications(messages: List[Dict[str, Any]]) -> int:
    """Append pending notifications as a single user message. Returns count."""
    notes = collect_notifications()
    if not notes:
        return 0
    combined = "\n\n".join(notes)
    messages.append({"role": "user", "content": combined})
    return len(notes)
