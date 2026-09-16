"""Tests for the unified hook registry (s04 pattern)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.hooks import (  # noqa: E402
    HookRegistry,
    PRE_TOOL_USE,
    POST_TOOL_USE,
    STOP,
    get_default_registry,
    register_hook,
    unregister_hook,
)


def test_pre_tool_use_block_returns_non_none():
    reg = HookRegistry()

    def blocker(tool_name, arguments, messages):
        if tool_name == "bash" and "rm" in arguments.get("command", ""):
            return "Permission denied: destructive command"
        return None

    reg.register(PRE_TOOL_USE, blocker)
    result = reg.trigger_pre_tool_use("bash", {"command": "rm -rf /"}, [])
    assert result == "Permission denied: destructive command"

    result = reg.trigger_pre_tool_use("read_file", {"path": "x.txt"}, [])
    assert result is None


def test_pre_tool_use_pass_returns_none():
    reg = HookRegistry()
    reg.register(PRE_TOOL_USE, lambda name, args, msgs: None)
    assert reg.trigger_pre_tool_use("bash", {"command": "ls"}, []) is None


def test_post_tool_use_is_side_effect_only():
    reg = HookRegistry()
    calls = []

    def logger(tool_name, arguments, result, messages):
        calls.append((tool_name, result))

    reg.register(POST_TOOL_USE, logger)
    reg.trigger_post_tool_use("read_file", {"path": "x"}, "content", [])
    assert calls == [("read_file", "content")]


def test_stop_hook_can_force_continue():
    reg = HookRegistry()
    reg.register(STOP, lambda msgs, text: "[Goal still active] Continue.")
    result = reg.trigger_stop([], "I'm done.")
    assert result == "[Goal still active] Continue."


def test_stop_hook_none_means_stop():
    reg = HookRegistry()
    reg.register(STOP, lambda msgs, text: None)
    assert reg.trigger_stop([], "done") is None


def test_user_prompt_submit_can_rewrite():
    reg = HookRegistry()
    reg.register("UserPromptSubmit", lambda msg, msgs: msg.upper())
    rewritten = reg.trigger_user_prompt_submit("hello", [])
    assert rewritten == "HELLO"


def test_hook_exception_does_not_crash_loop():
    reg = HookRegistry()

    def bad_hook(tool_name, arguments, messages):
        raise RuntimeError("boom")

    def good_hook(tool_name, arguments, messages):
        return "blocked by good hook"

    reg.register(PRE_TOOL_USE, bad_hook)
    reg.register(PRE_TOOL_USE, good_hook)
    result = reg.trigger_pre_tool_use("bash", {}, [])
    assert result == "blocked by good hook"


def test_unregister_removes_callback():
    reg = HookRegistry()

    def cb(tool_name, arguments, messages):
        return "blocked"

    reg.register(PRE_TOOL_USE, cb)
    assert reg.trigger_pre_tool_use("x", {}, []) == "blocked"
    reg.unregister(PRE_TOOL_USE, cb)
    assert reg.trigger_pre_tool_use("x", {}, []) is None


def test_default_registry_is_singleton():
    a = get_default_registry()
    b = get_default_registry()
    assert a is b
