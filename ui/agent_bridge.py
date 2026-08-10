"""Bridge ui/ frontend to sRNAgent tool-loop backend."""
from __future__ import annotations

import queue
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

UI_ROOT = Path(__file__).resolve().parent
SRNAGENT_PROJECT = UI_ROOT.parent

if str(SRNAGENT_PROJECT) not in sys.path:
    sys.path.insert(0, str(SRNAGENT_PROJECT))

from sRNAgent.agent.agent_config import EXECUTION_TIMEOUT_SEC, ExecutionConfig, SandboxFallbackPolicy  # noqa: E402
from sRNAgent.agent.bootstrap import initialize_registries  # noqa: E402
from sRNAgent.agent.context import estimate_tokens, truncate_text  # noqa: E402
from sRNAgent.agent.checkpoint import load_checkpoint  # noqa: E402
from sRNAgent.agent.llm_client import LLMConfig  # noqa: E402
from sRNAgent.agent.srn_agent import AgentCancelledError, SRNAgent  # noqa: E402

from chat_kernel_manager import (  # noqa: E402
    delete_chat_session,
    get_chat_execution,
    interrupt_chat_kernel,
    kernel_is_busy,
    notebook_execution_enabled,
    release_chat_kernel,
)
from session_store import (  # noqa: E402
    SessionSaveConflict,
    acquire_operator_lease,
    clear_operator_lease,
    get_operator_lease,
    load_chat_record,
    load_chat_store,
    load_kernel_state,
    purge_orphan_sessions,
    renew_operator_lease,
    save_chat_record,
    save_kernel_state,
    session_artifacts,
    session_dir,
)
from session_memory import append_work_log, build_session_memory_context, record_stream_event, remember_user_query  # noqa: E402
from session_errors import (
    clear_run_context,
    record_session_error,
    record_sse_disconnect,
    record_stream_event_error,
    record_user_cancellation,
    update_run_context,
)
from session_plan import clear_plan, load_plan, plan_progress_summary, save_plan  # noqa: E402
from session_live import (  # noqa: E402
    close_live_bus,
    get_live_run_id,
    has_live_bus,
    iter_live_events,
    publish_live_event,
    start_live_bus,
)
from run_ledger import append_ledger_event, clear_run_ledger  # noqa: E402
from supervisor_agent import (  # noqa: E402
    assess_code_risk,
    clear_run_report,
    generate_run_report,
    load_run_report,
    render_report_markdown,
    stream_supervisor_chat,
)
from work_space import get_work_space, list_work_space_files  # noqa: E402

_runs_lock = threading.Lock()
_active_runs: Dict[str, threading.Event] = {}
_active_run_chat_ids: Dict[str, str] = {}
_active_code_by_chat: Dict[str, Dict[str, Any]] = {}
_approval_lock = threading.Lock()
_pending_approvals: Dict[str, threading.Event] = {}
_approval_results: Dict[str, bool] = {}
_STREAM_SENTINEL = object()
_APPROVAL_TIMEOUT_SEC = 600
_SSE_HEARTBEAT_SEC = 10
_KERNEL_DRAIN_MAX_SEC = 6 * 3600
_MAX_MEMORY_CONTEXT_TOKENS = 2400
_MAX_EXECUTION_CONTEXT_TOKENS = 1200
_MAX_RUN_CONTEXT_TOKENS = 3200


def _default_execution_config(chat_id: str = "") -> ExecutionConfig:
    cfg = ExecutionConfig(
        use_notebook=notebook_execution_enabled(),
        strict_kernel_validation=False,
        strict_env_validation=False,
        sandbox_fallback_policy=SandboxFallbackPolicy.WARN_AND_FALLBACK,
        timeout=EXECUTION_TIMEOUT_SEC,
    )
    if chat_id:
        cfg.checkpoint_dir = session_dir(chat_id) / "checkpoints"
    return cfg


def _resolve_chat_id(body: Dict[str, Any]) -> str:
    chat_id = str(body.get("chatId") or "").strip()
    if not chat_id:
        raise ValueError("chatId 不能为空")
    return chat_id


def _looks_binary_preview(text: str) -> bool:
    if not text:
        return False
    if text in {"empty", "binary data", "gzip compressed", "zip archive"}:
        return False
    if text.startswith("hex "):
        return False
    sample = text[:160]
    bad = 0
    for char in sample:
        code = ord(char)
        if code == 0xFFFD:
            bad += 1
        elif code < 32 and char not in "\n\r\t":
            bad += 1
        elif 127 <= code < 160:
            bad += 1
    return bad >= max(2, int(len(sample) * 0.12))


def _sanitize_kernel_variables(variables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sanitized: List[Dict[str, Any]] = []
    for item in variables:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        if str(entry.get("type") or "") == "bytes":
            preview = str(entry.get("preview") or "")
            if _looks_binary_preview(preview):
                entry["preview"] = "binary data"
        sanitized.append(entry)
    return sanitized


def _cached_kernel_variables(chat_id: str) -> List[Dict[str, Any]]:
    cached = load_kernel_state(chat_id)
    if not cached:
        return []
    variables = cached.get("variables")
    if not isinstance(variables, list):
        return []
    return _sanitize_kernel_variables(variables)


def _chat_has_active_run(chat_id: str) -> bool:
    with _runs_lock:
        return chat_id in _active_run_chat_ids.values()


def _track_code_execution(chat_id: str, run_id: str, event: Dict[str, Any]) -> None:
    """Track execute_code independently from an otherwise-live LLM loop."""
    event_type = str(event.get("type") or "")
    if event_type in {"code_execution_started", "code_execution_progress"}:
        with _runs_lock:
            current = _active_code_by_chat.get(chat_id)
            if not current or str(current.get("runId") or "") != run_id:
                current = {"runId": run_id, "startedAt": time.time()}
            for source, target in (
                ("toolCallId", "toolCallId"),
                ("summary", "summary"),
                ("description", "description"),
                ("stage", "stage"),
                ("elapsedSec", "elapsedSec"),
                ("elapsedLabel", "elapsedLabel"),
                ("highlights", "highlights"),
            ):
                if event.get(source) not in (None, ""):
                    current[target] = event[source]
            _active_code_by_chat[chat_id] = current
    elif event_type == "tool_result" and str(event.get("name") or "") == "execute_code":
        with _runs_lock:
            state = _active_code_by_chat.get(chat_id)
            if state and str(state.get("runId") or "") == run_id:
                _active_code_by_chat.pop(chat_id, None)


def _persist_final_chat_message(chat_id: str, text: str) -> None:
    """Persist the terminal reply even when the browser stream has disconnected."""
    if not chat_id or not str(text or "").strip():
        return
    chat = load_chat_record(chat_id)
    if not chat:
        return
    messages = list(chat.get("messages") or [])
    for message in reversed(messages):
        if isinstance(message, dict) and str(message.get("role") or "") == "assistant":
            message["content"] = str(text).strip()
            message["_finalReplyLocked"] = True
            break
    else:
        messages.append({"role": "assistant", "content": str(text).strip()})
    chat["messages"] = messages
    save_chat_record(chat_id, chat, force=True)


def kernel_environment(chat_id: str) -> Dict[str, Any]:
    # Do not create empty session shells just because KernelPanel is polling.
    execution = get_chat_execution(SRNAGENT_PROJECT, chat_id, create=False)
    if execution is None:
        cached_vars = _cached_kernel_variables(chat_id)
        if cached_vars:
            return {
                "ok": True,
                "ready": True,
                "variables": cached_vars,
                "message": "显示缓存的环境快照（内核尚未在本会话中启动）",
            }
        return {
            "ok": True,
            "ready": False,
            "variables": [],
            "message": "内核尚未启动，执行一次代码后将显示变量",
        }
    if not execution.use_notebook or execution.notebook_executor is None:
        return {
            "ok": True,
            "ready": False,
            "variables": [],
            "message": "Notebook 内核未启用",
        }
    executor = execution.notebook_executor

    # 仅在内核真正 busy 时回退缓存；Agent run 进行中但两次 execute_code 之间内核空闲时，仍应能扫到最新变量
    if getattr(executor, "is_busy", lambda: False)():
        cached_vars = _cached_kernel_variables(chat_id)
        return {
            "ok": True,
            "ready": bool(cached_vars),
            "busy": True,
            "variables": cached_vars,
            "message": "内核正在执行代码，显示缓存的环境快照",
        }

    if not executor.use_notebook_ready():
        cached_vars = _cached_kernel_variables(chat_id)
        if cached_vars:
            return {
                "ok": True,
                "ready": True,
                "variables": cached_vars,
                "message": "显示缓存的环境快照（内核尚未在本会话中启动）",
            }
        return {
            "ok": True,
            "ready": False,
            "variables": [],
            "message": "内核尚未启动，执行一次代码后将显示变量",
        }

    try:
        from sRNAgent.agent.session_notebook_executor import KernelBusyError

        variables = _sanitize_kernel_variables(executor.inspect_variables(wait=False))
        snapshot = _build_kernel_snapshot(execution, executor, variables)
        save_kernel_state(chat_id, snapshot)
        return {"ok": True, "ready": True, "variables": variables}
    except KernelBusyError:
        cached_vars = _cached_kernel_variables(chat_id)
        return {
            "ok": True,
            "ready": bool(cached_vars),
            "busy": True,
            "variables": cached_vars,
            "message": "内核正在执行代码，稍后自动刷新",
        }
    except Exception as exc:  # noqa: BLE001
        cached_vars = _cached_kernel_variables(chat_id)
        if cached_vars:
            return {
                "ok": True,
                "ready": True,
                "variables": cached_vars,
                "message": f"读取内核变量失败，显示缓存快照: {exc}",
            }
        return {"ok": False, "ready": False, "variables": [], "error": str(exc)}


def _build_kernel_snapshot(execution: Any, executor: Any, variables: List[Dict[str, Any]]) -> Dict[str, Any]:
    meta_path = getattr(executor, "meta_file", None)
    meta_payload: Dict[str, Any] = {}
    if meta_path is not None and Path(meta_path).exists():
        try:
            import json

            meta_payload = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        except Exception:
            meta_payload = {}

    connection_path = getattr(executor, "connection_file", None)
    connection_payload: Dict[str, Any] = {}
    if connection_path is not None and Path(connection_path).exists():
        try:
            import json

            connection_payload = json.loads(Path(connection_path).read_text(encoding="utf-8"))
        except Exception:
            connection_payload = {}

    return {
        "variables": variables,
        "runtime": execution.runtime.to_dict() if execution.runtime else {},
        "execution": execution.to_dict(),
        "kernel": {
            "kernelName": getattr(executor, "kernel_name", None),
            "condaEnv": getattr(executor, "conda_env", None),
            "workspaceDir": str(getattr(executor, "workspace_dir", get_work_space())),
            "timeoutSec": getattr(executor, "timeout", None),
            "sessionPromptCount": getattr(executor, "session_prompt_count", None),
        },
        "meta": meta_payload,
        "connection": connection_payload,
    }


def list_sessions() -> Dict[str, Any]:
    purged = purge_orphan_sessions()
    store = load_chat_store()
    return {"ok": True, "purgedOrphans": purged, **store}


def delete_session_api(body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        chat_id = _resolve_chat_id(body)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        deleted = delete_chat_session(chat_id)
        # Also sweep other empty shells left by abandoned New Chats.
        purged = purge_orphan_sessions()
        return {
            "ok": True,
            "deleted": deleted or True,
            "chatId": chat_id,
            "purgedOrphans": purged,
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def get_session(chat_id: str) -> Dict[str, Any]:
    chat_id = _resolve_chat_id({"chatId": chat_id})
    chat = load_chat_record(chat_id)
    if chat is None:
        return {"ok": False, "error": "会话不存在"}
    kernel_state = load_kernel_state(chat_id)
    return {
        "ok": True,
        "chat": chat,
        "kernelState": kernel_state,
        "artifacts": session_artifacts(chat_id),
    }


def session_replay_code(chat_id: str) -> Dict[str, Any]:
    from session_store import load_replay_chunks

    chat_id = _resolve_chat_id({"chatId": chat_id})
    chunks = load_replay_chunks(chat_id)
    return {"ok": True, "chunks": chunks}


def save_session(body: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = _resolve_chat_id(body)
    chat = body.get("chat") or {}
    if not isinstance(chat, dict):
        return {"ok": False, "error": "chat 必须是对象"}
    # Per-device operators: do NOT rewrite shared index.activeChatId by default.
    update_global_active = bool(body.get("updateGlobalActive", False))
    active_chat_id = str(body.get("activeChatId") or chat_id).strip() or chat_id
    device_id = str(body.get("deviceId") or "").strip() or None
    force = bool(body.get("force", False))
    expected_raw = body.get("expectedUpdatedAt", body.get("expected_updated_at"))
    expected_updated_at = None
    if expected_raw is not None and str(expected_raw).strip() != "":
        try:
            expected_updated_at = int(expected_raw)
        except (TypeError, ValueError):
            return {"ok": False, "error": "expectedUpdatedAt 无效"}
    try:
        saved = save_chat_record(
            chat_id,
            chat,
            active_chat_id=active_chat_id if update_global_active else None,
            expected_updated_at=expected_updated_at,
            device_id=device_id,
            force=force,
        )
        return {
            "ok": True,
            "chat": saved,
            "lease": get_operator_lease(chat_id),
            "sessionsRoot": str(get_work_space() / "sessions"),
        }
    except SessionSaveConflict as exc:
        return {
            "ok": False,
            "conflict": True,
            "error": str(exc) or "会话已被其他设备更新",
            "chat": exc.chat,
            "lease": exc.lease or get_operator_lease(chat_id),
        }


def work_space_files(relative_path: str = "", pattern: str = "*", recursive: bool = False) -> Dict[str, Any]:
    try:
        payload = list_work_space_files(relative_path, pattern=pattern, recursive=recursive)
        return {"ok": True, **payload}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def kernel_figures(chat_id: str) -> Dict[str, Any]:
    execution = get_chat_execution(SRNAGENT_PROJECT, chat_id, create=False)
    if execution is None:
        return {"ok": True, "ready": False, "figures": [], "message": "内核尚未启动"}
    if not execution.use_notebook or execution.notebook_executor is None:
        return {"ok": True, "ready": False, "figures": [], "message": "Notebook 内核未启用"}
    executor = execution.notebook_executor
    figures = executor.get_figures()
    if _chat_has_active_run(chat_id) or getattr(executor, "is_busy", lambda: False)():
        return {
            "ok": True,
            "ready": bool(figures),
            "busy": True,
            "figures": figures,
            "message": "Agent 正在运行，显示已缓存的图表",
        }
    return {"ok": True, "ready": bool(figures), "figures": figures}


def release_kernel(chat_id: str) -> Dict[str, Any]:
    try:
        released = release_chat_kernel(chat_id)
        return {"ok": True, "released": released}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _heartbeat_event(chat_id: str) -> Dict[str, Any]:
    busy = kernel_is_busy(SRNAGENT_PROJECT, chat_id)
    active = _chat_has_active_run(chat_id)
    if busy and not active:
        message = "内核仍在执行，保持连接…"
    elif active:
        message = "Agent 运行中…"
    else:
        message = "任务运行中…"
    return {
        "type": "heartbeat",
        "kernelBusy": busy,
        "hasActiveRun": active,
        "message": message,
    }


def resolve_chat_id_for_run(run_id: str) -> str:
    run_id = str(run_id or "").strip()
    if not run_id:
        return ""
    with _runs_lock:
        return _active_run_chat_ids.get(run_id, "")


def register_run(run_id: str, chat_id: str) -> threading.Event:
    cancel_event = threading.Event()
    with _runs_lock:
        _active_runs[run_id] = cancel_event
        _active_run_chat_ids[run_id] = chat_id
    return cancel_event


def cancel_run(
    run_id: str,
    chat_id: str = "",
    *,
    interrupt_kernel: Optional[bool] = None,
    force_interrupt: bool = False,
) -> bool:
    resolved_chat_id = str(chat_id or "").strip()
    cancelled = False
    runs_to_cleanup: List[str] = []
    with _runs_lock:
        if run_id:
            event = _active_runs.get(run_id)
            if event is not None:
                event.set()
                cancelled = True
                runs_to_cleanup.append(run_id)
            if not resolved_chat_id:
                resolved_chat_id = _active_run_chat_ids.get(run_id, "")
        if resolved_chat_id:
            for active_run_id, active_chat_id in list(_active_run_chat_ids.items()):
                if active_chat_id == resolved_chat_id:
                    event = _active_runs.get(active_run_id)
                    if event is not None:
                        event.set()
                        cancelled = True
                    if active_run_id not in runs_to_cleanup:
                        runs_to_cleanup.append(active_run_id)

    interrupted = False
    if resolved_chat_id:
        should_interrupt = interrupt_kernel
        if should_interrupt is None:
            should_interrupt = kernel_is_busy(SRNAGENT_PROJECT, resolved_chat_id)
        if should_interrupt:
            try:
                interrupted = interrupt_chat_kernel(
                    SRNAGENT_PROJECT,
                    resolved_chat_id,
                    force=force_interrupt,
                )
            except ValueError:
                interrupted = False

    for active_run_id in runs_to_cleanup:
        cleanup_run(active_run_id)

    if resolved_chat_id:
        try:
            close_live_bus(resolved_chat_id)
        except Exception:
            pass

    if resolved_chat_id and run_id and (cancelled or interrupted):
        if force_interrupt:
            record_user_cancellation(
                resolved_chat_id,
                run_id=run_id,
                interrupted=interrupted,
                source="cancel_api",
            )
        elif cancelled:
            record_session_error(
                resolved_chat_id,
                kind="agent_cancelled",
                summary="Agent 运行被取消",
                run_id=run_id,
                source="cancel_run",
                context={"kernelInterrupted": interrupted},
            )
    elif resolved_chat_id and interrupted and not run_id:
        record_session_error(
            resolved_chat_id,
            kind="kernel_interrupted",
            summary="Jupyter 内核执行被中断",
            source="cancel_run",
        )

    return cancelled or interrupted


def cleanup_run(run_id: str) -> None:
    chat_id = ""
    with _runs_lock:
        chat_id = _active_run_chat_ids.get(run_id, "")
        _active_runs.pop(run_id, None)
        _active_run_chat_ids.pop(run_id, None)
        if chat_id and str(_active_code_by_chat.get(chat_id, {}).get("runId") or "") == run_id:
            _active_code_by_chat.pop(chat_id, None)
    if chat_id:
        clear_run_context(chat_id)


def _approval_key(run_id: str, request_id: str) -> str:
    return f"{run_id}:{request_id}"


def approve_code(run_id: str, request_id: str, approved: bool) -> bool:
    key = _approval_key(run_id, request_id)
    with _approval_lock:
        gate = _pending_approvals.get(key)
        if gate is None:
            # Already auto-approved or processed — idempotent success for allow clicks.
            return approved
        _approval_results[key] = approved
        gate.set()
        return True


def _trim_context_block(text: str, *, max_tokens: int) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if estimate_tokens(value) <= max_tokens:
        return value
    max_chars = max(800, max_tokens * 4)
    return truncate_text(value, max_chars).strip()


def _build_run_context(chat_id: str, *, user_query: str = "") -> str:
    blocks: List[str] = []
    memory_context = build_session_memory_context(chat_id, user_query=user_query) if chat_id else ""
    memory_context = _trim_context_block(memory_context, max_tokens=_MAX_MEMORY_CONTEXT_TOKENS)
    if memory_context:
        blocks.append(memory_context)

    if not blocks:
        return ""
    combined = "\n\n".join(blocks)
    return _trim_context_block(combined, max_tokens=_MAX_RUN_CONTEXT_TOKENS)


def _plan_mode_enabled(agent_cfg: Dict[str, Any]) -> bool:
    val = agent_cfg.get("planMode", True)
    if isinstance(val, str):
        return val.strip().lower() not in ("false", "0", "no", "off")
    return bool(val)


def _build_agent(body: Dict[str, Any]) -> tuple[SRNAgent, Dict[str, Any]]:
    account = body.get("account") or {}
    vendor = body.get("vendor") or {}
    agent_cfg = body.get("agent") or {}
    chat_id = _resolve_chat_id(body)
    llm_config = LLMConfig.from_ui_payload(account, vendor, agent_cfg)
    extra_system = str(agent_cfg.get("systemPrompt") or "").strip()
    max_turns = int(agent_cfg.get("maxTurns") or 100)
    max_turns = max(1, min(max_turns, 100))
    agent = SRNAgent(
        llm_config=llm_config,
        cwd=get_work_space(),
        max_turns=max_turns,
        extra_system_prompt=extra_system,
        execution_config=_default_execution_config(chat_id),
        execution_backend=get_chat_execution(SRNAGENT_PROJECT, chat_id),
    )
    return agent, agent_cfg


def _chat_code_panel_running(chat_id: str) -> bool:
    chat = load_chat_record(chat_id)
    if not chat:
        return False
    code_panel = chat.get("codePanel")
    if not isinstance(code_panel, list):
        return False
    # UI synthetic "Agent 运行中" card must not count as a real stuck execution.
    background_id = "background-kernel-run"
    return any(
        isinstance(item, dict)
        and item.get("type") == "execution"
        and str(item.get("id") or "") != background_id
        and not item.get("done")
        and not item.get("stopped")
        for item in code_panel
    )


def agent_run_status(chat_id: str) -> Dict[str, Any]:
    """Lightweight run snapshot for UI polling when SSE is silent or disconnected."""
    try:
        chat_id = _resolve_chat_id({"chatId": chat_id})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    has_active_run = _chat_has_active_run(chat_id)
    busy = kernel_is_busy(SRNAGENT_PROJECT, chat_id)
    with _runs_lock:
        code_state = dict(_active_code_by_chat.get(chat_id) or {})
    if code_state and not has_active_run:
        with _runs_lock:
            _active_code_by_chat.pop(chat_id, None)
        code_state = {}
    live_run_id = get_live_run_id(chat_id)
    plan = load_plan(chat_id)
    plan_summary = plan_progress_summary(plan) if plan else ""
    running_step = None
    if plan and isinstance(plan.get("steps"), list):
        running_step = next(
            (step for step in plan["steps"] if str(step.get("status") or "") == "running"),
            None,
        )
    plan_step_running = running_step is not None
    code_panel_running = _chat_code_panel_running(chat_id)
    # Only kernel busy or an active agent loop means work is truly running.
    # Stale plan.json / codePanel "running" flags alone are not enough.
    task_active = has_active_run or busy
    stale_plan_step = plan_step_running and not task_active
    stale_code_panel = code_panel_running and not task_active

    return {
        "ok": True,
        "chatId": chat_id,
        "hasActiveRun": has_active_run,
        "kernelBusy": busy,
        "codeActive": bool(code_state),
        "codeStartedAt": code_state.get("startedAt"),
        "codeSummary": code_state.get("summary") or "",
        "codeDescription": code_state.get("description") or "",
        "codeStage": code_state.get("stage") or "",
        "codeElapsedSec": code_state.get("elapsedSec"),
        "codeElapsedLabel": code_state.get("elapsedLabel") or "",
        "codeHighlights": code_state.get("highlights") or [],
        "planStepRunning": plan_step_running,
        "codePanelRunning": code_panel_running,
        "stalePlanStep": stale_plan_step,
        "staleCodePanel": stale_code_panel,
        "taskActive": task_active,
        "backgroundActive": busy and not has_active_run,
        "liveAvailable": has_live_bus(chat_id),
        "runId": live_run_id,
        "plan": plan,
        "planSummary": plan_summary,
        "runningStepTitle": str((running_step or {}).get("title") or "").strip(),
    }


def agent_status() -> Dict[str, Any]:
    from sRNAgent.agent.env import detect_runtime_environment

    workspace = get_work_space()
    function_registry, skill_registry, overview = initialize_registries(cwd=workspace)
    runtime = detect_runtime_environment()
    return {
        "backend": "sRNAgent",
        "workspace": str(workspace),
        "skills": list(skill_registry.skill_metadata.keys()),
        "skill_overview": overview,
        "functions": [
            entry.get("full_name")
            for entry in function_registry.find("fastq")
        ],
        "execution": {
            "mode": "per_chat_kernel" if notebook_execution_enabled() else "per_chat_in_process",
            "use_notebook": notebook_execution_enabled(),
            "runtime": runtime.to_dict(),
        },
    }


def run_agent_chat(body: Dict[str, Any]) -> Dict[str, Any]:
    messages = _normalize_message_list(body.get("messages"))
    if not messages:
        return {"ok": False, "error": "messages 不能为空"}

    try:
        agent, agent_cfg = _build_agent(body)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        chat_id = _resolve_chat_id(body)
        user_query = _latest_user_message(messages)
        remember_user_query(chat_id, user_query)
        use_plan_mode = _plan_mode_enabled(agent_cfg)
        resume = bool(body.get("resume") or body.get("continueRun") or False)
        if _FRESH_WORKFLOW_KEYWORDS.search(user_query):
            resume = False
        if not resume and _plan_awaits_approval(chat_id):
            resume = True
        if not resume:
            clear_plan(chat_id)
        run_context = _build_run_context(chat_id, user_query=user_query)
        if use_plan_mode:
            text = agent.run_planned(
                messages,
                extra_context=run_context,
                chat_id=chat_id,
                save_plan=save_plan,
                load_plan=load_plan,
                resume=resume,
            )
        else:
            text = agent.run_with_history(
                messages,
                chat_id=chat_id,
                resume=resume,
                extra_context=run_context,
            )
    except AgentCancelledError:
        return {"ok": False, "error": "已停止", "cancelled": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "text": text,
        "meta": {
            "skills": list(agent.skill_registry.skill_metadata.keys()),
            "backend": "sRNAgent",
            "execution": agent.execution.to_dict(),
        },
    }


def get_run_report(chat_id: str) -> Dict[str, Any]:
    try:
        chat_id = _resolve_chat_id({"chatId": chat_id})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    report = load_run_report(chat_id)
    if not report:
        return {"ok": False, "error": "尚无运行报告"}
    tasks = report.get("tasks") if isinstance(report.get("tasks"), list) else []
    return {
        "ok": True,
        "report": report,
        "markdown": render_report_markdown(report),
        "taskCount": len(tasks),
    }


def clear_run_report_api(body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        chat_id = _resolve_chat_id(body)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    result = clear_run_report(chat_id)
    result["ok"] = True
    return result


def _normalize_approval_mode(body: Dict[str, Any]) -> str:
    raw = str(body.get("approvalMode") or "").strip().lower()
    if raw in {"manual", "smart", "auto"}:
        return raw
    # Backward compatible with legacy autoApproveCode boolean.
    if body.get("autoApproveCode") is True:
        return "auto"
    return "manual"


_RESUME_KEYWORDS = (
    "继续", "继续刚才", "继续任务", "继续对话", "接着", "接着做",
    "从断的地方", "从上次", "go on", "continue", "resume",
)
_FRESH_WORKFLOW_KEYWORDS = re.compile(
    r"(?:清空|清除|删除|重建|重新生成|重跑).{0,48}(?:mirtop|iso[- ]?mir|isomiR)|"
    r"(?:从|自).{0,16}(?:hairpin )?(?:比对|alignment|bowtie).{0,32}(?:开始|重建|重新|生成)",
    re.I,
)


def _normalize_message_list(messages):
    if not isinstance(messages, list):
        return []
    out = []
    for item in messages:
        if isinstance(item, dict):
            out.append({str(k): v for k, v in item.items() if isinstance(k, str)})
    return out


def _latest_user_message(source):
    """Return the latest user message text from either a messages list or a body dict."""
    history = []
    if isinstance(source, list):
        history = source
    elif isinstance(source, dict):
        history = source.get("history") if isinstance(source.get("history"), list) else []
    for item in reversed(history):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "") == "user":
            return str(item.get("content") or "").strip()
    if isinstance(source, dict):
        return str(source.get("query") or "").strip()
    return ""




def _auto_detect_resume(body: Dict[str, Any], chat_id: str) -> bool:
    """True if the new user message looks like 'continue the interrupted task'.

    Resume only when persisted state actually contains unfinished work. A
    completed run also has a checkpoint (written before ``finish``), so a
    keyword-only check would incorrectly route a new follow-up into the old
    plan.
    """
    if not chat_id:
        return False
    msg = _latest_user_message(body).lower()
    # A destructive/rebuild request establishes a new execution scope even
    # when it contains words such as "continue". Reusing a checkpoint here
    # can resurrect an interrupted subprocess with incompatible outputs.
    if _FRESH_WORKFLOW_KEYWORDS.search(msg):
        return False
    if not msg or len(msg) > 60:
        return False
    if not any(kw.lower() in msg for kw in _RESUME_KEYWORDS):
        return False

    try:
        plan = load_plan(chat_id)
    except Exception:
        plan = None
    if isinstance(plan, dict):
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        if any(
            isinstance(step, dict)
            and str(step.get("status") or "pending").strip().lower()
            in {"pending", "running", "failed"}
            for step in steps
        ):
            return True
        # An all-done plan is historical context, not a resumable run.
        return False

    # Non-plan mode: inspect the checkpoint transcript. Tool results or a
    # non-terminal assistant tool call mean another LLM turn is required.
    try:
        checkpoint = load_checkpoint(session_dir(chat_id) / "checkpoints", chat_id)
    except Exception:
        checkpoint = None
    messages = checkpoint.get("messages") if isinstance(checkpoint, dict) else None
    if not isinstance(messages, list) or not messages:
        return False
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("role") == "system":
            continue
        role = str(item.get("role") or "")
        if role == "tool":
            return True
        if role == "assistant":
            calls = item.get("tool_calls") if isinstance(item.get("tool_calls"), list) else []
            return any(
                str((call.get("function") or {}).get("name") or call.get("name") or "") != "finish"
                for call in calls
                if isinstance(call, dict)
            )
        return False
    return False


def _plan_awaits_approval(chat_id: str) -> bool:
    """A user reply after a plan approval gate resumes that exact plan."""
    if not chat_id:
        return False
    try:
        plan = load_plan(chat_id)
    except Exception:
        return False
    steps = plan.get("steps") if isinstance(plan, dict) and isinstance(plan.get("steps"), list) else []
    return any(
        isinstance(step, dict) and str(step.get("status") or "").strip().lower() == "awaiting_approval"
        for step in steps
    )


def _append_work_log_event(chat_id: str, event: Dict[str, Any], run_id: str) -> None:
    """Append a meaningful subset of on_progress events to work_log.jsonl.

    Heartbeat events (code_execution_progress) are skipped to avoid filling the
    log; we only record run / step boundaries and tool completions so a
    restarted agent can read the last 15 entries via build_session_memory_context
    and pick up where the previous run left off.
    """
    if not chat_id:
        return
    etype = str(event.get("type") or "")
    kind: Optional[str] = None
    label = str(event.get("summary") or event.get("message") or "").strip()
    paths: List[str] = []
    note = ""

    if etype == "run_start":
        kind = "run_start"
    elif etype == "plan_step_done":
        kind = "step_done"
        label = label or str(event.get("title") or "")
    elif etype == "plan_step_failed":
        kind = "step_failed"
        note = str(event.get("result") or "")[:240]
    elif etype == "tool_result" and event.get("name") == "execute_code":
        kind = "tool_done"
        note = str(event.get("content") or "")[:240]
    elif etype == "done":
        kind = "run_done"
        label = str(event.get("text") or "")[:200]
    elif etype in {"cancelled", "error", "agent_error"}:
        kind = "error"
        note = str(event.get("message") or "")[:240]
    elif etype == "run_report_ready":
        kind = "report_ready"
        label = str(event.get("reportSummary") or label)

    if kind is None:
        return
    try:
        append_work_log(
            chat_id,
            kind=kind,
            label=label,
            paths=paths,
            note=note,
            run_id=run_id,
        )
    except Exception:
        pass


def run_agent_chat_stream(body: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    messages = _normalize_message_list(body.get("messages"))
    if not messages:
        yield {"type": "error", "message": "messages 不能为空"}
        return

    run_id = str(body.get("runId") or uuid.uuid4())
    chat_id = _resolve_chat_id(body)
    device_id = str(body.get("deviceId") or "").strip()
    approval_mode = _normalize_approval_mode(body)
    resume = bool(body.get("resume") or body.get("continueRun") or False)
    if _FRESH_WORKFLOW_KEYWORDS.search(_latest_user_message(body)):
        resume = False
    # 自动检测"继续中断任务"：用户消息含这些关键词 + 该 chat 有 checkpoint
    # → 当作 resume=true，让 agent 从上次中断的 tool_loop 消息恢复
    if not resume:
        resume = _plan_awaits_approval(chat_id) or _auto_detect_resume(body, chat_id)

    # Exclusive operator lease: another device mid-run cannot steal this chat.
    existing_lease = get_operator_lease(chat_id)
    if (
        existing_lease
        and device_id
        and existing_lease.get("deviceId") != device_id
        and (_chat_has_active_run(chat_id) or kernel_is_busy(SRNAGENT_PROJECT, chat_id))
    ):
        yield {
            "type": "error",
            "message": "该会话正由其他设备操作中。请新建对话作为本机操作者，或等待对方结束后再进入。",
            "conflict": True,
            "lease": existing_lease,
        }
        return

    if device_id:
        try:
            acquire_operator_lease(chat_id, device_id, run_id=run_id)
        except SessionSaveConflict as exc:
            yield {
                "type": "error",
                "message": str(exc) or "无法获取会话操作权",
                "conflict": True,
                "lease": exc.lease,
            }
            return

    # Remove the previous plan only after this request has acquired the chat;
    # a rejected observer must not erase the active operator's plan.
    if not resume:
        clear_plan(chat_id)

    # Stop any in-flight agent loop for this chat; interrupt kernel only if it is busy.
    cancel_run("", chat_id)
    cancel_event = register_run(run_id, chat_id)
    event_queue: queue.Queue = queue.Queue()
    start_live_bus(chat_id, run_id)
    try:
        clear_run_ledger(chat_id, run_id=run_id)
    except Exception:
        pass

    def _publish(event: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(event or {})
        if device_id and payload.get("type") in {"heartbeat", "status", "run_start"}:
            try:
                renew_operator_lease(chat_id, device_id, run_id=run_id)
            except Exception:
                pass
        try:
            payload = publish_live_event(chat_id, payload)
        except Exception:
            pass
        return payload

    def on_progress(event: Dict[str, Any]) -> None:
        try:
            _track_code_execution(chat_id, run_id, event)
            update_run_context(chat_id, event)
            record_stream_event(chat_id, event)
            # 持久化错误时用完整 head+tail 结果（fullContent），
            # 不用 UI 预览的 600 字符截断（避免 traceback 尾部错误行丢失）
            persist_event = dict(event)
            full = event.get("fullContent")
            if full:
                persist_event["content"] = full
            record_stream_event_error(chat_id, persist_event)
            append_ledger_event(chat_id, event, run_id=run_id)
            _append_work_log_event(chat_id, event, run_id)
        except Exception:
            pass
        # Queue the same sequenced payload sent to live followers. Without the
        # sequence, a browser that reloads cannot resume the direct stream
        # after the last event it has already rendered.
        event_queue.put(_publish(event))

    def request_code_approval(request_id: str, code: str, description: str) -> bool:
        if approval_mode == "auto":
            on_progress(
                {
                    "type": "supervisor_approval",
                    "requestId": request_id,
                    "action": "allow",
                    "level": "low",
                    "reason": "全部自动批准",
                    "mode": "auto",
                }
            )
            return True

        decision: Optional[Dict[str, Any]] = None
        if approval_mode == "smart":
            decision = assess_code_risk(
                code,
                description=description,
                llm_body=body,
                chat_id=chat_id,
            )
            on_progress(
                {
                    "type": "supervisor_approval",
                    "requestId": request_id,
                    "action": decision.get("action"),
                    "level": decision.get("level"),
                    "reason": decision.get("reason"),
                    "mode": "smart",
                    "source": decision.get("source"),
                    "code": code[:400],
                    "description": description,
                }
            )
            action = str(decision.get("action") or "escalate")
            if action == "allow":
                return True
            if action == "deny":
                return False
            # escalate → fall through to manual gate

        on_progress(
            {
                "type": "code_approval_required",
                "requestId": request_id,
                "code": code,
                "description": description,
                "supervisor": decision,
                "approvalMode": approval_mode,
            }
        )

        key = _approval_key(run_id, request_id)
        gate = threading.Event()
        with _approval_lock:
            _pending_approvals[key] = gate
            _approval_results.pop(key, None)

        approved = gate.wait(timeout=_APPROVAL_TIMEOUT_SEC)
        with _approval_lock:
            _pending_approvals.pop(key, None)
            result = _approval_results.pop(key, False)

        if cancel_event.is_set():
            raise AgentCancelledError("Agent run cancelled.")
        if not approved:
            return False
        return result

    def worker() -> None:
        final_text = ""
        try:
            on_progress({"type": "status", "message": "正在初始化 Agent 执行环境…"})
            try:
                agent, agent_cfg = _build_agent(body)
            except ValueError as exc:
                event_queue.put({"type": "error", "message": str(exc)})
                return

            on_progress({"type": "status", "message": "Agent 就绪，正在请求 LLM…"})
            use_plan_mode = _plan_mode_enabled(agent_cfg)
            user_query = _latest_user_message(messages)
            remember_user_query(
                chat_id,
                user_query,
                reset_request_scope=not resume,
            )
            # A new user request starts a new planning context. Clear the
            # previous unfinished plan before building session memory; doing
            # it afterward leaked the old pending steps into the planner and
            # could make an unrelated follow-up inherit the prior workflow.
            if not resume:
                clear_plan(chat_id)
            run_context = _build_run_context(chat_id, user_query=user_query)
            if use_plan_mode:
                text = agent.run_planned(
                    messages,
                    extra_context=run_context,
                    chat_id=chat_id,
                    save_plan=save_plan,
                    load_plan=load_plan,
                    resume=resume,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                    code_approval_callback=request_code_approval,
                )
            else:
                # 普通对话模式也注入会话级持久记忆（之前做过什么、产物在哪），
                # 避免 run_with_history 路径"失忆"
                text = agent.run_with_history(
                    messages,
                    chat_id=chat_id,
                    resume=resume,
                    extra_context=run_context,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                    code_approval_callback=request_code_approval,
                )
            final_text = text
            try:
                _persist_final_chat_message(chat_id, text)
            except Exception:
                pass
            on_progress(
                {
                    "type": "done",
                    "text": text,
                    "meta": {
                        "skills": list(agent.skill_registry.skill_metadata.keys()),
                        "backend": "sRNAgent",
                        "execution": agent.execution.to_dict(),
                    },
                }
            )
        except AgentCancelledError:
            on_progress({"type": "cancelled", "message": "已停止生成"})
        except Exception as exc:  # noqa: BLE001
            import traceback as _tb
            record_session_error(
                chat_id,
                kind="agent_error",
                summary="Agent 执行异常终止",
                # 包含完整 traceback，让 session_errors 能定位崩溃的具体行
                detail=f"{exc}\n{_tb.format_exc()}",
                run_id=run_id,
                source="agent_worker",
            )
            on_progress({"type": "error", "message": str(exc)})
        finally:
            try:
                report = generate_run_report(
                    chat_id,
                    llm_body=body,
                    final_text=final_text,
                    run_id=run_id,
                )
                tasks = report.get("tasks") if isinstance(report.get("tasks"), list) else []
                latest = tasks[-1] if tasks else {}
                on_progress(
                    {
                        "type": "run_report_ready",
                        "message": "运行报告已追加",
                        "reportSummary": latest.get("summary")
                        or latest.get("taskLabel")
                        or "可在左侧 Report 页查看",
                        "taskLabel": latest.get("taskLabel") or "",
                        "taskId": latest.get("taskId") or run_id,
                        "taskCount": len(tasks),
                        "chatId": chat_id,
                    }
                )
            except Exception:
                pass
            event_queue.put(_STREAM_SENTINEL)
            cleanup_run(run_id)

    thread = threading.Thread(target=worker, daemon=True)
    # Publish the sequence root before the worker can emit progress frames.
    # This makes `_seq` monotonic in the direct stream as well as the replay
    # stream, so a reconnect checkpoint cannot skip an earlier event.
    yield _publish({"type": "run_start", "runId": run_id, "chatId": chat_id, "approvalMode": approval_mode})
    try:
        update_run_context(chat_id, {"type": "run_start", "runId": run_id})
    except Exception:
        pass
    thread.start()

    worker_finished = False
    drain_started_at: Optional[float] = None

    try:
        while True:
            try:
                item = event_queue.get(timeout=_SSE_HEARTBEAT_SEC)
            except queue.Empty:
                if cancel_event.is_set():
                    break
                if worker_finished:
                    if not kernel_is_busy(SRNAGENT_PROJECT, chat_id):
                        break
                    if drain_started_at is not None and time.time() - drain_started_at > _KERNEL_DRAIN_MAX_SEC:
                        break
                if worker_finished or _chat_has_active_run(chat_id) or kernel_is_busy(SRNAGENT_PROJECT, chat_id):
                    yield _publish(_heartbeat_event(chat_id))
                continue

            if item is _STREAM_SENTINEL:
                worker_finished = True
                drain_started_at = time.time()
                if not kernel_is_busy(SRNAGENT_PROJECT, chat_id):
                    break
                yield _publish(_heartbeat_event(chat_id))
                continue
            # 多数事件在 on_progress 时已 publish；队列取出的终态事件也已 publish
            yield item
    finally:
        if device_id:
            try:
                clear_operator_lease(chat_id, device_id)
            except Exception:
                pass
        try:
            close_live_bus(chat_id, run_id=run_id)
        except Exception:
            pass


def run_agent_live_stream(chat_id: str, after_seq: int = 0) -> Iterator[Dict[str, Any]]:
    """Secondary-client live subscription (does not own / cancel the agent run)."""
    yield from iter_live_events(chat_id, after_seq=after_seq)
