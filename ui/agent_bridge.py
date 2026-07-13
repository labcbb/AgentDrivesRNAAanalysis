"""Bridge ui/ frontend to sRNAgent tool-loop backend."""
from __future__ import annotations

import queue
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
from sRNAgent.agent.llm_client import LLMConfig  # noqa: E402
from sRNAgent.agent.srn_agent import AgentCancelledError, SRNAgent  # noqa: E402

from chat_kernel_manager import get_chat_execution, interrupt_chat_kernel, kernel_is_busy, release_chat_kernel  # noqa: E402
from session_store import (  # noqa: E402
    load_chat_record,
    load_chat_store,
    load_kernel_state,
    save_chat_record,
    save_kernel_state,
    session_artifacts,
)
from session_memory import build_session_memory_context, record_stream_event  # noqa: E402
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
from work_space import get_work_space, list_work_space_files  # noqa: E402

_runs_lock = threading.Lock()
_active_runs: Dict[str, threading.Event] = {}
_active_run_chat_ids: Dict[str, str] = {}
_approval_lock = threading.Lock()
_pending_approvals: Dict[str, threading.Event] = {}
_approval_results: Dict[str, bool] = {}
_STREAM_SENTINEL = object()
_APPROVAL_TIMEOUT_SEC = 600
_SSE_HEARTBEAT_SEC = 10
_KERNEL_DRAIN_MAX_SEC = 6 * 3600


def _default_execution_config() -> ExecutionConfig:
    return ExecutionConfig(
        use_notebook=True,
        strict_kernel_validation=False,
        strict_env_validation=False,
        sandbox_fallback_policy=SandboxFallbackPolicy.WARN_AND_FALLBACK,
        timeout=EXECUTION_TIMEOUT_SEC,
    )


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


def kernel_environment(chat_id: str) -> Dict[str, Any]:
    execution = get_chat_execution(SRNAGENT_PROJECT, chat_id)
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
    store = load_chat_store()
    return {"ok": True, **store}


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
    active_chat_id = str(body.get("activeChatId") or chat_id).strip() or chat_id
    saved = save_chat_record(chat_id, chat, active_chat_id=active_chat_id)
    return {"ok": True, "chat": saved, "sessionsRoot": str(get_work_space() / "sessions")}


def work_space_files(relative_path: str = "", pattern: str = "*", recursive: bool = False) -> Dict[str, Any]:
    try:
        payload = list_work_space_files(relative_path, pattern=pattern, recursive=recursive)
        return {"ok": True, **payload}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def kernel_figures(chat_id: str) -> Dict[str, Any]:
    execution = get_chat_execution(SRNAGENT_PROJECT, chat_id)
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


def _inject_execution_context(extra_system: str, body: Dict[str, Any]) -> str:
    execution_context = str(body.get("executionContext") or "").strip()
    if not execution_context:
        return extra_system
    block = (
        "## Recent tool execution (internal context only — do not show or repeat to the user)\n"
        f"{execution_context}"
    )
    if extra_system:
        return f"{extra_system}\n\n{block}"
    return block


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
    memory_context = build_session_memory_context(chat_id) if chat_id else ""
    if memory_context:
        extra_system = f"{extra_system}\n\n{memory_context}".strip() if extra_system else memory_context
    extra_system = _inject_execution_context(extra_system, body)
    max_turns = int(agent_cfg.get("maxTurns") or 100)
    max_turns = max(1, min(max_turns, 100))
    agent = SRNAgent(
        llm_config=llm_config,
        cwd=get_work_space(),
        max_turns=max_turns,
        extra_system_prompt=extra_system,
        execution_config=_default_execution_config(),
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
    return any(
        isinstance(item, dict)
        and item.get("type") == "execution"
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
            "mode": "per_chat_kernel",
            "use_notebook": True,
            "runtime": runtime.to_dict(),
        },
    }


def run_agent_chat(body: Dict[str, Any]) -> Dict[str, Any]:
    messages: List[Dict[str, str]] = body.get("messages") or []
    if not messages:
        return {"ok": False, "error": "messages 不能为空"}

    try:
        agent, agent_cfg = _build_agent(body)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        chat_id = _resolve_chat_id(body)
        use_plan_mode = _plan_mode_enabled(agent_cfg)
        if use_plan_mode:
            clear_plan(chat_id)
            memory_context = build_session_memory_context(chat_id)
            text = agent.run_planned(
                messages,
                extra_context=memory_context,
                chat_id=chat_id,
                save_plan=save_plan,
            )
        else:
            text = agent.run_with_history(messages)
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


def run_agent_chat_stream(body: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    messages: List[Dict[str, str]] = body.get("messages") or []
    if not messages:
        yield {"type": "error", "message": "messages 不能为空"}
        return

    run_id = str(body.get("runId") or uuid.uuid4())
    chat_id = _resolve_chat_id(body)
    # Stop any in-flight agent loop for this chat; interrupt kernel only if it is busy.
    cancel_run("", chat_id)
    auto_approve_code = bool(body.get("autoApproveCode"))
    cancel_event = register_run(run_id, chat_id)
    event_queue: queue.Queue = queue.Queue()
    start_live_bus(chat_id, run_id)

    def _publish(event: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(event or {})
        try:
            publish_live_event(chat_id, payload)
        except Exception:
            pass
        return payload

    def on_progress(event: Dict[str, Any]) -> None:
        event_queue.put(event)
        try:
            update_run_context(chat_id, event)
            record_stream_event(chat_id, event)
            record_stream_event_error(chat_id, event)
        except Exception:
            pass
        _publish(event)

    def request_code_approval(request_id: str, code: str, description: str) -> bool:
        if auto_approve_code:
            return True
        on_progress(
            {
                "type": "code_approval_required",
                "requestId": request_id,
                "code": code,
                "description": description,
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
        try:
            on_progress({"type": "status", "message": "正在初始化 Agent 和 Jupyter 内核…"})
            try:
                agent, agent_cfg = _build_agent(body)
            except ValueError as exc:
                event_queue.put({"type": "error", "message": str(exc)})
                return

            on_progress({"type": "status", "message": "Agent 就绪，正在请求 LLM…"})
            use_plan_mode = _plan_mode_enabled(agent_cfg)
            if use_plan_mode:
                clear_plan(chat_id)
                memory_context = build_session_memory_context(chat_id)
                text = agent.run_planned(
                    messages,
                    extra_context=memory_context,
                    chat_id=chat_id,
                    save_plan=save_plan,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                    code_approval_callback=request_code_approval,
                )
            else:
                text = agent.run_with_history(
                    messages,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                    code_approval_callback=request_code_approval,
                )
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
            record_session_error(
                chat_id,
                kind="agent_error",
                summary="Agent 执行异常终止",
                detail=str(exc),
                run_id=run_id,
                source="agent_worker",
            )
            on_progress({"type": "error", "message": str(exc)})
        finally:
            event_queue.put(_STREAM_SENTINEL)
            cleanup_run(run_id)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    yield _publish({"type": "run_start", "runId": run_id, "chatId": chat_id})
    try:
        update_run_context(chat_id, {"type": "run_start", "runId": run_id})
    except Exception:
        pass

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
        try:
            close_live_bus(chat_id, run_id=run_id)
        except Exception:
            pass


def run_agent_live_stream(chat_id: str, after_seq: int = 0) -> Iterator[Dict[str, Any]]:
    """Secondary-client live subscription (does not own / cancel the agent run)."""
    yield from iter_live_events(chat_id, after_seq=after_seq)
