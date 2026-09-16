"""Plan-and-Execute orchestrator for sRNAgent.

Planner creates/revises a structured plan; each step runs in an isolated tool-loop
with its own turn budget (max_turns per step).
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

from .plan_state import (
    PlanGraph,
    STEP_AWAITING_APPROVAL,
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
)
from .plan_lifecycle import PlanLifecycle, clone_plan
from .context import normalize_text_payload
from .intent_router import IntentRouter, RouteIntent, is_gate_actionable_reply, is_plan_approval_confirm
from .tools import list_available_skills, rank_skill_matches, resolve_skill_query
from .workflow_contracts import WorkflowCompiler, apply_workflow_nodes, load_workflow_nodes, wire_artifact_dependencies

if TYPE_CHECKING:
    from .srn_agent import SRNAgent, ProgressCallback, CodeApprovalCallback

_APPROVAL_ACCEPT_RE = re.compile(
    r"^\s*(?:可以|确认(?:使用|执行|继续|运行)?|同意|继续|按(?:此|上述)|采用|yes|ok|okay)(?:\s|[，,。.!！]|$)",
    re.I,
)
_APPROVAL_REJECT_RE = re.compile(
    r"^\s*(?:不可以|不同意|不确认|不要(?:执行|使用)?|否|no)(?:\s|[，,。.!！]|$)",
    re.I,
)
_APPROVAL_ASSIGNMENT_RE = re.compile(
    r"(?:adapter(?:_3)?|strandedness|group(?:_col)?|control_group|design|min_length|max_length|"
    r"quality_cutoff|error_rate|min_overlap|no_indels|times|trim_n|poly_a|output_dir|json_report)\s*=\s*\S+",
    re.I,
)
_APPROVAL_EXPLICIT_VALUE_RE = re.compile(r"\b(?:unstranded|forward|reverse|paired|unpaired)\b|\b[ACGTUN]{8,}\b", re.I)
_APPROVAL_AFFIRMATION_RE = re.compile(
    r"^\s*(?:可以(?:的|啊|呀)?|可(?:以|行)|好(?:的|啊|呀)?|没问题|行|同意|确认|"
    r"ok(?:ay)?(?:\s+go\s+ahead)?|yes|yep|sure|go\s+ahead)\s*[，,。.!！]?\s*$",
    re.I,
)
_GROUP_CONFIRMATION_RE = re.compile(
    r"(?:确认|同意|采用|按|根据|使用).{0,24}(?:分组|组别|group)|"
    r"(?:分组|组别|group).{0,24}(?:确认|同意|采用|继续)",
    re.I,
)
_NATURAL_CONTROL_GROUP_RE = re.compile(
    r"(?:对照组|control(?:\s*group)?)\s*(?:为|是|=|:|：)\s*`?([A-Za-z][\w.-]*)`?|"
    r"\b([A-Za-z][\w.-]*)\s*(?:是|作为|as)\s*(?:对照组|control(?:\s*group)?)",
    re.I,
)
_NATURAL_DESIGN_RE = re.compile(
    r"(?:design|设计)\s*(?:为|是|=|:|：)?\s*`?((?:un)?paired|配对|非配对|不配对)`?",
    re.I,
)
_MAX_REPLAN_ATTEMPTS = 8
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_INTERNAL_REPORT_RE = re.compile(
    r"已向用户|已向用户发送|已向.*发送|等待.{0,8}下一步|等待用户|"
    r"已发送问候|已介绍|介绍.*功能.*等待|"
    r"task completed|step (is )?done|waiting for (the )?user|"
    r"回复用户|向用户回复|发送问候",
    re.I,
)
_TRIMMED_FASTQ_RE = re.compile(
    r"\b(trimmed_path|clean_fastq_path)\b|\b(?:trimmed|clean)\s*(?:fastq|fq)\b|"
    r"(?:^|[/\\])[^ \n\t]+(?:trimmed|clean)[^ \n\t]*\.f(?:ast)?q(?:\.gz)?\b",
    re.I,
)
_RAW_FASTQ_RE = re.compile(r"\bfastq_path\b|(?:^|[/\\])[^ \n\t]+\.(?:fastq|fq)(?:\.gz)?\b", re.I)
_BAM_RE = re.compile(r"\bbam_path\b|(?:^|[/\\])[^ \n\t]+\.bam\b", re.I)
_GENOME_FASTA_RE = re.compile(
    r"\b(genome_fasta|reference_fasta|fasta_path)\b|(?:^|[/\\])[^ \n\t]+\.(?:fa|fasta)(?:\.gz)?\b",
    re.I,
)
_GENOME_INDEX_RE = re.compile(r"\bgenome_index\b|\.ebwt\b|index_basename\b", re.I)
_COUNTS_RE = re.compile(
    r"\blayers\[['\"]counts['\"]\]|\badata\.X\b|\bcounts_csv\b|\bfc_counts_csv\b|\bidxstats_file\b|"
    r"\btrnacounts\b|\bshared raw count matrix\b|\braw counts\b",
    re.I,
)
_LOGCPM_RE = re.compile(r'\blayers\["logcpm"\]\b|\blogcpm\b', re.I)
_GROUP_RE = re.compile(r"\b(group|condition|treatment|group_col|分组|组别)\b", re.I)
_DE_STEP_RE = re.compile(r"\bdifferential-analysis\b|\bde\b|differential|差异分析|limma", re.I)
_UNPAIRED_RE = re.compile(r"\bunpaired\b|非配对|不配对", re.I)
_PAIRED_RE = re.compile(r"(?<!un)\bpaired\b|(?<!非)配对", re.I)
_PATIENT_BLOCKING_RE = re.compile(
    r"patient[_\s-]*blocking|patient[_\s-]*block|donor[_\s-]*block|block(?:ing)?\s+by\s+patient|"
    r"group\s*\+\s*patient(?:_id)?|patient_id",
    re.I,
)
_PAIRED_INFEASIBLE_RE = re.compile(
    r"paired_feasible\s*[:=]\s*false|无配对|没有任何一个.*同时出现|not\s+paired|no\s+paired|paired\s+.*不可行",
    re.I,
)
_MIRNA_RE = re.compile(r"\bmirna\b|miRNA|mirdeep", re.I)
_PIRNA_RE = re.compile(r"\bpirna\b|piRNA", re.I)
_TRNA_RE = re.compile(r"\btrna\b|tRNA|tRF|tRAX", re.I)
_ISOMIR_RE = re.compile(r"\biso[- ]?mir\b|isomiR|mirtop", re.I)
_ISOMIR_PARALLEL_RE = re.compile(
    r"(?:iso[- ]?mir|isomiR|mirtop|样本).{0,32}(?:并行|并发|同时|parallel|concurrent)|"
    r"(?:并行|并发|同时|parallel|concurrent).{0,32}(?:iso[- ]?mir|isomiR|mirtop|样本)",
    re.I,
)
_ISOMIR_REBUILD_RE = re.compile(
    r"(?:清空|清除|删除|重建|重新生成|重跑).{0,48}(?:mirtop|iso[- ]?mir|isomiR)|"
    r"(?:从|自).{0,16}(?:hairpin )?(?:比对|alignment|bowtie).{0,32}(?:开始|重建|重新|生成)",
    re.I,
)
_FRAGMENTOMICS_RE = re.compile(r"fragmentomics|fragomics|fragment-analysis|片段组学|FSD|FSC|RCD|EDM|BPM", re.I)
_UNIFIED_RE = re.compile(r"统一做|统一跑|都跑|一起跑|两组学|两种组学|miRNA\s*\+\s*fragmentomics", re.I)
_CONTINUATION_RE = re.compile(r"继续|接着|刚才|前面|上一(?:个|轮|次)?|上次|之前|在此基础|resume|continue", re.I)
_HTML_REPORT_RE = re.compile(
    r"html\s*报告|html report|report\.html|生成.*html|写.*html|报告|"
    r"(?:re[- ]?write|rewrite|re[- ]?style|restyle|finali[sz]e).{0,40}\bhtml\b|"
    r"\bhtml\b.{0,40}(?:re[- ]?write|rewrite|re[- ]?style|restyle|finali[sz]e)",
    re.I,
)
_DE_SUMMARY_RE = re.compile(r"汇总|summary|切片|输出.*结果|结果.*输出|top\s*(?:de|差异|feature)", re.I)
_MUDATA_RE = re.compile(r"\bmudata\b|MuData|h5mu|放在\s*mudata|放到\s*mudata|返回\s*mudata", re.I)
_WHOLE_GENOME_BAM_RE = re.compile(r"全基因组.*bam|whole[-\s]*genome\s+bam|genome[-\s]*aligned\s+bam", re.I)
_REQUIREMENT_CUE_RE = re.compile(
    r"(必须|需要|要|不要|不能|默认|优先|如果|若|只有|除非|确保|记得|统一做|放在|生成|写入|保存|返回)",
    re.I,
)
_EXPLICIT_QUANT_METHOD_RE = re.compile(
    r"feature[-_ ]?counts?|idxstats?|samtools|mirdeep(?:2)?|mirtop|trax",
    re.I,
)
_QUANT_METHOD_TEXT_RE = re.compile(r"feature[-_ ]?counts?|samtools\s+idxstats?|idxstats?|mirdeep(?:2)?", re.I)
_QUANTIFICATION_RE = re.compile(r"定量|quant(?:if(?:y|ication))?|count|表达", re.I)
_TARGET_PREDICTION_RE = re.compile(
    r"miranda|starbase|encori|(?:3['’′]?\s*)?utr|seed|靶标预测|靶基因|靶标",
    re.I,
)
_EXPLICIT_DE_REQUEST_RE = re.compile(
    r"(?:运行|执行|进行|完成|重跑|重新跑|run|perform|execute|rerun).{0,24}"
    r"(?:差异分析|differential(?:-analysis)?|limma|DE)"
    r"|(?:差异分析|differential(?:-analysis)?|limma|DE).{0,24}"
    r"(?:运行|执行|进行|完成|重跑|重新跑|run|perform|execute|rerun)",
    re.I,
)


class PlanJsonParseError(ValueError):
    """A planner response was not valid JSON after extracting its object body."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


def _extract_user_query(history: List[Dict[str, str]]) -> str:
    for item in reversed(history):
        if item.get("role") == "user":
            content = str(item.get("content") or "").strip()
            if content:
                return content
    return ""


def _format_recent_history(
    history: Optional[List[Dict[str, Any]]],
    *,
    max_messages: int = 8,
    max_chars: int = 6000,
) -> str:
    """Compact recent dialogue for planner/executor context.

    The plan creator previously received only the latest user sentence and
    session memory. That loses conversational references such as "在刚才
    定量结果基础上继续" when the durable memory contains only file paths.
    Keep a bounded transcript so the model can resolve those references.
    """
    if not history:
        return ""
    lines: List[str] = []
    used = 0
    for item in history[-max_messages:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if not content:
            continue
        if len(content) > 1100:
            content = f"{content[:700]} … {content[-300:]}"
        line = f"{role}: {content}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(line) > remaining:
            line = line[:remaining]
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _is_conversational_query(query: str) -> bool:
    """Compatibility wrapper around the single intent-routing policy."""
    return IntentRouter.route(query).intent == RouteIntent.ANSWER


def _is_read_only_query(query: str) -> bool:
    """Compatibility wrapper for the router's non-mutating answer branch."""
    return IntentRouter.route(query).intent == RouteIntent.ANSWER


def _looks_like_internal_report(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _INTERNAL_REPORT_RE.search(t):
        return True
    if re.search(r"(已向|已对|已向)(用户|您)", t):
        return True
    return False


def _parse_plan_json(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise PlanJsonParseError("Planner returned an empty response.", raw)

    candidates = [raw]
    candidates.extend(match.group(1).strip() for match in _JSON_BLOCK_RE.finditer(raw))

    # Extract balanced JSON objects without being confused by braces in quoted
    # text. The old first-{ / last-} fallback could turn a useful response into
    # a different malformed payload and leak JSONDecodeError to the user.
    depth = 0
    start: Optional[int] = None
    quote = ""
    escaped = False
    for index, char in enumerate(raw):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char == '"':
            quote = char
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(raw[start:index + 1])
                start = None

    last_error: Optional[json.JSONDecodeError] = None
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(payload, dict):
            return payload

    detail = str(last_error) if last_error else "no JSON object found"
    raise PlanJsonParseError(f"Planner returned invalid JSON: {detail}", raw)


def _normalize_steps(raw_steps: Any, *, goal: str) -> List[Dict[str, Any]]:
    if not isinstance(raw_steps, list) or not raw_steps:
        return [
            {
                "id": "1",
                "title": "完成任务",
                "goal": goal or "Complete the user request.",
                "skill": "",
                "status": STEP_PENDING,
                "result": "",
            }
        ]

    steps: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or f"Step {index}").strip()
        step_goal = str(item.get("goal") or title).strip()
        normalized = {
            "id": str(item.get("id") or index),
            "title": title,
            "goal": step_goal,
            "skill": str(item.get("skill") or "").strip(),
            "status": STEP_PENDING,
            "result": "",
        }
        for field in ("approval", "depends_on", "inputs", "outputs"):
            if field in item:
                normalized[field] = deepcopy(item[field])
        steps.append(normalized)

    if not steps:
        return _normalize_steps([], goal=goal)
    return steps


def _make_plan_step(
    *,
    step_id: str,
    title: str,
    goal: str,
    skill: str = "",
    status: str = STEP_PENDING,
    result: str = "",
    auto_inserted: bool = False,
    approval: Optional[Dict[str, Any]] = None,
    execution_contracts: Optional[List[Dict[str, Any]]] = None,
    inputs: Optional[List[str]] = None,
    outputs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    step = {
        "id": str(step_id),
        "title": str(title).strip(),
        "goal": str(goal).strip(),
        "skill": str(skill).strip(),
        "status": status,
        "result": str(result or ""),
        "autoInserted": bool(auto_inserted),
    }
    if approval:
        step["approval"] = dict(approval)
    if execution_contracts:
        step["executionContracts"] = [dict(item) for item in execution_contracts]
    if inputs:
        step["inputs"] = [str(item) for item in inputs if str(item).strip()]
    if outputs:
        step["outputs"] = [str(item) for item in outputs if str(item).strip()]
    return step


def _context_has_trimmed_fastq(extra_context: str) -> bool:
    return bool(_TRIMMED_FASTQ_RE.search(extra_context or ""))


def _context_has_raw_fastq(extra_context: str) -> bool:
    return bool(_RAW_FASTQ_RE.search(extra_context or ""))


def _context_has_bam(extra_context: str) -> bool:
    return bool(_BAM_RE.search(extra_context or ""))


def _context_has_genome_fasta(extra_context: str) -> bool:
    return bool(_GENOME_FASTA_RE.search(extra_context or ""))


def _context_has_genome_index(extra_context: str) -> bool:
    return bool(_GENOME_INDEX_RE.search(extra_context or ""))


def _context_has_counts(extra_context: str) -> bool:
    return bool(_COUNTS_RE.search(extra_context or ""))


def _context_has_counts_for_modality(extra_context: str, modality: str) -> bool:
    """Require count evidence for the same modality, not merely any AnnData."""
    text = str(extra_context or "")
    normalized = str(modality or "").strip().lower()
    if normalized == "fragmentomics":
        return bool(re.search(
            r"(?:fragmentomics|fragomics).{0,240}(?:\.h5ad\b|raw counts|layers\[['\"]counts['\"]\]|counts? matrix|count_matrix)",
            text,
            re.I | re.S,
        ))
    if normalized == "isomir":
        return bool(re.search(
            r"(?:iso[- ]?mir|mirtop).{0,240}(?:\.h5ad\b|raw counts|layers\[['\"]counts['\"]\]|counts? matrix|count_matrix)",
            text,
            re.I | re.S,
        ))
    return _context_has_counts(text)


def _context_has_logcpm(extra_context: str) -> bool:
    return bool(_LOGCPM_RE.search(extra_context or ""))


def _load_planning_skill_guidance(skill_registry: Any, user_query: str) -> str:
    """Bind the most relevant workflow guides before the planner creates steps."""
    matches = rank_skill_matches(skill_registry, user_query)
    sections: List[str] = []
    for metadata, _score in matches[:6]:
        skill = skill_registry.load_full_skill(metadata.slug)
        if skill is None:
            continue
        sections.append(
            f"=== {skill.name} ({skill.slug}) ===\n"
            f"{skill.prompt_instructions(max_chars=4500)}"
        )
    return "\n\n".join(sections)


def _load_skill_plan_contracts(
    skill_registry: Any,
    user_query: str,
    *,
    step_skills: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Load workflow rules only for skills that the plan actually executes.

    Semantic ranking is useful while drafting a plan, but it is not an
    authorization to add another workflow.  In particular, an isomiR target
    prediction also matches the isomiR-quantification skill by name; loading
    that contract would incorrectly add upstream FASTQ trimming and its
    adapter approval to a target-only request.
    """
    contracts: List[Dict[str, Any]] = []
    slugs: List[str] = []
    available = {
        str(slug).strip().lower()
        for slug in getattr(skill_registry, "skill_metadata", {})
    }
    for slug in step_skills or []:
        normalized = str(slug or "").strip().lower()
        if normalized in available and normalized not in slugs:
            slugs.append(normalized)

    # A direct caller without a drafted plan still needs a deterministic
    # bootstrap contract.  This also resolves a legacy/non-canonical step
    # skill name through the query.  Use only the best match: lower-ranked
    # keyword matches are alternatives, not implicit prerequisite workflows.
    if not slugs:
        matches = rank_skill_matches(skill_registry, user_query)
        if matches:
            slugs.append(matches[0][0].slug)
    for slug in slugs:
        skill = skill_registry.load_full_skill(slug) if skill_registry else None
        if skill is None:
            continue
        contract_path = skill.path / "plan_contract.json"
        typed_nodes = load_workflow_nodes(contract_path, skill=slug)
        if typed_nodes:
            contracts.extend(typed_nodes)
    return contracts


def _plan_step_text(step: Dict[str, Any]) -> str:
    return " ".join(str(step.get(key) or "") for key in ("title", "goal", "skill"))


def _apply_skill_plan_contracts(
    steps: List[Dict[str, Any]],
    contracts: List[Dict[str, Any]],
    *,
    user_query: str,
    extra_context: str,
    available_artifacts: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Apply skill-owned order, approval, and execution constraints."""
    typed_contracts = [
        contract for contract in contracts
        if isinstance(contract, dict) and contract.get("workflowVersion") == 2
    ]
    if typed_contracts:
        return apply_workflow_nodes(
            steps,
            typed_contracts,
            context={"available_artifacts": list(available_artifacts or [])},
        )

    return wire_artifact_dependencies([deepcopy(step) for step in steps if isinstance(step, dict)])


def _enforce_workflow_contracts(
    steps: List[Dict[str, Any]],
    *,
    skill_registry: Any,
    user_query: str,
    extra_context: str,
    available_artifacts: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Apply deterministic prerequisites after either planner pass.

    The LLM proposes scope and scientific intent; skill contracts own required
    transformations and their order. This boundary is deliberately shared by
    fresh plans and replans so a reviewer cannot move isomiR ahead of trim.
    """
    if skill_registry is None:
        # Tests and minimal integrations may intentionally run without the
        # skill registry. Without contracts, preserve the planner output
        # instead of synthesizing a partial generic pipeline.
        return steps
    # These are the plan's requested entry points.  Automatic prerequisite
    # expansion may add skills later, but it must never turn a semantic match
    # from the user wording into another workflow root.
    entry_skills = [str(step.get("skill") or "") for step in steps]
    expanded = _expand_plan_prerequisites(
        steps,
        extra_context=extra_context,
        user_query=user_query,
    )
    compiler = WorkflowCompiler(
        lambda skills: _load_skill_plan_contracts(
            skill_registry, user_query, step_skills=skills,
        )
    )
    constrained = compiler.compile(
        expanded,
        entry_skills=entry_skills,
        context={
            "available_artifacts": list(available_artifacts or []),
        },
    )
    return _order_de_workflow_steps(constrained)


def _context_has_group_info(extra_context: str) -> bool:
    return bool(_GROUP_RE.search(extra_context or ""))


def _is_de_step(step: Dict[str, Any]) -> bool:
    text = " ".join(
        str(step.get(key) or "")
        for key in ("skill", "title", "goal", "result")
    )
    return bool(_DE_STEP_RE.search(text))


def _step_design(step: Dict[str, Any]) -> str:
    text = " ".join(
        str(step.get(key) or "")
        for key in ("title", "goal", "result")
    )
    if _UNPAIRED_RE.search(text):
        return "unpaired"
    if _PAIRED_RE.search(text) or _PATIENT_BLOCKING_RE.search(text):
        return "paired"
    return ""


def _step_modality(step: Dict[str, Any]) -> str:
    text = " ".join(
        str(step.get(key) or "")
        for key in ("title", "goal", "result")
    )
    if _FRAGMENTOMICS_RE.search(text):
        return "fragmentomics"
    if _ISOMIR_RE.search(text):
        return "isomir"
    if _MIRNA_RE.search(text) or _PIRNA_RE.search(text) or _TRNA_RE.search(text):
        return "srna"
    return "general"


def _step_assay(step: Dict[str, Any]) -> str:
    """Return the concrete assay represented by a workflow step."""
    text = " ".join(str(step.get(key) or "") for key in ("title", "goal", "result", "skill"))
    if _ISOMIR_RE.search(text):
        return "isomir"
    if _PIRNA_RE.search(text):
        return "pirna"
    if _TRNA_RE.search(text):
        return "trna"
    if _MIRNA_RE.search(text):
        return "mirna"
    return ""


_COUNT_PROVIDER_SKILLS = {
    "mirna": "mirdeep2-mirna",
    "isomir": "isomir-quantification",
    "pirna": "samtools_idxstats",
    "trna": "trna-fragment-quantification-with-trax",
}


_FEATURE_COUNT_RE = re.compile(r"feature[-_ ]?counts?", re.I)
_FEATURE_COUNT_NEGATION_RE = re.compile(
    r"(?:不|别|勿|无须|无需|不要|不能)\s*(?:再\s*)?(?:用|使用|采用|跑|做|选择)?\s*feature[-_ ]?counts?"
    r"|feature[-_ ]?counts?\s*(?:不|别|勿|无须|无需|不要|不能)(?:用|使用|采用|跑|做|选择)?",
    re.I,
)
_FEATURE_COUNT_REQUEST_RE = re.compile(
    r"(?:用|使用|采用|运行|跑|做|通过)\s*feature[-_ ]?counts?"
    r"|feature[-_ ]?counts?\s*(?:计数|定量|流程|分析|运行|跑|做)",
    re.I,
)


def _user_explicitly_requests_feature_count(text: str) -> bool:
    """Return true only for an affirmative user selection of featureCounts."""
    value = str(text or "")
    return bool(
        _FEATURE_COUNT_RE.search(value)
        and not _FEATURE_COUNT_NEGATION_RE.search(value)
        and _FEATURE_COUNT_REQUEST_RE.search(value)
    )


def _has_planned_count_provider(steps: List[Dict[str, Any]], assay: str) -> bool:
    provider = _COUNT_PROVIDER_SKILLS.get(str(assay or "").strip().lower())
    return bool(provider and any(
        str(step.get("skill") or "").strip().lower() == provider
        for step in steps if isinstance(step, dict)
    ))


def _step_identity(step: Dict[str, Any]) -> str:
    """Stable matching key for re-plans; positional plan IDs are not stable."""
    skill = str(step.get("skill") or "").strip().lower()
    title = re.sub(r"\s+", " ", str(step.get("title") or step.get("goal") or "").strip().lower())
    return "|".join((skill, _step_modality(step), title))


def _scope_request_text(user_query: str, history: Optional[List[Dict[str, Any]]] = None) -> str:
    """Keep the user's stated assay scope separate from generated plan text."""
    messages = [
        str(item.get("content") or "").strip()
        for item in (history or [])
        if isinstance(item, dict) and str(item.get("role") or "").lower() == "user"
    ]
    messages.append(str(user_query or "").strip())
    return "\n".join(message for message in messages if message)


def _derive_requested_scope(user_text: str) -> Dict[str, Any]:
    """Build a deterministic assay allowlist from user-authored text only."""
    text = str(user_text or "")
    assays = {
        "mirna": bool(_MIRNA_RE.search(text)),
        "pirna": bool(_PIRNA_RE.search(text)),
        "trna": bool(_TRNA_RE.search(text)),
        "isomir": bool(_ISOMIR_RE.search(text)),
        "fragmentomics": bool(_FRAGMENTOMICS_RE.search(text)),
    }
    requested = [name for name, enabled in assays.items() if enabled]
    modalities: List[str] = []
    if any(assays[name] for name in ("mirna", "pirna", "trna")):
        modalities.append("srna")
    if assays["isomir"]:
        modalities.append("isomir")
    if assays["fragmentomics"]:
        modalities.append("fragmentomics")
    target_prediction_only = bool(
        _TARGET_PREDICTION_RE.search(text)
        and not _QUANTIFICATION_RE.search(text)
        and not _EXPLICIT_QUANT_METHOD_RE.search(text)
        and not _EXPLICIT_DE_REQUEST_RE.search(text)
    )
    return {
        "requested_assays": requested,
        "requested_modalities": modalities,
        # isomiR is a biological entity as well as a quantification assay.
        # A target-only request must not therefore authorize an upstream FASTQ
        # workflow merely because its candidate sequences are isomiRs.
        "target_prediction_only": target_prediction_only,
        # An empty allowlist means the request did not name an assay. Keep
        # generic planning available rather than guessing an exclusion.
        "restricted": bool(requested),
    }


def _scope_block(scope: Dict[str, Any]) -> str:
    assays = ", ".join(scope.get("requested_assays") or []) or "unspecified"
    modalities = ", ".join(scope.get("requested_modalities") or []) or "unspecified"
    return (
        "Authorized analysis scope (derived only from user-authored requests):\n"
        f"- requested assays: [{assays}]\n"
        f"- requested modalities: [{modalities}]\n"
        "Do not add an assay, modality, or its prerequisites merely because it appears in session context, "
        "a skill description, or a draft plan. Expansion requires an explicit user request."
    )


def _enforce_requested_scope(
    goal: str,
    steps: List[Dict[str, Any]],
    *,
    scope: Dict[str, Any],
    fallback_goal: str,
) -> tuple[str, List[Dict[str, Any]]]:
    """Reject planner-introduced assays before they can become future context."""
    if not scope.get("restricted"):
        return str(goal or fallback_goal).strip(), [dict(step) for step in steps if isinstance(step, dict)]

    requested = set(scope.get("requested_assays") or [])
    target_prediction_only = bool(scope.get("target_prediction_only"))
    # featureCounts is a whole-genome annotation counter, not a generic
    # small-RNA count provider. Remove planner-inserted feature-count nodes
    # from miRNA/isomiR scopes unless the user explicitly named that method.
    explicit_feature_count = _user_explicitly_requests_feature_count(fallback_goal)
    keep: List[Dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        text = _plan_step_text(step)
        skill = str(step.get("skill") or "").strip().lower()
        if (
            skill == "feature-count"
            and not explicit_feature_count
            and bool(requested & {"mirna", "isomir"})
        ):
            continue
        is_fragment = bool(_FRAGMENTOMICS_RE.search(text)) or skill == "fragment-analysis"
        is_pirna = skill == "samtools_idxstats" or bool(
            _PIRNA_RE.search(text) and (_QUANTIFICATION_RE.search(text) or _QUANT_METHOD_TEXT_RE.search(text))
        )
        is_isomir = skill == "isomir-quantification" or bool(
            _ISOMIR_RE.search(text) and _QUANTIFICATION_RE.search(text)
        )
        is_upstream_isomir_work = skill in {
            "fastq-qc", "isomir-quantification", "alignment-srna",
        } or is_isomir
        is_target_work = skill == "mirna-target-prediction" or bool(_TARGET_PREDICTION_RE.search(text))
        is_unrequested_de_work = target_prediction_only and not is_target_work and (
            skill == "differential-analysis"
            or bool(_DE_STEP_RE.search(text))
            or bool(_COUNTS_RE.search(text))
            or bool(_GROUP_RE.search(text) and re.search(r"核查|读取|确认|设计|矩阵|count", text, re.I))
        )
        is_whole_genome_fragment_prereq = bool(
            re.search(r"whole[- ]?genome|全基因组", text, re.I)
            and re.search(r"align|alignment|比对|bowtie|bam", text, re.I)
        )
        if (
            (target_prediction_only and is_upstream_isomir_work)
            or is_unrequested_de_work
            or (is_fragment and "fragmentomics" not in requested)
            or (is_pirna and "pirna" not in requested)
            or (is_isomir and "isomir" not in requested)
            or (is_whole_genome_fragment_prereq and "fragmentomics" not in requested)
        ):
            continue
        cleaned = dict(step)
        # Shared reference checks may list an unrequested assay. Retain the
        # common assets but remove that assay from its executable instruction.
        for key in ("title", "goal"):
            value = str(cleaned.get(key) or "")
            if "pirna" not in requested:
                value = re.sub(r"(?:\s*[、,/+，]\s*)?(?:piRNA|piRBase)(?:\s*\([^)]*\))?", "", value, flags=re.I)
            if "isomir" not in requested:
                value = re.sub(r"(?:\s*[、,/+，]\s*)?(?:iso[- ]?miR|isomiR|mirtop)(?:\s*\([^)]*\))?", "", value, flags=re.I)
            cleaned[key] = re.sub(r"\s{2,}", " ", value).strip(" 、,，/+；;")
        keep.append(cleaned)

    goal_text = str(goal or "").strip()
    if (
        ("fragmentomics" not in requested and _FRAGMENTOMICS_RE.search(goal_text))
        or ("pirna" not in requested and _PIRNA_RE.search(goal_text))
        or ("isomir" not in requested and _ISOMIR_RE.search(goal_text))
    ):
        goal_text = str(fallback_goal or goal_text).strip()
    # A scope repair may remove an incompatible prerequisite (for example a
    # planner-inserted featureCounts gate). Do not leave surviving steps
    # blocked forever by dependencies on nodes that were just rejected.
    kept_ids = {str(step.get("id")) for step in keep if step.get("id") is not None}
    for step in keep:
        dependencies = step.get("depends_on")
        if isinstance(dependencies, list):
            step["depends_on"] = [
                str(dep) for dep in dependencies if str(dep) in kept_ids
            ]
    return goal_text, keep


def _requires_srna_and_fragmentomics(user_query: str, steps: List[Dict[str, Any]]) -> bool:
    # Never let model-generated step text expand the requested modality set.
    combined = str(user_query or "")
    return bool(
        (_MIRNA_RE.search(combined) or _PIRNA_RE.search(combined) or _TRNA_RE.search(combined))
        and _FRAGMENTOMICS_RE.search(combined)
    )


def _apply_modality_boundaries(
    goal: str,
    steps: List[Dict[str, Any]],
    *,
    user_query: str,
) -> tuple[str, List[Dict[str, Any]]]:
    """Enforce the current one-AnnData-per-modality analysis boundary."""
    normalized_goal = str(goal or user_query).strip()
    has_srna_and_fragmentomics = _requires_srna_and_fragmentomics(user_query, steps)
    if has_srna_and_fragmentomics:
        normalized_goal = re.sub(
            r"miRNA(?:\s*[、,，和与+]+\s*piRNA)?\s*[、,，和与+]+\s*tRNA\s*[、,，和与+]+\s*片段组学\s*(?:三|四|3|4)(?:类|种|模态)",
            "miRNA、piRNA 与 tRNA（同属 srna）及片段组学两种模态",
            normalized_goal,
            flags=re.I,
        )
        normalized_goal = re.sub(r"(?:三|四|3|4)\s*模态", "两种模态", normalized_goal, flags=re.I)
        normalized_goal = re.sub(
            r"各自独立的?\s*AnnData",
            "srna 与 fragmentomics 的独立 AnnData",
            normalized_goal,
            flags=re.I,
        )
        if "srna" not in normalized_goal.lower() or "两种模态" not in normalized_goal:
            normalized_goal = (
                f"{normalized_goal}（miRNA、piRNA 与 tRNA 共用 srna AnnData；"
                "片段组学使用独立 fragmentomics AnnData）"
            )

    normalized_goal = re.sub(r"\s*(?:→|->)?\s*MuData\b", "", normalized_goal, flags=re.I)
    normalized_goal = re.sub(r"\s*(?:→|->)?\s*h5mu\b", "", normalized_goal, flags=re.I)

    bounded: List[Dict[str, Any]] = []
    for step in steps:
        updated = dict(step)
        modality = _step_modality(updated)
        step_text = " ".join(str(updated.get(key) or "") for key in ("title", "goal", "skill"))
        # The current workflow never creates a joint MuData container. A
        # planner-generated packaging step is redundant and must not survive.
        if _MUDATA_RE.search(step_text) and re.search(r"封装|打包|包装|联合|整合|h5mu", step_text, re.I):
            continue
        step_goal = str(updated.get("goal") or "").strip()
        step_goal = re.sub(r"(?:MuData|h5mu)[^；;。]*", "独立 fragmentomics AnnData", step_goal, flags=re.I)
        updated["goal"] = step_goal
        if modality == "srna" and "srna AnnData" not in step_goal:
            updated["goal"] = f"{step_goal}；miRNA、piRNA 与 tRNA/tRF 结果共用 srna AnnData。".strip("；")
        elif modality == "fragmentomics" and "fragmentomics AnnData" not in step_goal:
            updated["goal"] = f"{step_goal}；结果写入独立 fragmentomics AnnData。".strip("；")
        bounded.append(updated)
    return normalized_goal, bounded


def _user_named_method_for_modality(user_text: str, modality_re: re.Pattern[str]) -> bool:
    """Whether the user, rather than a generated plan, chose a method for one RNA type."""
    text = str(user_text or "")
    hits = list(modality_re.finditer(text))
    if not hits:
        return False
    for hit in hits:
        window = text[max(0, hit.start() - 96):min(len(text), hit.end() + 96)]
        if _EXPLICIT_QUANT_METHOD_RE.search(window):
            return True
    requested_types = int(bool(_PIRNA_RE.search(text))) + int(bool(_MIRNA_RE.search(text)))
    return requested_types == 1 and bool(_EXPLICIT_QUANT_METHOD_RE.search(text))


def _skill_default_for_quantification_step(
    skill_registry: Any,
    step: Dict[str, Any],
    *,
    user_text: str,
) -> str:
    """Select mandatory defaults from skill metadata, never from a planner method."""
    if not skill_registry:
        return ""
    text = _plan_step_text(step)
    if not _QUANTIFICATION_RE.search(text):
        return ""
    target = ""
    if _MIRNA_RE.search(text) and not _PIRNA_RE.search(text) and not _user_named_method_for_modality(user_text, _MIRNA_RE):
        target = "mirna_quantification"
    elif _PIRNA_RE.search(text) and not _MIRNA_RE.search(text) and not _user_named_method_for_modality(user_text, _PIRNA_RE):
        target = "pirna_quantification"
    if not target:
        return ""
    for metadata in getattr(skill_registry, "skill_metadata", {}).values():
        if str(metadata.metadata.get("default_for") or "").strip().lower() == target:
            return metadata.slug
    return ""


def _bind_execution_skill_from_registry(
    step: Dict[str, Any],
    skill_registry: Any,
    history: List[Dict[str, str]],
) -> None:
    """Make a skill-declared default authoritative immediately before execution."""
    user_text = "\n".join(
        str(item.get("content") or "")
        for item in history
        if isinstance(item, dict) and str(item.get("role") or "") == "user"
    )
    selected = _skill_default_for_quantification_step(
        skill_registry, step, user_text=user_text,
    )
    if not selected:
        return
    previous = str(step.get("skill") or "").strip()
    step["skill"] = selected
    replacement = "miRDeep2" if selected == "mirdeep2-mirna" else "samtools idxstats"
    for key in ("title", "goal"):
        step[key] = _QUANT_METHOD_TEXT_RE.sub(replacement, str(step.get(key) or ""))
    if previous != selected:
        step["skillBoundAtExecution"] = selected


def _replace_paired_with_unpaired(text: str) -> str:
    value = str(text or "")
    if not value:
        return value
    value = re.sub(r"(?<!un)\bpaired\b", "unpaired", value, flags=re.I)
    value = re.sub(r"(?<!非)配对", "非配对", value)
    value = re.sub(
        r"(patient[_\s-]*blocking|patient[_\s-]*block|donor[_\s-]*block|block(?:ing)?\s+by\s+patient)",
        "no patient blocking",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s{2,}", " ", value).strip()
    return value


def _rewrite_de_step_for_design(step: Dict[str, Any], design: str) -> Dict[str, Any]:
    next_step = dict(step)
    if design == "unpaired":
        next_step["title"] = _replace_paired_with_unpaired(str(step.get("title") or ""))
        goal = _replace_paired_with_unpaired(str(step.get("goal") or ""))
        if "非配对" not in goal and "unpaired" not in goal.lower():
            goal = f"{goal}；默认按非配对设计（不使用 patient blocking）".strip("；")
        next_step["goal"] = goal
    elif design == "paired":
        title = str(step.get("title") or "")
        goal = str(step.get("goal") or "")
        if not (_PAIRED_RE.search(title) or _UNPAIRED_RE.search(title)):
            next_step["title"] = f"{title}（paired）".strip()
        if "paired" not in goal.lower() and "配对" not in goal:
            next_step["goal"] = f"{goal}；按配对设计（允许 patient blocking）".strip("；")
    return next_step


def _infer_modalities_from_steps(steps: List[Dict[str, Any]]) -> List[str]:
    seen: List[str] = []
    for step in steps:
        if not _is_de_step(step):
            continue
        modality = _step_modality(step)
        if modality == "general":
            continue
        if modality not in seen:
            seen.append(modality)
    return seen


def _resolve_analysis_policy(
    user_query: str,
    extra_context: str,
    steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    combined = "\n".join(
        part for part in (str(user_query or "").strip(), str(extra_context or "").strip()) if part
    )
    explicit_unpaired = bool(_UNPAIRED_RE.search(user_query or ""))
    explicit_paired = bool(_PAIRED_RE.search(user_query or "")) and not explicit_unpaired
    paired_feasible: Optional[bool] = None
    if _PAIRED_INFEASIBLE_RE.search(combined):
        paired_feasible = False

    is_de_request = bool(_DE_STEP_RE.search(str(user_query or "")))
    if not is_de_request:
        design = ""
        source = "not_applicable"
    elif explicit_paired and paired_feasible is False:
        design = "needs_confirmation"
        source = "explicit_paired_but_infeasible"
    elif explicit_paired:
        design = "paired"
        source = "explicit_paired"
    elif explicit_unpaired:
        design = "unpaired"
        source = "explicit_unpaired"
    else:
        design = "unpaired"
        source = "default_unpaired"

    modalities = _infer_modalities_from_steps(steps)
    if not modalities and _UNIFIED_RE.search(combined):
        modalities = ["srna", "fragmentomics"]
    elif not modalities:
        if _MIRNA_RE.search(combined) or _PIRNA_RE.search(combined) or _TRNA_RE.search(combined):
            modalities.append("srna")
        if _FRAGMENTOMICS_RE.search(combined):
            modalities.append("fragmentomics")

    reason = ""
    if design == "needs_confirmation":
        reason = "用户要求 paired，但当前上下文显示 paired 不可行，必须先确认是否改为 unpaired。"
    elif paired_feasible is False and design == "unpaired":
        reason = "当前上下文显示 paired 不可行，因此默认按 unpaired 设计。"
    elif design == "unpaired" and source == "default_unpaired":
        reason = "差异分析默认按 unpaired 设计，除非用户明确指定 paired。"

    return {
        "design": design,
        "source": source,
        "paired_feasible": paired_feasible,
        "modalities": modalities,
        "reason": reason,
    }


def _apply_analysis_policy(
    steps: List[Dict[str, Any]],
    *,
    analysis: Dict[str, Any],
) -> List[Dict[str, Any]]:
    design = str(analysis.get("design") or "").strip().lower()
    if not design:
        return steps

    if design == "needs_confirmation":
        confirmation = _make_plan_step(
            step_id="analysis-confirmation",
            title="确认差异分析设计",
            goal=(
                "当前数据不支持 paired，但用户请求了 paired。必须先向用户确认："
                "是否改为 unpaired，或提供真实配对样本/配对表。"
            ),
            skill="",
            auto_inserted=True,
        )
        filtered = [step for step in steps if not _is_de_step(step)]
        return [confirmation, *filtered]

    kept: List[Dict[str, Any]] = []
    has_target_design: set[str] = set()
    for step in steps:
        if not _is_de_step(step):
            kept.append(step)
            continue
        modality = _step_modality(step)
        step_design = _step_design(step)
        if step_design == design:
            has_target_design.add(modality)

    for step in steps:
        if not _is_de_step(step):
            continue
        modality = _step_modality(step)
        step_design = _step_design(step)
        if step_design == design:
            kept.append(_rewrite_de_step_for_design(step, design))
            continue
        if step_design and step_design != design and modality in has_target_design:
            continue
        kept.append(_rewrite_de_step_for_design(step, design))

    # Rebuild sequential ids, but preserve special confirmation ids if any.
    renumbered: List[Dict[str, Any]] = []
    next_id = 1
    for step in kept:
        item = dict(step)
        if str(item.get("id") or "").startswith("analysis-"):
            renumbered.append(item)
            continue
        item["id"] = str(next_id)
        next_id += 1
        renumbered.append(item)
    return renumbered


def _resolve_deliverables_policy(
    user_query: str,
    extra_context: str,
    steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    # Workspace/session context may contain existing artifacts such as
    # multiqc_report.html. Those are evidence of prior work, not a request to
    # generate another report. Only the current user message can authorize it.
    html_report_requested = bool(_HTML_REPORT_RE.search(str(user_query or "")))
    has_report_step = any(
        _HTML_REPORT_RE.search(
            " ".join(str(step.get(key) or "") for key in ("title", "goal", "result"))
        )
        for step in steps
    )
    return {
        "html_report_requested": html_report_requested,
        "has_report_step": has_report_step,
    }


def _strip_unrequested_html_report(goal: str, *, requested: bool) -> str:
    """Keep an old report request from leaking into a new task's plan title."""
    text = str(goal or "").strip()
    if requested or not text:
        return text
    text = re.sub(
        r"(?:[，,、]\s*|\s+(?:and|with)\s+)?(?:并)?(?:生成|创建|输出|写出)?\s*(?:一个|一份|可交付的?)?\s*(?:HTML|html)\s*(?:报告|report)\b",
        "",
        text,
        flags=re.I,
    )
    return re.sub(r"\s{2,}", " ", text).strip(" ，,、；;")


def _apply_deliverables_policy(
    steps: List[Dict[str, Any]],
    *,
    deliverables: Dict[str, Any],
) -> List[Dict[str, Any]]:
    if not bool(deliverables.get("html_report_requested")):
        # Do not let the planner manufacture an unrequested HTML report step.
        return [
            step for step in steps
            if not _HTML_REPORT_RE.search(
                " ".join(str(step.get(key) or "") for key in ("title", "goal", "result"))
            )
        ]
    if bool(deliverables.get("has_report_step")):
        return steps

    appended = list(steps)
    report_step = _make_plan_step(
        step_id=str(len(appended) + 1),
        title="汇总差异结果并生成 HTML 报告",
        goal=(
            "汇总关键差异结果，生成真实的 HTML 报告文件（例如 report.html），"
            "并把 HTML 路径登记到产物/manifest 中；不要只给文字总结。"
        ),
        skill="",
        auto_inserted=True,
    )
    appended.append(report_step)
    deliverables["has_report_step"] = True
    return appended


def _is_quantification_step(step: Dict[str, Any]) -> bool:
    skill = str(step.get("skill") or "").strip().lower()
    text = " ".join(str(step.get(key) or "") for key in ("title", "goal", "skill"))
    return skill in {"isomir-quantification", "fragment-analysis"} or bool(
        _ISOMIR_RE.search(text) and re.search(r"定量|quant", text, re.I)
    ) or bool(
        re.search(r"fragmentomics|fragomics|片段组学", text, re.I)
        and re.search(r"定量|quant|提取|extract", text, re.I)
    )


def _is_de_input_validation_step(step: Dict[str, Any]) -> bool:
    text = " ".join(str(step.get(key) or "") for key in ("title", "goal", "skill"))
    return bool(re.search(
        r"核查.*(?:定量矩阵|counts?|count_matrix)|确认.*(?:样本)?分组|读取.*差异分析.*(?:分组|设计)",
        text,
        re.I,
    ))


def _order_de_workflow_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Enforce prerequisites -> quantification -> validation -> DE -> report."""
    # A target-candidate selector legitimately consumes differential results,
    # but it is not itself a DE workflow. Reordering it by title text breaks
    # its confirmation edge and can execute miRanda first.
    if not any(
        str(step.get("skill") or "").strip().lower() == "differential-analysis"
        for step in steps
        if isinstance(step, dict)
    ):
        return steps

    prerequisites: List[Dict[str, Any]] = []
    quantification: List[Dict[str, Any]] = []
    validation: List[Dict[str, Any]] = []
    de_steps: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    reports: List[Dict[str, Any]] = []
    for step in steps:
        text = " ".join(str(step.get(key) or "") for key in ("title", "goal", "skill"))
        if _is_de_input_validation_step(step):
            validation.append(step)
        elif _is_quantification_step(step):
            quantification.append(step)
        elif _is_de_step(step):
            de_steps.append(step)
        elif _HTML_REPORT_RE.search(text):
            reports.append(step)
        elif _DE_SUMMARY_RE.search(text):
            summaries.append(step)
        else:
            prerequisites.append(step)
    return [*prerequisites, *quantification, *validation, *de_steps, *summaries, *reports]


def _normalise_requirement_text(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip(" ，,。；;：:-")
    return value[:180]


def _extract_requirement_items_from_user_query(user_query: str) -> List[str]:
    raw = str(user_query or "").strip()
    if not raw:
        return []
    candidates = re.split(r"[\n。；;]", raw)
    items: List[str] = []
    seen: set[str] = set()
    for chunk in candidates:
        value = _normalise_requirement_text(chunk)
        if not value:
            continue
        if not (
            _REQUIREMENT_CUE_RE.search(value)
            or _HTML_REPORT_RE.search(value)
            or _MUDATA_RE.search(value)
            or _WHOLE_GENOME_BAM_RE.search(value)
            or _UNPAIRED_RE.search(value)
        ):
            continue
        if value not in seen:
            seen.add(value)
            items.append(value)
    return items[:8]


def _resolve_requirements_policy(
    user_query: str,
    extra_context: str,
    *,
    analysis: Optional[Dict[str, Any]] = None,
    deliverables: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    combined = "\n".join(
        part for part in (str(user_query or "").strip(), str(extra_context or "").strip()) if part
    )
    items = _extract_requirement_items_from_user_query(user_query)
    seen = {item for item in items}

    html_report_requested = bool((deliverables or {}).get("html_report_requested")) or bool(
        _HTML_REPORT_RE.search(str(user_query or ""))
    )
    # Existing session notes may mention an old MuData plan. They are not a
    # new user requirement, and the current workflow intentionally stays on
    # independent AnnData objects.
    mudata_required = bool(_MUDATA_RE.search(str(user_query or "")))
    whole_genome_bam_required = bool(_WHOLE_GENOME_BAM_RE.search(combined))
    isomir_parallel_workers: Optional[int] = None
    isomir_jobs_match = re.search(r"\bjobs\s*=\s*(\d{1,2})\b", str(user_query or ""), re.I)
    if _ISOMIR_PARALLEL_RE.search(str(user_query or "")) or (_ISOMIR_RE.search(str(user_query or "")) and isomir_jobs_match):
        worker_match = re.search(
            r"(?:最多|至多|用|以|)(\d{1,2})\s*(?:个|路)?\s*(?:样本|worker|workers|并发|并行|同时)",
            str(user_query or ""),
            re.I,
        )
        chinese_worker_match = re.search(
            r"([一二三四五六七八九十])\s*(?:个|路)?\s*(?:样本|并发|并行|同时)",
            str(user_query or ""),
        )
        if isomir_jobs_match:
            isomir_parallel_workers = max(2, min(int(isomir_jobs_match.group(1)), 32))
        elif worker_match:
            isomir_parallel_workers = max(2, min(int(worker_match.group(1)), 32))
        elif chinese_worker_match:
            isomir_parallel_workers = {
                "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
            }[chinese_worker_match.group(1)]
    isomir_rebuild_from_alignment = bool(_ISOMIR_REBUILD_RE.search(str(user_query or "")))
    default_unpaired = str((analysis or {}).get("design") or "").strip().lower() == "unpaired"
    if not default_unpaired and not bool(_PAIRED_RE.search(user_query or "")):
        if _UNPAIRED_RE.search(str(user_query or "")) or _DE_STEP_RE.search(str(user_query or "")):
            default_unpaired = True

    derived_items = []
    if default_unpaired:
        derived_items.append("差异分析默认使用非配对（unpaired），除非用户明确指定配对。")
    if html_report_requested:
        derived_items.append("若用户要求报告，必须生成真实交付物文件并登记路径，而不是只给文字总结。")
    if mudata_required:
        derived_items.append("如果已经有小RNA定量，片段组学结果必须放在 MuData 下并优先写出 h5mu。")
    if whole_genome_bam_required:
        derived_items.append("片段组学输入 BAM 必须是全基因组坐标系、与 genome FASTA 一致的 whole-genome BAM。")
    if isomir_rebuild_from_alignment:
        derived_items.append("这是一次新的 isomiR 重建：旧 mirtop 输出、旧 isomir AnnData 和中断计划均不可复用；从 hairpin 比对产物开始核验并重建。")
    if isomir_parallel_workers:
        derived_items.append(
            f"isomiR mirtop 必须按样本隔离输出目录并最多 {isomir_parallel_workers} 路并发；"
            "不得把全部 BAM 交给单次 sa.quant.mirtop 调用。"
        )

    for item in derived_items:
        normalized = _normalise_requirement_text(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            items.append(normalized)

    return {
        "items": items[:10],
        "html_report_requested": html_report_requested,
        "mudata_required": mudata_required,
        "whole_genome_bam_required": whole_genome_bam_required,
        "default_unpaired": default_unpaired,
        "isomir_parallel_workers": isomir_parallel_workers,
        "isomir_rebuild_from_alignment": isomir_rebuild_from_alignment,
    }


def _ensure_step(
    expanded: List[Dict[str, Any]],
    *,
    next_id: int,
    title: str,
    goal: str,
    skill: str,
) -> int:
    if skill and any(str(item.get("skill") or "").strip().lower() == skill.lower() for item in expanded):
        return next_id
    expanded.append(_make_plan_step(
        step_id=str(next_id),
        title=title,
        goal=goal,
        skill=skill,
        auto_inserted=True,
    ))
    return next_id + 1


def _expand_plan_prerequisites(
    steps: List[Dict[str, Any]],
    *,
    extra_context: str,
    user_query: str = "",
) -> List[Dict[str, Any]]:
    expanded: List[Dict[str, Any]] = []
    next_id = 1
    has_trimmed_fastq = _context_has_trimmed_fastq(extra_context)
    has_raw_fastq = _context_has_raw_fastq(extra_context)
    has_bam = _context_has_bam(extra_context)
    has_genome_fasta = _context_has_genome_fasta(extra_context)
    has_genome_index = _context_has_genome_index(extra_context)
    has_counts = _context_has_counts(extra_context)
    has_group_info = _context_has_group_info(extra_context)
    has_planned_count_providers = {
        assay: _has_planned_count_provider(steps, assay)
        for assay in _COUNT_PROVIDER_SKILLS
    }
    has_fragment_count_validation = False
    has_isomir_count_validation = False

    for step in steps:
        skill_slug = str(step.get("skill") or "").strip().lower()
        if skill_slug == "alignment-srna":
            if not has_trimmed_fastq and has_raw_fastq:
                next_id = _ensure_step(
                    expanded,
                    next_id=next_id,
                    title="完成 FASTQ 质控与修剪",
                    goal="先对原始 small RNA FASTQ 进行 adapter trimming 和质控，产出 trimmed_path / clean_fastq_path，供后续比对使用。",
                    skill="fastq-qc",
                )
                has_trimmed_fastq = True
            if not has_genome_fasta:
                next_id = _ensure_step(
                    expanded,
                    next_id=next_id,
                    title="准备参考基因组 FASTA",
                    goal="先下载或确认可用的参考基因组 FASTA，供构建 Bowtie index 和后续比对使用。",
                    skill="reference-download",
                )
                has_genome_fasta = True
            if not has_genome_index:
                has_genome_index = True
        elif skill_slug == "feature-count":
            if not has_bam:
                if not has_trimmed_fastq and has_raw_fastq:
                    next_id = _ensure_step(
                        expanded,
                        next_id=next_id,
                        title="完成 FASTQ 质控与修剪",
                        goal="先对原始 small RNA FASTQ 进行 adapter trimming 和质控，产出 trimmed_path / clean_fastq_path，供后续比对和计数使用。",
                        skill="fastq-qc",
                    )
                    has_trimmed_fastq = True
                if not has_genome_fasta:
                    next_id = _ensure_step(
                        expanded,
                        next_id=next_id,
                        title="准备参考基因组 FASTA",
                        goal="确保已有参考基因组 FASTA，用于构建 Bowtie index 和后续基因组比对。",
                        skill="reference-download",
                    )
                    has_genome_fasta = True
                next_id = _ensure_step(
                    expanded,
                    next_id=next_id,
                    title="完成参考基因组比对",
                    goal="先生成 genome-aligned coordinate-sorted BAM（bam_path），供 featureCounts 计数使用。",
                    skill="alignment-srna",
                )
                has_bam = True
        elif skill_slug == "samtools_idxstats":
            if not has_bam:
                if not has_trimmed_fastq and has_raw_fastq:
                    next_id = _ensure_step(
                        expanded,
                        next_id=next_id,
                        title="完成 FASTQ 质控与修剪",
                        goal="先对原始 small RNA FASTQ 做 adapter trimming 和质控，产出 trimmed_path / clean_fastq_path，供小 RNA 参考序列比对使用。",
                        skill="fastq-qc",
                    )
                    has_trimmed_fastq = True
                next_id = _ensure_step(
                    expanded,
                    next_id=next_id,
                    title="完成 small-RNA 参考序列比对",
                    goal="先生成针对 small-RNA FASTA reference 的 BAM（bam_path），再执行 samtools idxstats 定量。",
                    skill="alignment-srna",
                )
                has_bam = True
        elif skill_slug == "fragment-analysis":
            if not has_trimmed_fastq and has_raw_fastq:
                next_id = _ensure_step(
                    expanded,
                    next_id=next_id,
                    title="完成 FASTQ 质控与修剪",
                    goal="先对原始 small RNA FASTQ 进行 adapter trimming 和质控，产出 trimmed_path / clean_fastq_path，供后续 fragmentomics 使用。",
                    skill="fastq-qc",
                )
                has_trimmed_fastq = True
            if not has_bam:
                next_id = _ensure_step(
                    expanded,
                    next_id=next_id,
                    title="完成参考基因组比对",
                    goal="生成 coordinate-sorted BAM（bam_path）以支持 FSC、RCD 和 BPM 特征提取；若缺上游 QC FASTQ，则先使用已有 trimmed FASTQ 进行比对。",
                    skill="alignment-srna",
                )
                has_bam = True
            if not has_genome_fasta:
                next_id = _ensure_step(
                    expanded,
                    next_id=next_id,
                    title="准备参考基因组 FASTA",
                    goal="确保 reference-download 提供 genome_fasta 或已有可用参考 FASTA，供 BPM 断点基序计算使用。",
                    skill="reference-download",
                )
                has_genome_fasta = True
        elif skill_slug == "differential-analysis":
            modality = _step_modality(step)
            if modality == "fragmentomics":
                if (
                    not _context_has_counts_for_modality(extra_context, "fragmentomics")
                    and not has_fragment_count_validation
                ):
                    next_id = _ensure_step(
                        expanded,
                        next_id=next_id,
                        title="核查 fragmentomics 定量矩阵",
                        goal=(
                            "读取独立 fragmentomics AnnData，确认 layers['counts'] / X 为 raw counts、"
                            "样本与 feature 对齐；不运行 small-RNA 定量，也不修改 srna AnnData。"
                        ),
                        skill="",
                    )
                    has_fragment_count_validation = True
                if not has_group_info:
                    step_copy = _make_plan_step(
                        step_id=str(next_id),
                        title="确认 fragmentomics 样本分组信息",
                        goal="检查 fragmentomics_adata.obs 的 group/Condition/treatment 列，向用户确认后再运行差异分析。",
                        skill="",
                        auto_inserted=True,
                    )
                    expanded.append(step_copy)
                    next_id += 1
                    has_group_info = True
                expanded.append(_make_plan_step(
                    step_id=str(next_id),
                    title=str(step.get("title") or "fragmentomics 差异分析（limma-voom）"),
                    goal=str(step.get("goal") or "使用独立 fragmentomics AnnData 的 raw counts 运行 limma-voom 差异分析。"),
                    skill=str(step.get("skill") or "differential-analysis"),
                    status=str(step.get("status") or STEP_PENDING),
                    result=str(step.get("result") or ""),
                    auto_inserted=bool(step.get("autoInserted")),
                ))
                next_id += 1
                continue
            if modality == "isomir":
                if (
                    not _context_has_counts_for_modality(extra_context, "isomir")
                    and not has_isomir_count_validation
                ):
                    next_id = _ensure_step(
                        expanded,
                        next_id=next_id,
                        title="核查 isomiR 定量矩阵",
                        goal=(
                            "读取独立 isomir AnnData，确认 mirtop 已写入 layers['counts'] / X，"
                            "样本、isomiR 特征与分组可用于差异分析；不读取 srna AnnData 作为 isomiR counts。"
                        ),
                        skill="",
                    )
                    has_isomir_count_validation = True
            assay = _step_assay(step)
            has_provider = has_planned_count_providers.get(assay, False)
            if not has_counts and not has_provider:
                # Count matrices must come from the assay's declared provider.
                # In particular, never synthesize featureCounts for a miRNA or
                # isomiR DE step just because the context lacks a generic
                # `counts` token.
                provider = _COUNT_PROVIDER_SKILLS.get(assay)
                if provider and assay in {"mirna", "isomir", "pirna", "trna"}:
                    provider_titles = {
                        "mirna": ("miRNA 定量（miRDeep2）", "用 miRDeep2 生成已知 miRNA raw counts，写入 srna AnnData。"),
                        "isomir": ("isomiR 定量（mirtop）", "用 mirtop 从 hairpin BAM 生成独立 isomiR raw counts。"),
                        "pirna": ("piRNA 定量（samtools idxstats）", "用 samtools idxstats 对 piRNA FASTA 参考计数。"),
                        "trna": ("tRNA/tRF 定量（tRAX）", "用 tRAX 生成独立 tRNA/tRF raw counts。"),
                    }
                    title, goal = provider_titles[assay]
                    next_id = _ensure_step(
                        expanded,
                        next_id=next_id,
                        title=title,
                        goal=goal,
                        skill=provider,
                    )
                    has_planned_count_providers[assay] = True
                    # Keep the DE node after its assay-specific count
                    # provider. The provider is a prerequisite, not a
                    # replacement for the requested differential analysis.
                    expanded.append(_make_plan_step(
                        step_id=str(next_id),
                        title=str(step.get("title") or "差异分析"),
                        goal=str(step.get("goal") or "运行差异分析"),
                        skill="differential-analysis",
                        status=str(step.get("status") or STEP_PENDING),
                        result=str(step.get("result") or ""),
                        auto_inserted=bool(step.get("autoInserted")),
                    ))
                    next_id += 1
                    continue
                # A generic DE request does not authorize choosing a counting
                # engine. Validate the existing raw-count input and let the
                # user or an explicit method request determine any missing
                # quantification instead of defaulting to featureCounts.
                if not _user_explicitly_requests_feature_count(user_query):
                    next_id = _ensure_step(
                        expanded,
                        next_id=next_id,
                        title="核查现有 raw counts 矩阵",
                        goal="只读检查对应 AnnData 是否已有 raw counts；不得自行选择 featureCounts 或替换既定定量方法。",
                        skill="",
                    )
                    has_counts = True
                else:
                    if not has_bam:
                        if not has_trimmed_fastq and has_raw_fastq:
                            next_id = _ensure_step(
                                expanded,
                                next_id=next_id,
                                title="完成 FASTQ 质控与修剪",
                                goal="先做 small RNA FASTQ 质控与 adapter trimming，为后续定量准备输入。",
                                skill="fastq-qc",
                            )
                            has_trimmed_fastq = True
                        if not has_genome_fasta:
                            next_id = _ensure_step(
                                expanded,
                                next_id=next_id,
                                title="准备参考基因组 FASTA",
                                goal="先准备参考基因组 FASTA，供显式请求的 featureCounts 流程使用。",
                                skill="reference-download",
                            )
                            has_genome_fasta = True
                        next_id = _ensure_step(
                            expanded,
                            next_id=next_id,
                            title="完成参考基因组比对",
                            goal="先生成 BAM（bam_path），供用户明确请求的 featureCounts 计数使用。",
                            skill="alignment-srna",
                        )
                        has_bam = True
                    next_id = _ensure_step(
                        expanded,
                        next_id=next_id,
                        title="生成 featureCounts 定量矩阵",
                        goal="仅按用户明确指定的 featureCounts 方法生成 raw counts。",
                        skill="feature-count",
                    )
                    has_counts = True
                # The differential-analysis workflow contract owns the
                # evidence preflight and approval gate.
                expanded.append(_make_plan_step(
                    step_id=str(next_id),
                    title=str(step.get("title") or "差异分析"),
                    goal=str(step.get("goal") or "运行差异分析"),
                    skill="differential-analysis",
                    status=str(step.get("status") or STEP_PENDING),
                    result=str(step.get("result") or ""),
                    auto_inserted=bool(step.get("autoInserted")),
                ))
                next_id += 1
                continue
            # The differential-analysis workflow contract owns the evidence
            # preflight and approval gate. Do not create an untyped duplicate
            # here; it cannot carry the artifact dependency to DE.
        elif skill_slug == "mirdeep2-mirna":
            if not has_genome_fasta:
                next_id = _ensure_step(
                    expanded,
                    next_id=next_id,
                    title="准备 miRNA 参考资源",
                    goal="先准备 genome FASTA 与 miRBase 参考资源，供 miRDeep2 定量或预测使用。",
                    skill="reference-download",
                )
                has_genome_fasta = True
            if not has_trimmed_fastq and has_raw_fastq:
                next_id = _ensure_step(
                    expanded,
                    next_id=next_id,
                    title="完成 FASTQ 质控与修剪",
                    goal="优先用 cutadapt 完成 small RNA FASTQ 的 adapter trimming，为 miRDeep2 提供更干净的输入。",
                    skill="fastq-qc",
                )
                has_trimmed_fastq = True
        elif skill_slug == "isomir-quantification":
            # mirtop creates the dedicated isomiR AnnData and its raw counts;
            # a generic small-RNA feature-count step is neither required nor
            # biologically equivalent for variant-level isomiR analysis.
            has_counts = True

        if skill_slug not in {
            "alignment-srna",
            "feature-count",
            "samtools_idxstats",
            "fragment-analysis",
            "differential-analysis",
            "mirdeep2-mirna",
            "isomir-quantification",
        }:
            # This is not an expansion target. Preserve the complete node:
            # contract IDs, artifact edges, approval state, and completed
            # status are semantic data, not presentational fields that can be
            # recreated from title/goal text.
            preserved = deepcopy(step)
            preserved.setdefault("id", str(next_id))
            preserved.setdefault("title", f"Step {next_id}")
            preserved.setdefault("goal", str(preserved.get("title") or ""))
            preserved.setdefault("skill", "")
            preserved.setdefault("status", STEP_PENDING)
            preserved.setdefault("result", "")
            expanded.append(preserved)
            next_id += 1
            continue
        expanded.append(_make_plan_step(
            step_id=str(next_id),
            title=str(step.get("title") or "执行片段组学分析"),
            goal=str(step.get("goal") or step.get("title") or ""),
            skill=str(step.get("skill") or ""),
            status=str(step.get("status") or STEP_PENDING),
            result=str(step.get("result") or ""),
            auto_inserted=bool(step.get("autoInserted")),
        ))
        next_id += 1

    return expanded or steps


def _build_planner_system_prompt(skill_overview: str, workflow_guidance: str = "") -> str:
    skills_block = skill_overview or "(no skills loaded)"
    workflow_block = ""
    if workflow_guidance:
        workflow_block = (
            "\n## Bound workflow guidance\n"
            "The following matched SKILL.md content is the source of truth for this request. "
            "Turn every mandatory transformation, required input, and prohibited shortcut into an explicit "
            "ordered plan step. Do not replace a skill requirement with a generic shortcut.\n\n"
            f"{workflow_guidance}\n"
        )
    try:
        from .srn_agent import _load_agent_constitution

        constitution = _load_agent_constitution()
    except Exception:
        constitution = ""
    constitution_block = ""
    if constitution:
        constitution_block = (
            "\n## Agent constitution (from sRNAgent/AGENT.md)\n"
            "When planning pipeline steps, respect these hard rules "
            "(adata must receive return values; check obs columns before re-running steps):\n\n"
            f"{constitution}\n"
        )
    return (
        "You are the Planner for sRNAgent, a small RNA-seq analysis assistant.\n"
        "Your job is to break the user's request into clear, sequential subtasks.\n"
        "You do NOT execute tools yourself — only output a JSON plan.\n\n"
        "## Output format (strict JSON only, no markdown fences)\n"
        "{\n"
        '  "goal": "one-line summary of the overall task",\n'
        '  "steps": [\n'
        "    {\n"
        '      "id": "1",\n'
        '      "title": "short step title",\n'
        '      "goal": "specific objective for this step only",\n'
        '      "skill": "optional skill slug from registered skills, or empty string",\n'
        '      "approval": "optional object; required when this step needs a user decision. Include id, prompt, and review.fields/edit_hint"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "## Planning rules\n"
        "1. For simple questions (no code/pipeline), use a single step.\n"
        "2. For multi-step pipelines, split by natural phases: download → QC → "
        "reference → alignment → quantification.\n"
        "3. Each step must be independently completable in one focused execution session.\n"
        "4. Do not duplicate work already marked done in session context.\n"
        "4a. Before returning the plan, reason over an artifact dependency graph: every step that consumes an "
        "artifact must follow its producer. A synthesis deliverable (report, table, dashboard, export, or summary) "
        "must follow every requested analysis whose outputs it presents.\n"
        "4b. A user confirmation is a blocking state, not an executable step. Whenever a step requires a choice, "
        "confirmation, parameter, or grouping from the user, emit an `approval` object containing an id, prompt, "
        "and review fields that display the concrete values or clearly say what is unknown.\n"
        "5. Prefer skill slugs when a step matches a registered skill.\n"
        "6. Keep 1–8 steps; split oversized steps rather than one giant step.\n"
        "7. For differential analysis, default to unpaired unless the user explicitly asks for paired.\n"
        "8. If session context says paired is not feasible, do NOT plan paired DE steps.\n"
        "9. Do not keep paired and unpaired DE branches simultaneously unless the user explicitly asks to compare both.\n"
        "10. Model requested deliverables by the artifacts they consume; do not schedule a consumer before its inputs exist.\n"
        "11. miRNA, piRNA, and tRNA/tRF are one `srna` modality and must share one srna AnnData. "
        "They are not separate modalities. A request for miRNA + piRNA + tRNA + fragmentomics therefore has exactly "
        "two independent AnnData outputs: srna and fragmentomics. Do not propose MuData or joint analysis.\n"
        "12. If the latest request refers to earlier work ('continue', 'on this basis', '刚才的结果'), "
        "use the recent conversation and session context to identify completed prerequisites. "
        "Plan only the new requested work; never recreate completed QC, alignment, or quantification steps "
        "unless the relevant artifact is missing.\n"
        "13. A request that explicitly clears/rebuilds mirtop or says to restart from hairpin alignment starts "
        "a NEW isomiR workflow. Do not restore an interrupted mirtop plan or reuse its in-memory AnnData. "
        "If the user requests parallel isomiR samples, plan per-sample isolated mirtop GFF jobs and a final "
        "aggregation step. mirtop gff has no jobs/threads option, so never claim that sa.quant.mirtop can "
        "provide sample-level parallelism.\n"
        "14. For any isomiR workflow starting from trimmed/clean FASTQ, insert sequence collapse before hairpin "
        "alignment: `sa.fastq.seqcluster_collapse` -> `collapsed_path` -> Bowtie -> new hairpin BAM -> mirtop. "
        "Never describe or execute a trimmed FASTQ -> Bowtie -> mirtop path, and never reuse BAM produced from "
        "un-collapsed FASTQ.\n"
        "15. Default small-RNA quantification methods: for piRNA use `samtools_idxstats` after alignment to a "
        "piRNA FASTA reference; do not use featureCounts unless the user explicitly requests featureCounts. "
        "For miRNA quantification use `mirdeep2-mirna` / miRDeep2; do not substitute featureCounts unless "
        "the user explicitly requests it.\n"
        "16. A plan is a revisable working model, not a commitment to a fixed workflow. When the user adds a "
        "constraint, correction, priority, or requested prerequisite, revise ordering and scope around that newest "
        "instruction while preserving completed artifacts. Infer the requested action from the user's words; do not "
        "force it into a prewritten menu or ask for approval when the user has already specified what to investigate.\n\n"
        "## Registered skills\n"
        f"{skills_block}\n"
        f"{workflow_block}"
        f"{constitution_block}"
    )


def _build_plan_review_system_prompt(skill_guidance: str = "") -> str:
    guidance = f"\n\n## Relevant SKILL.md guidance\n{skill_guidance}" if skill_guidance else ""
    return (
        "You are the plan reviewer for a scientific analysis agent. You do not execute work. "
        "Review a proposed JSON plan against the user's request and the supplied skill guidance, then return a "
        "corrected JSON plan only.\n\n"
        "Construct the dependency graph from declared inputs and outputs. Every consumer must be after its producer. "
        "A synthesis deliverable such as a report, table, dashboard, export, or summary must be after every requested "
        "analysis whose output it consumes; it cannot claim unavailable results. Preserve valid work, remove duplicates, "
        "and add missing prerequisite steps only when required by a skill. Do not invent methods, files, or assumptions.\n\n"
        "A step that asks the user to confirm, choose, provide, or validate any configuration MUST carry an `approval` "
        "object (`id`, `prompt`, and `review.fields`/`review.edit_hint`). The review fields must show concrete known "
        "values; if a required value is unknown, they must state what the user should provide or where to retrieve it. "
        "Such a step is a blocking gate and cannot be an ordinary executable "
        "step. In particular, do not place any downstream analysis after an unrepresented user decision.\n\n"
        "Return exactly {\"goal\": string, \"steps\": [{\"id\": string, \"title\": string, \"goal\": string, "
        "\"skill\": string, \"depends_on\": [step id], \"inputs\": [artifact], \"outputs\": [artifact], "
        "optional \"approval\": object}]}. Include every approval required by a skill contract in the plan."
        f"{guidance}"
    )


def _load_plan_review_skill_guidance(
    skill_registry: Any,
    raw_steps: Any,
    user_query: str,
) -> str:
    """Give the reviewer the skills named by the draft plus relevant discovered skills."""
    if not skill_registry:
        return ""
    slugs: List[str] = []
    for step in raw_steps if isinstance(raw_steps, list) else []:
        if isinstance(step, dict):
            slug = str(step.get("skill") or "").strip().lower()
            if slug and slug not in slugs:
                slugs.append(slug)
    for metadata, _score in rank_skill_matches(skill_registry, user_query)[:6]:
        if metadata.slug not in slugs:
            slugs.append(metadata.slug)
    sections: List[str] = []
    for slug in slugs[:8]:
        skill = skill_registry.load_full_skill(slug)
        if skill is not None:
            section = f"=== {skill.slug} ===\n{skill.prompt_instructions(max_chars=2500)}"
            contract_path = skill.path / "plan_contract.json"
            try:
                contract = json.loads(contract_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                contract = None
            if contract:
                section += "\n\nPlan contract (follow its dependencies and approvals):\n" + json.dumps(
                    contract, ensure_ascii=False,
                )
            sections.append(section)
    return "\n\n".join(sections)


def _build_replanner_system_prompt(skill_overview: str, workflow_guidance: str = "") -> str:
    base = _build_planner_system_prompt(skill_overview, workflow_guidance)
    return (
        f"{base}\n"
        "## Replanning mode\n"
        "You are revising an existing plan based on step results or failures.\n"
        "- Keep completed steps as status \"done\" with their results.\n"
        "- Mark failed steps as \"failed\" or replace them with smaller retry steps.\n"
        "- Add new steps only if needed; remove redundant pending steps.\n"
        "- Treat the newest user instruction as a change request to the plan, including an instruction to perform "
        "a prerequisite investigation before an earlier approval. Preserve completed work, but reorder, replace, "
        "or remove pending steps when that is necessary to satisfy the new instruction.\n"
        "- This replanning pass also runs after successful steps. Treat their result as newly observed evidence, "
        "not merely a completion note. Before retaining each pending step, decide whether its required artifact "
        "already exists, is compatible with the requested analysis, and can be safely reused. Skip or replace "
        "redundant work; keep validation when compatibility is uncertain. In particular, never rebuild or download "
        "a high-cost reference/index solely because the original plan said to do so after evidence shows a usable one.\n"
        "- Output the FULL updated plan JSON (all steps with status).\n"
    )


def _build_executor_system_prompt(
    agent_system_prompt: str,
    *,
    step: Dict[str, Any],
    step_index: int,
    step_total: int,
    plan_goal: str,
    analysis: Optional[Dict[str, Any]] = None,
    deliverables: Optional[Dict[str, Any]] = None,
    requirements: Optional[Dict[str, Any]] = None,
    confirmed_approvals: str = "",
    skill_prompt: str = "",
    conversation_context: str = "",
) -> str:
    skill_hint = ""
    if step.get("skill"):
        skill_hint = (
            f"\nRecommended skill for this step: `{step['skill']}` "
            "(call search_skills first if you need workflow guidance)."
        )
    skill_block = ""
    if skill_prompt:
        skill_block = (
            "\n## Bound workflow skill for this subtask\n"
            "Follow the workflow constraints below as hard guidance for this step.\n\n"
            f"{skill_prompt}\n"
        )
    execution_contract_block = ""
    execution_contracts = step.get("executionContracts") if isinstance(step.get("executionContracts"), list) else []
    instructions = [
        str(item.get("instructions") or "").strip()
        for item in execution_contracts
        if isinstance(item, dict) and str(item.get("instructions") or "").strip()
    ]
    if instructions:
        execution_contract_block = (
            "\n## Bound execution contract\n"
            "These constraints are supplied by the selected skill and are mandatory for this step.\n"
            + "".join(f"- {instruction}\n" for instruction in instructions)
        )
    analysis_block = ""
    if isinstance(analysis, dict) and analysis:
        modalities = analysis.get("modalities") or []
        modalities_text = ", ".join(str(item) for item in modalities) if modalities else "(unspecified)"
        analysis_block = (
            "\n## Structured analysis policy\n"
            f"- analysis.design = {analysis.get('design') or 'unknown'}\n"
            f"- analysis.modalities = [{modalities_text}]\n"
            f"- analysis.paired_feasible = {analysis.get('paired_feasible')}\n"
            f"- analysis.reason = {analysis.get('reason') or ''}\n"
            "Hard rule: for differential analysis, default to unpaired unless the user explicitly confirmed paired.\n"
            "If analysis.design is unpaired, do not use paired design, patient blocking, donor blocking, or group+patient_id formulas.\n"
            "If analysis.design needs_confirmation, do not run DE code; first ask the user to resolve the conflict.\n"
        )
    deliverables_block = ""
    if isinstance(deliverables, dict) and deliverables:
        deliverables_block = (
            "\n## Required deliverables\n"
            f"- html_report_requested = {bool(deliverables.get('html_report_requested'))}\n"
        )
        if bool(deliverables.get("html_report_requested")):
            deliverables_block += (
                "Hard rule: if the current task is report generation, write a real .html file and surface its path.\n"
                "Do not replace the HTML artifact with a plain-text summary or only session run_report output.\n"
            )
    requirements_block = ""
    if isinstance(requirements, dict) and requirements:
        items = [str(item).strip() for item in (requirements.get("items") or []) if str(item).strip()]
        requirements_block = (
            "\n## High priority user requirements\n"
            f"- requirements.default_unpaired = {bool(requirements.get('default_unpaired'))}\n"
            f"- requirements.html_report_requested = {bool(requirements.get('html_report_requested'))}\n"
            f"- requirements.mudata_required = {bool(requirements.get('mudata_required'))}\n"
            f"- requirements.whole_genome_bam_required = {bool(requirements.get('whole_genome_bam_required'))}\n"
            f"- requirements.isomir_rebuild_from_alignment = {bool(requirements.get('isomir_rebuild_from_alignment'))}\n"
            f"- requirements.isomir_parallel_workers = {requirements.get('isomir_parallel_workers') or 'none'}\n"
        )
        if items:
            requirements_block += "\n".join(f"- requirement: {item}\n" for item in items)
        requirements_block += (
            "Hard rule: do not ignore, overwrite, or silently drop these user requirements during replanning or execution.\n"
        )
        if requirements.get("isomir_rebuild_from_alignment"):
            requirements_block += (
                "Hard rule: this is a new workflow scope. Verify or recreate the requested hairpin BAM inputs, "
                "then rebuild isomiR outputs. Do not resume an interrupted mirtop subprocess or trust its old plan status.\n"
            )
        if requirements.get("isomir_parallel_workers"):
            requirements_block += (
                "Hard rule: upstream `mirtop gff` has no `jobs` or `threads` option. Do not call "
                "`sa.quant.mirtop` once over all BAMs for this task. Launch one mirtop GFF job per sample in "
                "a private staging directory, limit concurrent jobs to the requested worker count, validate each "
                "per-sample GFF, then aggregate and persist the independent isomir AnnData. Drive UI progress from "
                "worker completion/return events: after each completed worker print exactly `progress: N / TOTAL <sample>` "
                "with `flush=True`. Do not wait until all worker threads join to print progress, and do not use "
                "`capture_output=True` for mirtop's high-volume logs; redirect each worker's output to its own log file.\n"
            )
    approval_block = ""
    if confirmed_approvals:
        approval_block = (
            "\n## User-confirmed configuration\n"
            "The user explicitly confirmed or changed these settings. Apply them exactly; "
            "they override skill examples and defaults. An explicit assignment in the user's reply "
            "overrides any earlier detected value.\n"
            f"{confirmed_approvals}\n"
        )
    conversation_block = ""
    if conversation_context:
        conversation_block = (
            "\n## Recent conversation context\n"
            "Use this context to resolve references to earlier completed work. "
            "Do not repeat a completed operation unless the current request explicitly asks to rerun it.\n"
            f"{conversation_context}\n"
        )
    step_block = (
        f"\n## Current subtask ({step_index}/{step_total})\n"
        f"Title: {step.get('title') or 'Subtask'}\n"
        f"Goal: {step.get('goal') or plan_goal}\n"
        f"{skill_hint}\n\n"
        f"{execution_contract_block}"
        "IMPORTANT: Complete ONLY this subtask in this session.\n"
        "- Do not start later pipeline stages.\n"
        "- When done, call `finish` with your message TO THE USER.\n"
        "- The finish message is shown directly in chat — reply naturally in second person.\n"
        "- NEVER write internal status reports (e.g. '已向用户…', '等待用户下一步', "
        "'Task completed', 'Step done').\n"
        "- The Jupyter kernel state (e.g. adata) persists across steps.\n"
        "- For any multi-item or parallel operation, use `sRNAgent._utils.run_threads` instead of hand-written "
        "ThreadPoolExecutor, ProcessPoolExecutor, Semaphore, or thread join loops. It emits standard `progress: N/M` "
        "and `inflight:` events compatible with the UI.\n"
    )
    return f"{agent_system_prompt}\n{skill_block}{analysis_block}{deliverables_block}{requirements_block}{approval_block}{conversation_block}{step_block}"


def _build_step_user_message(
    *,
    user_query: str,
    step: Dict[str, Any],
    step_total: int = 1,
) -> str:
    # Single-step conversational tasks: pass user message through directly.
    if step_total == 1 and not str(step.get("skill") or "").strip():
        return user_query
    return (
        f"Execute this subtask now:\n\n"
        f"**{step.get('title') or 'Subtask'}**\n"
        f"{step.get('goal') or ''}\n\n"
        f"Original user request for context:\n{user_query}"
    )


# Whole-phrase cancel markers only. A bare "cancelled" substring used to false-
# fail steps whose success prose mentioned cancelled samples / filters.
_CANCELLED_RESULT_MARKERS = (
    "agent run cancelled",
    "agent run canceled",
    "agent cancelled",
    "agent canceled",
    "cancelled by user",
    "canceled by user",
    "user cancelled",
    "user canceled",
    "task was cancelled",
    "task was canceled",
)
_TRANSIENT_ERROR_MARKERS = (
    "llm http 408",
    "llm http 409",
    "llm http 425",
    "llm http 429",
    "llm http 500",
    "llm http 502",
    "llm http 503",
    "llm http 504",
    "llm connection error",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
    "broken pipe",
    "temporary failure",
)
_MAX_STEP_TRANSIENT_RETRIES = 2
_REL_OUTPUT_PATH_RE = re.compile(
    r"(?:^|[\s`'\"(=])((?:results|references|reports|figures)/[A-Za-z0-9_./\-]+)"
)


def _is_max_turns_result(result: str) -> bool:
    lowered = (result or "").lower()
    return "max turns" in lowered or "reached max turns" in lowered


def _is_cancelled_result(result: str) -> bool:
    lowered = (result or "").lower()
    return any(marker in lowered for marker in _CANCELLED_RESULT_MARKERS)


def _is_transient_error_text(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_ERROR_MARKERS)


def _is_transient_exception(exc: BaseException) -> bool:
    if type(exc).__name__ == "AgentCancelledError":
        return False
    return _is_transient_error_text(f"{type(exc).__name__}: {exc}")


def _is_transient_step_error(result: str) -> bool:
    lowered = (result or "").lower()
    if "step_execution_error:" not in lowered:
        return False
    return _is_transient_error_text(lowered)


def _step_failed(result: str) -> bool:
    lowered = (result or "").lower()
    if _is_max_turns_result(result):
        return True
    if "agent stopped without" in lowered:
        return True
    if _is_cancelled_result(result):
        return True
    if "step_execution_error:" in lowered:
        return True
    return False


def _step_failure_message(step_index: int, result: str) -> str:
    """Human-facing failure reason; do not always blame max turns."""
    if _is_max_turns_result(result):
        return f"步骤 {step_index} 未在轮次上限内完成"
    if _is_transient_step_error(result):
        return f"步骤 {step_index} 因临时 API/网络错误中断"
    if _is_cancelled_result(result):
        return f"步骤 {step_index} 已取消"
    lowered = (result or "").lower()
    if "agent stopped without" in lowered:
        return f"步骤 {step_index} 未返回最终结果"
    if "step_execution_error:" in lowered:
        detail = (result or "").split(":", 1)[-1].strip()
        if len(detail) > 160:
            detail = detail[:157] + "…"
        return f"步骤 {step_index} 执行异常：{detail or '未知错误'}"
    return f"步骤 {step_index} 未完成"


def _existing_workspace_paths(workspace: Path, text: str) -> List[Path]:
    found: List[Path] = []
    seen: set[str] = set()
    for match in _REL_OUTPUT_PATH_RE.finditer(text or ""):
        rel = match.group(1).rstrip(".,;:)'\"")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        path = workspace / rel
        try:
            if path.is_file() and path.stat().st_size > 0:
                found.append(path)
            elif path.is_dir() and any(path.rglob("*")):
                found.append(path)
        except OSError:
            continue
    return found


def _step_has_salvageable_outputs(step: Dict[str, Any]) -> bool:
    """True when max-turns hit but durable step outputs already exist on disk."""
    workspace = _resolve_analysis_workspace()
    if workspace is None:
        return False

    # Only trust explicit step.outputs paths — title/goal prose often names
    # older results/… paths from prior steps and used to false-salvage.
    outputs_blob = "\n".join(str(item) for item in (step.get("outputs") or []))
    if outputs_blob and _existing_workspace_paths(workspace, outputs_blob):
        return True

    title_goal = f"{step.get('title') or ''} {step.get('goal') or ''}".lower()
    if "候选" in title_goal or "candidate" in title_goal:
        ok, _reason = _target_candidates_products_consistent(workspace)
        if ok:
            return True

    if "miranda" in title_goal or ("seed" in title_goal and "target" in title_goal):
        miranda_root = workspace / "results" / "targets" / "miranda"
        complete = 0
        preds: List[Path] = []
        if miranda_root.is_dir():
            preds = sorted(miranda_root.glob("*/miranda_predictions.txt"))
            for pred in preds:
                try:
                    size = pred.stat().st_size
                    if size < 1000:
                        continue
                    tail = pred.read_text(encoding="utf-8", errors="ignore")[-80:]
                    if "Scan Complete" in tail:
                        complete += 1
                except OSError:
                    continue
        if preds and complete >= max(1, (len(preds) + 1) // 2):
            return True

        combined = workspace / "results" / "targets" / "tRF_targets_combined.tsv"
        try:
            # Stale combined from a previous plan must not salvage a mid-scan step.
            if combined.is_file() and combined.stat().st_size > 0:
                age_sec = datetime.now(timezone.utc).timestamp() - combined.stat().st_mtime
                if age_sec <= 6 * 3600:
                    return True
        except OSError:
            pass
    return False


def _resolve_step_result(result: str, step: Dict[str, Any]) -> Tuple[str, str]:
    """Classify a step result.

    Returns ``(outcome, result)`` where outcome is ``\"done\"`` or ``\"failed\"``.
    Max-turns with durable artifacts is salvaged to done so the original plan
    can continue instead of replanning around a false failure.
    """
    if _is_max_turns_result(result) and _step_has_salvageable_outputs(step):
        salvaged = (
            f"{result}\n"
            "（检测到本步骤相关产物已落盘，按完成处理，避免因轮次上限中断原计划）"
        )
        return "done", salvaged
    if _step_failed(result):
        return "failed", result
    return "done", result


def _format_plan_for_planner(plan: Dict[str, Any]) -> str:
    lines = [f"Goal: {plan.get('goal') or '(unspecified)'}", "Steps:"]
    for step in plan.get("steps") or []:
        status = step.get("status") or STEP_PENDING
        title = step.get("title") or step.get("goal") or step.get("id")
        result = str(step.get("result") or "").strip()
        line = f"  - [{status}] {step.get('id')}: {title}"
        if step.get("skill"):
            line += f" (skill: {step['skill']})"
        if result:
            preview = result[:300] + ("…" if len(result) > 300 else "")
            line += f"\n    result: {preview}"
        lines.append(line)
    return "\n".join(lines)


def _approval_value_from_context(source: str, context: str, plan: Dict[str, Any]) -> str:
    text = str(context or "")
    normalized = str(source or "").strip().lower()
    if normalized == "adapter":
        match = re.search(
            r"(?:adapter(?:_3)?|3['’]?\s*(?:adapter|接头)|接头)\s*(?:=|:|：|为|使用)?\s*([ACGTUN]{8,})",
            text,
            re.I,
        )
        return match.group(1).upper() if match else ""
    if normalized == "strandedness":
        match = re.search(r"\b(unstranded|forward|reverse)\b", text, re.I)
        return match.group(1).lower() if match else ""
    if normalized == "target_candidate_selection":
        workspace = _resolve_analysis_workspace()
        if workspace is not None:
            summary = _format_target_candidate_selection_summary(
                _load_target_candidate_manifest(workspace)
            )
            if summary:
                return summary
        return ""
    if normalized == "analysis_design":
        explicit = re.search(r"(?:DESIGN|设计)\s*[:=：]\s*`?((?:un)?paired|配对|非配对|不配对)`?", text, re.I)
        if explicit:
            value = explicit.group(1).lower()
            return "unpaired" if value in {"非配对", "不配对"} else ("paired" if value == "配对" else value)
        analysis = plan.get("analysis") if isinstance(plan.get("analysis"), dict) else {}
        design = str(analysis.get("design") or "").strip()
        reason = str(analysis.get("reason") or "").strip()
        return f"{design}{f' ({reason})' if reason else ''}" if design else ""
    # Read-only preflight steps emit one canonical value per line.  Parse
    # these first so evidence collected by the preflight is the only source
    # used for adapter selection at the approval gate.
    canonical_sources = {
        "input_fastq", "sample_count", "library_kit", "adapter_3", "adapter_evidence", "min_length",
        "max_length", "quality_cutoff", "error_rate", "min_overlap", "no_indels",
        "times", "trim_n", "poly_a", "output_dir", "json_report", "bam_input", "annotation",
        "strandedness_evidence", "group_evidence", "trim_parameter_evidence",
    }
    if normalized in canonical_sources:
        canonical_labels = "|".join(re.escape(item.upper()) for item in canonical_sources)
        match = re.search(
            rf"\b{re.escape(normalized.upper())}\s*[:=：]\s*(.+?)"
            rf"(?=\s+(?:{canonical_labels})\s*[:=：]|$)",
            text,
            re.I | re.S,
        )
        # Approval fields are human-readable configuration, never a place to
        # surface an arbitrary session transcript. A malformed legacy result
        # must not turn into megabytes of user-facing text.
        return match.group(1).strip()[:240] if match else ""
    labels = {
        "group_column": r"GROUP_COLUMN\s*[:=：]\s*(.+?)(?=\s+(?:GROUP_COUNTS|CONTROL_GROUP|DESIGN)\s*[:=：]|$)",
        "group_counts": r"GROUP_COUNTS\s*[:=：]\s*(.+?)(?=\s+(?:CONTROL_GROUP|DESIGN)\s*[:=：]|$)",
        "control_group": r"CONTROL_GROUP\s*[:=：]\s*(.+?)(?=\s+DESIGN\s*[:=：]|$)",
        "input_fastq": r"INPUT_FASTQ\s*[:=：]\s*(.+?)(?=\s+(?:SAMPLE_COUNT|ADAPTER_3|MIN_LENGTH|MAX_LENGTH)\s*[:=：]|$)",
        "sample_count": r"SAMPLE_COUNT\s*[:=：]\s*(.+?)(?=\s+(?:ADAPTER_3|MIN_LENGTH|MAX_LENGTH)\s*[:=：]|$)",
        "adapter_3": r"ADAPTER_3\s*[:=：]\s*(.+?)(?=\s+(?:MIN_LENGTH|MAX_LENGTH)\s*[:=：]|$)",
        "min_length": r"MIN_LENGTH\s*[:=：]\s*(.+?)(?=\s+MAX_LENGTH\s*[:=：]|$)",
        "max_length": r"MAX_LENGTH\s*[:=：]\s*(.+?)(?=$)",
    }
    if normalized == "group_column":
        explicit = re.search(labels[normalized], text, re.I)
        if explicit:
            return explicit.group(1).strip()
        # Existing reports often summarize this as `group: 15 Tumor / 15 Normal`
        # instead of emitting the preflight contract's canonical labels.
        if re.search(r"\b(?:adata\.obs\[['\"])?group(?:['\"]\])?\s*[:=：]", text, re.I):
            return "group"
        return ""
    if normalized == "group_counts":
        explicit = re.search(labels[normalized], text, re.I)
        if explicit:
            return explicit.group(1).strip()
        summary = re.search(
            r"\bgroup\s*[:=：]\s*((?:\d+\s+[A-Za-z][\w.-]*\s*(?:/|,|；|;)?\s*){2,})",
            text,
            re.I,
        )
        return summary.group(1).strip(" /,;；") if summary else ""
    pattern = labels.get(normalized)
    if pattern:
        match = re.search(pattern, text, re.I)
        return match.group(1).strip() if match else ""
    return ""


_KIT_ADAPTER_RECOMMENDATIONS: Tuple[Tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"\bnebnext\b.*\bsmall\s*rna\b|\bnebnext\b", re.I),
        "AGATCGGAAGAGCACACGTCTGAACTCCAGTCAC",
        "建库元数据匹配 NEBNext Small RNA",
    ),
    (
        re.compile(r"\btruseq\b.*\bsmall\s*rna\b|\bnextflex\b", re.I),
        "TGGAATTCTCGGGTGCCAAGG",
        "建库元数据匹配 TruSeq/NEXTflex Small RNA",
    ),
    (
        re.compile(r"\bqiaseq\b.*\bmirna\b", re.I),
        "AACTGTAGGCACCATCAAT",
        "建库元数据匹配 QIAseq miRNA",
    ),
    (
        re.compile(r"\bsmarter\b.*(?:smrna|small\s*rna)", re.I),
        "GTTCAGAGTTCTACAGTCCGACGATC",
        "建库元数据匹配 SMARTer smRNA-Seq",
    ),
)


def _recommend_adapter_from_library_kit(library_kit: str) -> Tuple[str, str]:
    """Return a protocol-specific recommendation only for an identified kit."""
    kit = str(library_kit or "").strip()
    if not kit or kit.casefold() in {"未记录", "unknown", "none", "n/a"}:
        return "", ""
    for pattern, sequence, evidence in _KIT_ADAPTER_RECOMMENDATIONS:
        if pattern.search(kit):
            return sequence, evidence
    return "", ""


def _hydrate_adapter_approval_from_context(
    approval: Dict[str, Any],
    context: str,
    plan: Dict[str, Any],
) -> None:
    """Persist detected kit evidence and its protocol-specific recommendation."""
    reviewed = approval.setdefault("reviewed", {})
    if not isinstance(reviewed, dict):
        reviewed = {}
        approval["reviewed"] = reviewed
    for source in ("library_kit", "adapter_3", "adapter_evidence"):
        value = _approval_value_from_context(source, context, plan)
        if value and value.casefold() not in {"未记录", "unknown", "none", "null", "n/a", "na", "-"}:
            reviewed[source] = value
    if str(reviewed.get("adapter_3") or "").strip():
        return
    recommendation, evidence = _recommend_adapter_from_library_kit(reviewed.get("library_kit", ""))
    if recommendation:
        reviewed["adapter_3"] = recommendation
        reviewed["adapter_evidence"] = evidence


def _recommend_strandedness_from_library_kit(library_kit: str) -> Tuple[str, str]:
    kit = str(library_kit or "").strip()
    if not kit or kit.casefold() in {"未记录", "unknown", "none", "n/a"}:
        return "", ""
    if re.search(r"\bnebnext\b", kit, re.I):
        return "reverse", "建库元数据匹配 NEBNext Small RNA"
    if re.search(r"\btruseq\b|\bnextflex\b|\bqiaseq\b", kit, re.I):
        return "forward", "建库元数据匹配 TruSeq/NEXTflex/QIAseq 小 RNA"
    return "", ""


def _hydrate_strandedness_approval_from_context(
    approval: Dict[str, Any],
    context: str,
    plan: Dict[str, Any],
) -> None:
    reviewed = approval.setdefault("reviewed", {})
    if not isinstance(reviewed, dict):
        reviewed = {}
        approval["reviewed"] = reviewed
    for source in ("library_kit", "strandedness", "strandedness_evidence"):
        value = _approval_value_from_context(source, context, plan)
        if value and value.casefold() not in {"未记录", "unknown", "none", "null", "n/a", "na", "-"}:
            reviewed[source] = value
    if str(reviewed.get("strandedness") or "").strip().lower() in {"forward", "reverse", "unstranded"}:
        return
    recommendation, evidence = _recommend_strandedness_from_library_kit(reviewed.get("library_kit", ""))
    if recommendation:
        reviewed["strandedness"] = recommendation
        reviewed["strandedness_evidence"] = evidence


def _format_completed_approval_evidence(plan: Dict[str, Any], step: Dict[str, Any]) -> List[str]:
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    try:
        current_index = steps.index(step)
    except ValueError:
        current_index = len(steps)
    evidence: List[str] = []
    for prior in reversed(steps[:current_index]):
        if not isinstance(prior, dict) or prior.get("status") != STEP_DONE:
            continue
        result = re.sub(r"\s+", " ", normalize_text_payload(prior.get("result"))).strip()
        if not result:
            continue
        title = str(prior.get("title") or prior.get("id") or "已完成检查").strip()
        evidence.append(f"{title}: {result[:800]}{'…' if len(result) > 800 else ''}")
        if len(evidence) >= 2:
            break
    return list(reversed(evidence))


def _approval_review_context(
    plan: Dict[str, Any],
    step: Dict[str, Any],
    history: List[Dict[str, str]],
) -> str:
    """Use only bounded, relevant data to populate an approval form.

    Session memory can contain prior rendered replies and workspace listings.
    Feeding it back into the form parser made a malformed legacy value expand
    into the complete session transcript on every confirmation attempt.
    """
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    try:
        current_index = steps.index(step)
    except ValueError:
        current_index = len(steps)
    values = [
        normalize_text_payload(prior.get("result")).strip()[:8000]
        for prior in steps[:current_index]
        if isinstance(prior, dict) and prior.get("status") == STEP_DONE
    ]
    values.extend(
        str(item.get("content") or "").strip()[:2000]
        for item in history[-8:]
        if isinstance(item, dict) and str(item.get("role") or "").lower() == "user"
    )
    return "\n".join(value for value in values if value)


def _build_approval_request(
    plan: Dict[str, Any],
    step: Dict[str, Any],
    *,
    history: List[Dict[str, str]],
    extra_context: str,
) -> str:
    """Render a reviewable approval request instead of a blind yes/no gate."""
    approval = step.get("approval") if isinstance(step.get("approval"), dict) else {}
    review = approval.get("review") if isinstance(approval.get("review"), dict) else {}
    context = _approval_review_context(plan, step, history)
    lines = [f"配置审阅（尚未执行）：{step.get('title') or step.get('goal') or '当前配置'}", "", "当前已知信息与拟使用配置："]
    fields = review.get("fields") if isinstance(review.get("fields"), list) else []
    reviewed = approval.setdefault("reviewed", {})
    if not isinstance(reviewed, dict):
        reviewed = {}
        approval["reviewed"] = reviewed
    if str(approval.get("id") or "") == "confirm-adapter-before-trimming":
        _hydrate_adapter_approval_from_context(approval, context, plan)
    if str(approval.get("id") or "") == "confirm-strandedness-before-feature-count":
        _hydrate_strandedness_approval_from_context(approval, context, plan)
    for field in fields:
        if not isinstance(field, dict):
            continue
        label = str(field.get("label") or field.get("key") or "配置").strip()
        value = str(field.get("value") or "").strip()
        source = str(field.get("source") or "").strip()
        if source:
            detected = _approval_value_from_context(source, context, plan)
            # A preflight may correctly report no historical configuration.
            # In that case keep the skill-declared proposal visible rather
            # than replacing it with an unhelpful "未记录".
            if detected and detected.strip().casefold() not in {
                "未记录", "unknown", "none", "null", "n/a", "na", "-",
            }:
                value = detected
            elif source == "adapter_3":
                recommendation, adapter_evidence = _recommend_adapter_from_library_kit(
                    reviewed.get("library_kit", "")
                )
                if recommendation:
                    value = recommendation
                    reviewed["adapter_3"] = recommendation
                    reviewed["adapter_evidence"] = adapter_evidence
            elif source in reviewed:
                value = str(reviewed[source] or "").strip()
        unknown = str(field.get("unknown") or "未记录").strip()
        if not value:
            value = unknown
        lines.append(f"- {label}: {value}")
        key = str(field.get("key") or source or label).strip()
        if key and value and value != unknown:
            reviewed[key] = value
        elif key:
            # Remove stale values produced by a previous malformed render.
            reviewed.pop(key, None)

    evidence = _format_completed_approval_evidence(plan, step)
    if evidence:
        lines.append("- 已完成检查:")
        lines.extend(f"  - {item}" for item in evidence)

    if str(approval.get("id") or "") == "confirm-groups-before-de":
        group_column = str(reviewed.get("group_column") or "未记录").strip()
        group_counts = str(reviewed.get("group_counts") or "未记录").strip()
        control_group = str(reviewed.get("control_group") or "未记录").strip()
        design = str(reviewed.get("analysis_design") or "未记录").strip()
        lines.extend([
            "",
            f"摘要：目前分组列为 `{group_column}`（{group_counts}）；"
            f"拟使用 `{control_group}` 作为对照组，统计设计为 `{design}`。",
        ])
    if str(approval.get("id") or "") == "confirm-adapter-before-trimming":
        input_fastq = str(reviewed.get("input_fastq") or "未记录").strip()
        sample_count = str(reviewed.get("sample_count") or "未记录").strip()
        adapter = str(reviewed.get("adapter_3") or "未记录").strip()
        adapter_evidence = str(reviewed.get("adapter_evidence") or "未记录").strip()
        min_length = str(reviewed.get("min_length") or "未记录").strip()
        max_length = str(reviewed.get("max_length") or "未记录").strip()
        lines.extend([
            "",
            f"摘要：对 `{input_fastq}` 中的 {sample_count} 个样本，基于 `{adapter_evidence}` "
            f"建议使用 3' adapter `{adapter}`，长度过滤为 `{min_length}`-{max_length} nt。",
        ])

    last_response = str(approval.get("lastResponse") or "").strip()
    if last_response:
        lines.append(f"- 你的上一条反馈（尚未执行）: {last_response}")

    prompt = str(approval.get("prompt") or step.get("goal") or "请确认上述配置。").strip()
    edit_hint = str(review.get("edit_hint") or "").strip()
    lines.extend(["", prompt])
    lines.append(edit_hint or "请直接回复“可以”采用上述配置，回复“不可以”并说明原因；要修改请直接回复“字段=新值”。")
    return "\n".join(lines)


def _format_confirmed_approvals(plan: Dict[str, Any]) -> str:
    lines: List[str] = []
    for step in plan.get("steps") or []:
        if not isinstance(step, dict) or step.get("status") != STEP_DONE:
            continue
        approval = step.get("approval") if isinstance(step.get("approval"), dict) else {}
        response = str(approval.get("response") or "").strip()
        if response:
            title = str(step.get("title") or step.get("id") or "确认项").strip()
            reviewed = approval.get("reviewed") if isinstance(approval.get("reviewed"), dict) else {}
            reviewed_text = ", ".join(
                f"{key}={re.sub(r'\s+', ' ', str(value)).strip()[:240]}"
                for key, value in reviewed.items()
            )
            suffix = f"; 已知配置: {reviewed_text}" if reviewed_text else ""
            lines.append(f"- {title}: 用户回复={response}{suffix}")
    return "\n".join(lines)


def _sanitize_restored_approval(approval: Dict[str, Any]) -> None:
    """Discard corrupted persisted review fields before a plan is resumed."""
    reviewed = approval.get("reviewed")
    if isinstance(reviewed, dict):
        approval["reviewed"] = {
            str(key): str(value).strip()
            for key, value in reviewed.items()
            if len(str(value or "").strip()) <= 240
        }
    for key in ("lastResponse", "response"):
        value = str(approval.get(key) or "").strip()
        if len(value) > 1_000:
            approval[key] = value[:1_000] + "…"


def _build_final_summary(plan: Dict[str, Any]) -> str:
    goal = str(plan.get("goal") or "任务").strip()
    steps = plan.get("steps") or []
    done = [s for s in steps if s.get("status") == STEP_DONE]
    failed = [s for s in steps if s.get("status") == STEP_FAILED]

    # Single-step tasks: show executor reply directly (no task-report wrapper).
    if len(steps) == 1 and len(done) == 1 and not failed:
        result = str(done[0].get("result") or "").strip()
        return result or "完成。"

    # Multi-step success: lead with the last step's user-facing result.
    if done and not failed:
        last_result = str(done[-1].get("result") or "").strip()
        if last_result:
            if len(done) == 1:
                return last_result
            titles = "、".join(str(s.get("title") or s.get("id")) for s in done)
            return f"{last_result}\n\n---\n已完成：{titles}"

    lines = [f"## {'任务未完成' if failed else '任务完成'}：{goal}", ""]
    if done:
        lines.append(f"已完成 {len(done)}/{len(steps)} 个步骤：")
        for step in done:
            title = step.get("title") or step.get("id")
            result = str(step.get("result") or "").strip()
            if result:
                preview = result[:500] + ("…" if len(result) > 500 else "")
                lines.append(f"- **{title}**：{preview}")
            else:
                lines.append(f"- **{title}**：完成")
    if failed:
        lines.append("")
        lines.append("以下步骤未完成：")
        for step in failed:
            lines.append(f"- {step.get('title') or step.get('id')}")
    return "\n".join(lines).strip()


def approval_response_is_actionable(response: str) -> bool:
    """True for an unambiguous approval or a concrete parameter change.

    Must stay aligned with IntentRouter gate routing via ``is_gate_actionable_reply``
    so CONTINUE replies like「adapter_3=…」/「好的，执行吧」actually reach
    ``_prepare_restored_plan``.
    """
    text = str(response or "").strip()
    if not text or _APPROVAL_REJECT_RE.search(text):
        return False
    if is_gate_actionable_reply(text, awaiting_gate=True):
        return True
    return bool(
        _APPROVAL_ACCEPT_RE.search(text)
        or _APPROVAL_AFFIRMATION_RE.search(text)
        or _APPROVAL_ASSIGNMENT_RE.search(text)
        or _APPROVAL_EXPLICIT_VALUE_RE.search(text)
    )


def _approval_response_requests_followup(response: str) -> bool:
    """Whether a non-approval reply should be handled as a requested prerequisite."""
    text = str(response or "").strip()
    return bool(
        text
        and not _APPROVAL_REJECT_RE.search(text)
        and not approval_response_is_actionable(text)
    )


def _group_approval_is_ready(approval: Dict[str, Any]) -> bool:
    reviewed = approval.get("reviewed") if isinstance(approval.get("reviewed"), dict) else {}
    required = ("group_column", "group_counts", "control_group", "analysis_design")
    return all(str(reviewed.get(key) or "").strip() not in {"", "未记录"} for key in required)


_TARGET_CANDIDATE_SELECT_CONTRACT = "select-target-candidates-before-prediction"
_TARGET_CANDIDATE_CONFIRM_CONTRACT = "confirm-target-candidates-before-prediction"
_TARGET_CANDIDATE_MANIFEST_REL = Path("results/differential/target_candidate_selection_manifest.json")
_TARGET_CANDIDATE_CONFIRM_MARKER_REL = Path(
    "results/differential/target_candidate_selection_confirmed.json"
)


def _resolve_analysis_workspace() -> Optional[Path]:
    """Best-effort workspace root for durable candidate artifacts."""
    try:
        from work_space import get_work_space  # type: ignore

        root = get_work_space()
        if root is not None:
            return Path(root)
    except Exception:  # noqa: BLE001 - optional UI workspace binding
        pass
    cwd = Path.cwd()
    if (cwd / "results" / "differential").is_dir() or (cwd / "results" / "adata").is_dir():
        return cwd
    return None


def _load_target_candidate_manifest(workspace: Path) -> Optional[Dict[str, Any]]:
    path = workspace / _TARGET_CANDIDATE_MANIFEST_REL
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return payload if isinstance(payload, dict) else None


def _manifest_candidate_fingerprint(manifest: Dict[str, Any]) -> str:
    payload = []
    for entry in manifest.get("per_dataset") or []:
        if not isinstance(entry, dict):
            continue
        payload.append({
            "dataset": str(entry.get("dataset") or "").strip(),
            "up": [str(x) for x in (entry.get("up_candidates") or [])],
            "down": [str(x) for x in (entry.get("down_candidates") or [])],
        })
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _format_target_candidate_selection_summary(manifest: Optional[Dict[str, Any]]) -> str:
    if not isinstance(manifest, dict):
        return ""
    filters = manifest.get("filters") if isinstance(manifest.get("filters"), dict) else {}
    parts = [
        f"top_n={filters.get('top_n', '?')}",
        f"FDR<{filters.get('adj_p_max', '?')}",
        f"|logFC|>{filters.get('log_fc_min', '?')}",
        f"meanCPM>{filters.get('mean_cpm_min', '?')}",
    ]
    datasets: List[str] = []
    for entry in manifest.get("per_dataset") or []:
        if not isinstance(entry, dict):
            continue
        dataset = str(entry.get("dataset") or "").strip()
        if not dataset:
            continue
        n_up = entry.get("n_up_selected")
        n_down = entry.get("n_down_selected")
        if n_up is None:
            n_up = len(entry.get("up_candidates") or [])
        if n_down is None:
            n_down = len(entry.get("down_candidates") or [])
        datasets.append(f"{dataset}: up={n_up} down={n_down}")
    summary = "; ".join(parts)
    if datasets:
        summary = f"{summary}; " + "; ".join(datasets)
    return summary[:800]


def _feature_id_list(value: Any) -> List[str]:
    if value is None:
        return []
    try:
        items = list(value)
    except TypeError:
        return [str(value)]
    return [str(item) for item in items]


def _read_h5ad_candidate_features(h5_path: Path) -> Tuple[List[str], List[str]]:
    import anndata as ad

    adata = ad.read_h5ad(h5_path, backed="r")
    try:
        uns_up = adata.uns.get("target_candidate_selection")
        uns_down = adata.uns.get("target_candidate_selection_down")
        up = _feature_id_list(uns_up.get("features") if isinstance(uns_up, dict) else None)
        down = _feature_id_list(uns_down.get("features") if isinstance(uns_down, dict) else None)
        return up, down
    finally:
        file_handle = getattr(adata, "file", None)
        if file_handle is not None:
            try:
                file_handle.close()
            except Exception:  # noqa: BLE001
                pass


def _target_candidates_products_consistent(workspace: Path) -> Tuple[bool, str]:
    """True when manifest exists and each non-empty selection matches adata.uns."""
    manifest = _load_target_candidate_manifest(workspace)
    if not manifest:
        return False, "manifest missing"
    per_dataset = manifest.get("per_dataset")
    if not isinstance(per_dataset, list) or not per_dataset:
        return False, "manifest empty"
    adata_dir = workspace / "results" / "adata"
    matched = 0
    for entry in per_dataset:
        if not isinstance(entry, dict):
            continue
        dataset = str(entry.get("dataset") or "").strip()
        if not dataset:
            continue
        expected_up = [str(x) for x in (entry.get("up_candidates") or [])]
        expected_down = [str(x) for x in (entry.get("down_candidates") or [])]
        if not expected_up and not expected_down:
            continue
        h5_path = adata_dir / f"srna_{dataset}.h5ad"
        if not h5_path.is_file():
            return False, f"missing h5ad {dataset}"
        try:
            actual_up, actual_down = _read_h5ad_candidate_features(h5_path)
        except Exception as exc:  # noqa: BLE001
            return False, f"h5ad read fail {dataset}: {exc}"
        if expected_up and set(expected_up) != set(actual_up):
            return False, f"up mismatch {dataset}"
        if expected_down and set(expected_down) != set(actual_down):
            return False, f"down mismatch {dataset}"
        matched += 1
    if matched == 0:
        return False, "manifest present but no non-empty selections matched"
    return True, f"manifest matches {matched} dataset(s)"


def _prior_target_candidate_confirmation_matches(workspace: Path) -> Tuple[bool, str]:
    marker_path = workspace / _TARGET_CANDIDATE_CONFIRM_MARKER_REL
    if not marker_path.is_file():
        return False, "no prior confirmation"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False, "confirmation marker unreadable"
    if not isinstance(marker, dict):
        return False, "confirmation marker invalid"
    manifest = _load_target_candidate_manifest(workspace)
    # Without a current manifest we cannot prove the confirmation still applies
    # to this analysis — refuse so a NEW_WORKFLOW cannot inherit a stale skip.
    if not manifest:
        return False, "manifest missing"
    expected = str(marker.get("manifest_sha256") or "").strip()
    if not expected:
        return False, "confirmation missing manifest fingerprint"
    if expected == _manifest_candidate_fingerprint(manifest):
        return True, "prior confirmation matches manifest"
    return False, "manifest changed since confirmation"


def _persist_target_candidate_confirmation(workspace: Path, response: str = "") -> None:
    manifest = _load_target_candidate_manifest(workspace) or {}
    marker = {
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "response": str(response or "").strip()[:500],
        "manifest_sha256": _manifest_candidate_fingerprint(manifest) if manifest else "",
    }
    path = workspace / _TARGET_CANDIDATE_CONFIRM_MARKER_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def invalidate_target_candidate_confirmation(workspace: Optional[Path] = None) -> bool:
    """Drop durable confirmation so a brand-new analysis cannot auto-skip gates.

    Called when the UI clears the plan for NEW_WORKFLOW. Manifest/products are
    left intact; only the user-confirmation marker is removed.
    """
    root = workspace or _resolve_analysis_workspace()
    if root is None:
        return False
    path = root / _TARGET_CANDIDATE_CONFIRM_MARKER_REL
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        return False
    return False


def _is_target_candidate_confirm_gate(step: Dict[str, Any]) -> bool:
    approval = step.get("approval") if isinstance(step.get("approval"), dict) else {}
    contract = str(step.get("workflowContract") or approval.get("id") or "").strip()
    return contract == _TARGET_CANDIDATE_CONFIRM_CONTRACT


def _is_target_candidate_select_step(step: Dict[str, Any]) -> bool:
    if str(step.get("workflowContract") or "").strip() == _TARGET_CANDIDATE_SELECT_CONTRACT:
        return True
    # Replan sometimes drops workflowContract — recognize the canonical select
    # step by title/goal, but never confuse it with the confirm gate.
    if _is_target_candidate_confirm_gate(step):
        return False
    blob = f"{step.get('title') or ''} {step.get('goal') or ''}"
    lowered = blob.lower()
    if "confirm-target-candidates" in lowered or "确认靶标" in blob or "确认候选" in blob:
        return False
    if "select_target_candidates" in lowered or "select-target-candidates" in lowered:
        return True
    return bool(
        re.search(
            r"从差异结果确定靶标|确定靶标分析候选|选择靶标.*候选|select\s+target\s+candidates",
            blob,
            re.I,
        )
    )


def _auto_complete_target_candidate_gates(
    plan: Dict[str, Any],
    *,
    workspace: Optional[Path] = None,
) -> bool:
    """Mark select/confirm done when products match manifest or were confirmed once.

    Select skips when products are consistent OR a confirmation marker still
    matches the *current* manifest. A NEW_WORKFLOW must call
    ``invalidate_target_candidate_confirmation`` so stale markers cannot skip.
    """
    root = workspace or _resolve_analysis_workspace()
    if root is None:
        return False
    products_ok, products_reason = _target_candidates_products_consistent(root)
    confirmed_ok, confirmed_reason = _prior_target_candidate_confirmation_matches(root)
    if not products_ok and not confirmed_ok:
        return False
    reason = products_reason if products_ok else confirmed_reason
    if products_ok and not confirmed_ok:
        # Keep confirmation durable once products prove the selection exists.
        _persist_target_candidate_confirmation(root, "auto: products consistent with manifest")
        confirmed_ok = True
        confirmed_reason = "auto: products consistent with manifest"
        reason = products_reason
    changed = False
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        status = str(step.get("status") or "")
        if status in {STEP_DONE, STEP_SKIPPED, STEP_FAILED}:
            continue
        if status not in {STEP_PENDING, STEP_RUNNING, STEP_AWAITING_APPROVAL}:
            continue
        # Skip select when products match OR confirmation still matches this manifest.
        if _is_target_candidate_select_step(step) and (products_ok or confirmed_ok):
            step["status"] = STEP_DONE
            step["result"] = f"已有候选产物/确认记录，跳过重跑（{reason}）"
            changed = True
            continue
        if _is_target_candidate_confirm_gate(step):
            step["status"] = STEP_DONE
            step["result"] = f"候选集已确认（{reason}）"
            approval = step.get("approval")
            if not isinstance(approval, dict):
                approval = {}
                step["approval"] = approval
            approval.setdefault("id", _TARGET_CANDIDATE_CONFIRM_CONTRACT)
            approval["response"] = str(approval.get("response") or "auto-confirmed").strip()
            approval.pop("lastResponse", None)
            changed = True
    return changed


_DOWNLOAD_STEP_RE = re.compile(
    r"fastq-dl|fastq_dl|download.*fastq|fastq.*download|下载.{0,12}(?:fastq|原始)|raw fastq",
    re.I,
)
_PLAN_ACCESSION_RE = re.compile(
    r"\b((?:SRP|ERP|DRP|PRJNA|PRJEB|PRJDB|SRR|ERR|DRR)\d+)\b",
    re.I,
)


def _is_fastq_download_step(step: Dict[str, Any]) -> bool:
    blob = " ".join(
        str(step.get(key) or "")
        for key in ("id", "title", "goal", "skill")
    )
    return bool(_DOWNLOAD_STEP_RE.search(blob))


def _skip_existing_fastq_download_steps(plan: Dict[str, Any]) -> bool:
    """Mark pending FASTQ download steps skipped when run files already exist."""
    root = _resolve_analysis_workspace()
    if root is None:
        return False
    try:
        from sRNAgent.Tools.fastq.fastq_dl import workspace_fastq_runs
    except Exception:  # noqa: BLE001
        return False
    changed = False
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        status = str(step.get("status") or "")
        if status not in {STEP_PENDING, STEP_RUNNING}:
            continue
        if not _is_fastq_download_step(step):
            continue
        blob = " ".join(str(step.get(key) or "") for key in ("id", "title", "goal"))
        match = _PLAN_ACCESSION_RE.search(blob)
        if not match:
            continue
        runs = workspace_fastq_runs(root, match.group(1))
        if not runs:
            continue
        step["status"] = STEP_SKIPPED
        step["result"] = (
            f"工作区已有 {len(runs)} 个 FASTQ run，跳过下载"
            f"（{', '.join(sorted(runs)[:8])}{'…' if len(runs) > 8 else ''}）"
        )
        changed = True
    return changed


def _is_adapter_approval_gate(step: Dict[str, Any]) -> bool:
    approval = step.get("approval") if isinstance(step.get("approval"), dict) else {}
    return str(approval.get("id") or "").strip() == "confirm-adapter-before-trimming"


def _adapter_approval_is_ready(approval: Dict[str, Any]) -> bool:
    reviewed = approval.get("reviewed") if isinstance(approval.get("reviewed"), dict) else {}
    sequence = str(reviewed.get("adapter_3") or "").strip().upper()
    evidence = str(reviewed.get("adapter_evidence") or "").strip()
    return bool(re.fullmatch(r"[ACGTUN]{8,}", sequence)) and evidence not in {"", "未记录"}


def _apply_adapter_override(approval: Dict[str, Any], response: str) -> None:
    """Persist an explicit user-provided adapter as evidence-backed input."""
    match = re.search(r"\b(?:adapter_?3|adapter|接头)\s*[:=：]\s*([ACGTUN]{8,})\b", response or "", re.I)
    if not match:
        return
    reviewed = approval.setdefault("reviewed", {})
    if not isinstance(reviewed, dict):
        reviewed = {}
        approval["reviewed"] = reviewed
    reviewed["adapter_3"] = match.group(1).upper()
    reviewed["adapter_evidence"] = "用户指定"


def _is_strandedness_approval_gate(step: Dict[str, Any]) -> bool:
    approval = step.get("approval") if isinstance(step.get("approval"), dict) else {}
    return str(approval.get("id") or "").strip() == "confirm-strandedness-before-feature-count"


def _strandedness_approval_is_ready(approval: Dict[str, Any]) -> bool:
    reviewed = approval.get("reviewed") if isinstance(approval.get("reviewed"), dict) else {}
    strandedness = str(reviewed.get("strandedness") or "").strip().lower()
    evidence = str(reviewed.get("strandedness_evidence") or "").strip()
    return strandedness in {"unstranded", "forward", "reverse"} and evidence not in {"", "未记录"}


def _apply_strandedness_override(approval: Dict[str, Any], response: str) -> None:
    match = re.search(r"\bstrandedness\s*[:=：]\s*(unstranded|forward|reverse)\b", response or "", re.I)
    if not match:
        return
    reviewed = approval.setdefault("reviewed", {})
    if not isinstance(reviewed, dict):
        reviewed = {}
        approval["reviewed"] = reviewed
    reviewed["strandedness"] = match.group(1).lower()
    reviewed["strandedness_evidence"] = "用户指定"


def _is_group_approval_gate(step: Dict[str, Any]) -> bool:
    """Identify a plan gate that blocks differential-analysis grouping."""
    if not isinstance(step.get("approval"), dict):
        return False
    approval = step["approval"]
    approval_id = str(approval.get("id") or "").casefold()
    if "group" in approval_id or "分组" in approval_id:
        return True
    fields = (approval.get("review") or {}).get("fields") if isinstance(approval.get("review"), dict) else []
    sources = {
        str(field.get("source") or field.get("id") or "").casefold()
        for field in fields if isinstance(field, dict)
    }
    if {"group_column", "group_counts", "control_group"} & sources:
        return True
    text = " ".join(str(step.get(key) or "") for key in ("title", "goal"))
    return bool(_GROUP_RE.search(text) and re.search(r"确认|confirm", text, re.I))


def _group_gate_has_concrete_values(step: Dict[str, Any]) -> bool:
    """Return whether the gate can show an actual group configuration."""
    approval = step.get("approval") if isinstance(step.get("approval"), dict) else {}
    reviewed = approval.get("reviewed") if isinstance(approval.get("reviewed"), dict) else {}
    required = ("group_column", "group_counts", "control_group", "analysis_design")
    if all(str(reviewed.get(key) or "").strip() not in {"", "未记录"} for key in required):
        return True
    review = approval.get("review") if isinstance(approval.get("review"), dict) else {}
    fields = review.get("fields") if isinstance(review.get("fields"), list) else []
    values: Dict[str, str] = {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        key = str(field.get("source") or field.get("id") or "").strip()
        if key:
            values[key] = str(field.get("value") or "").strip()
    return all(values.get(key, "") not in {"", "未记录"} for key in required)


def _ensure_group_metadata_preflight(plan: Dict[str, Any]) -> bool:
    """Insert evidence collection before an otherwise empty group approval.

    An approval card is meaningful only after it can display the sample-to-group
    mapping, control group, and statistical design. LLM planners sometimes put
    that card first and promise to discover the values afterwards, which asks
    the user to approve unknowns. This deterministic repair makes the required
    metadata lookup an executable, read-only prerequisite instead.
    """
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    changed = False
    lifecycle = PlanLifecycle(plan)
    for index, gate in enumerate(list(steps)):
        if (
            not isinstance(gate, dict)
            or gate.get("status") not in {STEP_PENDING, STEP_AWAITING_APPROVAL}
            or not _is_group_approval_gate(gate)
        ):
            continue
        approval = gate.get("approval") if isinstance(gate.get("approval"), dict) else {}
        gate["approval"] = approval
        # Use the canonical ID so rendering and a later concise confirmation
        # share one state-machine path.
        approval["id"] = "confirm-groups-before-de"
        # A v2 contract already declares the evidence-producing preflight as
        # an artifact dependency. It is authoritative, so lifecycle repair
        # must not inject another title-based preflight.
        if (
            str(gate.get("workflowContract") or "") == "confirm-groups-before-de"
            and "group_design_evidence" in [str(value) for value in gate.get("inputs") or []]
        ):
            continue
        if _group_gate_has_concrete_values(gate):
            continue

        gate_id = str(gate.get("id") or index + 1).strip()
        preflight_id = f"{gate_id}-group-preflight"
        preflight = _make_plan_step(
            step_id=preflight_id,
            title="核验差异分析样本分组与统计设计",
            goal=(
                "只读核验分组依据：检查已有 AnnData 的 obs，以及 fastq-dl 下载的 run-info.tsv "
                "或样本元数据；建立每个样本/Run 到实验组的可追溯映射。仅当元数据明确支持时确定 "
                "对照组和 paired/unpaired 设计。必须输出 GROUP_COLUMN、GROUP_COUNTS、CONTROL_GROUP、"
                "DESIGN 四个规范字段；缺失时明确说明缺少什么证据。不得运行定量、差异分析、靶标分析，"
                "不得写入或修改 AnnData。"
            ),
            skill="",
            auto_inserted=True,
        )
        before = len(steps)
        lifecycle.insert_prerequisite(gate, preflight, index=index)
        changed = changed or len(steps) != before
    return changed


def _hydrate_group_approval_from_completed_steps(plan: Dict[str, Any], step_index: int, approval: Dict[str, Any]) -> None:
    """Persist canonical group values from completed preflight/lookup results."""
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    context = "\n".join(
        str(step.get("result") or "")
        for step in steps[:step_index]
        if isinstance(step, dict) and step.get("status") == STEP_DONE
    )
    reviewed = approval.setdefault("reviewed", {})
    if not isinstance(reviewed, dict):
        reviewed = {}
        approval["reviewed"] = reviewed
    for source in ("group_column", "group_counts", "control_group", "analysis_design"):
        value = _approval_value_from_context(source, context, plan)
        if value:
            reviewed[source] = value


def _apply_natural_group_overrides(approval: Dict[str, Any], response: str) -> None:
    """Accept concise confirmations such as 'normal是对照组' without requiring DSL syntax."""
    reviewed = approval.setdefault("reviewed", {})
    if not isinstance(reviewed, dict):
        reviewed = {}
        approval["reviewed"] = reviewed
    control = _NATURAL_CONTROL_GROUP_RE.search(response or "")
    if control:
        supplied = next((value for value in control.groups() if value), "")
        labels = re.findall(r"[A-Za-z][\w.-]*", str(reviewed.get("group_counts") or ""))
        reviewed["control_group"] = next(
            (label for label in labels if label.casefold() == supplied.casefold()), supplied,
        )
    design = _NATURAL_DESIGN_RE.search(response or "")
    if design:
        supplied = design.group(1).lower()
        reviewed["analysis_design"] = "unpaired" if supplied in {"非配对", "不配对"} else (
            "paired" if supplied == "配对" else supplied
        )


PlanStore = Callable[[str, Dict[str, Any]], None]
PlanLoader = Callable[[str], Optional[Dict[str, Any]]]


def _refresh_plan_artifact_state(plan: Dict[str, Any]) -> List[str]:
    """Persist completed plan outputs as typed workflow capabilities.

    This is deliberately separate from result prose: a later compiler can
    decide whether ``trimmed_fastq`` exists without guessing from chat text or
    treating a pending step's declared output as evidence.
    """
    raw_state = plan.get("artifactState") if isinstance(plan.get("artifactState"), dict) else {}
    available = [
        str(value).strip()
        for value in raw_state.get("available") or []
        if str(value).strip()
    ]
    provenance = {
        str(name): str(source)
        for name, source in (raw_state.get("provenance") or {}).items()
        if str(name).strip() and str(source).strip()
    }
    for step in plan.get("steps") or []:
        if not isinstance(step, dict) or step.get("status") != STEP_DONE:
            continue
        source = str(step.get("id") or "completed-step")
        for artifact in step.get("outputs") or []:
            name = str(artifact).strip()
            if name and name not in available:
                available.append(name)
            if name:
                provenance.setdefault(name, source)
    plan["artifactState"] = {
        "schema": "srnagent.artifacts/v1",
        "available": available,
        "provenance": provenance,
    }
    return available


class PlanOrchestrator:
    """Orchestrates plan → execute → replan loops."""

    def __init__(
        self,
        agent: "SRNAgent",
        *,
        chat_id: str = "",
        save_plan: Optional[PlanStore] = None,
        load_plan: Optional[PlanLoader] = None,
        max_replan_attempts: int = _MAX_REPLAN_ATTEMPTS,
    ) -> None:
        self.agent = agent
        self.chat_id = chat_id
        self._save_plan = save_plan
        self._load_plan = load_plan
        self.max_replan_attempts = max_replan_attempts
        self.skill_overview = list_available_skills(agent.skill_registry)

    def _persist_plan(self, plan: Dict[str, Any]) -> None:
        _refresh_plan_artifact_state(plan)
        if self._save_plan and self.chat_id:
            self._save_plan(self.chat_id, plan)

    def _load_checkpoint(self) -> Optional[Dict[str, Any]]:
        if not self.chat_id:
            return None
        return self.agent._load_run_checkpoint(self.chat_id)

    def _load_persisted_plan(self) -> Optional[Dict[str, Any]]:
        """Return the UI's durable plan when a process crash lost its checkpoint."""
        if not self.chat_id or self._load_plan is None:
            return None
        try:
            plan = self._load_plan(self.chat_id)
        except Exception:  # noqa: BLE001 - plan persistence must not block a run
            return None
        return plan if isinstance(plan, dict) and isinstance(plan.get("steps"), list) else None

    @staticmethod
    def _normalize_plan_lifecycle(plan: Dict[str, Any]) -> Dict[str, Any]:
        """Apply all deterministic plan invariants at one lifecycle boundary."""
        # Persisted plans may have been interrupted between a replan and its
        # dependency rewrite. Keep reports behind DE work even when restoring
        # such an older plan; this is a local ordering repair, never an LLM
        # replanning pass.
        if isinstance(plan.get("steps"), list):
            plan["steps"] = _order_de_workflow_steps(plan["steps"])
        lifecycle = PlanLifecycle(plan)
        lifecycle.normalize_structure()
        _ensure_group_metadata_preflight(plan)
        # A prerequisite insertion changes the graph, so canonicalize it once
        # more before persistence or execution.
        lifecycle.normalize_structure()
        _auto_complete_target_candidate_gates(plan)
        _skip_existing_fastq_download_steps(plan)
        _refresh_plan_artifact_state(plan)
        return plan

    @staticmethod
    def _prepare_restored_plan(
        plan: Dict[str, Any],
        *,
        approval_response: str = "",
    ) -> Dict[str, Any]:
        """Make an interrupted plan executable without changing its agreed scope."""
        restored = clone_plan(plan)
        lifecycle = PlanLifecycle(restored)
        lifecycle.prepare_for_resume()
        for index, step in enumerate(restored.get("steps") or []):
            if isinstance(step, dict) and isinstance(step.get("approval"), dict):
                _sanitize_restored_approval(step["approval"])
            if (
                isinstance(step, dict)
                and step.get("status") == STEP_AWAITING_APPROVAL
                and approval_response.strip()
            ):
                approval = step.get("approval")
                if isinstance(approval, dict):
                    group_gate = _is_group_approval_gate(step)
                    adapter_gate = _is_adapter_approval_gate(step)
                    strandedness_gate = _is_strandedness_approval_gate(step)
                    if group_gate:
                        _hydrate_group_approval_from_completed_steps(restored, index, approval)
                        _apply_natural_group_overrides(approval, approval_response)
                    if adapter_gate:
                        _apply_adapter_override(approval, approval_response)
                    if strandedness_gate:
                        completed_context = "\n".join(
                            str(prior.get("result") or "")
                            for prior in restored.get("steps", [])[:index]
                            if isinstance(prior, dict) and prior.get("status") == STEP_DONE
                        )
                        _hydrate_strandedness_approval_from_context(approval, completed_context, restored)
                        _apply_strandedness_override(approval, approval_response)
                    actionable = approval_response_is_actionable(approval_response) or bool(
                        group_gate and _GROUP_CONFIRMATION_RE.search(approval_response)
                    )
                    if actionable:
                        if (
                            group_gate
                            and _APPROVAL_ACCEPT_RE.search(approval_response)
                            and not _group_approval_is_ready(approval)
                        ):
                            # A bare "可以" cannot approve an unknown group
                            # configuration. Keep the gate open so its missing
                            # preflight/lookup step can run first.
                            approval["lastResponse"] = approval_response.strip()
                            continue
                        if (
                            strandedness_gate
                            and _APPROVAL_ACCEPT_RE.search(approval_response)
                            and not _strandedness_approval_is_ready(approval)
                        ):
                            approval["lastResponse"] = approval_response.strip()
                            continue
                        if (
                            adapter_gate
                            and _APPROVAL_ACCEPT_RE.search(approval_response)
                            and not _adapter_approval_is_ready(approval)
                        ):
                            # A bare confirmation cannot turn an unknown
                            # adapter into a default. Preserve the gate until
                            # sequence evidence or an explicit user sequence
                            # is available.
                            approval["lastResponse"] = approval_response.strip()
                            continue
                        step["status"] = STEP_DONE
                        step["result"] = f"用户确认：{approval_response.strip()}"
                        approval["response"] = approval_response.strip()
                        approval.pop("lastResponse", None)
                        if _is_target_candidate_confirm_gate(step):
                            workspace = _resolve_analysis_workspace()
                            if workspace is not None:
                                _persist_target_candidate_confirmation(
                                    workspace, approval_response.strip()
                                )
                    else:
                        approval["lastResponse"] = approval_response.strip()
                        if _approval_response_requests_followup(approval_response):
                            approval["followupRequest"] = approval_response.strip()[:800]
        # Approval consumption can change the same graph, so run the lifecycle
        # repair once more after applying the current user response.
        _auto_complete_target_candidate_gates(restored)
        _skip_existing_fastq_download_steps(restored)
        lifecycle.repair_completed_approval_dependencies()
        _refresh_plan_artifact_state(restored)
        return restored

    @staticmethod
    def _materialize_approval_followup(plan: Dict[str, Any]) -> bool:
        """Turn a fact-finding reply at a gate into one read-only subtask."""
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        for index, gate in enumerate(steps):
            if not isinstance(gate, dict) or gate.get("status") != STEP_AWAITING_APPROVAL:
                continue
            approval = gate.get("approval") if isinstance(gate.get("approval"), dict) else {}
            request = str(approval.pop("followupRequest", "") or "").strip()
            if not request:
                continue
            gate_id = str(gate.get("id") or index + 1).strip()
            followup_base_id = f"{gate_id}-clarify"
            existing_ids = {
                str(step.get("id") or "")
                for step in steps
                if isinstance(step, dict)
            }
            followup_id = followup_base_id
            sequence = 2
            while followup_id in existing_ids:
                followup_id = f"{followup_base_id}-{sequence}"
                sequence += 1
            title = "响应用户的前置核查请求"
            goal = (
                f"用户在当前审批前要求优先处理：{request}\n"
                "将这条反馈视为对计划的有效修改，而不是要求用户重复确认旧计划。结合完整对话和已有 "
                "结果，推断用户要先核对的对象、来源和交付内容，并立即完成该只读核查。用户已经指定 "
                "来源或顺序时，直接使用该来源和顺序；不要把请求改写为固定的来源菜单，也不要要求用户 "
                "再次选择来源。给出可核验的证据、映射或缺失项；完成后再回到仍然相关的审批步骤。"
                "禁止运行 cutadapt、FastQC、比对、定量或修改任何数据/文件。"
            )
            contracts = [{
                "id": "approval-followup-read-only",
                "instructions": (
                    "Treat the user's latest feedback as a concrete change to the plan. Perform the requested "
                    "read-only investigation now, following any source and priority the user named. Do not replace "
                    "it with a generic options list or ask for another source approval. Do not transform data or "
                    "write files."
                ),
            }]
            followup = _make_plan_step(
                step_id=followup_id,
                title=title,
                goal=goal,
                # A follow-up may concern any modality or external source;
                # inheriting an unrelated pipeline skill biases the executor.
                skill="",
                auto_inserted=True,
                execution_contracts=contracts,
            )
            steps.insert(index, followup)
            return True
        return False

    def _save_step_checkpoint(
        self,
        plan: Dict[str, Any],
        step_id: Optional[str],
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Persist plan + active step messages so a run can resume mid-step."""
        if not self.chat_id:
            return
        payload: Dict[str, Any] = {"plan": plan, "step_id": step_id}
        if messages is not None:
            payload["messages"] = messages
        self.agent._persist_checkpoint(payload, self.chat_id)

    def _emit(
        self,
        on_progress: Optional["ProgressCallback"],
        event_type: str,
        **payload: Any,
    ) -> None:
        # Plan state continues to mutate after an event is queued (for example,
        # a step changes from ``running`` to ``done`` after its tool call
        # returns).  Events travel through queues and the live replay buffer,
        # so a shallow event copy leaves their nested ``plan`` reference live.
        # Snapshot it here to keep the UI's "进行中" count consistent with the
        # event that was emitted.
        event_payload = dict(payload)
        if isinstance(event_payload.get("plan"), dict):
            event_payload["plan"] = deepcopy(event_payload["plan"])
        self.agent._emit_progress(on_progress, event_type, **event_payload)

    def _complete_plan_json(
        self,
        messages: List[Dict[str, Any]],
        *,
        on_progress: Optional["ProgressCallback"],
        cancel_event: Optional[Any],
    ) -> Dict[str, Any]:
        """Request a plan and repair malformed LLM JSON before giving up.

        Planning output is an untrusted transport format. A malformed quote in
        an otherwise useful plan must trigger a constrained JSON-only repair,
        not terminate the user's workflow with a decoder traceback.
        """
        current_messages = messages
        last_error: Optional[PlanJsonParseError] = None
        for attempt in range(3):
            completion = self.agent._llm_complete_cancellable(
                current_messages,
                tools=None,
                cancel_event=cancel_event,
                on_progress=on_progress,
                enable_thinking=False,
            )
            raw = str(completion.content or "")
            try:
                return _parse_plan_json(raw)
            except PlanJsonParseError as exc:
                last_error = exc
                if attempt == 2:
                    break
                self._emit(on_progress, "status", message="规划输出格式异常，正在自动修复…")
                current_messages = [
                    {
                        "role": "system",
                        "content": (
                            "Repair the following invalid plan JSON. Return only one valid JSON object, with no "
                            "Markdown or explanation. Preserve the intended goal, steps, approvals, dependencies, "
                            "and text. Escape every quote inside JSON strings."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Invalid JSON:\n{exc.raw[:16000]}",
                    },
                ]
        raise ValueError(
            "规划器连续三次返回无效 JSON，未执行任何分析；请重试该请求。"
        ) from last_error

    def _create_plan(
        self,
        user_query: str,
        extra_context: str,
        *,
        available_artifacts: Optional[List[str]] = None,
        history: Optional[List[Dict[str, Any]]] = None,
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
    ) -> Dict[str, Any]:
        recent_history = _format_recent_history(history)
        scope = _derive_requested_scope(_scope_request_text(user_query, history))
        planning_skill_guidance = _load_planning_skill_guidance(
            getattr(self.agent, "skill_registry", None), user_query,
        )
        conversation_block = (
            f"\nRecent conversation (use to resolve references to earlier work):\n{recent_history}\n"
            if recent_history
            else ""
        )
        messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": _build_planner_system_prompt(self.skill_overview, planning_skill_guidance),
            },
            {
                "role": "user",
                "content": (
                    f"User request:\n{user_query}\n\n"
                    f"{_scope_block(scope)}\n\n"
                    f"{conversation_block}"
                    f"Session context:\n{extra_context or '(none)'}"
                ),
            },
        ]
        raw = self._complete_plan_json(
            messages, on_progress=on_progress, cancel_event=cancel_event,
        )
        # A second LLM pass performs semantic dependency review. It is not a
        # fixed workflow rewriter: the reviewer derives ordering from the
        # requested artifacts and the relevant SKILL.md instructions.
        review_guidance = _load_plan_review_skill_guidance(
            getattr(self.agent, "skill_registry", None),
            raw.get("steps"),
            user_query,
        )
        review_messages = [
            {"role": "system", "content": _build_plan_review_system_prompt(review_guidance)},
            {
                "role": "user",
                "content": (
                    f"User request:\n{user_query}\n\n"
                    f"{_scope_block(scope)}\n\n"
                    f"Session context:\n{extra_context or '(none)'}\n\n"
                    f"Proposed plan:\n{json.dumps(raw, ensure_ascii=False)}"
                ),
            },
        ]
        try:
            reviewed_completion = self.agent._llm_complete_cancellable(
                review_messages,
                tools=None,
                cancel_event=cancel_event,
                on_progress=on_progress,
                enable_thinking=False,
            )
            reviewed_raw = _parse_plan_json(str(reviewed_completion.content or ""))
            if isinstance(reviewed_raw.get("steps"), list) and reviewed_raw["steps"]:
                raw = reviewed_raw
        except (ValueError, TypeError, json.JSONDecodeError):
            # Preserve the primary planner result if the reviewer does not
            # return valid JSON; the normal skill contracts still apply.
            pass
        # The LLM plan is authoritative for scope. Required transformations
        # and artifact order come from deterministic skill contracts below.
        plan_goal, scoped_steps = _enforce_requested_scope(
            str(raw.get("goal") or user_query), raw.get("steps") or [],
            scope=scope, fallback_goal=user_query,
        )
        steps = _enforce_workflow_contracts(
            _normalize_steps(scoped_steps, goal=user_query),
            skill_registry=getattr(self.agent, "skill_registry", None),
            user_query=user_query,
            extra_context=extra_context,
            available_artifacts=available_artifacts,
        )
        analysis = deepcopy(raw.get("analysis")) if isinstance(raw.get("analysis"), dict) else {}
        analysis["requested_assays"] = scope["requested_assays"]
        analysis["requested_modalities"] = scope["requested_modalities"]
        analysis["target_prediction_only"] = bool(scope.get("target_prediction_only"))
        deliverables = deepcopy(raw.get("deliverables")) if isinstance(raw.get("deliverables"), dict) else {}
        requirements = deepcopy(raw.get("requirements")) if isinstance(raw.get("requirements"), dict) else {}
        plan = {
            "goal": plan_goal,
            "steps": steps,
            "analysis": analysis,
            "deliverables": deliverables,
            "requirements": requirements,
            "version": 1,
        }
        return self._normalize_plan_lifecycle(plan)

    def _replan(
        self,
        plan: Dict[str, Any],
        *,
        user_query: str,
        extra_context: str,
        available_artifacts: Optional[List[str]] = None,
        failed_step: Optional[Dict[str, Any]] = None,
        failure_reason: str = "",
        history: Optional[List[Dict[str, Any]]] = None,
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
    ) -> Dict[str, Any]:
        current_plan_text = _format_plan_for_planner(plan)
        recent_history = _format_recent_history(history)
        prior_scope = plan.get("analysis") if isinstance(plan.get("analysis"), dict) else {}
        scope = {
            "requested_assays": list(prior_scope.get("requested_assays") or []),
            "requested_modalities": list(prior_scope.get("requested_modalities") or []),
            "target_prediction_only": bool(prior_scope.get("target_prediction_only")),
            "restricted": bool(prior_scope.get("requested_assays")),
        }
        if not scope["restricted"]:
            scope = _derive_requested_scope(_scope_request_text(user_query, history))
        conversation_block = (
            f"\n\nRecent conversation:\n{recent_history}"
            if recent_history
            else ""
        )
        failure_block = ""
        if failed_step:
            failure_block = (
                f"\n\nFailed step:\n"
                f"  id={failed_step.get('id')} title={failed_step.get('title')}\n"
                f"  reason: {failure_reason or failed_step.get('result') or 'unknown'}\n"
            )
        planning_skill_guidance = _load_planning_skill_guidance(
            getattr(self.agent, "skill_registry", None), user_query,
        )
        messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": _build_replanner_system_prompt(self.skill_overview, planning_skill_guidance),
            },
            {
                "role": "user",
                "content": (
                    f"User request:\n{user_query}\n\n"
                    f"{_scope_block(scope)}\n\n"
                    f"Session context:\n{extra_context or '(none)'}\n\n"
                    f"{conversation_block}"
                    f"Current plan:\n{current_plan_text}"
                    f"{failure_block}\n\n"
                    "Revise the plan. Output full updated JSON."
                ),
            },
        ]
        self._emit(on_progress, "plan_revising", message="根据执行结果修正计划…")
        raw = self._complete_plan_json(
            messages, on_progress=on_progress, cancel_event=cancel_event,
        )
        review_guidance = _load_plan_review_skill_guidance(
            getattr(self.agent, "skill_registry", None), raw.get("steps"), user_query,
        )
        try:
            reviewed_completion = self.agent._llm_complete_cancellable(
                [
                    {"role": "system", "content": _build_plan_review_system_prompt(review_guidance)},
                    {
                        "role": "user",
                        "content": (
                            f"User request:\n{user_query}\n\n"
                            f"{_scope_block(scope)}\n\n"
                            f"Session context:\n{extra_context or '(none)'}\n\n"
                            f"Proposed revised plan:\n{json.dumps(raw, ensure_ascii=False)}"
                        ),
                    },
                ],
                tools=None,
                cancel_event=cancel_event,
                on_progress=on_progress,
                enable_thinking=False,
            )
            reviewed_raw = _parse_plan_json(str(reviewed_completion.content or ""))
            if isinstance(reviewed_raw.get("steps"), list) and reviewed_raw["steps"]:
                raw = reviewed_raw
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        replanned_goal, scoped_steps = _enforce_requested_scope(
            str(raw.get("goal") or plan.get("goal") or user_query), raw.get("steps") or [],
            scope=scope, fallback_goal=str(plan.get("goal") or user_query),
        )
        new_steps = _enforce_workflow_contracts(
            _normalize_steps(scoped_steps, goal=plan.get("goal") or user_query),
            skill_registry=getattr(self.agent, "skill_registry", None),
            user_query=user_query,
            extra_context=extra_context,
            available_artifacts=list(dict.fromkeys([
                *_refresh_plan_artifact_state(plan), *(available_artifacts or []),
            ])),
        )
        analysis = deepcopy(raw.get("analysis")) if isinstance(raw.get("analysis"), dict) else {}
        analysis["requested_assays"] = scope["requested_assays"]
        analysis["requested_modalities"] = scope["requested_modalities"]
        analysis["target_prediction_only"] = bool(scope.get("target_prediction_only"))
        deliverables = deepcopy(raw.get("deliverables")) if isinstance(raw.get("deliverables"), dict) else {}
        requirements = deepcopy(raw.get("requirements")) if isinstance(raw.get("requirements"), dict) else {}

        # Prerequisite insertion and workflow ordering can change positional
        # IDs.  Match logical steps by skill/modality/title so a completed
        # result cannot be attached to a different re-planned step.
        old_by_identity = {
            _step_identity(step): step
            for step in (plan.get("steps") or [])
            if isinstance(step, dict)
        }
        raw_by_identity = {
            _step_identity(step): step
            for step in (raw.get("steps") or [])
            if isinstance(step, dict)
        }
        for step in new_steps:
            identity = _step_identity(step)
            old = old_by_identity.get(identity)
            replanner_status = str((raw_by_identity.get(identity) or {}).get("status") or "").strip()
            if replanner_status in {
                STEP_DONE, STEP_FAILED, STEP_SKIPPED, STEP_PENDING, STEP_RUNNING, STEP_AWAITING_APPROVAL,
            }:
                step["status"] = replanner_status
            elif old and old.get("status") == STEP_DONE:
                step["status"] = STEP_DONE
                step["result"] = old.get("result") or step.get("result") or ""
            # Never copy STEP_FAILED onto a replan step when the LLM omitted
            # status — replan exists to retry. Explicit replanner_status=failed
            # above still allows abandoning a step on purpose.
            elif old and old.get("status") == STEP_FAILED:
                step["status"] = STEP_PENDING
                if not str(step.get("result") or "").strip():
                    step["result"] = ""

        revised = {
            "goal": replanned_goal,
            "steps": new_steps,
            "analysis": analysis,
            "deliverables": deliverables,
            "requirements": requirements,
            "artifactState": deepcopy(plan.get("artifactState") or {}),
            "version": int(plan.get("version") or 1) + 1,
        }
        return self._normalize_plan_lifecycle(revised)

    def _ensure_user_facing_reply(
        self,
        user_query: str,
        text: str,
        *,
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
    ) -> str:
        text = (text or "").strip()
        if not _looks_like_internal_report(text):
            return text
        self._emit(on_progress, "status", message="正在整理回复…")
        messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are sRNAgent. Output ONLY the message to show the user in chat. "
                    "Use the same language as the user. Reply directly in second person. "
                    "Never describe your actions in third person (no '已向用户', '等待下一步')."
                ),
            },
            {"role": "user", "content": user_query},
        ]
        completion = self.agent._llm_complete_cancellable(
            messages,
            tools=None,
            cancel_event=cancel_event,
            on_progress=on_progress,
            enable_thinking=False,
        )
        reply = str(completion.content or "").strip()
        return reply or text

    def _execute_step(
        self,
        step: Dict[str, Any],
        *,
        step_index: int,
        step_total: int,
        plan_goal: str,
        user_query: str,
        history: List[Dict[str, str]],
        plan: Dict[str, Any],
        resume_messages: Optional[List[Dict[str, Any]]] = None,
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
        code_approval_callback: Optional["CodeApprovalCallback"] = None,
    ) -> str:
        # The plan may have been produced by an LLM or restored from an older
        # session. Resolve quantitative defaults again from the registered
        # SKILL.md metadata at the execution boundary.
        _bind_execution_skill_from_registry(
            step,
            getattr(self.agent, "skill_registry", None),
            history,
        )
        checkpoint_extra = {"plan": plan, "step_id": step.get("id")}

        def scoped_progress(event: Dict[str, Any]) -> None:
            """Keep LLM turn IDs unique across independently executed plan steps."""
            if on_progress is None:
                return
            payload = dict(event or {})
            payload.setdefault("planStepIndex", step_index)
            try:
                turn = int(payload.get("turn") or 0)
            except (TypeError, ValueError):
                turn = 0
            if turn > 0:
                payload["roundId"] = f"step-{step_index}:turn-{turn}"
            on_progress(payload)

        bound_skill = None
        skill_prompt = ""
        if str(step.get("skill") or "").strip():
            bound_skill = resolve_skill_query(self.agent.skill_registry, str(step.get("skill") or ""))
            if bound_skill is not None:
                skill_prompt = bound_skill.prompt_instructions(max_chars=5000)

        # Single-step chat-like tasks: use normal conversation loop (no subtask framing).
        if step_total == 1 and not str(step.get("skill") or "").strip():
            if resume_messages:
                result = self.agent._tool_loop(
                    resume_messages,
                    on_progress=scoped_progress,
                    cancel_event=cancel_event,
                    code_approval_callback=code_approval_callback,
                    chat_id=self.chat_id,
                    checkpoint_extra=checkpoint_extra,
                )
            else:
                result = self.agent.run_with_history(
                    history,
                    on_progress=scoped_progress,
                    cancel_event=cancel_event,
                    code_approval_callback=code_approval_callback,
                    chat_id=self.chat_id,
                    _attach_elapsed=False,
                )
            return self._ensure_user_facing_reply(
                user_query,
                result,
                on_progress=scoped_progress,
                cancel_event=cancel_event,
            )

        executor_system = _build_executor_system_prompt(
            self.agent.system_prompt,
            step=step,
            step_index=step_index,
            step_total=step_total,
            plan_goal=plan_goal,
            analysis=plan.get("analysis") if isinstance(plan, dict) else None,
            deliverables=plan.get("deliverables") if isinstance(plan, dict) else None,
            requirements=plan.get("requirements") if isinstance(plan, dict) else None,
            confirmed_approvals=_format_confirmed_approvals(plan),
            skill_prompt=skill_prompt,
            conversation_context=_format_recent_history(history),
        )
        if resume_messages:
            messages: List[Dict[str, Any]] = resume_messages
        else:
            messages = [
                {"role": "system", "content": executor_system},
                {"role": "user", "content": _build_step_user_message(
                    user_query=user_query, step=step, step_total=step_total
                )},
            ]
        return self._ensure_user_facing_reply(
            user_query,
            self.agent._tool_loop(
                messages,
                on_progress=scoped_progress,
                cancel_event=cancel_event,
                code_approval_callback=code_approval_callback,
                chat_id=self.chat_id,
                checkpoint_extra=checkpoint_extra,
            ),
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    def _execute_step_resilient(
        self,
        step: Dict[str, Any],
        *,
        step_index: int,
        step_total: int,
        plan_goal: str,
        user_query: str,
        history: List[Dict[str, str]],
        plan: Dict[str, Any],
        resume_messages: Optional[List[Dict[str, Any]]] = None,
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
        code_approval_callback: Optional["CodeApprovalCallback"] = None,
    ) -> str:
        """Run ``_execute_step`` with a few retries on transient API/network errors."""
        last_result = ""
        for attempt in range(_MAX_STEP_TRANSIENT_RETRIES + 1):
            attempt_resume = resume_messages
            if attempt > 0:
                checkpoint = self._load_checkpoint()
                if (
                    checkpoint
                    and checkpoint.get("step_id") == step.get("id")
                    and isinstance(checkpoint.get("messages"), list)
                    and checkpoint["messages"]
                ):
                    attempt_resume = checkpoint["messages"]
                self._emit(
                    on_progress,
                    "status",
                    message=(
                        f"步骤 {step_index} 遇到临时 API/网络错误，"
                        f"正在重试（{attempt}/{_MAX_STEP_TRANSIENT_RETRIES}）…"
                    ),
                )
            try:
                return self._execute_step(
                    step,
                    step_index=step_index,
                    step_total=step_total,
                    plan_goal=plan_goal,
                    user_query=user_query,
                    history=history,
                    plan=plan,
                    resume_messages=attempt_resume,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                    code_approval_callback=code_approval_callback,
                )
            except Exception as exc:  # noqa: BLE001 - convert to step result after retries
                if type(exc).__name__ == "AgentCancelledError":
                    raise
                last_result = f"STEP_EXECUTION_ERROR: {type(exc).__name__}: {exc}"
                if _is_transient_exception(exc) and attempt < _MAX_STEP_TRANSIENT_RETRIES:
                    continue
                return last_result
        return last_result or "STEP_EXECUTION_ERROR: transient retries exhausted"

    @staticmethod
    def _next_pending_step(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return the first pending step whose declared prerequisites are complete."""
        return PlanGraph(plan).next_runnable_pending(
            pending=STEP_PENDING,
            completed={STEP_DONE, STEP_SKIPPED},
        )

    def run(
        self,
        history: List[Dict[str, str]],
        *,
        extra_context: str = "",
        available_artifacts: Optional[List[str]] = None,
        resume: bool = False,
        route_intent: str = "",
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
        code_approval_callback: Optional["CodeApprovalCallback"] = None,
    ) -> str:
        user_query = _extract_user_query(history)
        if not user_query:
            raise ValueError("No user message in history")

        checkpoint: Optional[Dict[str, Any]] = None
        if resume and self.chat_id:
            checkpoint = self._load_checkpoint()

        # The router defaults all non-operational text to an answer. This is
        # deliberately broader than a question-mark heuristic: factual
        # English, Chinese, and mixed-language turns cannot create a plan.
        if IntentRouter.route(user_query).intent == RouteIntent.ANSWER and not resume:
            self._emit(on_progress, "status", message="正在回复…")
            result = self.agent.run_with_history(
                history,
                on_progress=on_progress,
                cancel_event=cancel_event,
                code_approval_callback=code_approval_callback,
                chat_id=self.chat_id,
                _attach_elapsed=False,
                extra_context=extra_context,
            )
            result = self._ensure_user_facing_reply(
                user_query,
                result,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            self._emit(on_progress, "final", content=result)
            return result

        # Resume a checkpointed run that has no plan (single-step / non-plan mode):
        # continue the interrupted message loop directly.
        if (
            resume
            and checkpoint
            and not checkpoint.get("plan")
            and isinstance(checkpoint.get("messages"), list)
            and checkpoint["messages"]
        ):
            self._emit(on_progress, "status", message="正在恢复上次运行…")
            result = self.agent._tool_loop(
                checkpoint["messages"],
                on_progress=on_progress,
                cancel_event=cancel_event,
                code_approval_callback=code_approval_callback,
                chat_id=self.chat_id,
            )
            result = self._ensure_user_facing_reply(
                user_query,
                result,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            self._emit(on_progress, "final", content=result)
            return result

        # Resume with the checkpointed plan whenever possible.  A SIGSEGV can
        # kill the process between writing plan.json and its checkpoint; in
        # that case plan.json is still the authoritative record of the user's
        # unfinished request.  Do not reapply policies based on a bare
        # "continue": doing so can remove an earlier explicit HTML report
        # requirement and overwrite the plan with a new, unrelated one.
        restored_plan = None
        if resume and checkpoint and isinstance(checkpoint.get("plan"), dict):
            restored_plan = checkpoint["plan"]
        elif resume:
            restored_plan = self._load_persisted_plan()

        resumed_existing_plan = restored_plan is not None
        is_plan_amendment = str(route_intent or "") == RouteIntent.AMEND_PLAN.value
        if resumed_existing_plan:
            # Normalize legacy plans before consuming a new reply. Otherwise a
            # bare "可以" can approve an old, empty group card before its
            # required metadata preflight has had a chance to run.
            self._normalize_plan_lifecycle(restored_plan)
            plan = self._prepare_restored_plan(
                restored_plan,
                approval_response="" if is_plan_amendment else user_query if resume else "",
            )
            if is_plan_amendment:
                plan = self._replan(
                    plan,
                    user_query=user_query,
                    extra_context=extra_context,
                    available_artifacts=available_artifacts,
                    history=history,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                )
                self._persist_plan(plan)
                self._save_step_checkpoint(plan, None)
                self._emit(
                    on_progress,
                    "plan_revised",
                    plan=plan,
                    message="已按用户明确的工作流变更更新计划。",
                )
            # Older persisted plans predate the scope contract.  Reconstruct
            # it from the user conversation before any pending step can run.
            existing_analysis = plan.get("analysis") if isinstance(plan.get("analysis"), dict) else {}
            scope = {
                "requested_assays": list(existing_analysis.get("requested_assays") or []),
                "requested_modalities": list(existing_analysis.get("requested_modalities") or []),
                "target_prediction_only": bool(existing_analysis.get("target_prediction_only")),
                "restricted": bool(existing_analysis.get("requested_assays")),
            }
            if not scope["restricted"]:
                scope_text = _scope_request_text(user_query, history)
                scope = _derive_requested_scope(scope_text)
                if not scope["restricted"] and _CONTINUATION_RE.fullmatch(str(user_query or "").strip()):
                    # Legacy plans may lack a recorded contract. A bare
                    # "continue" cannot narrow their original scope.
                    scope = {
                        "requested_assays": [], "requested_modalities": [],
                        "target_prediction_only": False, "restricted": False,
                    }
            elif "target_prediction_only" not in existing_analysis:
                scope["target_prediction_only"] = bool(
                    _derive_requested_scope(_scope_request_text(user_query, history)).get("target_prediction_only")
                )
            scoped_goal, scoped_steps = _enforce_requested_scope(
                str(plan.get("goal") or user_query), plan.get("steps") or [],
                scope=scope, fallback_goal=user_query,
            )
            plan["goal"] = scoped_goal
            # Preserve restored statuses and identities. `_normalize_steps`
            # intentionally creates a fresh pending plan, which is correct
            # for LLM output but wrong for an interrupted persisted plan.
            plan["steps"] = scoped_steps
            # Repair persisted target-only plans with the same deterministic
            # compiler used for new plans. Older plans could have been
            # reordered by the DE presentation helper after target contracts
            # were applied, leaving miRanda runnable ahead of its candidate
            # confirmation gate.
            if scope.get("target_prediction_only"):
                plan["steps"] = _enforce_workflow_contracts(
                    plan["steps"],
                    skill_registry=getattr(self.agent, "skill_registry", None),
                    user_query=str(plan.get("goal") or user_query),
                    extra_context=extra_context,
                    available_artifacts=list(dict.fromkeys([
                        *_refresh_plan_artifact_state(plan), *(available_artifacts or []),
                    ])),
                )
                self._normalize_plan_lifecycle(plan)
            analysis = dict(existing_analysis)
            analysis["requested_assays"] = scope["requested_assays"]
            analysis["requested_modalities"] = scope["requested_modalities"]
            analysis["target_prediction_only"] = bool(scope.get("target_prediction_only"))
            plan["analysis"] = analysis
            self._emit(
                on_progress,
                "plan_restored",
                plan=plan,
                message=f"已恢复上次计划（{len(plan.get('steps') or [])} 个步骤）",
            )
        else:
            self._emit(on_progress, "status", message="正在制定执行计划…")
            plan = self._create_plan(
                user_query,
                extra_context,
                available_artifacts=available_artifacts,
                history=history,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            self._persist_plan(plan)
            self._save_step_checkpoint(plan, None)
            self._emit(
                on_progress,
                "plan_created",
                plan=plan,
                message=f"计划已生成：{len(plan.get('steps') or [])} 个步骤",
            )

        # A resumed message is a control instruction (for example "继续"),
        # not a new scientific request.  The executor must receive the
        # persisted objective, otherwise it may answer the control message or
        # repeat resource discovery instead of running the next DAG node.
        execution_user_query = str(plan.get("goal") or user_query) if resumed_existing_plan else user_query

        from .lc_plan_graph import lc_plan_graph_enabled, run_lc_plan_steps

        if lc_plan_graph_enabled():
            return run_lc_plan_steps(
                self,
                plan=plan,
                history=history,
                user_query=user_query,
                execution_user_query=execution_user_query,
                extra_context=extra_context,
                available_artifacts=available_artifacts,
                resume=resume,
                checkpoint=checkpoint,
                on_progress=on_progress,
                cancel_event=cancel_event,
                code_approval_callback=code_approval_callback,
            )

        replan_attempts = 0
        if self._materialize_approval_followup(plan):
            self._persist_plan(plan)
            self._save_step_checkpoint(plan, None)
        steps_list = plan.get("steps") or []
        if not isinstance(steps_list, list) or not steps_list:
            # Never turn a planner/policy failure into a successful empty plan.
            # This used to emit `plan_complete` immediately, even though no
            # executor had run and no requested artifact could exist.
            message = "计划生成失败：未生成任何可执行步骤，任务尚未运行。"
            self._emit(on_progress, "plan_failed", plan=plan, message=message)
            raise ValueError(message)
        step_total = len(steps_list)

        while True:
            self.agent._check_cancelled(cancel_event)
            if _auto_complete_target_candidate_gates(plan):
                self._persist_plan(plan)
            pending = self._next_pending_step(plan)
            if pending is None:
                graph = PlanGraph(plan)
                waiting = graph.first_with_status(STEP_AWAITING_APPROVAL)
                if waiting:
                    if waiting.get("status") == STEP_DONE:
                        continue
                    prompt = _build_approval_request(
                        plan,
                        waiting,
                        history=history,
                        extra_context=extra_context,
                    )
                    self._persist_plan(plan)
                    self._save_step_checkpoint(plan, None)
                    self._emit(on_progress, "plan_approval_required", plan=plan, stepId=waiting.get("id"), message=prompt)
                    self._emit(on_progress, "final", content=prompt)
                    return prompt
                blocked = graph.blocked_pending(
                    pending=STEP_PENDING,
                    completed={STEP_DONE, STEP_SKIPPED},
                )
                if blocked:
                    labels = "; ".join(
                        f"{step.get('title') or step.get('id') or '未命名步骤'}"
                        f" <- {', '.join(graph.unmet_dependencies(step, {STEP_DONE, STEP_SKIPPED})) or '未知依赖'}"
                        for step in blocked[:3]
                    )
                    message = f"计划被未完成或缺失的依赖阻塞，尚未执行：{labels}。"
                    self._emit(on_progress, "plan_failed", plan=plan, message=message)
                    self._emit(on_progress, "final", content=message)
                    return message
                break

            step_index = steps_list.index(pending) + 1
            if isinstance(pending.get("approval"), dict):
                if pending.get("status") == STEP_DONE:
                    continue
                pending["status"] = STEP_AWAITING_APPROVAL
                prompt = _build_approval_request(
                    plan,
                    pending,
                    history=history,
                    extra_context=extra_context,
                )
                self._persist_plan(plan)
                self._save_step_checkpoint(plan, None)
                self._emit(
                    on_progress,
                    "plan_approval_required",
                    plan=plan,
                    stepId=pending.get("id"),
                    stepIndex=step_index,
                    message=prompt,
                )
                self._emit(on_progress, "final", content=prompt)
                return prompt
            pending["status"] = STEP_RUNNING
            self._persist_plan(plan)
            self._emit(
                on_progress,
                "plan_step_start",
                plan=plan,
                stepId=pending.get("id"),
                stepIndex=step_index,
                stepTotal=step_total,
                title=pending.get("title"),
                message=f"正在执行步骤 {step_index}/{step_total}：{pending.get('title')}",
            )

            resume_messages: Optional[List[Dict[str, Any]]] = None
            if (
                resume
                and checkpoint
                and checkpoint.get("step_id") == pending.get("id")
                and isinstance(checkpoint.get("messages"), list)
                and checkpoint["messages"]
            ):
                resume_messages = checkpoint["messages"]

            result = self._execute_step_resilient(
                pending,
                step_index=step_index,
                step_total=step_total,
                plan_goal=str(plan.get("goal") or ""),
                user_query=execution_user_query,
                history=history,
                plan=plan,
                resume_messages=resume_messages,
                on_progress=on_progress,
                cancel_event=cancel_event,
                code_approval_callback=code_approval_callback,
            )
            outcome, result = _resolve_step_result(result, pending)

            if outcome == "failed":
                pending["status"] = STEP_FAILED
                pending["result"] = result
                self._persist_plan(plan)
                self._save_step_checkpoint(plan, None)
                self._emit(
                    on_progress,
                    "plan_step_failed",
                    plan=plan,
                    stepId=pending.get("id"),
                    stepIndex=step_index,
                    message=_step_failure_message(step_index, result),
                )

                if replan_attempts >= self.max_replan_attempts:
                    summary = self._ensure_user_facing_reply(
                        user_query,
                        _build_final_summary(plan),
                        on_progress=on_progress,
                        cancel_event=cancel_event,
                    )
                    self._emit(on_progress, "plan_incomplete", plan=plan, message=summary)
                    self._emit(on_progress, "final", content=summary)
                    return summary

                replan_attempts += 1
                plan = self._replan(
                    plan,
                    user_query=user_query,
                    extra_context=extra_context,
                    available_artifacts=available_artifacts,
                    failed_step=pending,
                    failure_reason=result,
                    history=history,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                )
                steps_list = plan.get("steps") or []
                step_total = len(steps_list)
                self._persist_plan(plan)
                self._save_step_checkpoint(plan, None)
                self._emit(
                    on_progress,
                    "plan_revised",
                    plan=plan,
                    message=f"计划已更新（第 {plan.get('version')} 版）",
                )
                continue

            pending["status"] = STEP_DONE
            pending["result"] = result
            self._persist_plan(plan)
            self._save_step_checkpoint(plan, None)
            self._emit(
                on_progress,
                "plan_step_done",
                plan=plan,
                stepId=pending.get("id"),
                stepIndex=step_index,
                result=result[:600] if result else "",
                message=f"步骤 {step_index}/{step_total} 完成",
            )

            # Normal success is a deterministic state transition.  Do not ask
            # the LLM to formulate a second plan after every step: that used
            # to duplicate prerequisites and re-run resource checks.  Replan
            # remains reserved for an actual execution failure above; a new
            # user request enters through the bridge as a fresh plan.

        summary = self._ensure_user_facing_reply(
            user_query,
            _build_final_summary(plan),
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        # A completed plan is durable history, not resumable execution state.
        # Keep its result in session memory / run ledger, but remove the
        # checkpoint so a later "继续" request is interpreted as a new
        # follow-up instead of replaying the finished plan.
        self.agent._clear_run_checkpoint(self.chat_id)
        terminal_event = (
            "plan_incomplete"
            if any(step.get("status") == STEP_FAILED for step in plan.get("steps") or [])
            else "plan_complete"
        )
        self._emit(on_progress, terminal_event, plan=plan, message=summary)
        self._emit(on_progress, "final", content=summary)
        return summary
