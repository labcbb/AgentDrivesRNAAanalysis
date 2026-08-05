"""Session memory — medium-term context (steps, artifacts, workspace manifest)."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from session_errors import build_session_errors_context
from session_plan import load_plan, plan_progress_summary
from session_store import _read_json, _write_json, ensure_session_dir, sanitize_chat_id
from work_space import get_work_space

_MEMORY_FILE = "session_memory.json"
_WORK_LOG_FILE = "work_log.jsonl"
_LOCK = threading.RLock()
_MAX_STEPS = 48
_MAX_ARTIFACTS = 64
_MAX_FACTS = 32
_MAX_WORK_LOG_LINES = 200

_ARTIFACT_RE = re.compile(
    r"(?:[\w./-]+/)?[\w.-]+\.(?:"
    r"fastq(?:\.gz)?|fq(?:\.gz)?|fa(?:\.gz)?|fasta(?:\.gz)?|"
    r"gtf(?:\.gz)?|gff(?:\.gz)?|bed|tsv|csv|json|yaml|yml|"
    r"bam|bai|sam|dict|html|pdf|png|jpg|jpeg|svg|"
    r"h5ad|h5mu|ipynb|xlsx|xls|txt"
    r")(?:\b|$)",
    re.IGNORECASE,
)
_RUN_RE = re.compile(r"\b(SRR|ERR|DRR|SRP|GSE|GSM)\d+\b")
_IMPORTANT_DIRS = (
    "srna_fastq",
    "ref",
    "metadata_srna",
    "metadata",
    "pipeline",
    "results",
    "report",
    "reports",
    "figures",
    "plots",
    "aligned",
    "counts",
    "qc",
    "trimmed",
)
_FACT_PATTERNS = (
    re.compile(r"(adapter|接头).{0,40}(确认|使用|设为|设置为|为)\s*[:：]?\s*([^\n，。;；]{1,120})", re.I),
    re.compile(r"(分组|group).{0,20}(确认|使用|设为|设置为)\s*[:：]?\s*([^\n，。;；]{1,120})", re.I),
    re.compile(r"(novel miRNA).{0,20}(关闭|禁用|启用|开启|false|true)", re.I),
    re.compile(r"(链特异性|strand(?:ed)?).{0,20}(确认|为|设为|设置为)\s*[:：]?\s*([^\n，。;；]{1,120})", re.I),
    re.compile(r"\b(jobs\s*=\s*\d+)\b", re.I),
    re.compile(r"\b(force\s*=\s*(?:True|False))\b", re.I),
)
_HTML_REPORT_RE = re.compile(r"html\s*报告|html report|report\.html|生成.*html|写.*html|报告", re.I)
_MUDATA_RE = re.compile(r"\bmudata\b|MuData|h5mu|放在\s*mudata|放到\s*mudata|返回\s*mudata", re.I)
_WHOLE_GENOME_BAM_RE = re.compile(r"全基因组.*bam|whole[-\s]*genome\s+bam|genome[-\s]*aligned\s+bam", re.I)
_UNPAIRED_RE = re.compile(r"\bunpaired\b|非配对|不配对", re.I)
_PAIRED_RE = re.compile(r"(?<!un)\bpaired\b|(?<!非)配对", re.I)
_REQUIREMENT_CUE_RE = re.compile(
    r"(必须|需要|要|不要|不能|默认|优先|如果|若|只有|除非|确保|记得|统一做|放在|生成|写入|保存|返回)",
    re.I,
)
_KEYWORD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}")


def _memory_path(chat_id: str) -> Path:
    chat_id = sanitize_chat_id(chat_id)
    return ensure_session_dir(chat_id) / _MEMORY_FILE


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_session_memory(chat_id: str) -> Dict[str, Any]:
    if not chat_id:
        return {"steps": [], "artifacts": [], "facts": [], "analysis": {}, "deliverables": {}, "requirements": {}, "updatedAt": None}
    payload = _read_json(_memory_path(chat_id))
    if not payload:
        return {"steps": [], "artifacts": [], "facts": [], "analysis": {}, "deliverables": {}, "requirements": {}, "updatedAt": None}
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    deliverables = payload.get("deliverables") if isinstance(payload.get("deliverables"), dict) else {}
    requirements = payload.get("requirements") if isinstance(payload.get("requirements"), dict) else {}
    return {
        "steps": steps,
        "artifacts": [str(item) for item in artifacts if str(item).strip()],
        "facts": [str(item) for item in facts if str(item).strip()],
        "analysis": analysis,
        "deliverables": deliverables,
        "requirements": requirements,
        "updatedAt": payload.get("updatedAt"),
    }


def save_session_memory(chat_id: str, payload: Dict[str, Any]) -> None:
    if not chat_id:
        return
    chat_id = sanitize_chat_id(chat_id)
    body = {
        "chatId": chat_id,
        "steps": payload.get("steps") or [],
        "artifacts": payload.get("artifacts") or [],
        "facts": payload.get("facts") or [],
        "analysis": payload.get("analysis") or {},
        "deliverables": payload.get("deliverables") or {},
        "requirements": payload.get("requirements") or {},
        "updatedAt": _utc_now(),
    }
    with _LOCK:
        _write_json(_memory_path(chat_id), body)


def _extract_artifacts(text: str) -> List[str]:
    found: List[str] = []
    seen: set[str] = set()
    for match in _ARTIFACT_RE.finditer(text or ""):
        path = match.group(0).strip().strip("'").strip('"')
        if path and path not in seen:
            seen.add(path)
            found.append(path)
    for match in _RUN_RE.finditer(text or ""):
        token = match.group(0)
        if token.startswith(("SRR", "ERR", "DRR")):
            candidate = f"srna_fastq/{token}.fastq.gz"
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
    return found


def _extract_facts(text: str) -> List[str]:
    found: List[str] = []
    seen: set[str] = set()
    raw = str(text or "")
    if not raw:
        return found
    for pattern in _FACT_PATTERNS:
        for match in pattern.finditer(raw):
            fact = " ".join(part.strip() for part in match.groups() if part and str(part).strip())
            fact = re.sub(r"\s+", " ", fact).strip(" :：-")
            if fact and fact not in seen:
                seen.add(fact)
                found.append(fact[:180])
    return found


def _append_step(chat_id: str, summary: str, *, tool: str = "", detail: str = "") -> None:
    summary = str(summary or "").strip()
    if not summary:
        return
    with _LOCK:
        memory = load_session_memory(chat_id)
        steps: List[Dict[str, str]] = list(memory.get("steps") or [])
        if steps and steps[-1].get("summary") == summary:
            return
        steps.append(
            {
                "tool": str(tool or ""),
                "summary": summary[:500],
                "detail": str(detail or "")[:800],
                "at": _utc_now(),
            }
        )
        memory["steps"] = steps[-_MAX_STEPS:]
        artifacts = list(memory.get("artifacts") or [])
        for item in _extract_artifacts(f"{summary}\n{detail}"):
            if item not in artifacts:
                artifacts.append(item)
        memory["artifacts"] = artifacts[-_MAX_ARTIFACTS:]
        facts = list(memory.get("facts") or [])
        for item in _extract_facts(f"{summary}\n{detail}"):
            if item not in facts:
                facts.append(item)
        memory["facts"] = facts[-_MAX_FACTS:]
        save_session_memory(chat_id, memory)


def _save_analysis_memory(chat_id: str, analysis: Dict[str, Any]) -> None:
    if not chat_id or not isinstance(analysis, dict) or not analysis:
        return
    with _LOCK:
        memory = load_session_memory(chat_id)
        current = dict(memory.get("analysis") or {})
        merged = dict(current)
        for key in ("design", "source", "reason"):
            value = str(analysis.get(key) or "").strip()
            if value:
                merged[key] = value
        if analysis.get("paired_feasible") is not None:
            merged["paired_feasible"] = bool(analysis.get("paired_feasible"))
        modalities = [
            str(item).strip()
            for item in (analysis.get("modalities") or [])
            if str(item).strip()
        ]
        if modalities:
            merged["modalities"] = modalities
        if merged != current:
            memory["analysis"] = merged
            save_session_memory(chat_id, memory)


def _save_deliverables_memory(chat_id: str, deliverables: Dict[str, Any]) -> None:
    if not chat_id or not isinstance(deliverables, dict) or not deliverables:
        return
    with _LOCK:
        memory = load_session_memory(chat_id)
        current = dict(memory.get("deliverables") or {})
        merged = dict(current)
        if deliverables.get("html_report_requested") is not None:
            merged["html_report_requested"] = bool(deliverables.get("html_report_requested"))
        if deliverables.get("has_report_step") is not None:
            merged["has_report_step"] = bool(deliverables.get("has_report_step"))
        if merged != current:
            memory["deliverables"] = merged
            save_session_memory(chat_id, memory)


def _save_requirements_memory(chat_id: str, requirements: Dict[str, Any]) -> None:
    if not chat_id or not isinstance(requirements, dict) or not requirements:
        return
    with _LOCK:
        memory = load_session_memory(chat_id)
        current = dict(memory.get("requirements") or {})
        merged = dict(current)
        for key in (
            "html_report_requested",
            "mudata_required",
            "whole_genome_bam_required",
            "default_unpaired",
        ):
            if requirements.get(key) is not None:
                merged[key] = bool(requirements.get(key))
        items = [
            str(item).strip()
            for item in (requirements.get("items") or [])
            if str(item).strip()
        ]
        if items:
            merged["items"] = items[:10]
        if merged != current:
            memory["requirements"] = merged
            save_session_memory(chat_id, memory)


def _compact_execute_code_detail(detail: str) -> str:
    lines: List[str] = []
    seen: set[str] = set()
    for raw in str(detail or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        keep = False
        if _ARTIFACT_RE.search(line):
            keep = True
        elif any(pattern.search(line) for pattern in _FACT_PATTERNS):
            keep = True
        elif line.lower().startswith(("result:", "output:", "saved:", "wrote ", "written ")):
            keep = True
        if not keep:
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
        if len(lines) >= 6:
            break
    return "\n".join(lines)[:240]


def _normalise_requirement_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip(" ，,。；;：:-")[:180]


def _extract_requirement_items_from_query(text: str) -> List[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    candidates = re.split(r"[\n。；;]", raw)
    found: List[str] = []
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
            found.append(value)
    return found[:10]


def _extract_requirement_flags_from_query(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    return {
        "html_report_requested": bool(_HTML_REPORT_RE.search(raw)),
        "mudata_required": bool(_MUDATA_RE.search(raw)),
        "whole_genome_bam_required": bool(_WHOLE_GENOME_BAM_RE.search(raw)),
        "default_unpaired": bool(_UNPAIRED_RE.search(raw)) or (not bool(_PAIRED_RE.search(raw)) and "差异分析" in raw),
    }


def remember_user_query(chat_id: str, user_query: str) -> None:
    query = str(user_query or "").strip()
    if not chat_id or not query:
        return
    with _LOCK:
        memory = load_session_memory(chat_id)
        changed = False

        facts = list(memory.get("facts") or [])
        for item in _extract_facts(query):
            if item not in facts:
                facts.append(item)
                changed = True
        if changed:
            memory["facts"] = facts[-_MAX_FACTS:]

        artifacts = list(memory.get("artifacts") or [])
        for item in _extract_artifacts(query):
            if item not in artifacts:
                artifacts.append(item)
                changed = True
        if changed:
            memory["artifacts"] = artifacts[-_MAX_ARTIFACTS:]

        current_requirements = dict(memory.get("requirements") or {})
        next_requirements = dict(current_requirements)
        flags = _extract_requirement_flags_from_query(query)
        for key, value in flags.items():
            if value:
                next_requirements[key] = True
        items = list(current_requirements.get("items") or [])
        seen_items = {str(item).strip() for item in items if str(item).strip()}
        for item in _extract_requirement_items_from_query(query):
            if item not in seen_items:
                items.append(item)
                seen_items.add(item)
        if items:
            next_requirements["items"] = items[:10]
        if next_requirements != current_requirements:
            memory["requirements"] = next_requirements
            changed = True

        if changed:
            save_session_memory(chat_id, memory)




def record_stream_event(chat_id: str, event: Dict[str, Any]) -> None:
    if not chat_id or not event:
        return
    event_type = str(event.get("type") or "")
    if event_type == "tool_call":
        name = str(event.get("name") or "")
        if name and name != "finish":
            _append_step(chat_id, str(event.get("summary") or name), tool=name)
        return
    if event_type == "tool_result":
        name = str(event.get("name") or "")
        summary = str(event.get("summary") or name or "tool_result")
        detail = str(event.get("content") or "")
        if name == "execute_code":
            compact = _compact_execute_code_detail(detail)
            if compact:
                summary = f"{summary}（已压缩执行输出）"
            detail = compact
        _append_step(chat_id, summary, tool=name, detail=detail)
        return
    if event_type == "final":
        content = str(event.get("content") or "").strip()
        if content:
            _append_step(chat_id, f"结论: {content[:240]}", tool="finish", detail=content)
        return
    if event_type == "done":
        text = str(event.get("text") or "").strip()
        if text:
            _append_step(chat_id, f"结论: {text[:240]}", tool="finish", detail=text)
        return
    if event_type in ("plan_created", "plan_revised", "plan_complete"):
        message = str(event.get("message") or event_type).strip()
        plan_payload = event.get("plan") if isinstance(event.get("plan"), dict) else {}
        analysis = plan_payload.get("analysis") if isinstance(plan_payload.get("analysis"), dict) else {}
        deliverables = plan_payload.get("deliverables") if isinstance(plan_payload.get("deliverables"), dict) else {}
        requirements = plan_payload.get("requirements") if isinstance(plan_payload.get("requirements"), dict) else {}
        if analysis:
            _save_analysis_memory(chat_id, analysis)
        if deliverables:
            _save_deliverables_memory(chat_id, deliverables)
        if requirements:
            _save_requirements_memory(chat_id, requirements)
        if message:
            _append_step(chat_id, message, tool="plan", detail=json.dumps(plan_payload or {}, ensure_ascii=False)[:800])
        return
    if event_type in ("plan_step_start", "plan_step_done", "plan_step_failed"):
        message = str(event.get("message") or event_type).strip()
        if message:
            _append_step(chat_id, message, tool="plan_step", detail=str(event.get("result") or "")[:800])


def _format_bytes(value: int) -> str:
    size = float(max(value, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def build_workspace_manifest(*, max_files: int = 36) -> str:
    root = get_work_space()
    if not root.is_dir():
        return ""

    entries: List[tuple[str, int]] = []
    seen: set[str] = set()

    def add_file(path: Path) -> None:
        rel = str(path.relative_to(root))
        if rel.startswith("sessions/") or "/sessions/" in rel:
            return
        if rel in seen or not path.is_file():
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        seen.add(rel)
        entries.append((rel, size))

    for dirname in _IMPORTANT_DIRS:
        base = root / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                add_file(path)

    for pattern in (
        "**/fastq-run-info.tsv",
        "**/*run-info*.tsv",
        "**/*.fa.gz",
        "**/*.h5ad",
        "**/*.html",
        "**/*.pdf",
        "**/*.png",
        "**/*.svg",
        "**/*.json",
        "**/*.ipynb",
        "**/*.xlsx",
        "**/*counts*.csv",
        "**/*de_results*.csv",
    ):
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                add_file(path)

    entries.sort(key=lambda item: item[0].lower())
    if not entries:
        return ""

    lines = []
    for rel, size in entries[:max_files]:
        lines.append(f"- {rel} ({_format_bytes(size)})")
    if len(entries) > max_files:
        lines.append(f"- … 另有 {len(entries) - max_files} 个文件")
    return "\n".join(lines)


def _format_active_plan_context(chat_id: str) -> str:
    plan = load_plan(chat_id)
    if not isinstance(plan, dict):
        return ""
    steps_raw = plan.get("steps") or []
    steps = [step for step in steps_raw if isinstance(step, dict)]
    if not steps:
        return ""

    active_steps = []
    done_count = 0
    for step in steps:
        status = str(step.get("status") or "pending").strip().lower()
        if status == "done":
            done_count += 1
        else:
            active_steps.append(step)

    # Completed plans do not need to dominate the resume context.
    if not active_steps:
        return ""

    goal = str(plan.get("goal") or "").strip()
    lines = ["### 当前未完成计划"]
    if goal:
        lines.append(f"- 目标：{goal}")
    lines.append(f"- 进度：{plan_progress_summary(plan)}")

    running = [
        step for step in active_steps
        if str(step.get("status") or "").strip().lower() == "running"
    ]
    pending = [
        step for step in active_steps
        if str(step.get("status") or "").strip().lower() == "pending"
    ]
    failed = [
        step for step in active_steps
        if str(step.get("status") or "").strip().lower() == "failed"
    ]

    def _append_steps(label: str, items: List[Dict[str, Any]], *, limit: int) -> None:
        if not items:
            return
        lines.append(f"- {label}：")
        for step in items[:limit]:
            title = str(step.get("title") or step.get("goal") or step.get("id") or "未命名步骤").strip()
            goal_text = str(step.get("goal") or "").strip()
            skill = str(step.get("skill") or "").strip()
            extra = goal_text if goal_text and goal_text != title else ""
            if skill:
                extra = f"{extra}；skill={skill}" if extra else f"skill={skill}"
            if extra:
                lines.append(f"  - {title}：{extra[:220]}")
            else:
                lines.append(f"  - {title}")

    _append_steps("当前 running", running, limit=2)
    _append_steps("后续 pending", pending, limit=4)
    _append_steps("待处理 failed", failed, limit=2)

    if done_count:
        lines.append(f"- 已完成步骤数：{done_count}/{len(steps)}")
    return "\n".join(lines)


def build_session_memory_context(chat_id: str, *, user_query: str = "") -> str:
    if not chat_id:
        return ""

    memory = load_session_memory(chat_id)
    steps = memory.get("steps") or []
    artifacts = memory.get("artifacts") or []
    facts = memory.get("facts") or []
    analysis = memory.get("analysis") or {}
    deliverables = memory.get("deliverables") or {}
    requirements = memory.get("requirements") or {}
    manifest = build_workspace_manifest()
    errors_context = build_session_errors_context(chat_id)
    plan = load_plan(chat_id)
    if not analysis and isinstance(plan, dict) and isinstance(plan.get("analysis"), dict):
        analysis = dict(plan.get("analysis") or {})
    if not deliverables and isinstance(plan, dict) and isinstance(plan.get("deliverables"), dict):
        deliverables = dict(plan.get("deliverables") or {})
    if not requirements and isinstance(plan, dict) and isinstance(plan.get("requirements"), dict):
        requirements = dict(plan.get("requirements") or {})
    active_plan_context = _format_active_plan_context(chat_id)

    if not steps and not artifacts and not facts and not analysis and not deliverables and not requirements and not manifest and not errors_context and not active_plan_context:
        return ""

    lines = [
        "## Session Context（Cursor-style memory）",
        "以下是本会话的已知进度与产物。请在此基础上继续，不要重复已完成步骤或重复下载已有文件。",
    ]

    if errors_context:
        lines.append("")
        lines.append(errors_context)

    if analysis:
        lines.append("")
        lines.append("### 高优先级分析设计")
        design = str(analysis.get("design") or "").strip()
        if design:
            lines.append(f"- analysis.design = {design}")
        modalities = analysis.get("modalities") or []
        if modalities:
            joined = ", ".join(str(item) for item in modalities if str(item).strip())
            if joined:
                lines.append(f"- analysis.modalities = [{joined}]")
        if analysis.get("paired_feasible") is not None:
            lines.append(f"- analysis.paired_feasible = {str(bool(analysis.get('paired_feasible'))).lower()}")
        source = str(analysis.get("source") or "").strip()
        if source:
            lines.append(f"- analysis.source = {source}")
        reason = str(analysis.get("reason") or "").strip()
        if reason:
            lines.append(f"- analysis.reason = {reason}")

    if deliverables:
        lines.append("")
        lines.append("### 高优先级交付要求")
        if deliverables.get("html_report_requested") is not None:
            lines.append(
                f"- deliverables.html_report_requested = {str(bool(deliverables.get('html_report_requested'))).lower()}"
            )
        if deliverables.get("has_report_step") is not None:
            lines.append(
                f"- deliverables.has_report_step = {str(bool(deliverables.get('has_report_step'))).lower()}"
            )

    if requirements:
        lines.append("")
        lines.append("### 高优先级用户要求")
        for key in (
            "default_unpaired",
            "html_report_requested",
            "mudata_required",
            "whole_genome_bam_required",
        ):
            if requirements.get(key) is not None:
                lines.append(f"- requirements.{key} = {str(bool(requirements.get(key))).lower()}")
        for item in requirements.get("items") or []:
            text = str(item).strip()
            if text:
                lines.append(f"- requirement: {text}")

    if active_plan_context:
        lines.append("")
        lines.append(active_plan_context)

    if steps:
        lines.append("")
        lines.append("### 已完成步骤")
        for step in steps[-15:]:
            summary = str(step.get("summary") or "").strip()
            if summary:
                lines.append(f"- {summary}")

    if facts:
        lines.append("")
        lines.append("### 已确认的关键事实")
        for item in facts[-12:]:
            lines.append(f"- {item}")

    if artifacts:
        lines.append("")
        lines.append("### 已知产物路径")
        for item in artifacts[-20:]:
            lines.append(f"- {item}")

    if manifest:
        lines.append("")
        lines.append("### 工作区关键文件")
        lines.append(manifest)

    work_log_text = _format_work_log(read_work_log(chat_id, limit=15))
    if work_log_text:
        lines.append("")
        lines.append("### 最近工作日志（重启服务后 agent 会从这里读）")
        lines.append(work_log_text)

    if user_query:
        lines.append("")
        lines.append("### 用户当前意图（最新一条 user 原文，记住后不要再问）")
        lines.append(f"> {str(user_query).strip()[:400]}")

    return "\n".join(lines).strip()


def _work_log_path(chat_id: str) -> Path:
    chat_id = sanitize_chat_id(chat_id)
    return ensure_session_dir(chat_id) / _WORK_LOG_FILE


def append_work_log(
    chat_id: str,
    *,
    kind: str,
    label: str = "",
    paths: Optional[List[str]] = None,
    note: str = "",
    run_id: str = "",
) -> None:
    """Append a structured event to the per-chat work log.

    The log is JSONL (`{chat_id}/work_log.jsonl`) and is read back into the
    agent's system prompt on the next run via ``build_session_memory_context``,
    so after a service restart the next agent invocation can see exactly what
    was done in previous runs and pick up without re-asking.
    """
    if not chat_id:
        return
    entry = {
        "at": _utc_now(),
        "kind": str(kind or "event"),
        "label": str(label or "")[:240],
        "paths": [str(p) for p in (paths or [])][:10],
        "note": str(note or "")[:400],
        "runId": str(run_id or ""),
    }
    path = _work_log_path(chat_id)
    with _LOCK:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _trim_work_log(path)
        except OSError:
            pass


def read_work_log(chat_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    if not chat_id:
        return []
    path = _work_log_path(chat_id)
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries[-limit:]


def _trim_work_log(path: Path) -> None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        if len(lines) <= _MAX_WORK_LOG_LINES:
            return
        kept = lines[-_MAX_WORK_LOG_LINES:]
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(kept)
    except OSError:
        pass


def _format_work_log(entries: List[Dict[str, Any]]) -> str:
    if not entries:
        return ""
    lines = []
    for entry in entries:
        ts = str(entry.get("at") or "")
        kind = str(entry.get("kind") or "event")
        label = str(entry.get("label") or "").strip()
        paths = entry.get("paths") or []
        if ts and len(ts) > 19:
            ts = ts[:19]
        bits = [f"- {ts} {kind}"]
        if label:
            bits.append(label)
        line = " — ".join(bits)
        if paths:
            line += " | " + ", ".join(str(p) for p in paths[:3])
        lines.append(line)
    return "\n".join(lines)
