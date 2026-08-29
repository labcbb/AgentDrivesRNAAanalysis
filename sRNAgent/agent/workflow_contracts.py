"""Typed workflow contracts and artifact dependency wiring.

The planner may propose scientific intent, but workflow ordering belongs to
skill-owned data.  This module validates that data and turns declared artifact
inputs/outputs into executable plan dependencies.  It intentionally has no
LLM or UI dependency, so pipeline rules are testable without prompt text.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping


WORKFLOW_SCHEMA = "srnagent.workflow/v2"


def load_workflow_nodes(path: Path, *, skill: str) -> List[Dict[str, Any]]:
    """Load one v2 workflow specification, rejecting malformed contracts."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or payload.get("schema") != WORKFLOW_SCHEMA:
        return []
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return []
    result: List[Dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict) or not str(node.get("id") or "").strip():
            continue
        item = deepcopy(node)
        item["workflowSkill"] = skill
        item["workflowVersion"] = 2
        result.append(item)
    return result


def _artifact_names(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    names: List[str] = []
    for item in value:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def wire_artifact_dependencies(steps: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compile artifact edges and return a stable topological plan order."""
    wired = [step for step in steps if isinstance(step, dict)]
    producers: Dict[str, List[tuple[int, str]]] = {}
    for index, step in enumerate(wired):
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        for artifact in _artifact_names(step.get("outputs")):
            producers.setdefault(artifact, []).append((index, step_id))

    for index, step in enumerate(wired):
        step_id = str(step.get("id") or "").strip()
        if not step_id:
            continue
        dependencies = _artifact_names(step.get("depends_on"))
        for artifact in _artifact_names(step.get("inputs")):
            candidates = [item for item in producers.get(artifact, []) if item[1] != step_id]
            if not candidates:
                continue
            earlier = [item for item in candidates if item[0] < index]
            # Prefer the closest declared producer before this consumer. If a
            # malformed/restored plan placed the producer later, wire it
            # anyway and let the topological sort repair the visible order.
            producer = (earlier[-1] if earlier else candidates[0])[1]
            if producer not in dependencies:
                dependencies.append(producer)
        if dependencies:
            step["depends_on"] = dependencies

    by_id = {str(step.get("id") or "").strip(): step for step in wired}
    # Contract replacement may remove a planner node after its ID was copied
    # into another node's explicit dependency list.  A dangling edge is not a
    # real prerequisite and would leave the UI showing a graph that can never
    # become runnable, so prune it at the compiler boundary.
    for step in wired:
        dependencies = _artifact_names(step.get("depends_on"))
        if dependencies:
            step["depends_on"] = [dependency for dependency in dependencies if dependency in by_id]
    order = {str(step.get("id") or "").strip(): index for index, step in enumerate(wired)}
    remaining = list(wired)
    sorted_steps: List[Dict[str, Any]] = []
    resolved: set[str] = set()
    while remaining:
        ready = next(
            (
                step for step in remaining
                if all(
                    dependency not in by_id or dependency in resolved
                    for dependency in _artifact_names(step.get("depends_on"))
                )
            ),
            None,
        )
        if ready is None:
            # Keep cyclic/invalid input visible for the normal plan graph to
            # report rather than silently deleting a user-authored step.
            sorted_steps.extend(sorted(remaining, key=lambda step: order[str(step.get("id") or "").strip()]))
            break
        remaining.remove(ready)
        step_id = str(ready.get("id") or "").strip()
        resolved.add(step_id)
        sorted_steps.append(ready)
    return sorted_steps


_ARTIFACT_PATTERNS = {
    "raw_fastq": re.compile(r"\bfastq_path\b|(?:^|[/\\])[^ \n\t]+\.f(?:ast)?q(?:\.gz)?\b", re.I),
    "trimmed_fastq": re.compile(r"\b(?:trimmed_path|clean_fastq_path)\b|\b(?:trimmed|clean)\s*(?:fastq|fq)\b", re.I),
    "collapsed_fastq": re.compile(r"\bcollapsed_path\b|\b(?:collapsed|collapse)\s*(?:fastq|fq)\b", re.I),
    "genome_bam": re.compile(r"\b(?:genome_bam|whole_genome_bam)\b|全基因组.*\.bam\b", re.I),
    "genome_fasta": re.compile(r"\b(?:genome_fasta|reference_fasta)\b|\bgenome.*\.fa(?:sta)?\b", re.I),
}


def _typed_artifact_names(values: Any) -> List[str]:
    """Normalize persisted artifact IDs and workspace paths into capabilities."""
    normalized = _artifact_names(values)
    inferred: List[str] = []
    for value in normalized:
        lower = value.casefold()
        if re.search(r"\.(?:fastq|fq)(?:\.gz)?$", lower):
            inferred.append("trimmed_fastq" if re.search(r"trimmed|clean", lower) else "raw_fastq")
        if re.search(r"(?:^|[_/.-])(?:de|differential)[_/.-]?.*(?:results?|table)|de_results", lower):
            inferred.append("differential_results")
        if "count" in lower and re.search(r"\.(?:csv|tsv|h5ad)$", lower):
            inferred.append("raw_counts")
        if re.search(r"(?:three[_ -]?prime|3['’′]?[_ -]?utr|utr).*(?:\.fa(?:sta)?(?:\.gz)?)$", lower):
            inferred.append("three_prime_utr_fasta")
    return list(dict.fromkeys([*normalized, *inferred]))


@dataclass(frozen=True)
class ArtifactState:
    """Separate persisted artifacts from outputs merely planned in this DAG."""

    available: frozenset[str]
    planned: frozenset[str]

    @property
    def capabilities(self) -> frozenset[str]:
        return self.available | self.planned


def artifact_state(context: Any, steps: Iterable[Dict[str, Any]]) -> ArtifactState:
    """Build typed artifact state from persisted capabilities or file paths."""
    if isinstance(context, Mapping):
        external = context.get("available_artifacts", context.get("artifacts", []))
        available = frozenset(_typed_artifact_names(external))
    else:
        available = frozenset()
    planned: set[str] = set()
    for step in steps:
        if isinstance(step, dict):
            planned.update(_artifact_names(step.get("outputs")))
    return ArtifactState(available=available, planned=frozenset(planned))


def artifact_inventory(context: Any, steps: Iterable[Dict[str, Any]]) -> set[str]:
    """Compatibility view of all artifacts visible to a workflow DAG."""
    return set(artifact_state(context, steps).capabilities)


def _activation_matches(
    node: Dict[str, Any], artifacts: ArtifactState, steps: Iterable[Dict[str, Any]],
) -> bool:
    activation = node.get("activation") if isinstance(node.get("activation"), dict) else {}
    required = set(_artifact_names(activation.get("requires")))
    missing = set(_artifact_names(activation.get("missing")))
    required_skills = set(_artifact_names(activation.get("requires_skills")))
    planned_skills = {
        str(step.get("skill") or "").strip()
        for step in steps
        if isinstance(step, dict)
    }
    return (
        required.issubset(artifacts.capabilities)
        # A planned output can satisfy a downstream requirement, but it must
        # not masquerade as a persisted artifact and suppress its producer.
        and not bool(missing & artifacts.available)
        and required_skills.issubset(planned_skills)
    )


def _insert_index(steps: List[Dict[str, Any]], placement: Dict[str, Any]) -> int:
    after_contracts = set(_artifact_names(placement.get("after_contracts")))
    if after_contracts:
        positions = [
            index for index, step in enumerate(steps)
            if str(step.get("workflowContract") or "") in after_contracts
        ]
        if positions:
            return max(positions) + 1
    before = set(_artifact_names(placement.get("before_skills")))
    fallback = set(_artifact_names(placement.get("fallback_before_skills")))
    for candidates in (before, fallback):
        if candidates:
            index = next(
                (i for i, step in enumerate(steps) if str(step.get("skill") or "") in candidates),
                None,
            )
            if index is not None:
                return index
    return len(steps)


def _new_step(spec: Dict[str, Any], *, contract_id: str) -> Dict[str, Any]:
    return {
        "id": f"workflow:{contract_id}",
        "title": str(spec.get("title") or "Required workflow stage"),
        "goal": str(spec.get("goal") or ""),
        "skill": str(spec.get("skill") or ""),
        "status": "pending",
        "result": "",
        "autoInserted": True,
        "workflowContract": contract_id,
        "inputs": _artifact_names(spec.get("inputs")),
        "outputs": _artifact_names(spec.get("outputs")),
    }


def _replacement_patterns(node: Dict[str, Any]) -> List[re.Pattern[str]]:
    """Compile a contract-owned set of planner-step aliases safely."""
    replacement = node.get("replaces") if isinstance(node.get("replaces"), dict) else {}
    raw_patterns = _artifact_names(replacement.get("title_patterns"))
    patterns: List[re.Pattern[str]] = []
    for raw in raw_patterns:
        try:
            patterns.append(re.compile(raw, re.I))
        except re.error:
            continue
    return patterns


def _is_replaced_step(
    step: Dict[str, Any], *, titles: set[str], patterns: Iterable[re.Pattern[str]],
) -> bool:
    title = str(step.get("title") or "")
    return title in titles or any(pattern.search(title) for pattern in patterns)


def _select_steps(steps: List[Dict[str, Any]], selector: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Select contract targets through declarative skill and title criteria."""
    skills = set(_artifact_names(selector.get("skills")))
    targets = [
        step for step in steps
        if not skills or str(step.get("skill") or "") in skills
    ]
    if selector.get("exclude_workflow_contracts"):
        targets = [step for step in targets if not str(step.get("workflowContract") or "").strip()]
    patterns = _replacement_patterns({"replaces": {"title_patterns": selector.get("title_patterns")}})
    if patterns:
        pattern_matches = [
            step for step in targets
            if any(pattern.search(str(step.get("title") or "")) for pattern in patterns)
        ]
        # A compact one-step plan may put "miRanda" in its goal rather than
        # title. In that case, retain the non-contract target instead of
        # silently binding an approval card or no node at all.
        if pattern_matches:
            targets = pattern_matches
        elif selector.get("require_title_match"):
            return []
    return targets


def apply_workflow_nodes(
    steps: Iterable[Dict[str, Any]], nodes: Iterable[Dict[str, Any]], *, context: Any,
) -> List[Dict[str, Any]]:
    """Compile v2 skill nodes into plan stages and artifact DAG edges.

    Node activation, placement, and artifact contracts are structured data. No
    workflow-specific branch or title regex is needed in the orchestrator.
    """
    planned = [deepcopy(step) for step in steps if isinstance(step, dict)]
    for node in nodes:
        if not isinstance(node, dict):
            continue
        artifacts = artifact_state(context, planned)
        if not _activation_matches(node, artifacts, planned):
            continue
        kind = str(node.get("kind") or "stage").strip().lower()
        contract_id = str(node.get("id") or "").strip()
        if not contract_id:
            continue
        if kind in {"execution", "bind_io"}:
            selector = node.get("selector") if isinstance(node.get("selector"), dict) else {}
            aliases = set(_artifact_names(node.get("replaces_titles")))
            patterns = _replacement_patterns(node)
            if aliases or patterns:
                planned = [
                    step for step in planned
                    if not _is_replaced_step(step, titles=aliases, patterns=patterns)
                ]
            targets = _select_steps(planned, selector)
            if targets:
                target = targets[-1] if selector.get("select") == "last" else targets[0]
                if kind == "execution":
                    contracts = target.setdefault("executionContracts", [])
                    if isinstance(contracts, list):
                        if not any(
                            isinstance(existing, dict)
                            and str(existing.get("id") or "").strip() == contract_id
                            for existing in contracts
                        ):
                            contracts.append({"id": contract_id, **dict(node.get("execution") or {})})
                if kind == "bind_io" or isinstance(node.get("io"), dict):
                    io = node.get("io") if isinstance(node.get("io"), dict) else {}
                    target["inputs"] = _artifact_names(io.get("inputs"))
                    target["outputs"] = _artifact_names(io.get("outputs"))
            continue
        if kind == "order":
            placement = node.get("placement") if isinstance(node.get("placement"), dict) else {}
            before = set(_artifact_names(placement.get("before_skills")))
            after = set(_artifact_names(placement.get("after_skills")))
            left = next((i for i, step in enumerate(planned) if str(step.get("skill") or "") in before), None)
            right = next((i for i, step in enumerate(planned) if str(step.get("skill") or "") in after), None)
            if left is not None and right is not None and left > right:
                planned.insert(right, planned.pop(left))
            continue
        spec = node.get("step") if isinstance(node.get("step"), dict) else {}
        if not spec:
            continue
        aliases = set(_artifact_names(node.get("replaces_titles")))
        patterns = _replacement_patterns(node)
        if aliases or patterns:
            planned = [
                step for step in planned
                if str(step.get("workflowContract") or "") == contract_id
                or not _is_replaced_step(step, titles=aliases, patterns=patterns)
            ]
        if any(str(step.get("workflowContract") or "") == contract_id for step in planned):
            continue
        stage = _new_step(spec, contract_id=contract_id)
        if kind == "approval":
            approval = node.get("approval") if isinstance(node.get("approval"), dict) else {}
            stage["approval"] = {"id": contract_id, **approval}
        placement = node.get("placement") if isinstance(node.get("placement"), dict) else {}
        index = _insert_index(planned, placement)
        preflight = node.get("preflight") if isinstance(node.get("preflight"), dict) else {}
        if preflight:
            preflight_stage = _new_step(preflight, contract_id=f"{contract_id}:preflight")
            planned.insert(index, preflight_stage)
            index += 1
        planned.insert(index, stage)
    return wire_artifact_dependencies(planned)


class WorkflowCompiler:
    """Compile a workflow DAG from explicitly requested entry skills.

    A skill search result is intentionally not an entry point.  The caller
    supplies only the skills represented by the requested plan nodes; a
    contract may then introduce a new skill only through an actual generated
    prerequisite.  This makes the dependency closure directional:

    ``target prediction -> selected candidates -> DE artifacts``

    cannot wander into ``isomiR quantification -> trim -> adapter`` merely
    because both workflows mention the word ``isomiR``.
    """

    def __init__(self, contract_loader: Callable[[List[str]], List[Dict[str, Any]]]) -> None:
        self._contract_loader = contract_loader

    @staticmethod
    def _skills(steps: Iterable[Dict[str, Any]]) -> set[str]:
        return {
            str(step.get("skill") or "").strip().lower()
            for step in steps
            if isinstance(step, dict) and str(step.get("skill") or "").strip()
        }

    def compile(
        self,
        steps: Iterable[Dict[str, Any]],
        *,
        entry_skills: Iterable[str],
        context: Any,
    ) -> List[Dict[str, Any]]:
        """Materialize the artifact DAG reachable from ``entry_skills`` only."""
        planned = [deepcopy(step) for step in steps if isinstance(step, dict)]
        reached = {
            str(skill or "").strip().lower()
            for skill in entry_skills
            if str(skill or "").strip()
        }
        frontier = set(reached)
        loaded_contract_ids: set[str] = set()

        while frontier:
            nodes = self._contract_loader(sorted(frontier))
            fresh_nodes = [
                node for node in nodes
                if isinstance(node, dict)
                and str(node.get("id") or "").strip()
                and str(node.get("id") or "").strip() not in loaded_contract_ids
            ]
            if fresh_nodes:
                loaded_contract_ids.update(str(node["id"]).strip() for node in fresh_nodes)
                planned = apply_workflow_nodes(planned, fresh_nodes, context=context)

            discovered = self._skills(planned)
            frontier = discovered - reached
            reached.update(frontier)

        return wire_artifact_dependencies(planned)


__all__ = [
    "WORKFLOW_SCHEMA", "ArtifactState", "WorkflowCompiler", "apply_workflow_nodes", "artifact_inventory", "artifact_state",
    "load_workflow_nodes", "wire_artifact_dependencies",
]
