"""Plan-and-Execute orchestrator for sRNAgent.

Planner creates/revises a structured plan; each step runs in an isolated tool-loop
with its own turn budget (max_turns per step).
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from .tools import list_available_skills, resolve_skill_query

if TYPE_CHECKING:
    from .srn_agent import SRNAgent, ProgressCallback, CodeApprovalCallback

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

_MAX_REPLAN_ATTEMPTS = 8
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_PIPELINE_KEYWORDS_RE = re.compile(
    r"\b(SRR|ERR|DRR|SRP|GSE|GSM)\d+\b|fastq|fasta|bam|sam|mirdeep|bowtie|cutadapt|"
    r"annadata|adata|multiqc|fastqc|ena|sra|mirbase|ensembl",
    re.I,
)
_ACTION_KEYWORDS_RE = re.compile(r"下载|比对|比对|定量|质控|运行|执行|处理|分析|align|download|quant|trim", re.I)
_READ_ONLY_QUERY_RE = re.compile(
    r"总结|汇总|查看|查询|读取|结果是什么|有没有跑过|是否完成|数目分布|数量分布|特征分布|"
    r"summari[sz]e|inspect|review|distribution|how many|what were the results",
    re.I,
)
_MUTATING_QUERY_RE = re.compile(
    r"下载|重新|重跑|重算|运行|执行|生成|创建|处理|安装|比对|定量|质控|"
    r"download|rerun|recompute|run|generate|create|align|quant|trim",
    re.I,
)
_INTERNAL_REPORT_RE = re.compile(
    r"已向用户|已向用户发送|已向.*发送|等待.{0,8}下一步|等待用户|"
    r"已发送问候|已介绍|介绍.*功能.*等待|"
    r"task completed|step (is )?done|waiting for (the )?user|"
    r"回复用户|向用户回复|发送问候",
    re.I,
)
_TRIMMED_FASTQ_RE = re.compile(r"\b(trimmed_path|clean_fastq_path)\b|(?:^|[/\\])[^ \n\t]+(?:trimmed|clean)[^ \n\t]*\.f(?:ast)?q(?:\.gz)?\b", re.I)
_RAW_FASTQ_RE = re.compile(r"\bfastq_path\b|(?:^|[/\\])[^ \n\t]+\.(?:fastq|fq)(?:\.gz)?\b", re.I)
_BAM_RE = re.compile(r"\bbam_path\b|(?:^|[/\\])[^ \n\t]+\.bam\b", re.I)
_GENOME_FASTA_RE = re.compile(
    r"\b(genome_fasta|reference_fasta|fasta_path)\b|(?:^|[/\\])[^ \n\t]+\.(?:fa|fasta)(?:\.gz)?\b",
    re.I,
)
_GENOME_INDEX_RE = re.compile(r"\bgenome_index\b|\.ebwt\b|index_basename\b", re.I)
_COUNTS_RE = re.compile(
    r'\blayers\["counts"\]\b|\badata\.X\b|\bcounts_csv\b|\bfc_counts_csv\b|\bidxstats_file\b|'
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
_TRNA_RE = re.compile(r"\btrna\b|tRNA|tRF|tRAX", re.I)
_ISOMIR_RE = re.compile(r"\biso[- ]?mir\b|isomiR|mirtop", re.I)
_FRAGMENTOMICS_RE = re.compile(r"fragmentomics|fragomics|fragment-analysis|片段组学|FSD|FSC|RCD|EDM|BPM", re.I)
_UNIFIED_RE = re.compile(r"统一做|统一跑|都跑|一起跑|两组学|两种组学|miRNA\s*\+\s*fragmentomics", re.I)
_HTML_REPORT_RE = re.compile(r"html\s*报告|html report|report\.html|生成.*html|写.*html|报告", re.I)
_DE_SUMMARY_RE = re.compile(r"汇总|summary|切片|输出.*结果|结果.*输出|top\s*(?:de|差异|feature)", re.I)
_MUDATA_RE = re.compile(r"\bmudata\b|MuData|h5mu|放在\s*mudata|放到\s*mudata|返回\s*mudata", re.I)
_WHOLE_GENOME_BAM_RE = re.compile(r"全基因组.*bam|whole[-\s]*genome\s+bam|genome[-\s]*aligned\s+bam", re.I)
_REQUIREMENT_CUE_RE = re.compile(
    r"(必须|需要|要|不要|不能|默认|优先|如果|若|只有|除非|确保|记得|统一做|放在|生成|写入|保存|返回)",
    re.I,
)


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
    """Short chat / greetings — skip plan mode and reply directly."""
    q = (query or "").strip()
    if not q or len(q) > 160:
        return False
    if _PIPELINE_KEYWORDS_RE.search(q) or _ACTION_KEYWORDS_RE.search(q):
        return False
    if re.match(r"^(你好|您好|hi|hello|hey|谢谢|感谢|再见|好的|ok|okay)[!！。.~～\s]*$", q, re.I):
        return True
    if re.search(r"(你|您)(能|可以|会).*(做什么|干什么|什么功能|怎么用|如何使用)", q):
        return True
    if re.match(r"^(介绍|说明|帮助|help)\b", q, re.I):
        return True
    if len(q) <= 48 and ("?" in q or "？" in q):
        return True
    return False


def _is_read_only_query(query: str) -> bool:
    """Return True for result lookups that must not create a pipeline plan."""
    q = (query or "").strip()
    if not q or _MUTATING_QUERY_RE.search(q):
        return False
    return bool(_READ_ONLY_QUERY_RE.search(q))


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
        raise ValueError("Planner returned empty response.")

    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    block_match = _JSON_BLOCK_RE.search(raw)
    if block_match:
        payload = json.loads(block_match.group(1))
        if isinstance(payload, dict):
            return payload

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        payload = json.loads(raw[start : end + 1])
        if isinstance(payload, dict):
            return payload

    raise ValueError(f"Could not parse plan JSON from planner response: {raw[:400]}")


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
        steps.append(
            {
                "id": str(item.get("id") or index),
                "title": title,
                "goal": step_goal,
                "skill": str(item.get("skill") or "").strip(),
                "status": STEP_PENDING,
                "result": "",
            }
        )

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
) -> Dict[str, Any]:
    return {
        "id": str(step_id),
        "title": str(title).strip(),
        "goal": str(goal).strip(),
        "skill": str(skill).strip(),
        "status": status,
        "result": str(result or ""),
        "autoInserted": bool(auto_inserted),
    }


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
    if _MIRNA_RE.search(text) or _TRNA_RE.search(text):
        return "srna"
    return "general"


def _step_identity(step: Dict[str, Any]) -> str:
    """Stable matching key for re-plans; positional plan IDs are not stable."""
    skill = str(step.get("skill") or "").strip().lower()
    title = re.sub(r"\s+", " ", str(step.get("title") or step.get("goal") or "").strip().lower())
    return "|".join((skill, _step_modality(step), title))


def _requires_srna_and_fragmentomics(user_query: str, steps: List[Dict[str, Any]]) -> bool:
    step_text = "\n".join(
        " ".join(str(step.get(key) or "") for key in ("title", "goal", "skill"))
        for step in steps
    )
    combined = f"{user_query}\n{step_text}"
    return bool(
        (_MIRNA_RE.search(combined) or _TRNA_RE.search(combined))
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
            r"miRNA\s*[、,，和与+]+\s*tRNA\s*[、,，和与+]+\s*片段组学\s*(三类|三种|三模态|3 类|3 种|3 模态)",
            "miRNA 与 tRNA（同属 srna）及片段组学两种模态",
            normalized_goal,
            flags=re.I,
        )
        normalized_goal = re.sub(r"三模态|3\s*模态", "两种模态", normalized_goal, flags=re.I)
        normalized_goal = re.sub(
            r"各自独立的?\s*AnnData",
            "srna 与 fragmentomics 的独立 AnnData",
            normalized_goal,
            flags=re.I,
        )
        if "srna" not in normalized_goal.lower() or "两种模态" not in normalized_goal:
            normalized_goal = (
                f"{normalized_goal}（miRNA 与 tRNA 共用 srna AnnData；"
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
            updated["goal"] = f"{step_goal}；miRNA 与 tRNA/tRF 结果共用 srna AnnData。".strip("；")
        elif modality == "fragmentomics" and "fragmentomics AnnData" not in step_goal:
            updated["goal"] = f"{step_goal}；结果写入独立 fragmentomics AnnData。".strip("；")
        bounded.append(updated)
    return normalized_goal, bounded


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
        if _MIRNA_RE.search(combined) or _TRNA_RE.search(combined):
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
        _FRAGMENTOMICS_RE.search(text) and re.search(r"定量|quant|提取|extract", text, re.I)
    )


def _is_de_input_validation_step(step: Dict[str, Any]) -> bool:
    text = " ".join(str(step.get(key) or "") for key in ("title", "goal", "skill"))
    return bool(re.search(r"核查.*(?:定量矩阵|counts?|count_matrix)|确认.*(?:样本)?分组", text, re.I))


def _order_de_workflow_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Enforce prerequisites -> quantification -> validation -> DE -> report."""
    if not any(_is_de_step(step) for step in steps):
        return steps

    prerequisites: List[Dict[str, Any]] = []
    quantification: List[Dict[str, Any]] = []
    validation: List[Dict[str, Any]] = []
    de_steps: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    reports: List[Dict[str, Any]] = []
    for step in steps:
        text = " ".join(str(step.get(key) or "") for key in ("title", "goal", "skill"))
        if _is_de_step(step):
            de_steps.append(step)
        elif _is_quantification_step(step):
            quantification.append(step)
        elif _is_de_input_validation_step(step):
            validation.append(step)
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
    has_planned_isomir_quantification = any(
        _is_quantification_step(step) and _step_modality(step) == "isomir"
        for step in steps
    )
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
                next_id += 0
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
            if not has_counts and not (modality == "isomir" and has_planned_isomir_quantification):
                if not has_bam:
                    if not has_trimmed_fastq and has_raw_fastq:
                        next_id = _ensure_step(
                            expanded,
                            next_id=next_id,
                            title="完成 FASTQ 质控与修剪",
                            goal="先做 small RNA FASTQ 质控与 adapter trimming，为后续定量做准备。",
                            skill="fastq-qc",
                        )
                        has_trimmed_fastq = True
                    if not has_genome_fasta:
                        next_id = _ensure_step(
                            expanded,
                            next_id=next_id,
                            title="准备参考基因组 FASTA",
                            goal="先准备参考基因组 FASTA，供后续比对/定量使用。",
                            skill="reference-download",
                        )
                        has_genome_fasta = True
                    next_id = _ensure_step(
                        expanded,
                        next_id=next_id,
                        title="完成参考基因组比对",
                        goal="先生成 BAM（bam_path），再进行下游定量，最终为差异分析准备 counts 矩阵。",
                        skill="alignment-srna",
                    )
                    has_bam = True
                next_id = _ensure_step(
                    expanded,
                    next_id=next_id,
                    title="生成小 RNA 定量矩阵",
                    goal="先生成或确认 adata.layers['counts'] / adata.X 中已有可用于差异分析的 raw counts 矩阵。",
                    skill="feature-count",
                )
                has_counts = True
            if not has_group_info:
                step_copy = _make_plan_step(
                    step_id=str(next_id),
                    title="确认样本分组信息",
                    goal="先检查 adata.obs 中是否已有 group/Condition/treatment 等分组列，并向用户确认后再进行差异分析。",
                    skill="",
                    auto_inserted=True,
                )
                expanded.append(step_copy)
                next_id += 1
                has_group_info = True
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
            expanded.append(_make_plan_step(
                step_id=str(next_id),
                title=str(step.get("title") or f"Step {next_id}"),
                goal=str(step.get("goal") or step.get("title") or ""),
                skill=str(step.get("skill") or ""),
                status=str(step.get("status") or STEP_PENDING),
                result=str(step.get("result") or ""),
                auto_inserted=bool(step.get("autoInserted")),
            ))
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


def _build_planner_system_prompt(skill_overview: str) -> str:
    skills_block = skill_overview or "(no skills loaded)"
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
        '      "skill": "optional skill slug from registered skills, or empty string"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "## Planning rules\n"
        "1. For simple questions (no code/pipeline), use a single step.\n"
        "2. For multi-step pipelines, split by natural phases: download → QC → "
        "reference → alignment → quantification.\n"
        "3. Each step must be independently completable in one focused execution session.\n"
        "4. Do not duplicate work already marked done in session context.\n"
        "5. Prefer skill slugs when a step matches a registered skill.\n"
        "6. Keep 1–8 steps; split oversized steps rather than one giant step.\n"
        "7. For differential analysis, default to unpaired unless the user explicitly asks for paired.\n"
        "8. If session context says paired is not feasible, do NOT plan paired DE steps.\n"
        "9. Do not keep paired and unpaired DE branches simultaneously unless the user explicitly asks to compare both.\n"
        "10. If the user asks for an HTML report, keep an explicit final step that writes a real .html artifact.\n"
        "11. miRNA and tRNA/tRF are one `srna` modality and must share one srna AnnData. "
        "They are not two modalities. A request for miRNA + tRNA + fragmentomics therefore has exactly "
        "two independent AnnData outputs: srna and fragmentomics. Do not propose MuData or joint analysis.\n"
        "12. If the latest request refers to earlier work ('continue', 'on this basis', '刚才的结果'), "
        "use the recent conversation and session context to identify completed prerequisites. "
        "Plan only the new requested work; never recreate completed QC, alignment, or quantification steps "
        "unless the relevant artifact is missing.\n\n"
        "## Registered skills\n"
        f"{skills_block}\n"
        f"{constitution_block}"
    )


def _build_replanner_system_prompt(skill_overview: str) -> str:
    base = _build_planner_system_prompt(skill_overview)
    return (
        f"{base}\n"
        "## Replanning mode\n"
        "You are revising an existing plan based on step results or failures.\n"
        "- Keep completed steps as status \"done\" with their results.\n"
        "- Mark failed steps as \"failed\" or replace them with smaller retry steps.\n"
        "- Add new steps only if needed; remove redundant pending steps.\n"
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
        )
        if items:
            requirements_block += "\n".join(f"- requirement: {item}\n" for item in items)
        requirements_block += (
            "Hard rule: do not ignore, overwrite, or silently drop these user requirements during replanning or execution.\n"
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
        "IMPORTANT: Complete ONLY this subtask in this session.\n"
        "- Do not start later pipeline stages.\n"
        "- When done, call `finish` with your message TO THE USER.\n"
        "- The finish message is shown directly in chat — reply naturally in second person.\n"
        "- NEVER write internal status reports (e.g. '已向用户…', '等待用户下一步', "
        "'Task completed', 'Step done').\n"
        "- The Jupyter kernel state (e.g. adata) persists across steps.\n"
    )
    return f"{agent_system_prompt}\n{skill_block}{analysis_block}{deliverables_block}{requirements_block}{conversation_block}{step_block}"


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


def _step_failed(result: str) -> bool:
    lowered = (result or "").lower()
    if "max turns" in lowered or "reached max turns" in lowered:
        return True
    if "agent stopped without" in lowered:
        return True
    if "cancelled" in lowered or "canceled" in lowered:
        return True
    return False


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

    lines = [f"## 任务完成：{goal}", ""]
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


PlanStore = Callable[[str, Dict[str, Any]], None]
PlanLoader = Callable[[str], Optional[Dict[str, Any]]]


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
    def _prepare_restored_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
        """Make an interrupted plan executable without changing its agreed scope."""
        restored = deepcopy(plan)
        for step in restored.get("steps") or []:
            if isinstance(step, dict) and step.get("status") == STEP_RUNNING:
                # No process is still executing after a resume request.  Treat
                # its interrupted step as the next pending unit of work.
                step["status"] = STEP_PENDING
        return restored

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
        self.agent._emit_progress(on_progress, event_type, **payload)

    def _create_plan(
        self,
        user_query: str,
        extra_context: str,
        *,
        history: Optional[List[Dict[str, Any]]] = None,
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
    ) -> Dict[str, Any]:
        recent_history = _format_recent_history(history)
        conversation_block = (
            f"\nRecent conversation (use to resolve references to earlier work):\n{recent_history}\n"
            if recent_history
            else ""
        )
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _build_planner_system_prompt(self.skill_overview)},
            {
                "role": "user",
                "content": (
                    f"User request:\n{user_query}\n\n"
                    f"{conversation_block}"
                    f"Session context:\n{extra_context or '(none)'}"
                ),
            },
        ]
        completion = self.agent._llm_complete_cancellable(
            messages,
            tools=None,
            cancel_event=cancel_event,
            on_progress=on_progress,
            enable_thinking=False,
        )
        raw = _parse_plan_json(str(completion.content or ""))
        steps = _normalize_steps(raw.get("steps"), goal=user_query)
        plan_goal, steps = _apply_modality_boundaries(
            str(raw.get("goal") or user_query), steps, user_query=user_query,
        )
        steps = _expand_plan_prerequisites(
            steps,
            extra_context=extra_context,
        )
        analysis = _resolve_analysis_policy(user_query, extra_context, steps)
        steps = _apply_analysis_policy(steps, analysis=analysis)
        deliverables = _resolve_deliverables_policy(user_query, extra_context, steps)
        steps = _apply_deliverables_policy(steps, deliverables=deliverables)
        steps = _order_de_workflow_steps(steps)
        plan_goal = _strip_unrequested_html_report(
            plan_goal,
            requested=bool(deliverables.get("html_report_requested")),
        )
        analysis["modalities"] = _infer_modalities_from_steps(steps) or list(analysis.get("modalities") or [])
        requirements = _resolve_requirements_policy(
            user_query,
            extra_context,
            analysis=analysis,
            deliverables=deliverables,
        )
        plan = {
            "goal": plan_goal,
            "steps": steps,
            "analysis": analysis,
            "deliverables": deliverables,
            "requirements": requirements,
            "version": 1,
        }
        return plan

    def _replan(
        self,
        plan: Dict[str, Any],
        *,
        user_query: str,
        extra_context: str,
        failed_step: Optional[Dict[str, Any]] = None,
        failure_reason: str = "",
        history: Optional[List[Dict[str, Any]]] = None,
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
    ) -> Dict[str, Any]:
        current_plan_text = _format_plan_for_planner(plan)
        recent_history = _format_recent_history(history)
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

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _build_replanner_system_prompt(self.skill_overview)},
            {
                "role": "user",
                "content": (
                    f"User request:\n{user_query}\n\n"
                    f"Session context:\n{extra_context or '(none)'}\n\n"
                    f"{conversation_block}"
                    f"Current plan:\n{current_plan_text}"
                    f"{failure_block}\n\n"
                    "Revise the plan. Output full updated JSON."
                ),
            },
        ]
        self._emit(on_progress, "plan_revising", message="根据执行结果修正计划…")
        completion = self.agent._llm_complete_cancellable(
            messages,
            tools=None,
            cancel_event=cancel_event,
            on_progress=on_progress,
            enable_thinking=False,
        )
        raw = _parse_plan_json(str(completion.content or ""))
        new_steps = _normalize_steps(raw.get("steps"), goal=plan.get("goal") or user_query)
        replanned_goal, new_steps = _apply_modality_boundaries(
            str(raw.get("goal") or plan.get("goal") or user_query),
            new_steps,
            user_query=user_query,
        )
        new_steps = _expand_plan_prerequisites(
            new_steps,
            extra_context=extra_context,
        )
        analysis = _resolve_analysis_policy(user_query, extra_context, new_steps)
        new_steps = _apply_analysis_policy(new_steps, analysis=analysis)
        deliverables = _resolve_deliverables_policy(user_query, extra_context, new_steps)
        new_steps = _apply_deliverables_policy(new_steps, deliverables=deliverables)
        new_steps = _order_de_workflow_steps(new_steps)
        replanned_goal = _strip_unrequested_html_report(
            replanned_goal,
            requested=bool(deliverables.get("html_report_requested")),
        )
        analysis["modalities"] = _infer_modalities_from_steps(new_steps) or list(analysis.get("modalities") or [])
        requirements = _resolve_requirements_policy(
            user_query,
            extra_context,
            analysis=analysis,
            deliverables=deliverables,
        )

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
            if replanner_status in {STEP_DONE, STEP_FAILED, STEP_SKIPPED, STEP_PENDING, STEP_RUNNING}:
                step["status"] = replanner_status
            elif old and old.get("status") == STEP_DONE:
                step["status"] = STEP_DONE
                step["result"] = old.get("result") or step.get("result") or ""
            elif old and old.get("status") == STEP_FAILED:
                step["status"] = STEP_FAILED
                step["result"] = old.get("result") or step.get("result") or ""

        revised = {
            "goal": replanned_goal,
            "steps": new_steps,
            "analysis": analysis,
            "deliverables": deliverables,
            "requirements": requirements,
            "version": int(plan.get("version") or 1) + 1,
        }
        return revised

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

    @staticmethod
    def _next_pending_step(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for step in plan.get("steps") or []:
            if step.get("status") == STEP_PENDING:
                return step
        return None

    def run(
        self,
        history: List[Dict[str, str]],
        *,
        extra_context: str = "",
        resume: bool = False,
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

        # Greetings / short chat: skip planning, reply like normal agent.
        if _is_conversational_query(user_query):
            self._emit(on_progress, "status", message="正在回复…")
            result = self.agent.run_with_history(
                history,
                on_progress=on_progress,
                cancel_event=cancel_event,
                code_approval_callback=code_approval_callback,
                chat_id=self.chat_id,
                _attach_elapsed=False,
            )
            result = self._ensure_user_facing_reply(
                user_query,
                result,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            self._emit(on_progress, "final", content=result)
            return result

        # Result lookups should use the existing adata/manifest/output files
        # directly. Planning them as a new pipeline causes prerequisite
        # expansion (QC, reference preparation, reports) and makes a simple
        # summary appear to restart the previous analysis.
        if _is_read_only_query(user_query):
            self._emit(on_progress, "status", message="正在读取已有结果…")
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

        if restored_plan is not None:
            plan = self._prepare_restored_plan(restored_plan)
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

        replan_attempts = 0
        steps_list = plan.get("steps") or []
        step_total = len(steps_list)

        while True:
            self.agent._check_cancelled(cancel_event)
            pending = self._next_pending_step(plan)
            if pending is None:
                break

            step_index = steps_list.index(pending) + 1
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

            result = self._execute_step(
                pending,
                step_index=step_index,
                step_total=step_total,
                plan_goal=str(plan.get("goal") or ""),
                user_query=user_query,
                history=history,
                plan=plan,
                resume_messages=resume_messages,
                on_progress=on_progress,
                cancel_event=cancel_event,
                code_approval_callback=code_approval_callback,
            )

            if _step_failed(result):
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
                    message=f"步骤 {step_index} 未在轮次上限内完成",
                )

                if replan_attempts >= self.max_replan_attempts:
                    summary = self._ensure_user_facing_reply(
                        user_query,
                        _build_final_summary(plan),
                        on_progress=on_progress,
                        cancel_event=cancel_event,
                    )
                    self._emit(on_progress, "plan_complete", plan=plan, message=summary)
                    self._emit(on_progress, "final", content=summary)
                    return summary

                replan_attempts += 1
                plan = self._replan(
                    plan,
                    user_query=user_query,
                    extra_context=extra_context,
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
        self._emit(on_progress, "plan_complete", plan=plan, message=summary)
        self._emit(on_progress, "final", content=summary)
        return summary
