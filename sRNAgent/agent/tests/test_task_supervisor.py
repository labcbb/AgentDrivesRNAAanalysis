"""Tests for generic long-running task telemetry."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.task_supervisor import TaskProgressSupervisor  # noqa: E402


def test_mirtop_root_variable_and_sample_gff_are_tracked(tmp_path: Path):
    supervisor = TaskProgressSupervisor(
        workspace=tmp_path,
        code='mirtop_root = Path("mirtop_out")',
    )
    gff = tmp_path / "mirtop_out" / "SRR15720387" / "SRR15720387.gff"
    gff.parent.mkdir(parents=True)
    gff.write_text("## mirGFF3\n", encoding="utf-8")

    snapshot = supervisor.snapshot()

    assert snapshot["stage"] == "已完成 1 个样本级 GFF，剩余任务仍在运行"
    assert any("1 个样本级 GFF" in item for item in snapshot["highlights"])
