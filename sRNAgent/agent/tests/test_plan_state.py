"""Tests for the plan state machine, independent from LLM orchestration."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.plan_state import PlanGraph  # noqa: E402
from sRNAgent.agent.workflow_contracts import (  # noqa: E402
    WORKFLOW_SCHEMA,
    WorkflowCompiler,
    artifact_state,
    load_workflow_nodes,
    wire_artifact_dependencies,
)


def test_graph_only_returns_pending_steps_with_completed_dependencies():
    plan = {
        "steps": [
            {"id": "download", "status": "done"},
            {"id": "adapter", "status": "awaiting_approval", "depends_on": ["download"]},
            {"id": "fastqc", "status": "pending", "depends_on": ["adapter"]},
        ]
    }
    graph = PlanGraph(plan)

    assert graph.next_runnable_pending(pending="pending", completed={"done", "skipped"}) is None
    assert graph.blocked_pending(pending="pending", completed={"done", "skipped"}) == [plan["steps"][2]]


def test_graph_treats_unknown_dependencies_as_blocking_and_resets_interrupted_steps():
    plan = {"steps": [{"id": "qc", "status": "running", "depends_on": ["missing"]}]}
    graph = PlanGraph(plan)

    graph.reset_interrupted(running="running", pending="pending")

    assert plan["steps"][0]["status"] == "pending"
    assert graph.next_runnable_pending(pending="pending", completed={"done"}) is None
    assert graph.unmet_dependencies(plan["steps"][0], {"done"}) == ["missing"]


def test_workflow_contract_wires_declared_artifact_edges(tmp_path: Path):
    contract = tmp_path / "plan_contract.json"
    contract.write_text(
        '{"schema":"srnagent.workflow/v2","version":2,"nodes":'
        '[{"id":"trim","type":"stage","step":{"title":"trim"}}]}',
        encoding="utf-8",
    )
    nodes = load_workflow_nodes(contract, skill="fastq-qc")
    assert nodes[0]["workflowSkill"] == "fastq-qc"
    assert WORKFLOW_SCHEMA == "srnagent.workflow/v2"

    wired = wire_artifact_dependencies([
        {"id": "1", "outputs": ["trimmed_fastq"]},
        {"id": "2", "inputs": ["trimmed_fastq"]},
    ])
    assert wired[1]["depends_on"] == ["1"]


def test_artifact_state_keeps_persisted_and_planned_outputs_distinct():
    state = artifact_state(
        {"available_artifacts": ["raw_fastq"]},
        [{"id": "trim", "status": "pending", "outputs": ["trimmed_fastq"]}],
    )

    assert state.available == {"raw_fastq"}
    assert state.planned == {"trimmed_fastq"}
    assert state.capabilities == {"raw_fastq", "trimmed_fastq"}


def test_artifact_state_uses_structured_inventory_not_conversation_text():
    state = artifact_state(
        {"legacy_context": "adata.obs['fastq_path'] = 'raw/S1.fastq.gz'"},
        [],
    )
    typed_state = artifact_state(
        {"available_artifacts": ["raw/S1.fastq.gz"]},
        [],
    )

    assert state.available == set()
    assert typed_state.available == {"raw/S1.fastq.gz", "raw_fastq"}


def test_workflow_compiler_only_loads_contracts_reachable_from_entry_skill():
    calls = []

    def load_contracts(skills):
        calls.append(tuple(skills))
        if skills == ["mirna-target-prediction"]:
            return [{
                "id": "select-candidates",
                "kind": "stage",
                "activation": {"requires_skills": ["mirna-target-prediction"]},
                "placement": {"before_skills": ["mirna-target-prediction"]},
                "step": {
                    "title": "选择候选", "skill": "mirna-target-prediction",
                    "outputs": ["target_candidates"],
                },
            }]
        raise AssertionError(f"unreachable contracts requested: {skills}")

    planned = WorkflowCompiler(load_contracts).compile(
        [{"id": "predict", "title": "miRanda", "skill": "mirna-target-prediction"}],
        entry_skills=["mirna-target-prediction"],
        context={"available_artifacts": ["raw_fastq"]},
    )

    assert calls == [("mirna-target-prediction",)]
    assert [step["title"] for step in planned] == ["选择候选", "miRanda"]
