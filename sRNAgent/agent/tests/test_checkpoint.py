"""Tests for checkpoint persistence (runnable with pytest or directly)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sRNAgent.agent.checkpoint import (  # noqa: E402
    checkpoint_path,
    clear_checkpoint,
    load_checkpoint,
    sanitize_chat_id,
    save_checkpoint,
)


def test_sanitize_chat_id():
    assert sanitize_chat_id("abc-123_XYZ") == "abc-123_XYZ"
    assert sanitize_chat_id("../evil/chat") == "evil_chat"  # no path traversal
    assert sanitize_chat_id("") == "default"


def test_save_load_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        payload = {
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "tool", "content": "output"},
            ],
            "plan": {"goal": "定量 miRNA", "steps": []},
            "step_id": "s1",
        }
        save_checkpoint(base, "chat-1", payload)
        loaded = load_checkpoint(base, "chat-1")
        assert loaded is not None
        assert loaded["chat_id"] == "chat-1"
        assert loaded["updated_at"] > 0
        assert loaded["messages"] == payload["messages"]
        assert loaded["plan"] == payload["plan"]
        assert loaded["step_id"] == "s1"
        assert checkpoint_path(base, "chat-1").is_file()


def test_load_missing_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        assert load_checkpoint(Path(tmp), "nope") is None


def test_clear_checkpoint():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        save_checkpoint(base, "chat-2", {"messages": []})
        assert load_checkpoint(base, "chat-2") is not None
        clear_checkpoint(base, "chat-2")
        assert load_checkpoint(base, "chat-2") is None


def test_overwrite_replaces_payload():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        save_checkpoint(base, "chat-3", {"messages": ["old"]})
        save_checkpoint(base, "chat-3", {"plan": {"goal": "g"}, "step_id": None})
        loaded = load_checkpoint(base, "chat-3")
        assert "messages" not in loaded  # full replace
        assert loaded["plan"] == {"goal": "g"}
        assert loaded["step_id"] is None


def test_corrupt_file_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        path = checkpoint_path(base, "chat-4")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert load_checkpoint(base, "chat-4") is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
