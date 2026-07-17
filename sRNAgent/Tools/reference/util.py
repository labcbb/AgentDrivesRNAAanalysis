"""Shared download utilities for reference-data providers.

All reference download modules should use :func:`resumable_download` from
this module.  It provides timeout/retry handling, byte-range parallelism, and
resume support for the downloaded reference files.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


_DOWNLOAD_TIMEOUT = 60
_DOWNLOAD_RETRIES = 3


def _urlopen_with_retries(req: urllib.request.Request):
    last_error: Exception | None = None
    for attempt in range(_DOWNLOAD_RETRIES):
        try:
            return urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT)
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt + 1 < _DOWNLOAD_RETRIES:
                time.sleep(2**attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError("download failed before any request was attempted")


def _get_file_size(url: str) -> int:
    """Get remote file size via HEAD request (HTTP) or SIZE (FTP)."""
    if url.startswith("ftp://"):
        return _get_ftp_file_size(url)
    req = urllib.request.Request(url, method="HEAD")
    try:
        with _urlopen_with_retries(req) as resp:
            return int(resp.headers["Content-Length"])
    except (TimeoutError, urllib.error.URLError, KeyError):
        return _get_file_size_with_curl(url)


def _get_file_size_with_curl(url: str) -> int:
    """Get remote file size with curl when urllib HEAD is unreliable."""
    if shutil.which("curl") is None:
        raise RuntimeError(
            "Could not determine remote file size with urllib, and curl is not available"
        )
    proc = subprocess.run(
        ["curl", "-L", "-I", "--connect-timeout", "30", "--max-time", "120", url],
        check=True,
        text=True,
        capture_output=True,
    )
    sizes: list[int] = []
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(":")
        if key.lower() == "content-length":
            value = value.strip()
            if value.isdigit():
                sizes.append(int(value))
    if not sizes:
        raise RuntimeError(f"Could not find Content-Length for {url}")
    return sizes[-1]


def _get_ftp_file_size(url: str) -> int:
    """Get file size via FTP SIZE command."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 21
    path = parsed.path

    import ftplib

    with ftplib.FTP() as ftp:
        ftp.connect(host, port)
        ftp.login()
        return ftp.size(path)


def _download_range(url: str, start: int, end: int, part_path: Path) -> None:
    """Download a byte range [start, end] to a temporary part file."""
    resume = part_path.stat().st_size if part_path.exists() else 0
    actual_start = start + resume
    if actual_start > end:
        return

    req = urllib.request.Request(url)
    req.add_header("Range", f"bytes={actual_start}-{end}")
    mode = "ab" if resume else "wb"
    with _urlopen_with_retries(req) as resp, open(part_path, mode) as f:
        while chunk := resp.read(65536):
            f.write(chunk)


def resumable_download(
    url: str,
    output_path: str | Path,
    jobs: int = 4,
    force: bool = False,
) -> str:
    """Download a file with parallel byte-range resume support.

    Supports HTTP(S) and FTP URLs.  ``jobs`` controls the number of Python
    worker threads.  Existing ``.part.N`` files are resumed after interruption.
    """
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    total = _get_file_size(url)

    if not force and out.exists() and out.stat().st_size == total:
        print(f"[download] Skipping {out.name}: already exists", flush=True)
        return str(out)
    if force and out.exists():
        out.unlink()

    if jobs <= 1:
        print(f"[download] {out.name} ({_fmt_size(total)})", flush=True)
        resume = out.stat().st_size if out.exists() else 0
        if resume >= total:
            return str(out)
        req = urllib.request.Request(url)
        if resume:
            req.add_header("Range", f"bytes={resume}-")
        with _urlopen_with_retries(req) as resp, open(out, "ab" if resume else "wb") as f:
            while chunk := resp.read(65536):
                f.write(chunk)
        _verify_size(out, total)
        return str(out)

    jobs = max(1, jobs)
    chunk_size = total // jobs
    ranges = [
        (i * chunk_size, total - 1 if i == jobs - 1 else (i + 1) * chunk_size - 1)
        for i in range(jobs)
    ]
    print(f"[download] {out.name} ({_fmt_size(total)}, {jobs} threads)", flush=True)

    errors: list[tuple[int, BaseException]] = []
    error_lock = threading.Lock()

    def worker(i: int, start: int, end: int) -> None:
        try:
            _download_range(url, start, end, out.with_suffix(f".part.{i}"))
            print(f"  [{i + 1}/{jobs}] chunk {i} done", flush=True)
        except BaseException as exc:
            with error_lock:
                errors.append((i, exc))

    threads = [
        threading.Thread(target=worker, args=(i, start, end), daemon=True)
        for i, (start, end) in enumerate(ranges)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if errors:
        failed = ", ".join(f"chunk {i}: {exc}" for i, exc in errors)
        raise RuntimeError(f"Download failed for {out.name}: {failed}")

    parts = [out.with_suffix(f".part.{i}") for i in range(jobs)]
    for i, part in enumerate(parts):
        expected = ranges[i][1] - ranges[i][0] + 1
        if not part.exists() or part.stat().st_size != expected:
            actual = part.stat().st_size if part.exists() else 0
            raise RuntimeError(
                f"Download incomplete for {out.name}, chunk {i}: expected {expected}, got {actual}"
            )

    with open(out, "wb") as dest:
        for part in parts:
            with open(part, "rb") as src:
                shutil.copyfileobj(src, dest)
            part.unlink()
    _verify_size(out, total)
    return str(out)


def _verify_size(path: Path, expected: int) -> None:
    actual = path.stat().st_size
    if actual != expected:
        raise RuntimeError(f"Download size mismatch for {path.name}: expected {expected}, got {actual}")


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


__all__ = ["resumable_download"]
