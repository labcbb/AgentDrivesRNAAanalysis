"""Unified hook registry for sRNAgent (Claude Code s04 pattern).

Hooks are extension points *around* the agent loop — they never change the
loop's shape.  Permission checks, audit logging, output guards, and stop
interception all plug in here instead of being inlined in the tool loop.

Events
------
- ``UserPromptSubmit``  — before a user message enters the loop; can rewrite it
- ``PreToolUse``        — before a tool handler runs; non-None return blocks it
- ``PostToolUse``       — after a tool handler returns; side-effects only
- ``Stop``              — when the model produces no tool_use; non-None return
                          forces another turn by injecting the returned text

A hook callback receives ``(block, *extra)`` for Pre/PostToolUse and
``(messages,)`` for Stop / UserPromptSubmit.  Returning a non-None value from
PreToolUse or Stop is interpreted as a block / force-continue signal.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

# Canonical event names.
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
STOP = "Stop"
USER_PROMPT_SUBMIT = "UserPromptSubmit"

HookCallback = Callable[..., Any]


class HookRegistry:
    """Thread-safe registry of hook callbacks keyed by event name."""

    def __init__(self) -> None:
        self._hooks: Dict[str, List[HookCallback]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def register(self, event: str, callback: HookCallback) -> None:
        with self._lock:
            self._hooks.setdefault(event, []).append(callback)

    def unregister(self, event: str, callback: HookCallback) -> None:
        with self._lock:
            callbacks = self._hooks.get(event)
            if callbacks:
                try:
                    callbacks.remove(callback)
                except ValueError:
                    pass

    def clear(self, event: Optional[str] = None) -> None:
        with self._lock:
            if event is None:
                self._hooks.clear()
            else:
                self._hooks.pop(event, None)

    # ------------------------------------------------------------------ #
    # Triggering
    # ------------------------------------------------------------------ #
    def trigger_pre_tool_use(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        messages: List[Dict[str, Any]],
    ) -> Optional[str]:
        """Return a non-None string to block the tool call (used as result)."""
        with self._lock:
            callbacks = list(self._hooks.get(PRE_TOOL_USE, []))
        for cb in callbacks:
            try:
                result = cb(tool_name, arguments, messages)
            except Exception:  # noqa: BLE001 — hooks must never crash the loop
                continue
            if result is not None:
                return str(result)
        return None

    def trigger_post_tool_use(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        result: str,
        messages: List[Dict[str, Any]],
    ) -> None:
        with self._lock:
            callbacks = list(self._hooks.get(POST_TOOL_USE, []))
        for cb in callbacks:
            try:
                cb(tool_name, arguments, result, messages)
            except Exception:  # noqa: BLE001
                continue

    def trigger_stop(
        self,
        messages: List[Dict[str, Any]],
        final_text: str,
    ) -> Optional[str]:
        """Return non-None to force another turn (injected as a user message)."""
        with self._lock:
            callbacks = list(self._hooks.get(STOP, []))
        for cb in callbacks:
            try:
                result = cb(messages, final_text)
            except Exception:  # noqa: BLE001
                continue
            if result is not None:
                return str(result)
        return None

    def trigger_user_prompt_submit(
        self,
        message: str,
        messages: List[Dict[str, Any]],
    ) -> str:
        """Allow hooks to rewrite the user prompt before it enters the loop."""
        with self._lock:
            callbacks = list(self._hooks.get(USER_PROMPT_SUBMIT, []))
        current = message
        for cb in callbacks:
            try:
                rewritten = cb(current, messages)
                if isinstance(rewritten, str) and rewritten.strip():
                    current = rewritten
            except Exception:  # noqa: BLE001
                continue
        return current


# ---------------------------------------------------------------------- #
# Process-global default registry
# ---------------------------------------------------------------------- #
_default_registry: Optional[HookRegistry] = None
_default_lock = threading.Lock()


def get_default_registry() -> HookRegistry:
    global _default_registry
    with _default_lock:
        if _default_registry is None:
            _default_registry = HookRegistry()
        return _default_registry


def register_hook(event: str, callback: HookCallback) -> None:
    get_default_registry().register(event, callback)


def unregister_hook(event: str, callback: HookCallback) -> None:
    get_default_registry().unregister(event, callback)
