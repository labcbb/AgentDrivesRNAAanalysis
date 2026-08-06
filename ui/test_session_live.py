"""Tests for the live-stream sequence used by browser reconnects."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from session_live import close_live_bus, publish_live_event, start_live_bus  # noqa: E402


CHAT_ID = "abababab-abab-4aba-8aba-abababababab"


def test_published_live_events_return_the_replay_sequence():
    start_live_bus(CHAT_ID, "run-1")
    try:
        first = publish_live_event(CHAT_ID, {"type": "status", "message": "starting"})
        second = publish_live_event(CHAT_ID, {"type": "tool_call", "name": "inspect"})

        assert first["_seq"] == 1
        assert second["_seq"] == 2
        assert first["runId"] == "run-1"
        assert second["runId"] == "run-1"
    finally:
        close_live_bus(CHAT_ID, run_id="run-1")
