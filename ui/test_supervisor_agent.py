"""Regression tests for task-scoped supervisor reports."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import supervisor_agent  # noqa: E402


def test_run_report_errors_are_scoped_to_the_current_run():
    errors = {
        "events": [
            {"summary": "old", "context": {"runId": "run-old"}},
            {"summary": "current", "context": {"runId": "run-current"}},
            {"summary": "legacy", "context": {}},
        ]
    }

    assert [item["summary"] for item in supervisor_agent._errors_for_run(errors, "run-current")] == ["current"]
    assert len(supervisor_agent._errors_for_run(errors)) == 3


def test_supervisor_json_parser_handles_prose_around_a_balanced_object():
    payload = supervisor_agent._parse_json_object(
        'Here is the report: {"summary":"ok", "notes":["brace { in text"]} trailing text'
    )

    assert payload == {"summary": "ok", "notes": ["brace { in text"]}
