"""ENA FASTQ download helpers with tqdm + UI progress markers."""
from __future__ import annotations

import csv
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...agent.agent_config import DOWNLOAD_STALL_TIMEOUT_SEC

PROGRESS_MARKER = "__SRNAGENT_DL__"


def emit_download_progress(payload: Dict[str, Any]) -> None:
    """Print a machine-readable progress marker for the UI stream parser."""
    print(f"{PROGRESS_MARKER} {json.dumps(payload, ensure_ascii=False)}", flush=True)


def _overall_pct(file_index: int, file_total: int, file_pct: float) -> float:
    """Weighted overall progress: completed files + current file fraction."""
    total = max(int(file_total), 1)
    index = max(1, min(int(file_index), total))
    pct = max(0.0, min(100.0, float(file_pct)))
    return round(((index - 1) + pct / 100.0) / total * 100.0, 1)


def _download_one_url(
    url: str,
    dest: Path,
    *,
    file_index: int,
    file_total: int,
    run_id: str,
    min_interval: float = 0.4,
) -> Path:
    from tqdm import tqdm

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        size = dest.stat().st_size
        emit_download_progress(
            {
                "run": run_id,
                "fileIndex": file_index,
                "fileTotal": file_total,
                "filePct": 100.0,
                "overallPct": _overall_pct(file_index, file_total, 100.0),
                "bytes": size,
                "bytesTotal": size,
                "skipped": True,
            }
        )
        print(f"[fastq_dl] Skip existing {dest.name}", flush=True)
        return dest

    request = urllib.request.Request(url)
    read_timeout = max(120, DOWNLOAD_STALL_TIMEOUT_SEC)
    with urllib.request.urlopen(request, timeout=read_timeout) as response:
        total = int(response.headers.get("Content-Length") or 0)
        label = f"{run_id} ({file_index}/{file_total})"
        last_emit = 0.0
        downloaded = 0
        last_growth = time.monotonic()

        with tqdm(
            total=total or None,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=label,
            file=sys.stdout,
            mininterval=0.5,
        ) as bar, dest.open("wb") as handle:
            while True:
                if time.monotonic() - last_growth > DOWNLOAD_STALL_TIMEOUT_SEC:
                    raise RuntimeError(
                        f"Download stalled: no data received for {run_id} "
                        f"({dest.name}) for {DOWNLOAD_STALL_TIMEOUT_SEC}s"
                    )
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                last_growth = time.monotonic()
                bar.update(len(chunk))
                now = time.monotonic()
                if now - last_emit >= min_interval:
                    file_pct = (downloaded / total * 100) if total else 0.0
                    emit_download_progress(
                        {
                            "run": run_id,
                            "fileIndex": file_index,
                            "fileTotal": file_total,
                            "filePct": round(file_pct, 1),
                            "overallPct": _overall_pct(file_index, file_total, file_pct),
                            "bytes": downloaded,
                            "bytesTotal": total,
                        }
                    )
                    last_emit = now

        file_pct = 100.0
        emit_download_progress(
            {
                "run": run_id,
                "fileIndex": file_index,
                "fileTotal": file_total,
                "filePct": file_pct,
                "overallPct": _overall_pct(file_index, file_total, file_pct),
                "bytes": downloaded,
                "bytesTotal": total or downloaded,
            }
        )
    return dest


def _find_run_info_tsv(out_dir: Path) -> Optional[Path]:
    candidates = sorted(out_dir.glob("*run-info*.tsv"))
    if candidates:
        return candidates[0]
    legacy = out_dir / "fastq-run-info.tsv"
    return legacy if legacy.exists() else None


def _ftp_urls_for_run(row: Dict[str, str], protocol: str) -> List[str]:
    raw = str(row.get("fastq_ftp") or "").strip()
    if not raw:
        return []
    urls: List[str] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if part.startswith("ftp://") or part.startswith("http://") or part.startswith("https://"):
            url = part
        else:
            url = f"{protocol}://{part.lstrip('/')}"
        urls.append(url)
    return urls


def download_ena_from_run_info(
    out_dir: Path,
    *,
    protocol: str = "ftp",
    overwrite: bool = False,
) -> Tuple[int, int]:
    """Download all FASTQs listed in fastq-dl metadata TSV with tqdm progress."""
    tsv_path = _find_run_info_tsv(out_dir)
    if tsv_path is None:
        raise FileNotFoundError(
            f"No run-info TSV found under {out_dir}. "
            "Run fastq-dl with --only-download-metadata first."
        )

    rows = list(csv.DictReader(tsv_path.open(encoding="utf-8"), delimiter="\t"))
    if not rows:
        return 0, 0

    tasks: List[Tuple[str, str, Path]] = []
    for row in rows:
        run_id = str(row.get("run_accession") or "").strip()
        if not run_id:
            continue
        for url in _ftp_urls_for_run(row, protocol):
            filename = Path(url.split("/")[-1]).name or f"{run_id}.fastq.gz"
            dest = out_dir / filename
            if dest.exists() and dest.stat().st_size > 0 and not overwrite:
                continue
            tasks.append((run_id, url, dest))

    total = len(tasks)
    if total == 0:
        print(f"[fastq_dl] All FASTQ files already present in {out_dir}", flush=True)
        return len(rows), 0

    print(f"[fastq_dl] Downloading {total} FASTQ file(s) with tqdm progress...", flush=True)
    for index, (run_id, url, dest) in enumerate(tasks, start=1):
        print(f"[fastq_dl] {run_id}: {url}", flush=True)
        _download_one_url(
            url,
            dest,
            file_index=index,
            file_total=total,
            run_id=run_id,
        )
    return len(rows), total
