"""Internal download utilities for sRNAgent."""
from __future__ import annotations

import shutil
import threading
import urllib.request
from pathlib import Path


def _get_file_size(url: str) -> int:
    """Get remote file size via HEAD request (HTTP) or SIZE (FTP)."""
    if url.startswith("ftp://"):
        return _get_ftp_file_size(url)
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req) as resp:
        return int(resp.headers["Content-Length"])


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
    """Download a byte range [start, end] to a temp file."""
    resume = 0
    if part_path.exists():
        resume = part_path.stat().st_size

    actual_start = start + resume
    if actual_start > end:
        return

    req = urllib.request.Request(url)
    req.add_header("Range", f"bytes={actual_start}-{end}")

    mode = "ab" if resume > 0 else "wb"
    with urllib.request.urlopen(req) as resp, open(part_path, mode) as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)


def resumable_download(
    url: str,
    output_path: str | Path,
    jobs: int = 4,
    force: bool = False,
) -> str:
    """Download a file with multi-threaded resume support.

    Supports ``https://``, ``http://``, and ``ftp://`` URLs.

    Parameters
    ----------
    url
        Remote file URL.
    output_path
        Local file path to write to.
    jobs
        Number of parallel threads. Each downloads a separate byte range.
        Default 4. Set to 1 for single-threaded download.
    force
        Re-download even if the file exists and is complete.

    Returns
    -------
    str
        Absolute path to the downloaded file.
    """
    out = Path(output_path)
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    # Get remote file size
    total = _get_file_size(url)

    # Skip if complete
    if not force and out.exists() and out.stat().st_size == total:
        print(f"[download] Skipping {out.name}: already exists", flush=True)
        return str(out)

    # Remove partial output if forcing fresh download
    if force and out.exists():
        out.unlink()

    if jobs <= 1:
        # Single-threaded fallback
        print(f"[download] {out.name} ({_fmt_size(total)})", flush=True)
        mode = "ab" if out.exists() else "wb"
        resume = out.stat().st_size if out.exists() else 0
        if resume >= total:
            return str(out)

        req = urllib.request.Request(url)
        if resume > 0:
            req.add_header("Range", f"bytes={resume}-")

        with urllib.request.urlopen(req) as resp, open(out, mode) as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)

        out.rename(out)
        return str(out)

    # Multi-threaded: split into chunks
    chunk_size = total // jobs
    ranges: list[tuple[int, int]] = []
    for i in range(jobs):
        start = i * chunk_size
        end = total - 1 if i == jobs - 1 else (i + 1) * chunk_size - 1
        ranges.append((start, end))

    print(f"[download] {out.name} ({_fmt_size(total)}, {jobs} threads)", flush=True)

    completed: list[bool] = [False] * jobs
    lock = threading.Lock()

    def _download_with_progress(i: int, start: int, end: int, part: Path) -> None:
        _download_range(url, start, end, part)
        with lock:
            done = sum(1 for c in completed if c)
            completed[i] = True
            done_now = done + 1
            pct = done_now / jobs * 100
            print(
                f"  [{done_now}/{jobs}] chunk {i} done ({pct:.0f}%)",
                flush=True,
            )

    threads = []
    for i, (start, end) in enumerate(ranges):
        part = out.with_suffix(f".part.{i}")
        t = threading.Thread(
            target=_download_with_progress,
            args=(i, start, end, part),
            daemon=True,
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Verify all parts downloaded
    for i in range(jobs):
        part = out.with_suffix(f".part.{i}")
        if not part.exists():
            raise RuntimeError(f"Download failed: part {i} missing for {out.name}")

    # Assemble parts in order
    with open(out, "wb") as dest:
        for i in range(jobs):
            part = out.with_suffix(f".part.{i}")
            with open(part, "rb") as src:
                shutil.copyfileobj(src, dest)
            part.unlink()

    # Verify final size
    if out.stat().st_size != total:
        raise RuntimeError(
            f"Download size mismatch for {out.name}: "
            f"expected {total}, got {out.stat().st_size}"
        )

    return str(out)


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
