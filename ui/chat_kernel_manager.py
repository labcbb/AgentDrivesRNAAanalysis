"""Per-chat Jupyter kernel lifecycle (persist under work_space/sessions/{chatId}/)."""
from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import Dict, Optional

from sRNAgent.agent.agent_config import EXECUTION_TIMEOUT_SEC, ExecutionConfig, SandboxFallbackPolicy
from sRNAgent.agent.execution import ExecutionBackend, initialize_execution_backend

from session_store import delete_session, migrate_legacy_session, sanitize_chat_id, session_dir, sessions_root
from work_space import get_work_space

logger = logging.getLogger(__name__)

_CHAT_EXECUTIONS: Dict[str, ExecutionBackend] = {}
_CHAT_LOCK = threading.Lock()


def _chat_session_dir(chat_id: str) -> Path:
    return session_dir(chat_id)


def _default_execution_config() -> ExecutionConfig:
    return ExecutionConfig(
        use_notebook=True,
        max_prompts_per_session=10_000,
        storage_dir=sessions_root(),
        strict_kernel_validation=False,
        strict_env_validation=False,
        sandbox_fallback_policy=SandboxFallbackPolicy.WARN_AND_FALLBACK,
        workspace_dir=get_work_space(),
        timeout=EXECUTION_TIMEOUT_SEC,
    )


def _create_chat_execution(project_root: Path, chat_id: str) -> ExecutionBackend:
    chat_id = sanitize_chat_id(chat_id)
    migrate_legacy_session(chat_id)
    session_path = _chat_session_dir(chat_id)
    session_path.mkdir(parents=True, exist_ok=True)
    execution = initialize_execution_backend(
        project_root,
        config=_default_execution_config(),
    )
    executor = execution.notebook_executor
    if executor is not None:
        executor.configure_persistence(session_path)
        if not executor.try_reconnect():
            logger.info("Starting new kernel for chat %s", chat_id)
    return execution


def get_chat_execution(project_root: Path, chat_id: str) -> ExecutionBackend:
    chat_id = sanitize_chat_id(chat_id)
    with _CHAT_LOCK:
        execution = _CHAT_EXECUTIONS.get(chat_id)
        if execution is not None:
            return execution

        execution = _create_chat_execution(project_root, chat_id)
        _CHAT_EXECUTIONS[chat_id] = execution
        return execution


def kernel_is_busy(project_root: Path, chat_id: str) -> bool:
    """True when the chat kernel is executing code or inspect."""
    try:
        chat_id = sanitize_chat_id(chat_id)
    except ValueError:
        return False

    with _CHAT_LOCK:
        execution = _CHAT_EXECUTIONS.get(chat_id)

    if execution is None or execution.notebook_executor is None:
        return False
    return bool(getattr(execution.notebook_executor, "is_busy", lambda: False)())


def interrupt_chat_kernel(project_root: Path, chat_id: str, *, force: bool = False) -> bool:
    """Interrupt the Jupyter kernel for a chat (best-effort).

    By default only interrupts when the kernel is busy executing code.
    Pass force=True for explicit user stop requests.
    """
    try:
        chat_id = sanitize_chat_id(chat_id)
    except ValueError:
        return False

    with _CHAT_LOCK:
        execution = _CHAT_EXECUTIONS.get(chat_id)

    if execution is None:
        if not force:
            logger.debug("Skip kernel interrupt for chat %s (no active executor)", chat_id)
            return False
        connection_file = _chat_session_dir(chat_id) / "kernel.json"
        if not connection_file.exists():
            return False
        try:
            from jupyter_client import KernelManager

            km = KernelManager()
            km.load_connection_file(str(connection_file))
            if not km.is_alive():
                return False
            km.interrupt_kernel()
            logger.info("Interrupted orphan kernel for chat %s", chat_id)
            return True
        except Exception as exc:
            logger.warning("Failed to interrupt orphan kernel for chat %s: %s", chat_id, exc)
            return False

    executor = execution.notebook_executor
    if executor is not None and not force and not getattr(executor, "is_busy", lambda: False)():
        logger.debug("Skip kernel interrupt for chat %s (kernel idle)", chat_id)
        return False

    interrupted = execution.interrupt()
    if interrupted:
        logger.info("Interrupted kernel for chat %s", chat_id)
    return interrupted


def release_chat_kernel(chat_id: str) -> bool:
    chat_id = sanitize_chat_id(chat_id)
    with _CHAT_LOCK:
        execution = _CHAT_EXECUTIONS.pop(chat_id, None)

    if execution is not None:
        executor = execution.notebook_executor
        if executor is not None:
            executor.shutdown(remove_persisted=False)

    session_path = _chat_session_dir(chat_id)
    if not session_path.exists():
        return False

    connection_file = session_path / "kernel.json"
    if connection_file.exists():
        try:
            from jupyter_client import KernelManager

            km = KernelManager()
            km.load_connection_file(str(connection_file))
            if km.is_alive():
                km.shutdown_kernel(now=False, restart=False)
        except Exception as exc:
            logger.debug("Failed to shutdown orphan kernel: %s", exc)

    deleted = delete_session(chat_id)
    if deleted:
        logger.info("Removed session directory for chat %s", chat_id)
    return deleted
