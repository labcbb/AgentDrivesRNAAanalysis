"""Supervisor agent — readonly monitor for approvals, branch Q&A, and run reports."""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from session_errors import load_session_errors
from session_memory import load_session_memory
from session_plan import load_plan, plan_progress_summary
from session_store import _read_json, _write_json, ensure_session_dir, load_kernel_state, sanitize_chat_id
from run_ledger import load_run_ledger, summarize_ledger_for_prompt

logger = logging.getLogger(__name__)

_REPORT_FILE = "run_report.json"
_SUPERVISOR_META = "supervisor_meta.json"
_ASSESS_TIMEOUT_HINT_SEC = 25
_FORCE_ESCALATE_PATTERNS = (
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\bshutil\.rmtree\b", re.I),
    re.compile(r"\bos\.remove\b", re.I),
    re.compile(r"\bos\.unlink\b", re.I),
    re.compile(r"\bpathlib\.Path\([^)]*\)\.unlink\b", re.I),
    re.compile(r"\bDROP\s+(TABLE|DATABASE)\b", re.I),
    re.compile(r"\bDELETE\s+FROM\b", re.I),
    re.compile(r"/etc/|/usr/bin|/boot\b", re.I),
    re.compile(r"\bsubprocess\.(?:call|run|Popen)\b[^\n]*shell\s*=\s*True", re.I),
    re.compile(r"\beval\s*\(", re.I),
    re.compile(r"\bexec\s*\(", re.I),
    re.compile(r"\b__import__\s*\(\s*['\"]os['\"]", re.I),
    re.compile(r"curl\s+[^\n]*\|\s*(?:ba)?sh", re.I),
    re.compile(r"wget\s+[^\n]*\|\s*(?:ba)?sh", re.I),
)
_FORCE_DENY_PATTERNS = (
    re.compile(r"rm\s+-rf\s+/\s*$", re.I | re.M),
    re.compile(r"rm\s+-rf\s+/(\s|$)", re.I),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;?", re.I),  # fork bomb
)

_SYSTEM_RISK = """你是 sRNAgent 的监管者（Supervisor）。只做代码风险审查，不要执行代码。
根据将要在 Jupyter 中执行的代码，输出严格 JSON（不要 markdown 围栏）：
{
  "level": "low|medium|high|critical",
  "action": "allow|escalate|deny",
  "reason": "一句话中文理由"
}
规则：
- allow：普通分析（读文件、pandas、绘图、既有 sRNAgent API、写工作区内结果）
- escalate：可能删改重要数据、外网下载、安装包、改权限、大范围写盘 → 交给人工
- deny：明显破坏性命令（如 rm -rf /、fork bomb）
宁严勿松。仅输出 JSON。"""

_SYSTEM_CHAT = """你是 sRNAgent 监管者。你只读运行账本/计划/内存/错误/内核变量摘要，回答用户关于当前任务进度与产物的问题。
禁止声称你执行了代码或修改了环境。用简洁中文回答，必要时列出步骤与路径。"""

_SYSTEM_REPORT = """你是 sRNAgent 监管者。根据提供的运行证据生成结构化运行报告 JSON（不要 markdown 围栏）：
{
  "summary": "一段中文总结",
  "steps": [{"title": "", "status": "done|failed|running|skipped", "detail": "", "tools": []}],
  "errors": [{"summary": "", "detail": "", "possibleCause": "", "agentFix": ""}],
  "artifacts": ["path"],
  "approvals": [{"action": "", "reason": ""}],
  "notes": []
}
只依据证据，不要编造。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _report_path(chat_id: str):
    return ensure_session_dir(chat_id) / _REPORT_FILE


def _supervisor_meta_path(chat_id: str):
    return ensure_session_dir(chat_id) / _SUPERVISOR_META


def _empty_report_doc(chat_id: str) -> Dict[str, Any]:
    return {
        "chatId": chat_id,
        "updatedAt": _utc_now(),
        "tasks": [],
    }


def _normalize_report_doc(chat_id: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize legacy single-report files into a task log."""
    if not isinstance(payload, dict):
        return _empty_report_doc(chat_id)
    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        doc = dict(payload)
        doc["chatId"] = chat_id
        doc["tasks"] = [t for t in tasks if isinstance(t, dict)]
        return doc
    # Legacy flat report → one historical task
    task = {
        "taskId": str(payload.get("taskId") or payload.get("runId") or "legacy"),
        "taskIndex": 1,
        "taskLabel": "历史任务",
        "createdAt": payload.get("updatedAt") or _utc_now(),
        "summary": payload.get("summary") or "",
        "steps": payload.get("steps") if isinstance(payload.get("steps"), list) else [],
        "errors": payload.get("errors") if isinstance(payload.get("errors"), list) else [],
        "artifacts": payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else [],
        "approvals": payload.get("approvals") if isinstance(payload.get("approvals"), list) else [],
        "notes": payload.get("notes") if isinstance(payload.get("notes"), list) else [],
        "source": payload.get("source") or "legacy",
    }
    return {
        "chatId": chat_id,
        "updatedAt": payload.get("updatedAt") or _utc_now(),
        "tasks": [task],
    }


def load_run_report(chat_id: str) -> Optional[Dict[str, Any]]:
    try:
        chat_id = sanitize_chat_id(chat_id)
    except Exception:
        return None
    payload = _read_json(_report_path(chat_id))
    if not isinstance(payload, dict):
        return None
    doc = _normalize_report_doc(chat_id, payload)
    if not doc.get("tasks"):
        return None
    return doc


def save_run_report(chat_id: str, report: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = sanitize_chat_id(chat_id)
    doc = _normalize_report_doc(chat_id, report if isinstance(report, dict) else {})
    doc["chatId"] = chat_id
    doc["updatedAt"] = _utc_now()
    _write_json(_report_path(chat_id), doc)
    return doc


def clear_run_report(chat_id: str) -> Dict[str, Any]:
    """Delete stored report content for this chat."""
    chat_id = sanitize_chat_id(chat_id)
    path = _report_path(chat_id)
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning("clear_run_report unlink failed: %s", exc)
    return {"ok": True, "chatId": chat_id, "cleared": True}


def append_task_report(chat_id: str, task: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = sanitize_chat_id(chat_id)
    existing = _read_json(_report_path(chat_id))
    doc = _normalize_report_doc(chat_id, existing if isinstance(existing, dict) else None)
    tasks = list(doc.get("tasks") or [])
    task_id = str(task.get("taskId") or "").strip()
    # Same run regenerating → replace that entry instead of duplicating.
    if task_id:
        tasks = [t for t in tasks if str(t.get("taskId") or "") != task_id]
    task_index = len(tasks) + 1
    entry = dict(task)
    entry["taskId"] = task_id or f"task-{task_index}"
    entry["taskIndex"] = task_index
    if not entry.get("taskLabel"):
        entry["taskLabel"] = f"任务 {task_index}"
    entry["createdAt"] = entry.get("createdAt") or _utc_now()
    tasks.append(entry)
    # Re-number after possible replace/filter
    for i, item in enumerate(tasks, start=1):
        item["taskIndex"] = i
        if not str(item.get("taskLabel") or "").strip() or str(item.get("taskLabel")).startswith("任务 "):
            short = str(item.get("taskId") or "")[:8]
            item["taskLabel"] = f"任务 {i}" + (f" · {short}" if short else "")
    doc["tasks"] = tasks
    return save_run_report(chat_id, doc)

def load_supervisor_meta(chat_id: str) -> Dict[str, Any]:
    try:
        chat_id = sanitize_chat_id(chat_id)
    except Exception:
        return {}
    payload = _read_json(_supervisor_meta_path(chat_id))
    return payload if isinstance(payload, dict) else {}


def save_supervisor_meta(chat_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = sanitize_chat_id(chat_id)
    body = dict(meta)
    body["chatId"] = chat_id
    body["role"] = "supervisor"
    body["updatedAt"] = _utc_now()
    _write_json(_supervisor_meta_path(chat_id), body)
    return body


def hard_rule_assess(code: str) -> Optional[Dict[str, Any]]:
    text = str(code or "")
    if not text.strip():
        return {"level": "low", "action": "allow", "reason": "空代码", "source": "rule"}
    for pattern in _FORCE_DENY_PATTERNS:
        if pattern.search(text):
            return {
                "level": "critical",
                "action": "deny",
                "reason": f"命中强制拒绝规则：{pattern.pattern}",
                "source": "rule",
            }
    for pattern in _FORCE_ESCALATE_PATTERNS:
        if pattern.search(text):
            return {
                "level": "high",
                "action": "escalate",
                "reason": f"命中强制人工规则：{pattern.pattern}",
                "source": "rule",
            }
    return None


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        return None


def _build_client(body: Dict[str, Any]):
    from sRNAgent.agent.llm_client import ChatClient, LLMConfig

    account = body.get("account") or {}
    vendor = body.get("vendor") or {}
    agent_cfg = body.get("agent") or {}
    config = LLMConfig.from_ui_payload(account, vendor, agent_cfg)
    # Keep supervisor cheap/fast.
    config.max_tokens = min(int(config.max_tokens or 1024), 1024)
    config.temperature = min(float(config.temperature or 0.2), 0.2)
    return ChatClient(config)


def assess_code_risk(
    code: str,
    *,
    description: str = "",
    llm_body: Optional[Dict[str, Any]] = None,
    chat_id: str = "",
) -> Dict[str, Any]:
    """Return {level, action, reason, source}."""
    ruled = hard_rule_assess(code)
    if ruled is not None:
        return ruled

    if not llm_body:
        return {
            "level": "medium",
            "action": "escalate",
            "reason": "无 LLM 配置，降级为人工审批",
            "source": "fallback",
        }

    context_bits = []
    if description:
        context_bits.append(f"描述：{description}")
    if chat_id:
        plan = load_plan(chat_id)
        if plan:
            context_bits.append(f"计划进度：{plan_progress_summary(plan)}")
        context_bits.append(summarize_ledger_for_prompt(chat_id, max_events=12))

    user_prompt = (
        "\n".join(context_bits)
        + "\n\n即将执行的代码：\n```python\n"
        + str(code)[:4000]
        + "\n```"
    )

    result_box: Dict[str, Any] = {}
    error_box: Dict[str, str] = {}

    def _worker() -> None:
        try:
            client = _build_client(llm_body)
            completion = client.complete(
                [
                    {"role": "system", "content": _SYSTEM_RISK},
                    {"role": "user", "content": user_prompt},
                ],
                tools=None,
                enable_thinking=False,
            )
            parsed = _parse_json_object(completion.content)
            if not parsed:
                error_box["error"] = "监管者返回非 JSON"
                return
            action = str(parsed.get("action") or "escalate").strip().lower()
            if action not in {"allow", "escalate", "deny"}:
                action = "escalate"
            level = str(parsed.get("level") or "medium").strip().lower()
            if level not in {"low", "medium", "high", "critical"}:
                level = "medium"
            result_box.update(
                {
                    "level": level,
                    "action": action,
                    "reason": str(parsed.get("reason") or "监管者评估完成"),
                    "source": "supervisor",
                }
            )
        except Exception as exc:  # noqa: BLE001
            error_box["error"] = str(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=_ASSESS_TIMEOUT_HINT_SEC)
    if thread.is_alive():
        return {
            "level": "medium",
            "action": "escalate",
            "reason": "监管者评估超时，降级为人工审批",
            "source": "timeout",
        }
    if error_box:
        return {
            "level": "medium",
            "action": "escalate",
            "reason": f"监管者评估失败：{error_box['error']}",
            "source": "error",
        }
    if result_box:
        return result_box
    return {
        "level": "medium",
        "action": "escalate",
        "reason": "监管者无结果，降级为人工审批",
        "source": "fallback",
    }


def build_supervisor_context(chat_id: str) -> str:
    chat_id = sanitize_chat_id(chat_id)
    plan = load_plan(chat_id)
    memory = load_session_memory(chat_id)
    errors = load_session_errors(chat_id)
    kernel = load_kernel_state(chat_id) or {}
    ledger = summarize_ledger_for_prompt(chat_id, max_events=50)
    parts = [
        f"chatId: {chat_id}",
        f"计划：{plan_progress_summary(plan) if plan else '无'}",
        ledger,
    ]
    if plan and isinstance(plan.get("steps"), list):
        parts.append("计划步骤：")
        for step in plan["steps"]:
            parts.append(
                f"- [{step.get('status')}] {step.get('title') or step.get('id')}: "
                f"{str(step.get('result') or '')[:200]}"
            )
    arts = memory.get("artifacts") or []
    if arts:
        parts.append("产物：\n- " + "\n- ".join(str(a) for a in arts[:40]))
    err_events = errors.get("events") if isinstance(errors, dict) else None
    if isinstance(err_events, list) and err_events:
        parts.append("错误记录：")
        for item in err_events[-12:]:
            parts.append(f"- {item.get('kind')}: {item.get('summary')}")
    variables = kernel.get("variables") if isinstance(kernel.get("variables"), list) else []
    if variables:
        parts.append("内核变量摘要：")
        for item in variables[:30]:
            if isinstance(item, dict):
                parts.append(f"- {item.get('name')}: {item.get('type')} · {str(item.get('preview') or '')[:80]}")
    return "\n".join(parts)


def answer_supervisor_question(
    chat_id: str,
    question: str,
    *,
    llm_body: Dict[str, Any],
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    context = build_supervisor_context(chat_id)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_CHAT + "\n\n## 当前只读证据\n" + context},
    ]
    for item in history or []:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": str(question or "").strip() or "当前任务进展如何？"})
    client = _build_client(llm_body)
    completion = client.complete(messages, tools=None, enable_thinking=False)
    return str(completion.content or "").strip() or "（监管者无回复）"


def stream_supervisor_chat(body: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    chat_id = str(body.get("chatId") or body.get("parentChatId") or "").strip()
    question = ""
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    history: List[Dict[str, str]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            history.append({"role": role, "content": content})
    if history and history[-1]["role"] == "user":
        question = history[-1]["content"]
        history = history[:-1]
    else:
        question = str(body.get("question") or "").strip()

    if not chat_id:
        yield {"type": "error", "message": "chatId 不能为空"}
        return
    if not question:
        yield {"type": "error", "message": "问题不能为空"}
        return

    try:
        sanitize_chat_id(chat_id)
    except Exception as exc:
        yield {"type": "error", "message": str(exc)}
        return

    yield {"type": "status", "message": "监管者正在查阅运行账本…"}
    try:
        text = answer_supervisor_question(chat_id, question, llm_body=body, history=history)
        save_supervisor_meta(
            chat_id,
            {
                "parentChatId": chat_id,
                "lastQuestion": question[:500],
                "lastAnswerPreview": text[:500],
            },
        )
        yield {"type": "final", "content": text}
        yield {"type": "done", "text": text, "meta": {"backend": "supervisor"}}
    except Exception as exc:  # noqa: BLE001
        logger.exception("supervisor chat failed")
        yield {"type": "error", "message": str(exc)}


def _heuristic_report(chat_id: str) -> Dict[str, Any]:
    plan = load_plan(chat_id)
    memory = load_session_memory(chat_id)
    errors = load_session_errors(chat_id)
    ledger = load_run_ledger(chat_id)
    steps_out: List[Dict[str, Any]] = []
    if plan and isinstance(plan.get("steps"), list):
        for step in plan["steps"]:
            steps_out.append(
                {
                    "title": step.get("title") or step.get("id"),
                    "status": step.get("status") or "unknown",
                    "detail": str(step.get("result") or "")[:800],
                    "tools": [],
                }
            )
    else:
        for event in ledger.get("events") or []:
            if event.get("type") in {"tool_call", "plan_step_done", "plan_step_failed"}:
                steps_out.append(
                    {
                        "title": event.get("summary") or event.get("name") or event.get("type"),
                        "status": "failed" if "fail" in str(event.get("type")) else "done",
                        "detail": str(event.get("detailPreview") or event.get("message") or "")[:500],
                        "tools": [event.get("name")] if event.get("name") else [],
                    }
                )
    err_out = []
    for item in (errors.get("events") or [])[-20:]:
        err_out.append(
            {
                "summary": item.get("summary") or item.get("kind"),
                "detail": str(item.get("detail") or "")[:800],
                "possibleCause": "",
                "agentFix": "",
            }
        )
    approvals = []
    for event in ledger.get("events") or []:
        if event.get("type") in {"supervisor_approval", "approval_decision", "code_approval_required"}:
            approvals.append(
                {
                    "action": event.get("action") or event.get("type"),
                    "reason": event.get("reason") or event.get("message") or "",
                }
            )
    summary = plan_progress_summary(plan) if plan else f"记录了 {len(ledger.get('events') or [])} 条运行事件"
    return {
        "summary": summary,
        "steps": steps_out[-40:],
        "errors": err_out,
        "artifacts": list(memory.get("artifacts") or [])[:64],
        "approvals": approvals[-40:],
        "notes": ["本报告由启发式汇总生成（监管者 LLM 不可用或失败时）"],
        "source": "heuristic",
    }


def generate_run_report(
    chat_id: str,
    *,
    llm_body: Optional[Dict[str, Any]] = None,
    final_text: str = "",
    run_id: str = "",
) -> Dict[str, Any]:
    chat_id = sanitize_chat_id(chat_id)
    task_id = str(run_id or "").strip()
    evidence = build_supervisor_context(chat_id)
    if final_text:
        evidence += f"\n\n最终回复摘要：\n{final_text[:2000]}"
    if task_id:
        evidence = f"当前任务 runId={task_id}\n\n" + evidence

    def _as_task(report: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "taskId": task_id or f"task-{_utc_now()}",
            "summary": str(report.get("summary") or ""),
            "steps": report.get("steps") if isinstance(report.get("steps"), list) else [],
            "errors": report.get("errors") if isinstance(report.get("errors"), list) else [],
            "artifacts": report.get("artifacts") if isinstance(report.get("artifacts"), list) else [],
            "approvals": report.get("approvals") if isinstance(report.get("approvals"), list) else [],
            "notes": report.get("notes") if isinstance(report.get("notes"), list) else [],
            "source": report.get("source") or "unknown",
            "finalTextPreview": (final_text or "")[:500],
        }

    if not llm_body:
        return append_task_report(chat_id, _as_task(_heuristic_report(chat_id)))

    try:
        client = _build_client(llm_body)
        completion = client.complete(
            [
                {"role": "system", "content": _SYSTEM_REPORT},
                {"role": "user", "content": evidence[:14000]},
            ],
            tools=None,
            enable_thinking=False,
        )
        parsed = _parse_json_object(completion.content)
        if not parsed:
            report = _heuristic_report(chat_id)
            report["notes"] = list(report.get("notes") or []) + ["监管者 JSON 解析失败，已回退启发式报告"]
            return append_task_report(chat_id, _as_task(report))
        report = {
            "summary": str(parsed.get("summary") or ""),
            "steps": parsed.get("steps") if isinstance(parsed.get("steps"), list) else [],
            "errors": parsed.get("errors") if isinstance(parsed.get("errors"), list) else [],
            "artifacts": parsed.get("artifacts") if isinstance(parsed.get("artifacts"), list) else [],
            "approvals": parsed.get("approvals") if isinstance(parsed.get("approvals"), list) else [],
            "notes": parsed.get("notes") if isinstance(parsed.get("notes"), list) else [],
            "source": "supervisor",
        }
        return append_task_report(chat_id, _as_task(report))
    except Exception as exc:  # noqa: BLE001
        logger.warning("generate_run_report failed: %s", exc)
        report = _heuristic_report(chat_id)
        report["notes"] = list(report.get("notes") or []) + [f"监管者报告失败：{exc}"]
        return append_task_report(chat_id, _as_task(report))


def _render_task_markdown(task: Dict[str, Any]) -> str:
    label = task.get("taskLabel") or f"任务 {task.get('taskIndex') or '?'}"
    task_id = str(task.get("taskId") or "")
    created = str(task.get("createdAt") or "")
    head = f"## {label}"
    meta_bits = []
    if task_id:
        meta_bits.append(f"taskId=`{task_id}`")
    if created:
        meta_bits.append(f"生成于 {created}")
    lines = [head, ""]
    if meta_bits:
        lines.append(" · ".join(meta_bits))
        lines.append("")
    lines.append(str(task.get("summary") or ""))
    lines.append("")
    steps = task.get("steps") if isinstance(task.get("steps"), list) else []
    if steps:
        lines.append("### 步骤")
        for step in steps:
            if not isinstance(step, dict):
                continue
            lines.append(f"- **[{step.get('status')}]** {step.get('title')}: {step.get('detail')}")
        lines.append("")
    errors = task.get("errors") if isinstance(task.get("errors"), list) else []
    if errors:
        lines.append("### 错误与修复")
        for err in errors:
            if not isinstance(err, dict):
                continue
            lines.append(f"- {err.get('summary')}")
            if err.get("detail"):
                lines.append(f"  - 详情：{err.get('detail')}")
            if err.get("possibleCause"):
                lines.append(f"  - 可能原因：{err.get('possibleCause')}")
            if err.get("agentFix"):
                lines.append(f"  - 主 Agent 修复：{err.get('agentFix')}")
        lines.append("")
    arts = task.get("artifacts") if isinstance(task.get("artifacts"), list) else []
    if arts:
        lines.append("### 产物")
        for path in arts:
            lines.append(f"- `{path}`")
        lines.append("")
    notes = task.get("notes") if isinstance(task.get("notes"), list) else []
    if notes:
        lines.append("### 备注")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines).strip()


def render_report_markdown(report: Dict[str, Any]) -> str:
    if not isinstance(report, dict):
        return ""
    tasks = report.get("tasks") if isinstance(report.get("tasks"), list) else None
    if tasks is None:
        # Legacy flat shape
        return _render_task_markdown(
            {
                "taskLabel": "运行报告",
                "taskId": report.get("runId") or "",
                "createdAt": report.get("updatedAt") or "",
                "summary": report.get("summary") or "",
                "steps": report.get("steps") or [],
                "errors": report.get("errors") or [],
                "artifacts": report.get("artifacts") or [],
                "notes": report.get("notes") or [],
            }
        )
    if not tasks:
        return "（报告已清空，完成下一次任务后会重新写入。）"
    lines = ["# 运行报告", "", f"共 {len(tasks)} 个任务报告（未清空前会持续追加）。", ""]
    for task in tasks:
        if not isinstance(task, dict):
            continue
        lines.append(_render_task_markdown(task))
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).strip().rstrip("-").strip()
