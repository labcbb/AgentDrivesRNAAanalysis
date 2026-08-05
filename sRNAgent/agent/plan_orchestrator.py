"""Plan-and-Execute orchestrator for sRNAgent.

Planner creates/revises a structured plan; each step runs in an isolated tool-loop
with its own turn budget (max_turns per step).
"""
from __future__ import annotations

import json
import re
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
_FRAGMENTOMICS_RE = re.compile(r"fragmentomics|fragomics|fragment-analysis|片段组学|FSD|FSC|RCD|EDM|BPM", re.I)
_UNIFIED_RE = re.compile(r"统一做|统一跑|都跑|一起跑|两组学|两种组学|miRNA\s*\+\s*fragmentomics", re.I)
_HTML_REPORT_RE = re.compile(r"html\s*报告|html report|report\.html|生成.*html|写.*html|报告", re.I)
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
    if _MIRNA_RE.search(text):
        return "miRNA"
    return "general"


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

    if explicit_paired and paired_feasible is False:
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
        modalities = ["miRNA", "fragmentomics"]
    elif not modalities:
        if _MIRNA_RE.search(combined):
            modalities.append("miRNA")
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
    combined = "\n".join(
        part for part in (str(user_query or "").strip(), str(extra_context or "").strip()) if part
    )
    html_report_requested = bool(_HTML_REPORT_RE.search(combined))
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


def _apply_deliverables_policy(
    steps: List[Dict[str, Any]],
    *,
    deliverables: Dict[str, Any],
) -> List[Dict[str, Any]]:
    if not bool(deliverables.get("html_report_requested")):
        return steps
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

    html_report_requested = bool((deliverables or {}).get("html_report_requested")) or bool(_HTML_REPORT_RE.search(combined))
    mudata_required = bool(_MUDATA_RE.search(combined))
    whole_genome_bam_required = bool(_WHOLE_GENOME_BAM_RE.search(combined))
    default_unpaired = str((analysis or {}).get("design") or "").strip().lower() == "unpaired"
    if not default_unpaired and not bool(_PAIRED_RE.search(user_query or "")):
        if _UNPAIRED_RE.search(combined) or _DE_STEP_RE.search(combined):
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
    if any(str(item.get("skill") or "").strip().lower() == skill.lower() for item in expanded):
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
            if not has_counts:
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

        if skill_slug not in {
            "alignment-srna",
            "feature-count",
            "samtools_idxstats",
            "fragment-analysis",
            "differential-analysis",
            "mirdeep2-mirna",
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
        "10. If the user asks for an HTML report, keep an explicit final step that writes a real .html artifact.\n\n"
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
    return f"{agent_system_prompt}\n{skill_block}{analysis_block}{deliverables_block}{requirements_block}{step_block}"


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
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
    ) -> Dict[str, Any]:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _build_planner_system_prompt(self.skill_overview)},
            {
                "role": "user",
                "content": (
                    f"User request:\n{user_query}\n\n"
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
        steps = _expand_plan_prerequisites(
            steps,
            extra_context=extra_context,
        )
        analysis = _resolve_analysis_policy(user_query, extra_context, steps)
        steps = _apply_analysis_policy(steps, analysis=analysis)
        deliverables = _resolve_deliverables_policy(user_query, extra_context, steps)
        steps = _apply_deliverables_policy(steps, deliverables=deliverables)
        analysis["modalities"] = _infer_modalities_from_steps(steps) or list(analysis.get("modalities") or [])
        requirements = _resolve_requirements_policy(
            user_query,
            extra_context,
            analysis=analysis,
            deliverables=deliverables,
        )
        plan = {
            "goal": str(raw.get("goal") or user_query).strip(),
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
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
    ) -> Dict[str, Any]:
        current_plan_text = _format_plan_for_planner(plan)
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
        new_steps = _expand_plan_prerequisites(
            new_steps,
            extra_context=extra_context,
        )
        analysis = _resolve_analysis_policy(user_query, extra_context, new_steps)
        new_steps = _apply_analysis_policy(new_steps, analysis=analysis)
        deliverables = _resolve_deliverables_policy(user_query, extra_context, new_steps)
        new_steps = _apply_deliverables_policy(new_steps, deliverables=deliverables)
        analysis["modalities"] = _infer_modalities_from_steps(new_steps) or list(analysis.get("modalities") or [])
        requirements = _resolve_requirements_policy(
            user_query,
            extra_context,
            analysis=analysis,
            deliverables=deliverables,
        )

        # Preserve done results from old plan when replanner omits them
        old_by_id = {str(s.get("id")): s for s in (plan.get("steps") or [])}
        for step in new_steps:
            old = old_by_id.get(str(step.get("id")))
            replanner_status = str(
                next(
                    (
                        item.get("status")
                        for item in (raw.get("steps") or [])
                        if isinstance(item, dict)
                        and str(item.get("id") or "") == str(step.get("id"))
                    ),
                    "",
                )
            ).strip()
            if replanner_status in {STEP_DONE, STEP_FAILED, STEP_SKIPPED, STEP_PENDING, STEP_RUNNING}:
                step["status"] = replanner_status
            elif old and old.get("status") == STEP_DONE:
                step["status"] = STEP_DONE
                step["result"] = old.get("result") or step.get("result") or ""
            elif old and old.get("status") == STEP_FAILED:
                step["status"] = STEP_FAILED
                step["result"] = old.get("result") or step.get("result") or ""

        revised = {
            "goal": str(raw.get("goal") or plan.get("goal") or user_query).strip(),
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
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                    code_approval_callback=code_approval_callback,
                    chat_id=self.chat_id,
                    checkpoint_extra=checkpoint_extra,
                )
            else:
                result = self.agent.run_with_history(
                    history,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                    code_approval_callback=code_approval_callback,
                    chat_id=self.chat_id,
                    _attach_elapsed=False,
                )
            return self._ensure_user_facing_reply(
                user_query,
                result,
                on_progress=on_progress,
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
                on_progress=on_progress,
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

        # Resume with a persisted plan: reuse it instead of planning from scratch.
        if resume and checkpoint and checkpoint.get("plan"):
            plan = checkpoint["plan"]
            if isinstance(plan, dict):
                resumed_steps = list(plan.get("steps") or [])
                analysis = _resolve_analysis_policy(user_query, extra_context, resumed_steps)
                plan["steps"] = _apply_analysis_policy(resumed_steps, analysis=analysis)
                deliverables = _resolve_deliverables_policy(user_query, extra_context, plan.get("steps") or [])
                plan["steps"] = _apply_deliverables_policy(plan.get("steps") or [], deliverables=deliverables)
                analysis["modalities"] = _infer_modalities_from_steps(plan.get("steps") or []) or list(analysis.get("modalities") or [])
                plan["analysis"] = analysis
                plan["deliverables"] = deliverables
                plan["requirements"] = _resolve_requirements_policy(
                    user_query,
                    extra_context,
                    analysis=analysis,
                    deliverables=deliverables,
                )
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
        self._emit(on_progress, "plan_complete", plan=plan, message=summary)
        self._emit(on_progress, "final", content=summary)
        return summary
