"""Session-based Jupyter notebook executor for sRNAgent (omicverse-aligned)."""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import warnings
from datetime import datetime
from pathlib import Path
from queue import Empty
from typing import Any, Dict, List, Optional

from .agent_config import EXECUTION_TIMEOUT_SEC
from .env import detect_conda_environment, get_kernel_name

logger = logging.getLogger(__name__)


class KernelNotFoundError(Exception):
    """Raised when the required Jupyter kernel is unavailable."""


class KernelBusyError(Exception):
    """Raised when the kernel is already executing code."""


class SessionNotebookExecutor:
    """Persistent Jupyter kernel session for Agent code execution."""

    def __init__(
        self,
        *,
        project_root: Path,
        max_prompts_per_session: int = 5,
        storage_dir: Optional[Path] = None,
        keep_notebooks: bool = True,
        timeout: int = EXECUTION_TIMEOUT_SEC,
        strict_kernel_validation: bool = False,
        workspace_dir: Optional[Path] = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.workspace_dir = (workspace_dir or Path.cwd()).resolve()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.max_prompts_per_session = max_prompts_per_session
        self.storage_dir = storage_dir or (Path.home() / ".srnagent" / "sessions")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.keep_notebooks = keep_notebooks
        self.timeout = timeout
        self.strict_kernel_validation = strict_kernel_validation

        self.conda_env = detect_conda_environment()
        self.kernel_name = get_kernel_name(self.conda_env)

        self.current_session: Optional[Dict[str, Any]] = None
        self.session_prompt_count = 0
        self.figure_history: List[Dict[str, Any]] = []
        self._max_figure_history = 24
        self.connection_file: Optional[Path] = None
        self.meta_file: Optional[Path] = None
        self.replay_file: Optional[Path] = None
        self.persist_connection = False
        self._replaying = False
        self._execute_lock = threading.RLock()
        self._kernel_busy = threading.Event()

        self._ensure_kernel_installed(strict=self.strict_kernel_validation)

    def _session_init_code(self) -> str:
        project_root = str(self.project_root)
        workspace = str(self.workspace_dir)
        return f"""
import os
import sys
from pathlib import Path
_root = Path({project_root!r})
_workspace = Path({workspace!r})
_workspace.mkdir(parents=True, exist_ok=True)
os.chdir(_workspace)
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import sRNAgent as sa
try:
    import matplotlib
    matplotlib.use("Agg")
except Exception:
    pass
print("✓ sRNAgent session initialized")
print(f"  python: {{sys.executable}}")
print(f"  project_root: {{_root}}")
print(f"  workspace: {{os.getcwd()}}")
"""

    def _ensure_workspace_cwd(self) -> None:
        if not self.current_session or self.is_busy():
            return
        kc = self.current_session.get("kernel_client")
        if kc is None:
            return
        workspace = str(self.workspace_dir)
        snippet = f"import os; os.chdir({workspace!r})"
        acquired = self._execute_lock.acquire(blocking=False)
        if not acquired:
            return
        try:
            self._kernel_busy.set()
            self._execute_code_in_kernel(snippet, kc, auto_recover=False)
        except Exception as exc:
            logger.debug("Failed to set kernel workspace cwd: %s", exc)
        finally:
            self._kernel_busy.clear()
            self._execute_lock.release()

    _INSPECT_VARIABLES_CODE = """
import json
_skip = {
    "In", "Out", "get_ipython", "exit", "quit", "sa", "sRNAgent",
    "_root", "sys", "Path", "json", "_skip", "_describe", "builtins",
}
_result = []
def _describe(name, val):
    mod = getattr(type(val), "__module__", "") or ""
    typ = type(val).__name__
    detail = ""
    preview = ""
    try:
        cls_name = f"{mod}.{typ}" if mod and not mod.startswith("builtins") else typ
        if typ == "AnnData" or cls_name.endswith("AnnData"):
            detail = f"{val.n_obs} obs × {val.n_vars} vars"
            layers = list(getattr(val, "layers", {}).keys())
            if layers:
                detail += f" | layers: {', '.join(layers[:4])}"
        elif hasattr(val, "shape"):
            detail = " × ".join(str(x) for x in val.shape)
        elif isinstance(val, bytes):
            n = len(val)
            detail = f"{n:,} bytes"
            if n == 0:
                preview = "empty"
            elif val[:2] == bytes([0x1F, 0x8B]):
                preview = "gzip compressed"
            elif val[:2] == b"PK":
                preview = "zip archive"
            else:
                try:
                    text = val.decode("utf-8")
                    printable = sum(1 for c in text[:200] if c.isprintable() or c in "\\n\\r\\t")
                    if printable >= max(1, min(len(text), 200) * 0.85):
                        preview = text[:80] + ("…" if len(text) > 80 else "")
                    else:
                        raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary")
                except UnicodeDecodeError:
                    head = val[:12].hex(" ")
                    preview = f"hex {head}{' …' if n > 12 else ''}"
        elif isinstance(val, str):
            preview = val[:80] + ("…" if len(val) > 80 else "")
        elif isinstance(val, (int, float, bool)):
            preview = repr(val)
        elif isinstance(val, (list, tuple, set, dict)):
            detail = f"len={len(val)}"
        elif hasattr(val, "__len__"):
            try:
                detail = f"len={len(val)}"
            except Exception:
                pass
    except Exception as exc:
        detail = f"({exc})"
    return {"name": name, "type": typ, "module": mod.split(".")[0], "detail": detail, "preview": preview}
for _name, _val in sorted(globals().items()):
    if _name.startswith("_") or _name in _skip:
        continue
    _info = _describe(_name, _val)
    if _info["module"] == "builtins" and _info["type"] in {"function", "module", "type"}:
        continue
    _result.append(_info)
print("__SRNAGENT_ENV__" + json.dumps(_result, ensure_ascii=False))
"""

    def _ensure_kernel_installed(self, strict: bool = True) -> bool:
        try:
            from jupyter_client.kernelspec import KernelSpecManager
        except ImportError as exc:
            raise KernelNotFoundError(
                "jupyter_client 未安装。请运行: conda install jupyter ipykernel"
            ) from exc

        ksm = KernelSpecManager()
        available = ksm.find_kernel_specs()
        candidates = [
            f"conda-env-{self.conda_env}-py" if self.conda_env else None,
            self.conda_env,
            "python3",
        ]
        for name in candidates:
            if name and name in available:
                self.kernel_name = name
                logger.info("Using kernel %s (conda env: %s)", name, self.conda_env or "default")
                return True

        if strict:
            available_list = "\n".join(f"  - {k}" for k in available.keys())
            raise KernelNotFoundError(
                f"未找到 conda 环境 '{self.conda_env}' 对应的 Jupyter kernel。\n\n"
                f"可用 kernels:\n{available_list}\n\n"
                f"修复: python -m ipykernel install --user --name {self.conda_env or 'srnagent'}"
            )

        self.kernel_name = "python3"
        warnings.warn(
            f"⚠ 未找到 conda env '{self.conda_env}' 的 kernel，回退到 python3 "
            f"(可能使用不同环境!)"
        )
        return False

    def _wait_for_kernel_ready(self, kc: Any, timeout: int = 30) -> None:
        start = time.time()
        while time.time() - start < timeout:
            try:
                kc.kernel_info()
                reply = kc.get_shell_msg(timeout=2.0)
                if reply.get("msg_type") == "kernel_info_reply":
                    return
            except Empty:
                time.sleep(0.5)
            except Exception:
                time.sleep(0.5)
        raise TimeoutError(f"Kernel not ready within {timeout}s")

    def is_busy(self) -> bool:
        """True while Agent code (or inspect) holds the kernel execution lock."""
        return self._kernel_busy.is_set()

    def _is_kernel_alive(self) -> bool:
        """Lightweight liveness check — never probe shell/iopub (avoids ZMQ desync)."""
        if not self.current_session:
            return False
        km = self.current_session["kernel_manager"]
        try:
            return bool(km.is_alive())
        except Exception:
            return False

    def interrupt_execution(self) -> bool:
        """Send SIGINT to the active Jupyter kernel (stops running Python/subprocesses)."""
        if not self.current_session or not self.is_busy():
            logger.debug("Skip kernel interrupt (idle)")
            return False
        self._interrupt_kernel(reason="cancel")
        return True

    def _drain_iopub(self, kc: Any, *, max_messages: int = 100) -> None:
        """Discard stale iopub messages after interrupt/recovery."""
        for _ in range(max_messages):
            try:
                kc.get_iopub_msg(timeout=0.05)
            except Empty:
                break
            except Exception:
                break

    def _interrupt_kernel(self, *, reason: str = "manual") -> None:
        if not self.current_session:
            return
        km = self.current_session["kernel_manager"]
        kc = self.current_session["kernel_client"]
        log_fn = logger.warning if reason == "recovery" else logger.info
        log_fn("Interrupting kernel (%s)...", reason)
        try:
            km.interrupt_kernel()
            time.sleep(0.5)
            self._drain_iopub(kc)
        except Exception as exc:
            logger.warning("Failed to interrupt kernel (%s): %s", reason, exc)

    def _restart_kernel(self) -> None:
        if not self.current_session:
            return

        km = self.current_session["kernel_manager"]
        old_kc = self.current_session["kernel_client"]

        logger.info("Restarting kernel...")
        try:
            old_kc.stop_channels()
        except Exception as exc:
            logger.debug("Failed to stop old kernel channels: %s", exc)

        km.restart_kernel(now=True)
        kc = km.client()
        kc.start_channels()
        self.current_session["kernel_client"] = kc
        self._wait_for_kernel_ready(kc)

        init_outputs = self._execute_code_in_kernel(self._session_init_code(), kc, auto_recover=False)
        if init_outputs.get("stdout"):
            print("".join(init_outputs["stdout"]))

    def _shutdown_kernel(self) -> None:
        if not self.current_session:
            return

        km = self.current_session["kernel_manager"]
        kc = self.current_session["kernel_client"]
        try:
            kc.stop_channels()
            km.shutdown_kernel(now=False, restart=False)
        except Exception as exc:
            logger.debug("Kernel shutdown warning: %s", exc)
            try:
                km.shutdown_kernel(now=True, restart=False)
            except Exception:
                pass

    def _recover_from_kernel_failure(self) -> bool:
        if not self.current_session:
            return False

        km = self.current_session["kernel_manager"]
        if km.is_alive():
            logger.warning("Kernel unresponsive, attempting interrupt...")
            self._interrupt_kernel(reason="recovery")
            time.sleep(1)
            if self._is_kernel_alive():
                logger.info("Kernel recovered via interrupt")
                return True

        logger.warning("Kernel dead, attempting restart...")
        try:
            self._restart_kernel()
            logger.info("Kernel recovered via restart")
            return True
        except Exception as exc:
            logger.warning("Kernel recovery failed: %s; creating new session", exc)
            self.shutdown()
            self._start_new_session()
            return True

    def _should_start_new_session(self) -> bool:
        if self.current_session is None:
            return True

        if self.session_prompt_count >= self.max_prompts_per_session:
            logger.info("Session limit reached (%d prompts)", self.max_prompts_per_session)
            self.shutdown()
            return True

        if not self._is_kernel_alive():
            logger.warning("Kernel crashed, restarting session...")
            self.shutdown()
            return True

        return False

    def _save_session_meta(self) -> None:
        if not self.meta_file:
            return
        payload = {
            "session_prompt_count": self.session_prompt_count,
            "figure_history": self.figure_history,
        }
        try:
            self.meta_file.parent.mkdir(parents=True, exist_ok=True)
            self.meta_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.debug("Failed to save session meta: %s", exc)

    def _load_session_meta(self) -> None:
        if not self.meta_file or not self.meta_file.exists():
            return
        try:
            payload = json.loads(self.meta_file.read_text(encoding="utf-8"))
            self.session_prompt_count = int(payload.get("session_prompt_count") or 0)
            figures = payload.get("figure_history")
            if isinstance(figures, list):
                self.figure_history = figures[-self._max_figure_history :]
        except Exception as exc:
            logger.debug("Failed to load session meta: %s", exc)

    def _save_connection_file(self) -> None:
        if not self.persist_connection or not self.connection_file or not self.current_session:
            return
        km = self.current_session["kernel_manager"]
        try:
            self.connection_file.parent.mkdir(parents=True, exist_ok=True)
            src = getattr(km, "connection_file", None)
            if src and Path(src).exists():
                shutil.copy2(src, self.connection_file)
            self._save_session_meta()
        except Exception as exc:
            logger.debug("Failed to save kernel connection: %s", exc)

    def _remove_persisted_files(self) -> None:
        session_dir = None
        if self.connection_file:
            session_dir = self.connection_file.parent
        elif self.meta_file:
            session_dir = self.meta_file.parent
        self.connection_file = None
        self.meta_file = None
        self.replay_file = None
        if session_dir and session_dir.exists():
            try:
                shutil.rmtree(session_dir)
            except Exception as exc:
                logger.debug("Failed to remove session dir %s: %s", session_dir, exc)

    def configure_persistence(self, session_dir: Path) -> None:
        session_dir = session_dir.resolve()
        session_dir.mkdir(parents=True, exist_ok=True)
        self.persist_connection = True
        self.connection_file = session_dir / "kernel.json"
        self.meta_file = session_dir / "meta.json"
        self.replay_file = session_dir / "replay.py"

    def _append_replay(self, code: str) -> None:
        if not self.replay_file or self._replaying:
            return
        snippet = str(code or "").strip()
        if not snippet or snippet == self._INSPECT_VARIABLES_CODE.strip():
            return
        if snippet == self._session_init_code().strip():
            return
        try:
            with self.replay_file.open("a", encoding="utf-8") as handle:
                handle.write("\n\n# --- replay chunk ---\n")
                handle.write(snippet)
                if not snippet.endswith("\n"):
                    handle.write("\n")
        except Exception as exc:
            logger.debug("Failed to append replay code: %s", exc)

    def _run_replay_if_needed(self) -> None:
        if not self.replay_file or not self.replay_file.exists() or not self.current_session:
            return
        code = self.replay_file.read_text(encoding="utf-8").strip()
        if not code:
            return
        kc = self.current_session["kernel_client"]
        self._replaying = True
        try:
            logger.info("Replaying persisted code from %s", self.replay_file)
            outputs = self._execute_code_in_kernel(code, kc, auto_recover=False)
            if outputs.get("stdout"):
                print("".join(outputs["stdout"]))
            if outputs.get("errors"):
                err = outputs["errors"][0]
                logger.warning(
                    "Replay produced error: %s: %s",
                    err.get("ename"),
                    err.get("evalue"),
                )
        finally:
            self._replaying = False

    def try_reconnect(self) -> bool:
        if not self.connection_file or not self.connection_file.exists():
            return False

        from jupyter_client import KernelManager

        try:
            km = KernelManager(kernel_name=self.kernel_name)
            km.load_connection_file(str(self.connection_file))
            if not km.is_alive():
                logger.info("Saved kernel is not alive: %s", self.connection_file)
                return False

            kc = km.client()
            kc.start_channels()
            try:
                self._wait_for_kernel_ready(kc, timeout=8)
            except TimeoutError:
                logger.warning("Kernel reconnect timed out: %s", self.connection_file)
                try:
                    kc.stop_channels()
                except Exception:
                    pass
                return False

            session_dir = self.connection_file.parent
            self.current_session = {
                "session_id": session_dir.name,
                "session_dir": session_dir,
                "kernel_manager": km,
                "kernel_client": kc,
            }
            km.cleanup_resources = False
            self._load_session_meta()
            self._ensure_workspace_cwd()
            logger.info("Reconnected to notebook session %s", session_dir.name)
            return True
        except Exception as exc:
            logger.warning("Kernel reconnect failed: %s", exc)
            return False

    def _start_new_session(self, session_dir: Optional[Path] = None) -> None:
        from jupyter_client import KernelManager

        if session_dir is None:
            session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            session_dir = self.storage_dir / f"session_{session_id}"
        session_dir.mkdir(parents=True, exist_ok=True)

        km = KernelManager(kernel_name=self.kernel_name)
        km.cwd = str(self.workspace_dir)
        km.start_kernel(start_new_session=True)
        km.cleanup_resources = False
        kc = km.client()
        kc.start_channels()
        self._wait_for_kernel_ready(kc)

        init_outputs = self._execute_code_in_kernel(self._session_init_code(), kc, auto_recover=False)
        if init_outputs.get("stdout"):
            print("".join(init_outputs["stdout"]))

        self.current_session = {
            "session_id": session_dir.name,
            "session_dir": session_dir,
            "kernel_manager": km,
            "kernel_client": kc,
        }
        self.session_prompt_count = 0
        self._save_connection_file()
        self._run_replay_if_needed()
        logger.info("Started notebook session %s with kernel %s", session_dir.name, self.kernel_name)

    def _execute_code_in_kernel(
        self,
        code: str,
        kc: Any,
        *,
        auto_recover: bool = True,
        on_stream: Optional[Any] = None,
    ) -> Dict[str, List[Any]]:
        outputs: Dict[str, List[Any]] = {
            "stdout": [],
            "stderr": [],
            "errors": [],
            "images": [],
            "display_text": [],
        }

        start_time = time.time()
        timeout_attempt = 0
        max_timeout_retries = 2
        channel_attempt = 0
        max_channel_recoveries = 2

        def _restart_after_channel_failure(reason: Exception) -> bool:
            nonlocal kc, outputs, start_time, channel_attempt
            if not auto_recover or channel_attempt >= max_channel_recoveries:
                return False
            logger.warning(
                "Kernel channel failure: %s. Recovery %d/%d",
                reason,
                channel_attempt + 1,
                max_channel_recoveries,
            )
            if not self._recover_from_kernel_failure():
                return False
            channel_attempt += 1
            kc = self.current_session["kernel_client"]
            outputs = {"stdout": [], "stderr": [], "errors": [], "images": [], "display_text": []}
            start_time = time.time()
            return True

        try:
            msg_id = kc.execute(code, silent=False)
        except Exception as exc:
            if _restart_after_channel_failure(exc):
                msg_id = kc.execute(code, silent=False)
            else:
                raise

        while True:
            if time.time() - start_time > self.timeout:
                if auto_recover and timeout_attempt < max_timeout_retries:
                    logger.warning(
                        "Timeout after %ss, attempting recovery (%d/%d)",
                        self.timeout,
                        timeout_attempt + 1,
                        max_timeout_retries,
                    )
                    if self._recover_from_kernel_failure():
                        timeout_attempt += 1
                        kc = self.current_session["kernel_client"]
                        start_time = time.time()
                        msg_id = kc.execute(code, silent=False)
                        outputs = {"stdout": [], "stderr": [], "errors": [], "images": [], "display_text": []}
                        continue
                    raise TimeoutError(
                        f"Execution exceeded {self.timeout}s and recovery failed"
                    )
                raise TimeoutError(f"Execution exceeded {self.timeout}s")

            try:
                msg = kc.get_iopub_msg(timeout=1.0)
            except Empty:
                continue
            except Exception as exc:
                if _restart_after_channel_failure(exc):
                    msg_id = kc.execute(code, silent=False)
                    continue
                raise

            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue

            msg_type = msg["msg_type"]
            content = msg["content"]

            if msg_type == "stream":
                key = "stdout" if content.get("name") == "stdout" else "stderr"
                text = content.get("text", "")
                outputs[key].append(text)
                if on_stream and text:
                    on_stream(key, text)
            elif msg_type == "error":
                outputs["errors"].append(content)
            elif msg_type in ("display_data", "execute_result"):
                data = content.get("data") or {}
                if "image/png" in data:
                    outputs["images"].append(
                        {"format": "png", "data": data["image/png"]}
                    )
                elif "image/jpeg" in data:
                    outputs["images"].append(
                        {"format": "jpeg", "data": data["image/jpeg"]}
                    )
                text_plain = data.get("text/plain")
                if text_plain:
                    outputs["display_text"].append(
                        text_plain if isinstance(text_plain, str) else "".join(text_plain)
                    )
            elif msg_type == "status" and content.get("execution_state") == "idle":
                break

        return outputs

    def _record_figures(self, images: List[Dict[str, Any]]) -> None:
        if not images:
            return
        stamp = datetime.now().isoformat(timespec="seconds")
        for image in images:
            entry = {
                "format": image.get("format") or "png",
                "data": image.get("data") or "",
                "timestamp": stamp,
            }
            if entry["data"]:
                self.figure_history.append(entry)
        if len(self.figure_history) > self._max_figure_history:
            self.figure_history = self.figure_history[-self._max_figure_history :]
        self._save_session_meta()

    def ensure_session(self) -> bool:
        if self.use_notebook_ready():
            return True
        if self.connection_file and self.connection_file.exists() and self.try_reconnect():
            return True
        if self.current_session is None:
            session_dir = self.connection_file.parent if self.connection_file else None
            self._start_new_session(session_dir=session_dir)
        return self.use_notebook_ready()

    def inspect_variables(self, *, wait: bool = False) -> List[Dict[str, Any]]:
        if self.is_busy():
            raise KernelBusyError("Kernel is executing code")
        if not self.use_notebook_ready():
            return []
        acquired = self._execute_lock.acquire(blocking=wait)
        if not acquired:
            raise KernelBusyError("Kernel is executing code")
        self._kernel_busy.set()
        try:
            if not self._is_kernel_alive():
                return []
            return self._inspect_variables_unlocked()
        finally:
            self._kernel_busy.clear()
            self._execute_lock.release()

    def _inspect_variables_unlocked(self) -> List[Dict[str, Any]]:
        assert self.current_session is not None
        kc = self.current_session["kernel_client"]
        outputs = self._execute_code_in_kernel(
            self._INSPECT_VARIABLES_CODE,
            kc,
            auto_recover=False,
        )
        stdout = "".join(outputs.get("stdout", []))
        marker = "__SRNAGENT_ENV__"
        idx = stdout.rfind(marker)
        if idx < 0:
            return []
        payload = stdout[idx + len(marker) :].strip()
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            return []
        return []

    def get_figures(self) -> List[Dict[str, Any]]:
        return list(self.figure_history)

    def use_notebook_ready(self) -> bool:
        return self.current_session is not None and self._is_kernel_alive()

    def execute_code(
        self,
        code: str,
        on_stream: Optional[Any] = None,
        *,
        count_prompt: bool = True,
    ) -> Dict[str, Optional[str]]:
        with self._execute_lock:
            self._kernel_busy.set()
            try:
                return self._execute_code_unlocked(
                    code, on_stream=on_stream, count_prompt=count_prompt
                )
            finally:
                self._kernel_busy.clear()

    def _execute_code_unlocked(
        self,
        code: str,
        on_stream: Optional[Any] = None,
        *,
        count_prompt: bool = True,
    ) -> Dict[str, Optional[str]]:
        if self._should_start_new_session():
            session_dir = self.connection_file.parent if self.connection_file else None
            self._start_new_session(session_dir=session_dir)

        assert self.current_session is not None
        kc = self.current_session["kernel_client"]
        outputs = self._execute_code_in_kernel(code, kc, auto_recover=True, on_stream=on_stream)
        if count_prompt:
            self.session_prompt_count += 1
            self._save_session_meta()

        self._record_figures(outputs.get("images") or [])

        stdout = "".join(outputs.get("stdout", []))
        stderr = "".join(outputs.get("stderr", []))
        error = None
        if outputs.get("errors"):
            err = outputs["errors"][0]
            tb = "\n".join(err.get("traceback") or [])
            error = f"{err.get('ename')}: {err.get('evalue')}\n{tb}"

        if not error:
            self._append_replay(code)

        return {"stdout": stdout, "stderr": stderr, "error": error}

    def shutdown(self, *, remove_persisted: bool = False) -> None:
        self._shutdown_kernel()
        self.current_session = None
        self.session_prompt_count = 0
        self.figure_history = []
        if remove_persisted:
            self._remove_persisted_files()
        self.persist_connection = False
