"""Regression tests: do not false-fail plan steps on transient API / max-turns."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import urllib.error
import urllib.request


def test_step_failed_ignores_cancelled_substring_in_success_prose():
    from sRNAgent.agent.plan_orchestrator import _step_failed

    assert not _step_failed("Excluded cancelled samples from DE; done.")
    assert _step_failed("Agent run cancelled.")
    assert _step_failed("Agent reached max turns without calling finish.")
    assert _step_failed("STEP_EXECUTION_ERROR: RuntimeError: LLM HTTP 502: bad gateway")


def test_step_failure_message_distinguishes_reasons():
    from sRNAgent.agent.plan_orchestrator import _step_failure_message

    assert "轮次上限" in _step_failure_message(7, "Agent reached max turns without calling finish.")
    assert "临时 API" in _step_failure_message(
        3, "STEP_EXECUTION_ERROR: RuntimeError: LLM HTTP 502: bad gateway"
    )
    assert "执行异常" in _step_failure_message(
        2, "STEP_EXECUTION_ERROR: RuntimeError: writer crashed"
    )


def test_max_turns_salvaged_when_combined_targets_exist(tmp_path: Path, monkeypatch):
    from sRNAgent.agent import plan_orchestrator as po

    targets = tmp_path / "results" / "targets"
    targets.mkdir(parents=True)
    (targets / "tRF_targets_combined.tsv").write_text("a\tb\n1\t2\n", encoding="utf-8")
    monkeypatch.setattr(po, "_resolve_analysis_workspace", lambda: tmp_path)

    step = {
        "id": "5",
        "title": "运行 miRanda + 3' UTR seed-site 扫描",
        "goal": "写出 results/targets/",
    }
    outcome, result = po._resolve_step_result(
        "Agent reached max turns without calling finish.",
        step,
    )
    assert outcome == "done"
    assert "按完成处理" in result


def test_miranda_partial_dumps_not_salvaged(tmp_path: Path, monkeypatch):
    """Nonempty prediction files alone must not salvage a mid-scan max-turns step."""
    from sRNAgent.agent import plan_orchestrator as po

    miranda = tmp_path / "results" / "targets" / "miranda"
    for name in ("a", "b", "c"):
        d = miranda / name
        d.mkdir(parents=True)
        (d / "miranda_predictions.txt").write_text("partial scan…\n" * 80, encoding="utf-8")
    monkeypatch.setattr(po, "_resolve_analysis_workspace", lambda: tmp_path)

    step = {
        "id": "5",
        "title": "运行 miRanda + seed-site 扫描",
        "goal": "predict targets",
    }
    outcome, result = po._resolve_step_result(
        "Agent reached max turns without calling finish.",
        step,
    )
    assert outcome == "failed"
    assert "max turns" in result.lower()


def test_miranda_majority_scan_complete_salvaged(tmp_path: Path, monkeypatch):
    from sRNAgent.agent import plan_orchestrator as po

    miranda = tmp_path / "results" / "targets" / "miranda"
    for name, complete in (("a", True), ("b", True), ("c", False)):
        d = miranda / name
        d.mkdir(parents=True)
        body = ("x" * 1200) + ("\nScan Complete\n" if complete else "\npartial\n")
        (d / "miranda_predictions.txt").write_text(body, encoding="utf-8")
    monkeypatch.setattr(po, "_resolve_analysis_workspace", lambda: tmp_path)

    step = {
        "id": "5",
        "title": "运行 miRanda",
        "goal": "seed target scan",
    }
    outcome, result = po._resolve_step_result(
        "Agent reached max turns without calling finish.",
        step,
    )
    assert outcome == "done"
    assert "按完成处理" in result


def test_max_turns_still_fails_without_artifacts(monkeypatch):
    from sRNAgent.agent import plan_orchestrator as po

    monkeypatch.setattr(po, "_resolve_analysis_workspace", lambda: None)
    step = {"id": "5", "title": "运行 miRanda", "goal": "predict targets"}
    outcome, result = po._resolve_step_result(
        "Agent reached max turns without calling finish.",
        step,
    )
    assert outcome == "failed"
    assert "max turns" in result.lower()


def test_llm_client_retries_transient_502(monkeypatch):
    from sRNAgent.agent.llm_client import ChatClient, LLMConfig

    sleeps: list[float] = []
    monkeypatch.setattr("sRNAgent.agent.llm_client.time.sleep", sleeps.append)

    class FakeHTTPError(Exception):
        def __init__(self, code: int, body: bytes = b"bad"):
            self.code = code
            self._body = body

        def read(self):
            return self._body

    calls = {"n": 0}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "ok", "tool_calls": []}}]}
            ).encode("utf-8")

    def fake_urlopen(request, timeout=120):
        calls["n"] += 1
        if calls["n"] < 3:
            raise FakeHTTPError(502, b"Bad Gateway")
        return FakeResponse()

    # Make FakeHTTPError look like urllib.error.HTTPError for except clause.
    monkeypatch.setattr(urllib.error, "HTTPError", FakeHTTPError)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = ChatClient(
        LLMConfig(
            api_key="k",
            base_url="https://example.test/v1",
            model="m",
            protocol="openai-completions",
        )
    )
    completion = client.complete([{"role": "user", "content": "hi"}])
    assert completion.content == "ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_execute_step_resilient_retries_transient_then_succeeds():
    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    class FakeAgent:
        system_prompt = "system"

        def _emit_progress(self, *args, **kwargs):
            return None

        def _persist_checkpoint(self, *args, **kwargs):
            return None

        def _clear_run_checkpoint(self, *args, **kwargs):
            return None

        def _check_cancelled(self, cancel_event):
            return None

        def _load_run_checkpoint(self, chat_id):
            return None

    attempts = {"n": 0}

    orch = PlanOrchestrator.__new__(PlanOrchestrator)
    orch.agent = FakeAgent()
    orch.chat_id = "c"
    orch._save_plan = None
    orch._load_plan = None
    orch.skill_overview = ""
    orch._emit = lambda *args, **kwargs: None

    def flaky_execute(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("LLM HTTP 502: Bad Gateway")
        return "step ok"

    orch._execute_step = flaky_execute  # type: ignore
    result = orch._execute_step_resilient(
        {"id": "1", "title": "t"},
        step_index=1,
        step_total=1,
        plan_goal="g",
        user_query="q",
        history=[{"role": "user", "content": "q"}],
        plan={"goal": "g", "steps": []},
    )
    assert result == "step ok"
    assert attempts["n"] == 2


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
