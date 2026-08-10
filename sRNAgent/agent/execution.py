"""Code execution backend for sRNAgent (notebook-first, omicverse-aligned fallback)."""
from __future__ import annotations

import contextlib
import io
import logging
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .agent_config import ExecutionConfig, SandboxExecutionError, SandboxFallbackPolicy
from .env import RuntimeEnvironment, detect_runtime_environment, validate_expected_environment
from .task_supervisor import TaskProgressSupervisor, active_task_supervisor

logger = logging.getLogger(__name__)


class _StreamingStdout(io.StringIO):
    """Capture in-process output while forwarding it to the UI callback.

    The default UI runs code in-process.  ``redirect_stdout(StringIO())``
    kept the final tool result but hid output from long-running tools until
    they exited, including progress messages from long-running analysis tools.
    """

    def __init__(self, on_stream: Optional[Callable[[str, str], None]] = None):
        super().__init__()
        self._on_stream = on_stream
        self._write_lock = threading.Lock()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._write_lock:
            written = super().write(text)
        if self._on_stream is not None:
            try:
                self._on_stream("stdout", text)
            except Exception:  # Progress delivery must not break analysis code.
                logger.debug("Ignoring stream callback failure", exc_info=True)
        return written

    def getvalue(self) -> str:
        with self._write_lock:
            return super().getvalue()


@dataclass
class ExecutionBackend:
    use_notebook: bool
    runtime: RuntimeEnvironment
    notebook_executor: Any = None
    warnings: List[str] = field(default_factory=list)
    fallback_policy: SandboxFallbackPolicy = SandboxFallbackPolicy.WARN_AND_FALLBACK
    last_notebook_error: Optional[str] = None
    # Shared namespace for in-process fallback execution, so variables
    # (adata, pd, ...) defined in one execute_code call survive the next one,
    # mirroring a persistent notebook kernel.  None = not yet initialized.
    in_process_ns: Optional[Dict[str, Any]] = None
    _ns_lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {
            "use_notebook": self.use_notebook,
            "runtime": self.runtime.to_dict(),
            "warnings": self.warnings,
            "fallback_policy": self.fallback_policy.value,
            "last_notebook_error": self.last_notebook_error,
        }

    def interrupt(self) -> bool:
        if not self.use_notebook or self.notebook_executor is None:
            return False
        interrupt_fn = getattr(self.notebook_executor, "interrupt_execution", None)
        if not callable(interrupt_fn):
            return False
        return bool(interrupt_fn())


def initialize_execution_backend(
    project_root: Path,
    config: Optional[ExecutionConfig] = None,
    *,
    use_notebook: Optional[bool] = None,
    strict_kernel_validation: Optional[bool] = None,
    strict_env_validation: Optional[bool] = None,
    max_prompts_per_session: Optional[int] = None,
    timeout: Optional[int] = None,
    fallback_policy: Optional[SandboxFallbackPolicy] = None,
) -> ExecutionBackend:
    cfg = config or ExecutionConfig()
    if use_notebook is not None:
        cfg.use_notebook = use_notebook
    if strict_kernel_validation is not None:
        cfg.strict_kernel_validation = strict_kernel_validation
    if strict_env_validation is not None:
        cfg.strict_env_validation = strict_env_validation
    if max_prompts_per_session is not None:
        cfg.max_prompts_per_session = max_prompts_per_session
    if timeout is not None:
        cfg.timeout = timeout
    if fallback_policy is not None:
        cfg.sandbox_fallback_policy = fallback_policy

    runtime = detect_runtime_environment()
    warnings = validate_expected_environment(runtime, strict=cfg.strict_env_validation)

    if not cfg.use_notebook:
        logger.info("Using in-process execution (notebook disabled)")
        return ExecutionBackend(
            use_notebook=False,
            runtime=runtime,
            warnings=warnings,
            fallback_policy=cfg.sandbox_fallback_policy,
        )

    try:
        from .session_notebook_executor import SessionNotebookExecutor

        executor = SessionNotebookExecutor(
            project_root=project_root,
            max_prompts_per_session=cfg.max_prompts_per_session,
            storage_dir=cfg.storage_dir,
            keep_notebooks=cfg.keep_notebooks,
            timeout=cfg.timeout,
            strict_kernel_validation=cfg.strict_kernel_validation,
            workspace_dir=cfg.workspace_dir,
        )
        logger.info(
            "Notebook execution enabled (env=%s, kernel=%s)",
            executor.conda_env or "default",
            executor.kernel_name,
        )
        return ExecutionBackend(
            use_notebook=True,
            runtime=runtime,
            notebook_executor=executor,
            warnings=warnings,
            fallback_policy=cfg.sandbox_fallback_policy,
        )
    except Exception as exc:
        msg = f"Notebook execution init failed: {exc}. Falling back to in-process exec()."
        logger.warning(msg)
        warnings = list(warnings) + [msg]
        return ExecutionBackend(
            use_notebook=False,
            runtime=runtime,
            warnings=warnings,
            fallback_policy=cfg.sandbox_fallback_policy,
            last_notebook_error=str(exc),
        )


def _ensure_base_namespace(namespace: Dict[str, Any]) -> None:
    """Inject ``sa`` into *namespace* so agent code always has it, even if a
    previous execute_code call overwrote or deleted it."""
    import sRNAgent as sa  # noqa: WPS433

    namespace.setdefault("__name__", "__srnagent_exec__")
    namespace["sa"] = sa
    namespace["sRNAgent"] = sa


def _execute_in_process(
    code: str,
    project_root: Path,
    namespace: Optional[Dict[str, Any]] = None,
    on_stream: Optional[Callable[[str, str], None]] = None,
    supervisor: Optional[TaskProgressSupervisor] = None,
) -> str:
    project_root_str = str(project_root.resolve())
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)

    if namespace is None:
        namespace = {"__name__": "__srnagent_exec__"}
    _ensure_base_namespace(namespace)

    stdout = _StreamingStdout(on_stream)
    try:
        with contextlib.redirect_stdout(stdout), active_task_supervisor(supervisor):
            exec(code, namespace, namespace)
        output = stdout.getvalue().strip()
        return output or "Code executed successfully (no stdout)."
    except Exception:
        err = traceback.format_exc()
        partial = stdout.getvalue().strip()
        return f"{partial}\n\n{err}" if partial else err


def _run_in_process(
    backend: ExecutionBackend,
    code: str,
    project_root: Path,
    on_stream: Optional[Callable[[str, str], None]] = None,
    supervisor: Optional[TaskProgressSupervisor] = None,
) -> str:
    """Execute *code* against the backend's shared in-process namespace.

    The namespace persists across calls (like a notebook kernel), so state
    defined by earlier agent steps is available in later ones.
    """
    if backend.in_process_ns is None:
        backend.in_process_ns = {}
    with backend._ns_lock:
        return _execute_in_process(
            code, project_root, backend.in_process_ns, on_stream, supervisor,
        )


def _format_notebook_result(result: dict) -> str:
    if result.get("error"):
        parts = []
        if result.get("stdout"):
            parts.append(result["stdout"])
        if result.get("stderr"):
            parts.append(result["stderr"])
        parts.append(result["error"])
        return "\n\n".join(part for part in parts if part)
    parts = [result.get("stdout") or "", result.get("stderr") or ""]
    text = "\n".join(part for part in parts if part).strip()
    return text or "Code executed successfully in notebook kernel (no stdout)."


def _handle_notebook_failure(
    backend: ExecutionBackend,
    exc: Exception,
    code: str,
    project_root: Path,
    on_stream: Optional[Callable[[str, str], None]] = None,
    supervisor: Optional[TaskProgressSupervisor] = None,
) -> str:
    """Return a notebook failure without replaying code in the UI process.

    A notebook exception often means that code made partial state changes.
    Re-executing it in the HTTP server both duplicates work and can move large
    AnnData/pandas operations into a long-lived multithreaded process.
    """
    backend.last_notebook_error = str(exc)
    policy = backend.fallback_policy

    if policy == SandboxFallbackPolicy.RAISE:
        raise SandboxExecutionError(
            f"Notebook execution failed and fallback is disabled: {exc}"
        ) from exc

    if policy == SandboxFallbackPolicy.WARN_AND_FALLBACK:
        return (
            f"NOTEBOOK_EXECUTION_ERROR: {exc}\n"
            "The code was not replayed in the UI process. Inspect the kernel or persisted outputs before retrying."
        )
    return f"NOTEBOOK_EXECUTION_ERROR: {exc}"


def execute_agent_code(
    backend: ExecutionBackend,
    code: Any,
    project_root: Path,
    on_stream: Optional[Callable[[str, str], None]] = None,
    supervisor: Optional[TaskProgressSupervisor] = None,
) -> str:
    # A few OpenAI-compatible providers serialize a text tool field as
    # {"$text": "..."}.  Treat that as its string value rather than letting an
    # AttributeError terminate the whole agent worker.  Other malformed values
    # become a normal tool result, so the model can correct its next call.
    if isinstance(code, dict):
        for key in ("$text", "text"):
            candidate = code.get(key)
            if isinstance(candidate, str):
                code = candidate
                break
        else:
            return "TOOL_INPUT_ERROR: execute_code.code must be a string."
    elif code is None:
        code = ""
    elif not isinstance(code, str):
        return "TOOL_INPUT_ERROR: execute_code.code must be a string."

    code = code.strip()
    if not code:
        return "No code provided."

    if backend.use_notebook and backend.notebook_executor is not None:
        try:
            result = backend.notebook_executor.execute_code(code, on_stream=on_stream)
            if result.get("error"):
                if backend.fallback_policy == SandboxFallbackPolicy.RAISE:
                    raise SandboxExecutionError(result["error"])
                return _format_notebook_result(result)
            return _format_notebook_result(result)
        except SandboxExecutionError:
            raise
        except Exception as exc:
            return _handle_notebook_failure(
                backend, exc, code, project_root, on_stream, supervisor,
            )

    return _run_in_process(backend, code, project_root, on_stream, supervisor)
