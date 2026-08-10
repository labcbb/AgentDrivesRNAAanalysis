"""Tests for framework-level multi-item progress reporting."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent._utils import run_threads  # noqa: E402


def test_run_threads_reports_completed_and_active_work(capsys):
    def worker(item: str) -> str:
        time.sleep(0.01)
        return item.upper()

    assert run_threads(["S1", "S2", "S3"], worker, jobs=2) == ["S1", "S2", "S3"]
    output = capsys.readouterr().out
    assert "progress: 0/3" in output
    assert "progress: 3/3" in output
    assert "inflight: S1,S2" in output
