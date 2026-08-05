"""Internal CLI and threading utilities for sRNAgent."""
from __future__ import annotations

import inspect
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, TypeVar

from anndata import AnnData


def _get_mudata_cls():
    try:
        from mudata import MuData  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        return None
    return MuData


def is_mudata(value: Any) -> bool:
    mudata_cls = _get_mudata_cls()
    return bool(mudata_cls is not None and isinstance(value, mudata_cls))


def get_mod_adata(data: Any, mod: str = "srna") -> AnnData:
    if isinstance(data, AnnData):
        return data
    if is_mudata(data):
        if mod not in data.mod:
            raise KeyError(
                f"MuData does not contain modality {mod!r}. "
                f"Available modalities: {list(data.mod.keys())}"
            )
        adata = data.mod[mod]
        if not isinstance(adata, AnnData):
            raise TypeError(f"MuData modality {mod!r} is not an AnnData object.")
        return adata
    raise TypeError("Expected AnnData or MuData input.")


def merge_mod_result(container: Any, result: Any, *, mod: str = "srna", fallback: Optional[AnnData] = None):
    if is_mudata(result):
        return result
    if is_mudata(container):
        if result is None:
            if fallback is not None:
                container.mod[mod] = fallback
            return container
        if not isinstance(result, AnnData):
            raise TypeError(
                "MuData-wrapped tool must return AnnData (or MuData), "
                f"got {type(result).__name__}."
            )
        container.mod[mod] = result
        return container
    return result


def wrap_adata_tool_for_mudata(func: Callable, *, default_mod: str = "srna") -> Callable:
    """Allow AnnData tools to accept MuData by routing through ``mdata.mod[mod]``."""
    if not callable(func) or inspect.isclass(func):
        return func
    try:
        params = list(inspect.signature(func).parameters.values())
    except Exception:
        return func
    if not params:
        return func
    first = params[0]
    if first.name != "adata" or first.kind not in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        return func

    @wraps(func)
    def wrapper(*args, **kwargs):
        mod = str(kwargs.pop("mod", default_mod) or default_mod)
        if args:
            data = args[0]
            if is_mudata(data):
                adata = get_mod_adata(data, mod=mod)
                result = func(adata, *args[1:], **kwargs)
                return merge_mod_result(data, result, mod=mod, fallback=adata)
            return func(*args, **kwargs)
        if "adata" in kwargs and is_mudata(kwargs["adata"]):
            data = kwargs["adata"]
            adata = get_mod_adata(data, mod=mod)
            next_kwargs = dict(kwargs)
            next_kwargs["adata"] = adata
            result = func(**next_kwargs)
            return merge_mod_result(data, result, mod=mod, fallback=adata)
        return func(*args, **kwargs)

    return wrapper


def _watch_download_growth(
    proc: subprocess.Popen[str],
    watch_dir: Path,
    *,
    stall_timeout: int,
    poll_interval: int,
    stop: threading.Event,
    error_box: List[Optional[str]],
) -> None:
    last_sizes: dict[str, int] = {}
    last_growth = time.monotonic()
    saw_file = False

    while not stop.is_set() and proc.poll() is None:
        growth = False
        for path in watch_dir.glob("*.fastq.gz"):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            key = str(path)
            if last_sizes.get(key) != size:
                last_sizes[key] = size
                growth = True
                saw_file = True

        if growth:
            last_growth = time.monotonic()
        elif saw_file and time.monotonic() - last_growth > stall_timeout:
            try:
                proc.kill()
            except Exception:
                pass
            error_box[0] = (
                f"Download stalled: no file growth in {watch_dir} for "
                f"{stall_timeout}s"
            )
            return

        stop.wait(poll_interval)


def run_cli_cmd(
    cmd: Sequence[str],
    env: Optional[dict[str, str]] = None,
    cwd: Optional[str] = None,
    *,
    watch_dir: Optional[str] = None,
    stall_timeout: int = 180,
    stall_poll: int = 30,
) -> None:
    """Run a CLI command, streaming stdout/stderr, raise on failure.

    When *watch_dir* is set, a background watcher kills the process if no
    ``*.fastq.gz`` file grows for *stall_timeout* seconds.
    """
    print(">>", " ".join(shlex.quote(str(c)) for c in cmd), flush=True)
    proc = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
        env=env,
        bufsize=1,
    )
    assert proc.stdout is not None

    stop = threading.Event()
    error_box: List[Optional[str]] = [None]
    watcher: Optional[threading.Thread] = None
    if watch_dir:
        watcher = threading.Thread(
            target=_watch_download_growth,
            args=(proc, Path(watch_dir)),
            kwargs={
                "stall_timeout": stall_timeout,
                "poll_interval": stall_poll,
                "stop": stop,
                "error_box": error_box,
            },
            daemon=True,
        )
        watcher.start()

    try:
        for line in proc.stdout:
            print(line, end="", flush=True)
    finally:
        proc.stdout.close()
        stop.set()
        if watcher is not None:
            watcher.join(timeout=2.0)

    ret = proc.wait()
    if error_box[0]:
        raise RuntimeError(error_box[0])
    if ret != 0:
        raise RuntimeError(f"Command failed with exit code {ret}")


T = TypeVar("T")
R = TypeVar("R")


def run_threads(items: List[T], worker: Callable[[T], R], jobs: int) -> List[R]:
    """Execute *worker* on each *item* across up to *jobs* threads.

    When ``jobs <= 1`` or there is only one item, runs sequentially.

    Prints ``progress: N/M`` after each item finishes so the agent execution
    layer can surface cumulative sample progress (parsed by
    ``_parse_progress_output`` into "已完成 N/M 样本").
    Also prints ``inflight: <names...>`` with the items still running, so the
    UI can show "进行中: SRR1, SRR2, …" alongside the cumulative count.
    """
    n = len(items)
    if n == 0:
        return []
    if jobs is None or jobs <= 1 or n == 1:
        results = []
        for i, item in enumerate(items, start=1):
            results.append(worker(item))
            print(f"progress: {i}/{n}", flush=True)
        return results

    max_workers = max(1, min(jobs, n))
    results: List[Optional[R]] = [None] * n
    done = 0
    count_lock = threading.Lock()
    inflight: List[str] = []
    inflight_lock = threading.Lock()

    def _emit_status() -> None:
        with count_lock:
            d = done
        with inflight_lock:
            names = list(inflight)
        print(f"progress: {d}/{n}", flush=True)
        print(f"inflight: {','.join(names)}", flush=True)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for i, item in enumerate(items):
            name = str(item)
            with inflight_lock:
                inflight.append(name)
            futures[pool.submit(worker, item)] = (i, name)
        _emit_status()
        for fut in as_completed(futures):
            idx, name = futures[fut]
            with inflight_lock:
                if name in inflight:
                    inflight.remove(name)
            results[idx] = fut.result()
            with count_lock:
                done += 1
            _emit_status()
    return [r for r in results if r is not None]
