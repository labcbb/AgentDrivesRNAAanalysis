"""Generic runtime telemetry for long-running agent code.

The supervisor deliberately relies on common execution signals instead of a
tool-specific progress implementation: subprocess lifecycle, stdout, output
directories, file growth, and sample-named artifacts.  Tools can still emit a
precise ``progress: N/M`` marker, but they do not need bespoke UI support.
"""
from __future__ import annotations

import ast
import contextlib
import contextvars
import os
import re
import signal
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional


_ACTIVE_SUPERVISOR: contextvars.ContextVar[Optional["TaskProgressSupervisor"]] = (
    contextvars.ContextVar("srnagent_active_task_supervisor", default=None)
)
_OUTPUT_KEYS = frozenset({"output_dir", "out_dir", "output", "out", "dest", "destination"})
_PATH_CALLS = frozenset({"Path", "PurePath"})
_OUTPUT_VARIABLE_RE = re.compile(r"(?:out|output|result|artifact|mirtop|gff|stage|work|dir|root)", re.I)
_SAMPLE_ARTIFACT_RE = re.compile(r"(?:SRR|ERR|DRR)\d+", re.I)


@contextlib.contextmanager
def active_task_supervisor(supervisor: Optional["TaskProgressSupervisor"]):
    """Expose one supervisor to CLI helpers running inside agent code."""
    token = _ACTIVE_SUPERVISOR.set(supervisor)
    try:
        yield
    finally:
        _ACTIVE_SUPERVISOR.reset(token)


def report_subprocess_started(command: Iterable[str], pid: int) -> None:
    supervisor = _ACTIVE_SUPERVISOR.get()
    if supervisor is not None:
        supervisor.record_process(command, pid)


def report_subprocess_finished(pid: int, returncode: int) -> None:
    supervisor = _ACTIVE_SUPERVISOR.get()
    if supervisor is not None:
        supervisor.finish_process(pid, returncode)


def _literal_path(node: ast.AST, names: Dict[str, str]) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _PATH_CALLS:
        if node.args:
            return _literal_path(node.args[0], names)
    return None


def _infer_output_dirs(code: str, workspace: Optional[Path]) -> list[Path]:
    if workspace is None:
        return []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    names: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = _literal_path(node.value, names) if node.value is not None else None
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value:
                for target in targets:
                    if isinstance(target, ast.Name):
                        names[target.id] = value

    raw_dirs: list[str] = []
    # Long-running code often assigns an output directory to a variable and
    # passes it through helper functions instead of a literal ``output_dir=``
    # keyword. Track meaningful path variables as well, e.g. mirtop_root.
    raw_dirs.extend(
        value for name, value in names.items()
        if _OUTPUT_VARIABLE_RE.search(name)
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg in _OUTPUT_KEYS:
                value = _literal_path(keyword.value, names)
                if value:
                    raw_dirs.append(value)
    # Support direct CLI arguments in code such as ``[tool, '--out', 'result']``.
    raw_dirs.extend(re.findall(r"--(?:out|output(?:-dir)?)\s+['\"]?([^\s,'\"]+)", code))

    root = workspace.resolve()
    resolved: list[Path] = []
    for raw in raw_dirs:
        candidate = Path(raw).expanduser()
        candidate = candidate if candidate.is_absolute() else root / candidate
        try:
            candidate = candidate.resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            continue
        if candidate not in resolved:
            resolved.append(candidate)
    return resolved


def _infer_adata_names(code: str) -> list[str]:
    """Return likely AnnData variable names passed to analysis calls."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name) and "adata" in first.id.lower() and first.id not in names:
            names.append(first.id)
    return names


class TaskProgressSupervisor:
    """Observe a code task using process and artifact signals.

    It never declares a task complete.  Its snapshots only describe evidence
    observed while the execution thread is still alive.
    """

    def __init__(
        self,
        *,
        workspace: Optional[Path],
        code: str,
        namespace_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    ) -> None:
        self.workspace = workspace.resolve() if workspace is not None else None
        self.output_dirs = _infer_output_dirs(code, self.workspace)
        self.adata_names = _infer_adata_names(code)
        self.namespace_provider = namespace_provider
        self._lock = threading.Lock()
        self._processes: Dict[int, Dict[str, Any]] = {}
        self._last_files: Dict[Path, tuple[int, int]] = {}

    def record_process(self, command: Iterable[str], pid: int) -> None:
        command_text = " ".join(str(part) for part in command)
        with self._lock:
            self._processes[int(pid)] = {"command": command_text, "returncode": None}

    def finish_process(self, pid: int, returncode: int) -> None:
        with self._lock:
            entry = self._processes.get(int(pid))
            if entry is not None:
                entry["returncode"] = int(returncode)

    def terminate_active_processes(self) -> list[int]:
        """Best-effort termination for CLIs spawned by the current code cell."""
        with self._lock:
            active_pids = [
                pid for pid, entry in self._processes.items()
                if entry.get("returncode") is None
            ]
        terminated: list[int] = []
        for pid in active_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
            terminated.append(pid)
        return terminated

    def _sample_names(self) -> list[str]:
        if self.namespace_provider is None:
            return []
        namespace = self.namespace_provider() or {}
        for name in self.adata_names:
            value = namespace.get(name)
            obs_names = getattr(value, "obs_names", None)
            if obs_names is not None:
                names = [str(item) for item in obs_names]
                if names:
                    return names
        return []

    def _files(self) -> list[Path]:
        files: list[Path] = []
        for output_dir in self.output_dirs:
            if not output_dir.is_dir():
                continue
            try:
                files.extend(path for path in output_dir.rglob("*") if path.is_file())
            except OSError:
                continue
            if len(files) >= 10_000:
                break
        return files[:10_000]

    def snapshot(self) -> Dict[str, Any]:
        files = self._files()
        changed: list[Path] = []
        for path in files:
            try:
                state = (path.stat().st_size, path.stat().st_mtime_ns)
            except OSError:
                continue
            if self._last_files.get(path) != state:
                changed.append(path)
            self._last_files[path] = state

        samples = self._sample_names()
        completed: set[str] = set()
        for sample in samples:
            if any(sample in path.name for path in files):
                completed.add(sample)

        # Some parallel cells create their own sample list and do not pass an
        # AnnData object to the code being supervised. Infer completed samples
        # only from final GFF-like artifacts, not their log directories.
        sample_gffs = {
            match.group(0).upper()
            for path in files
            if path.suffix.lower() in {".gff", ".gff3", ".gft"}
            for match in [_SAMPLE_ARTIFACT_RE.search(path.name)]
            if match is not None
        }

        with self._lock:
            active = [entry for entry in self._processes.values() if entry.get("returncode") is None]

        output_label = ", ".join(
            str(path.relative_to(self.workspace)) if self.workspace is not None else str(path)
            for path in self.output_dirs[:2]
        )
        highlights: list[str] = []
        if output_label:
            highlights.append(f"监测产物目录: {output_label}")
        if changed:
            latest = changed[-1]
            highlights.append(f"最近写入: {latest.name}")
        if active:
            command = str(active[-1].get("command") or "")
            highlights.append(f"运行中进程: {command[:110]}")
        if sample_gffs:
            highlights.append(f"已发现 {len(sample_gffs)} 个样本级 GFF")

        if samples and completed:
            done, total = len(completed), len(samples)
            stage = (
                f"样本文件已就绪 {done}/{total}，正在汇总结果"
                if done >= total
                else f"已完成 {done}/{total} 样本"
            )
            return {
                "stage": stage,
                "highlights": highlights,
                "detail": stage,
                "activeProcess": bool(active),
                "recentArtifactChange": bool(changed),
            }
        if sample_gffs:
            stage = f"已完成 {len(sample_gffs)} 个样本级 GFF，剩余任务仍在运行"
            return {
                "stage": stage,
                "highlights": highlights,
                "detail": stage,
                "activeProcess": bool(active),
                "recentArtifactChange": bool(changed),
            }
        if files:
            stage = f"已发现 {len(files)} 个产物，任务仍在运行"
            return {
                "stage": stage,
                "highlights": highlights,
                "detail": stage,
                "activeProcess": bool(active),
                "recentArtifactChange": bool(changed),
            }
        if active:
            stage = "外部进程正在运行，等待首个产物"
            return {
                "stage": stage,
                "highlights": highlights,
                "detail": stage,
                "activeProcess": True,
                "recentArtifactChange": False,
            }
        stage = "任务正在运行，等待首个可追踪产物"
        return {
            "stage": stage,
            "highlights": highlights,
            "detail": stage,
            "activeProcess": False,
            "recentArtifactChange": False,
        }
