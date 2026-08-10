"""Execution backend regression tests."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.agent_config import ExecutionConfig  # noqa: E402
from sRNAgent.agent.execution import ExecutionBackend, execute_agent_code, initialize_execution_backend  # noqa: E402
from sRNAgent.agent.task_supervisor import TaskProgressSupervisor  # noqa: E402


def test_in_process_execution_streams_stdout_before_completion(tmp_path: Path):
    backend = initialize_execution_backend(
        tmp_path,
        config=ExecutionConfig(use_notebook=False),
    )
    streamed: list[tuple[str, str]] = []

    result = execute_agent_code(
        backend,
        "print('progress: 4/30', flush=True)",
        tmp_path,
        on_stream=lambda kind, text: streamed.append((kind, text)),
    )

    assert "progress: 4/30" in result
    assert ("stdout", "progress: 4/30") in streamed


def test_execute_code_unwraps_provider_text_object(tmp_path: Path):
    backend = initialize_execution_backend(
        tmp_path,
        config=ExecutionConfig(use_notebook=False),
    )

    result = execute_agent_code(
        backend,
        {"$text": "print('provider payload accepted')"},
        tmp_path,
    )

    assert result == "provider payload accepted"


def test_execute_code_reports_invalid_payload_without_crashing(tmp_path: Path):
    backend = initialize_execution_backend(
        tmp_path,
        config=ExecutionConfig(use_notebook=False),
    )

    result = execute_agent_code(backend, {"unexpected": "payload"}, tmp_path)

    assert result.startswith("TOOL_INPUT_ERROR:")


def test_notebook_error_is_not_replayed_in_the_ui_process(tmp_path: Path):
    class Notebook:
        def execute_code(self, _code, on_stream=None):
            return {"stdout": "partial output", "stderr": "", "error": "ValueError: bad input"}

    base = initialize_execution_backend(tmp_path, config=ExecutionConfig(use_notebook=False))
    backend = ExecutionBackend(
        use_notebook=True,
        runtime=base.runtime,
        notebook_executor=Notebook(),
        fallback_policy=base.fallback_policy,
    )

    result = execute_agent_code(backend, "raise AssertionError('must not run locally')", tmp_path)

    assert "ValueError: bad input" in result
    assert "Falling back" not in result
    assert backend.in_process_ns is None


def test_supervisor_tracks_sample_named_artifacts_without_tool_specific_logic(tmp_path: Path):
    output_dir = tmp_path / "quant_out"
    output_dir.mkdir()
    (output_dir / "S1.gff").write_text("one")
    (output_dir / "S2.gff").write_text("two")
    adata = SimpleNamespace(obs_names=["S1", "S2", "S3"])
    supervisor = TaskProgressSupervisor(
        workspace=tmp_path,
        code='result = sa.quant.any_tool(adata, output_dir="quant_out")',
        namespace_provider=lambda: {"adata": adata},
    )

    snapshot = supervisor.snapshot()

    assert snapshot["stage"] == "已完成 2/3 样本"
    assert "监测产物目录: quant_out" in snapshot["highlights"]
