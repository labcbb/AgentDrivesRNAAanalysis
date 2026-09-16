"""Tests for background task notification queue (s11 pattern)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.background import (  # noqa: E402
    active_count,
    collect_notifications,
    has_pending,
    inject_background_notifications,
    start_background,
)


def test_collect_empty():
    assert collect_notifications() == []
    assert has_pending() is False


def test_start_background_completes():
    start_background("bg_1", "echo test", lambda: "done")
    # Wait for thread
    for _ in range(50):
        if has_pending():
            break
        time.sleep(0.01)
    notes = collect_notifications()
    assert len(notes) == 1
    assert "completed" in notes[0]
    assert "done" in notes[0]
    assert has_pending() is False


def test_start_background_failure():
    def boom():
        raise RuntimeError("kaboom")

    start_background("bg_2", "fail", boom)
    for _ in range(50):
        if has_pending():
            break
        time.sleep(0.01)
    notes = collect_notifications()
    assert len(notes) == 1
    assert "failed" in notes[0]
    assert "kaboom" in notes[0]


def test_inject_into_messages():
    start_background("bg_3", "echo", lambda: "x")
    for _ in range(50):
        if has_pending():
            break
        time.sleep(0.01)
    messages = []
    count = inject_background_notifications(messages)
    assert count == 1
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "task_notification" in messages[0]["content"]


def test_inject_no_pending():
    messages = []
    count = inject_background_notifications(messages)
    assert count == 0
    assert messages == []
