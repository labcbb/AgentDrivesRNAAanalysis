"""Internal CLI and threading utilities for sRNAgent."""
from __future__ import annotations

import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List, Optional, Sequence, TypeVar


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
    """
    n = len(items)
    if n == 0:
        return []
    if jobs is None or jobs <= 1 or n == 1:
        return [worker(item) for item in items]

    max_workers = max(1, min(jobs, n))
    results: List[Optional[R]] = [None] * n
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(worker, item): i for i, item in enumerate(items)}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
    return [r for r in results if r is not None]
