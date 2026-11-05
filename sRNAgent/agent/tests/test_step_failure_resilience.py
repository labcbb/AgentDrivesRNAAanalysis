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


def test_ssl_eof_is_classified_as_transient():
    """A dropped TLS handshake must be retried, not treated as a hard failure."""
    from sRNAgent.agent.plan_orchestrator import (
        _is_transient_error_text,
        _is_transient_exception,
        _is_transient_step_error,
    )

    eof = (
        "STEP_EXECUTION_ERROR: RuntimeError: LLM connection error: "
        "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol "
        "(_ssl.c:1032) [url=https://api.minimax.io/anthropic/v1/messages]"
    )
    assert _is_transient_error_text(eof)
    assert _is_transient_step_error(eof)
    assert _is_transient_exception(
        RuntimeError(
            "LLM connection error: [SSL: UNEXPECTED_EOF_WHILE_READING] "
            "EOF occurred in violation of protocol (_ssl.c:1032)"
        )
    )
    # A genuine bug must not be retried forever.
    assert not _is_transient_step_error("STEP_EXECUTION_ERROR: RuntimeError: writer crashed")


def test_failed_transient_step_is_selectable_for_replay():
    """A step that failed on a transient error must not strand its dependants."""
    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    plan = {
        "steps": [
            {"id": "preflight", "status": "failed",
             "result": "STEP_EXECUTION_ERROR: RuntimeError: LLM connection error: "
                       "[SSL: UNEXPECTED_EOF_WHILE_READING]"},
            {"id": "approve", "status": "pending", "depends_on": ["preflight"]},
        ]
    }
    step = PlanOrchestrator._next_retryable_failed_step(plan)
    assert step is not None and step["id"] == "preflight"

    # A non-transient failure stays failed: the plan reports it instead of
    # silently looping.
    plan["steps"][0]["result"] = "STEP_EXECUTION_ERROR: RuntimeError: writer crashed"
    assert PlanOrchestrator._next_retryable_failed_step(plan) is None


def test_transient_replay_is_bounded():
    """A persistent outage must not replay the same step forever."""
    from sRNAgent.agent.plan_orchestrator import (
        _MAX_STEP_TRANSIENT_REPLAYS,
        PlanOrchestrator,
    )

    step = {"id": "s", "status": "failed"}
    allowed = 0
    while PlanOrchestrator._can_retry_step(step):
        allowed += 1
        assert allowed <= _MAX_STEP_TRANSIENT_REPLAYS + 1
    assert allowed == _MAX_STEP_TRANSIENT_REPLAYS
    assert step["transientRetries"] == _MAX_STEP_TRANSIENT_REPLAYS


def test_step_retry_budget_is_wider_than_the_old_two_attempts():
    """Three back-to-back attempts still fail ~1 time in 2 on the flaky route."""
    from sRNAgent.agent.plan_orchestrator import _MAX_STEP_TRANSIENT_RETRIES

    assert _MAX_STEP_TRANSIENT_RETRIES >= 5


def test_a_new_user_turn_refreshes_the_replay_budget():
    """A step that exhausted its budget while offline must be retryable again.

    The budget bounds the retries inside one run. Without a refresh a new
    user message could never revive a step that ran out of budget while the
    network was down -- the plan would report a blocked dependency forever.
    """
    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    plan = {
        "steps": [
            {
                "id": "preflight",
                "status": "failed",
                "transientRetries": 99,
                "result": (
                    "STEP_EXECUTION_ERROR: RuntimeError: LLM connection error: "
                    "[SSL: UNEXPECTED_EOF_WHILE_READING]"
                ),
            }
        ]
    }
    restored = PlanOrchestrator._prepare_restored_plan(plan)
    assert "transientRetries" not in restored["steps"][0], (
        "a new turn must clear the exhausted replay budget"
    )
    assert PlanOrchestrator._can_retry_step(restored["steps"][0]) is True


def test_dns_failure_is_not_retried_because_retrying_cannot_help():
    """A name that does not resolve fails identically on every attempt."""
    import socket

    from sRNAgent.agent.llm_client import _is_dns_failure

    assert _is_dns_failure(socket.gaierror(-2, "Name or service not known"))
    assert _is_dns_failure("Name or service not known")
    # A resolver timeout may be transient.
    assert not _is_dns_failure(socket.gaierror(-3, "Temporary failure in name resolution"))
    assert not _is_dns_failure(socket.timeout("timed out"))
    assert not _is_dns_failure(ConnectionResetError("reset by peer"))


def test_request_candidates_include_alternate_minimax_hosts():
    """An unreachable international route must have somewhere else to go."""
    from sRNAgent.agent.llm_client import _request_url_candidates

    candidates = _request_url_candidates(
        "https://api.minimax.io/anthropic/v1/messages"
    )
    assert candidates[0] == "https://api.minimax.io/anthropic/v1/messages"
    assert "https://api.minimax.chat/anthropic/v1/messages" in candidates
    assert "https://api.minimax.cn/anthropic/v1/messages" in candidates
    # The path must survive the host swap, or the request hits a different API.
    for candidate in candidates[1:]:
        assert candidate.endswith("/anthropic/v1/messages")

    # A non-MiniMax host gets no alternates.
    assert _request_url_candidates("https://example.com/v1/messages") == [
        "https://example.com/v1/messages"
    ]


def test_an_alternate_host_that_rejects_the_key_is_skipped(monkeypatch):
    """A 401 from a sibling host means another platform, not a bad key."""
    import json as _json
    import socket as _socket
    import ssl
    import urllib.error

    from sRNAgent.agent import llm_client

    calls = []

    class _Response:
        def __init__(self, payload):
            self._payload = _json.dumps(payload).encode("utf-8")

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        host = request.full_url.split("/")[2]
        if host == "api.minimax.io":
            # The real symptom on the international route: the TLS handshake
            # is cut off, not a name-resolution failure.
            raise urllib.error.URLError(
                ssl.SSLError(
                    "EOF occurred in violation of protocol (_ssl.c:1032)"
                )
            )
        if host == "api.minimax.chat":
            import io

            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {},
                io.BytesIO(
                    _json.dumps({"error": {"message": "invalid api key"}}).encode("utf-8")
                ),
            )
        return _Response({"ok": True})

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm_client.ChatClient, "_retry_sleep", staticmethod(lambda attempt: None))

    client = llm_client.ChatClient.__new__(llm_client.ChatClient)
    result = client._request(
        "https://api.minimax.io/anthropic/v1/messages",
        {"x-api-key": "x"},
        {"model": "m"},
    )
    assert result == {"ok": True}
    assert "api.minimax.io" in calls[0]
    assert any("api.minimax.chat" in call for call in calls)
    assert any("api.minimax.cn" in call for call in calls)


def test_the_attempt_sequence_is_capped_by_a_total_budget(monkeypatch):
    """A dead route must not turn one failed request into minutes of waiting."""
    import pytest

    from sRNAgent.agent import llm_client

    timeouts = []

    def fake_urlopen(request, timeout=None):
        timeouts.append(timeout)
        raise TimeoutError("handshake timed out")

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm_client.ChatClient, "_retry_sleep", staticmethod(lambda attempt: None))

    client = llm_client.ChatClient.__new__(llm_client.ChatClient)
    with pytest.raises(Exception):
        client._request(
            "https://api.minimax.io/anthropic/v1/messages",
            {"x-api-key": "x"},
            {"model": "m"},
        )
    assert timeouts, "the request must have been attempted"
    # No attempt may be allowed to outlive the overall budget.
    assert max(timeouts) <= llm_client._LLM_TOTAL_BUDGET_SEC
    # The first attempt is capped short so a dead route fails over quickly.
    assert timeouts[0] == llm_client._LLM_FIRST_ATTEMPT_TIMEOUT


def test_fastq_qc_contract_replaces_the_planner_duplicate_steps():
    """The contract owns the adapter gate, so the planner's copy must go."""
    from sRNAgent.agent.workflow_contracts import load_workflow_nodes

    contract = (
        Path(__file__).resolve().parents[2] / "skills" / "fastq-qc" / "plan_contract.json"
    )
    nodes = load_workflow_nodes(contract, skill="fastq-qc")
    assert nodes, "fastq-qc must declare its adapter gate"
    node = nodes[0]
    titles = {str(t) for t in (node.get("replaces_titles") or [])}
    assert "确认 sRNA-seq 3' adapter" in titles
    assert "确认sRNA-seq 3' adapter" in titles
    assert "确认新增样本修剪配置" in titles
    assert "读取现有 FASTQ 修剪配置" in titles


def test_contract_preflight_is_restored_when_only_the_stage_exists():
    """A plan holding the approval stage must still get its preflight."""
    from sRNAgent.agent.workflow_contracts import apply_workflow_nodes, load_workflow_nodes

    contract = (
        Path(__file__).resolve().parents[2] / "skills" / "fastq-qc" / "plan_contract.json"
    )
    nodes = load_workflow_nodes(contract, skill="fastq-qc")
    planned = [
        {
            "id": "workflow:confirm-adapter-before-trimming",
            "title": "确认 sRNA-seq 3' adapter",
            "skill": "fastq-qc",
            "status": "pending",
            "workflowContract": "confirm-adapter-before-trimming",
            "inputs": ["trim_config_evidence"],
            "outputs": ["trim_config_confirmed"],
        },
    ]
    compiled = apply_workflow_nodes(planned, nodes, context={"available_artifacts": []})
    ids = {str(s.get("id") or "") for s in compiled}
    assert "workflow:confirm-adapter-before-trimming:preflight" in ids, (
        "the missing preflight must be restored, otherwise the approval stage "
        "has no evidence step to wait for"
    )


def test_restored_plan_drops_the_duplicates_and_keeps_the_statuses():
    """Re-applying the contracts to a persisted plan must not reset it."""
    from sRNAgent.agent.plan_orchestrator import _recompile_persisted_plan_contracts
    from sRNAgent.agent.plan_state import PlanGraph
    from sRNAgent.skill_registry import SkillRegistry, discover_skill_roots

    roots = discover_skill_roots()
    registry = SkillRegistry(roots[0][1])
    registry.load()

    persisted = [
        {"id": "download-fastq", "title": "全量下载新增项目FASTQ",
         "skill": "fastq-dl-srna", "status": "done", "result": "ok", "depends_on": []},
        {"id": "workflow:confirm-adapter-before-trimming:preflight",
         "title": "读取现有 FASTQ 修剪配置", "skill": "fastq-qc", "status": "failed",
         "result": "STEP_EXECUTION_ERROR: RuntimeError: LLM connection error",
         "workflowContract": "confirm-adapter-before-trimming:preflight"},
        {"id": "preflight-trimming-config", "title": "读取现有FASTQ修剪配置",
         "skill": "fastq-qc", "status": "failed", "depends_on": ["download-fastq"]},
        {"id": "workflow:confirm-adapter-before-trimming", "title": "确认 sRNA-seq 3' adapter",
         "skill": "fastq-qc", "status": "pending",
         "depends_on": ["workflow:confirm-adapter-before-trimming:preflight",
                        "preflight-trimming-config"],
         "workflowContract": "confirm-adapter-before-trimming"},
        {"id": "confirm-adapter", "title": "确认sRNA-seq 3' adapter",
         "skill": "fastq-qc", "status": "pending",
         "depends_on": ["preflight-trimming-config",
                        "workflow:confirm-adapter-before-trimming:preflight"]},
        {"id": "confirm-trimming-config", "title": "确认新增样本修剪配置",
         "skill": "fastq-qc", "status": "pending",
         "depends_on": ["confirm-adapter",
                        "workflow:confirm-adapter-before-trimming:preflight",
                        "preflight-trimming-config"]},
    ]

    compiled = _recompile_persisted_plan_contracts(
        persisted,
        skill_registry=registry,
        user_query="下载并修剪新增项目的FASTQ",
        available_artifacts=[],
    )
    ids = [str(s.get("id") or "") for s in compiled]
    assert "preflight-trimming-config" not in ids, (
        "the planner's duplicate preflight must be dropped"
    )
    assert "confirm-adapter" not in ids, "the planner's duplicate gate must be dropped"
    assert "confirm-trimming-config" not in ids, "the duplicate config gate must be dropped"

    by_id = {str(s.get("id") or ""): s for s in compiled}
    assert by_id["download-fastq"]["status"] == "done", "a completed step must stay done"
    assert by_id["workflow:confirm-adapter-before-trimming:preflight"]["status"] == "failed", (
        "a failed step must keep its failure so it can be retried"
    )

    # No dependency may point at a step that no longer exists.
    known = set(ids)
    for step in compiled:
        for dep in step.get("depends_on") or []:
            assert str(dep) in known, f"dangling dependency: {dep}"

    # The failed preflight must be selectable for a replay.
    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    retryable = PlanOrchestrator._next_retryable_failed_step({"steps": compiled})
    assert retryable is not None
    assert retryable["id"] == "workflow:confirm-adapter-before-trimming:preflight"


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
    waited = []
    orch._wait_before_retry = lambda seconds, cancel_event: waited.append(seconds)  # type: ignore
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
    # A retry must wait before trying again: back-to-back attempts keep hitting
    # the same flaky route and exhaust the budget.
    assert waited == [1.0]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
