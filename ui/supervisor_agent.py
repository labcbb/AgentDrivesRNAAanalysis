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
from session_store import (
    _read_json,
    _write_json,
    ensure_session_dir,
    load_chat_record,
    load_kernel_state,
    sanitize_chat_id,
)
from run_ledger import load_run_ledger, summarize_ledger_for_prompt
from supervisor_skills import assess_skill_confirmation_gates
from work_space import get_work_space

logger = logging.getLogger(__name__)

_REPORT_FILE = "run_report.json"
_SUPERVISOR_META = "supervisor_meta.json"
_FORCE_DENY_PATTERNS = (
    re.compile(r"rm\s+-rf\s+/\s*$", re.I | re.M),
    re.compile(r"rm\s+-rf\s+/(\s|$)", re.I),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;?", re.I),  # fork bomb
)

_SYSTEM_CHAT = """你是 sRNAgent 监管者（旁路只读 Agent）。你综合下列只读证据回答用户关于主任务进度与产物的问题：
主会话对话、Thinking 步骤、执行计划状态、运行账本、会话产物/错误、内核变量摘要、工作区文件快照（含相对上次是否新增/增大）。

硬性规则：
- 禁止声称你执行了代码、修改了环境或与主 Agent 抢写内核。
- 回答进度时，以「计划步骤 status」和最近运行账本为准；若对话/thinking 与计划冲突，说明冲突并优先采信更新时间更近的计划/账本。
- 提到文件时给出相对工作区路径；若证据不足就直说不确定，不要编造。
- 用简洁中文回答。"""


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
    """Only hard-deny catastrophic patterns. No LLM / risk scoring."""
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
    """Fast approval gate: hard-deny + Skill confirmation only (no LLM risk review).

    Returns {level, action, reason, source}.
    """
    _ = description, llm_body, chat_id  # kept for call-site compatibility
    ruled = hard_rule_assess(code)
    if ruled is not None:
        return ruled

    gated = assess_skill_confirmation_gates(code)
    if gated is not None:
        return gated

    return {
        "level": "low",
        "action": "allow",
        "reason": "未触发 Skill 强制确认门槛，自动放行",
        "source": "skill_gate",
    }


def build_supervisor_context(chat_id: str) -> str:
    chat_id = sanitize_chat_id(chat_id)
    plan = load_plan(chat_id)
    memory = load_session_memory(chat_id)
    errors = load_session_errors(chat_id)
    kernel = load_kernel_state(chat_id) or {}
    meta = load_supervisor_meta(chat_id)
    prev_ws = meta.get("workspaceSnapshot") if isinstance(meta.get("workspaceSnapshot"), dict) else {}
    ledger = summarize_ledger_for_prompt(chat_id, max_events=60)
    parts = [
        f"chatId: {chat_id}",
        f"计划：{plan_progress_summary(plan) if plan else '无'}",
        ledger,
    ]
    if plan and isinstance(plan.get("steps"), list):
        parts.append("## 计划步骤（以 status 为准判断当前阶段）")
        running = []
        for idx, step in enumerate(plan["steps"], start=1):
            if not isinstance(step, dict):
                continue
            status = str(step.get("status") or "pending")
            title = step.get("title") or step.get("id") or f"step-{idx}"
            result = str(step.get("result") or "")[:240]
            line = f"- 步骤{idx} [{status}] {title}"
            if result:
                line += f"：{result}"
            parts.append(line)
            if status == "running":
                running.append(f"步骤{idx}:{title}")
        if running:
            parts.append("当前正在进行：" + "；".join(running))
        elif plan["steps"]:
            done_n = sum(1 for s in plan["steps"] if isinstance(s, dict) and s.get("status") == "done")
            parts.append(f"当前没有 running 步骤；已完成 {done_n}/{len(plan['steps'])}。")

    chat_block = _summarize_main_chat(chat_id)
    if chat_block:
        parts.append(chat_block)

    arts = memory.get("artifacts") or []
    if arts:
        parts.append("会话登记产物：\n- " + "\n- ".join(str(a) for a in arts[:40]))
    err_events = errors.get("events") if isinstance(errors, dict) else None
    if isinstance(err_events, list) and err_events:
        parts.append("错误记录：")
        for item in err_events[-12:]:
            parts.append(f"- {item.get('kind')}: {item.get('summary')}")
    variables = kernel.get("variables") if isinstance(kernel.get("variables"), list) else []
    if variables:
        parts.append("内核变量摘要（只读）：")
        for item in variables[:30]:
            if isinstance(item, dict):
                parts.append(
                    f"- {item.get('name')}: {item.get('type')} · {str(item.get('preview') or '')[:80]}"
                )

    ws_text, ws_snapshot = _summarize_workspace(prev_snapshot=prev_ws)
    if ws_text:
        parts.append(ws_text)
    # Persist snapshot for next branch question (growth detection). Keep other meta.
    try:
        save_supervisor_meta(
            chat_id,
            {
                **{k: v for k, v in meta.items() if k not in {"chatId", "role", "updatedAt"}},
                "workspaceSnapshot": ws_snapshot,
                "parentChatId": chat_id,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("workspace snapshot save skipped: %s", exc)

    return "\n".join(parts)


def _summarize_main_chat(chat_id: str, *, max_messages: int = 18, max_thinking: int = 10) -> str:
    """Main dialog + thinking stream from persisted chat.json (readonly)."""
    try:
        chat = load_chat_record(chat_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("main chat unavailable for %s: %s", chat_id, exc)
        return ""
    if not chat:
        return ""
    messages = chat.get("messages") if isinstance(chat.get("messages"), list) else []
    if not messages:
        return ""

    lines = ["## 主会话对话与 Thinking（只读；进度冲突时让位于计划/账本）"]
    for item in messages[-max_messages:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"}:
            continue
        label = "用户" if role == "user" else "助手"
        if content:
            lines.append(f"- {label}: {content[:700]}")
        steps = item.get("thinkingSteps") if isinstance(item.get("thinkingSteps"), list) else []
        if role == "assistant" and steps:
            lines.append("  Thinking:")
            for step in steps[-max_thinking:]:
                if isinstance(step, str):
                    lines.append(f"  · {step[:240]}")
                    continue
                if not isinstance(step, dict):
                    continue
                title = str(step.get("title") or step.get("kind") or "step").strip()
                body = str(step.get("body") or step.get("content") or step.get("text") or "").strip()
                bit = f"  · [{title}] {body}".rstrip()
                lines.append(bit[:320])
    return "\n".join(lines) if len(lines) > 1 else ""


def _summarize_workspace(
    *,
    prev_snapshot: Optional[Dict[str, Any]] = None,
    max_files: int = 45,
) -> tuple[str, Dict[str, Any]]:
    """List recent work_space files (exclude sessions/) and diff vs previous snapshot."""
    root = get_work_space()
    prev = prev_snapshot if isinstance(prev_snapshot, dict) else {}
    files: List[Dict[str, Any]] = []
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            parts = rel.parts
            if not parts or parts[0] in {"sessions", ".git", "__pycache__", ".ipynb_checkpoints"}:
                continue
            if any(p.startswith(".") and p not in {".claude"} for p in parts[:-1]):
                # skip hidden dirs but allow files under .claude/skills etc. only if needed
                if parts[0].startswith(".") and parts[0] != ".claude":
                    continue
            try:
                st = path.stat()
            except OSError:
                continue
            files.append(
                {
                    "path": str(rel).replace("\\", "/"),
                    "size": int(st.st_size),
                    "mtime": float(st.st_mtime),
                    "mtimeIso": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("workspace scan failed: %s", exc)
        return "", {}

    files.sort(key=lambda x: x["mtime"], reverse=True)
    snapshot = {
        item["path"]: {"size": item["size"], "mtime": item["mtime"]} for item in files[:200]
    }

    new_files: List[str] = []
    grew_files: List[str] = []
    for item in files[:max_files]:
        old = prev.get(item["path"]) if isinstance(prev.get(item["path"]), dict) else None
        if old is None and prev:
            new_files.append(f"{item['path']} ({item['size']} B)")
        elif isinstance(old, dict) and int(old.get("size") or 0) < item["size"]:
            grew_files.append(
                f"{item['path']} {int(old.get('size') or 0)}→{item['size']} B"
            )

    lines = ["## 工作区文件快照（只读，已排除 sessions/）"]
    lines.append(f"工作区根目录：{root}")
    lines.append(f"扫描到文件数：{len(files)}（下列为按修改时间最近的 {min(len(files), max_files)} 个）")
    if prev:
        if new_files:
            lines.append("相对上次监管者查看：新增 " + "；".join(new_files[:15]))
        if grew_files:
            lines.append("相对上次监管者查看：增大 " + "；".join(grew_files[:15]))
        if not new_files and not grew_files:
            lines.append("相对上次监管者查看：未见明显新增/增大（在最近文件窗口内）")
    else:
        lines.append("（首次快照，尚无增量对比）")

    for item in files[:max_files]:
        lines.append(f"- {item['path']}  {item['size']} B  mtime={item['mtimeIso']}")

    return "\n".join(lines), snapshot


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

    yield {"type": "status", "message": "监管者正在查阅主会话 / Thinking / 计划 / 工作区…"}
    try:
        text = answer_supervisor_question(chat_id, question, llm_body=body, history=history)
        prev_meta = load_supervisor_meta(chat_id)
        save_supervisor_meta(
            chat_id,
            {
                **{k: v for k, v in prev_meta.items() if k not in {"chatId", "role", "updatedAt"}},
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
