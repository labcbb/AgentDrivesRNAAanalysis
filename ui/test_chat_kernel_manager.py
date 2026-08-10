"""Tests for the persistent per-chat execution backend selection."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_kernel_manager import notebook_execution_enabled  # noqa: E402


def test_notebook_execution_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("SRNAGENT_USE_NOTEBOOK", raising=False)
    assert notebook_execution_enabled() is True


def test_notebook_execution_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("SRNAGENT_USE_NOTEBOOK", "0")
    assert notebook_execution_enabled() is False
