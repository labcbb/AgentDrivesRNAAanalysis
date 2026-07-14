"""FASTQ download via fastq-dl, enriched into AnnData.

Wraps `fastq-dl <https://github.com/rpetit3/fastq-dl>`_, a Python CLI tool
that queries ENA's Data Warehouse API to resolve any ENA/SRA accession
(BioProject, BioSample, Experiment, or Run) to one or more runs, then
downloads the corresponding FASTQ files.

Adds ``adata.obs['fastq_path']`` with the local path to downloaded FASTQ.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

import pandas as pd
from anndata import AnnData

from ..._registry import register_function
from ..._utils import run_cli_cmd, run_threads
from ...agent.agent_config import DOWNLOAD_STALL_POLL_SEC, DOWNLOAD_STALL_TIMEOUT_SEC


# ---------------------------------------------------------------------------
# Output discovery helpers
# ---------------------------------------------------------------------------

_VALID_FASTQ_SUFFIXES = (
    ".fastq.gz", ".fq.gz", ".fastq", ".fq",
)

_RUN_ACCESSION_RE = re.compile(r"^(?:SRR|ERR|DRR)\d+$", re.IGNORECASE)


def _is_fastq(path: Path) -> bool:
    return path.suffix in (".gz",) and any(
        str(path).endswith(s) for s in _VALID_FASTQ_SUFFIXES
    ) or path.suffix in (".fastq", ".fq")


def _discover_run_fastqs(out_dir: Path, run_acc: str) -> Dict[str, str]:
    """Find FASTQ files for a single run accession under *out_dir*."""
    run_dir = out_dir / run_acc
    if not run_dir.is_dir():
        return {"sample": run_acc, "fq1": "", "fq2": "", "layout": "unknown"}

    hits = sorted(
        p for p in run_dir.iterdir()
        if p.is_file() and any(str(p).endswith(s) for s in _VALID_FASTQ_SUFFIXES)
    )
    if not hits:
        return {"sample": run_acc, "fq1": "", "fq2": "", "layout": "unknown"}

    paired = [p for p in hits if "_1." in p.name]
    if paired:
        fq1 = paired[0]
        fq2_candidates = [p for p in hits if "_2." in p.name]
        fq2 = fq2_candidates[0] if fq2_candidates else None
        return {
            "sample": run_acc,
            "fq1": str(fq1),
            "fq2": str(fq2) if fq2 else "",
            "layout": "paired",
        }

    return {
        "sample": run_acc,
        "fq1": str(hits[0]),
        "fq2": "",
        "layout": "single",
    }


def _discover_all_fastqs(
    out_dir: Path,
    accessions: Sequence[str],
) -> Dict[str, Dict[str, str]]:
    """Walk *out_dir* for every run directory produced by fastq-dl."""
    results: Dict[str, Dict[str, str]] = {}

    for sub in sorted(out_dir.iterdir()):
        if not sub.is_dir():
            continue
        result = _discover_run_fastqs(out_dir, sub.name)
        if result.get("fq1"):
            results[sub.name] = result

    for fq in sorted(out_dir.iterdir()):
        if not fq.is_file() or not _is_fastq(fq):
            continue
        for acc in accessions:
            if fq.name.startswith(acc):
                key = acc
                results.setdefault(key, {
                    "sample": acc,
                    "fq1": "",
                    "fq2": "",
                    "layout": "unknown",
                })
                if not results[key]["fq1"]:
                    if "_1." in fq.name:
                        results[key]["fq1"] = str(fq)
                        mate = out_dir / fq.name.replace("_1.", "_2.")
                        if mate.exists():
                            results[key]["fq2"] = str(mate)
                            results[key]["layout"] = "paired"
                        else:
                            results[key]["layout"] = "paired"
                    elif "_2." in fq.name:
                        continue
                    else:
                        results[key]["fq1"] = str(fq)
                        results[key]["fq2"] = ""
                        results[key]["layout"] = "single"

    return results


def _discover_metadata_file(out_dir: Path, accession: str, prefix: Optional[str]) -> str:
    """Find the metadata TSV emitted by fastq-dl for *accession*."""
    candidates = [
        out_dir / f"{prefix or 'fastq'}-{accession}-metadata.tsv",
        out_dir / f"{accession}-metadata.tsv",
    ]
    # fastq-dl >= 1.x emits fastq-run-info.tsv (no accession in filename)
    run_info = out_dir / "fastq-run-info.tsv"
    if run_info.exists():
        return str(run_info)
    candidates.extend(sorted(out_dir.glob(f"*{accession}*metadata*.tsv")))
    for path in candidates:
        if path.exists() and path.is_file():
            return str(path)
    return ""


def _parse_metadata_runs(metadata_path: str) -> Dict[str, Dict[str, str]]:
    """Parse fastq-dl metadata TSV into the same run dict shape as FASTQ discovery."""
    if not metadata_path:
        return {}

    path = Path(metadata_path)
    if not path.exists():
        return {}

    try:
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if not reader.fieldnames:
                return {}
            columns = list(reader.fieldnames)
            lower_map = {col.lower(): col for col in columns}
            run_col = next(
                (
                    lower_map[name]
                    for name in ("run_accession", "run", "run_id", "accession")
                    if name in lower_map
                ),
                "",
            )
            if not run_col:
                run_col = next((col for col in columns if "run" in col.lower()), "")
            if not run_col:
                return {}

            fq_col = next(
                (
                    col
                    for col in columns
                    if col.lower() in ("fastq_ftp", "fastq_aspera", "fastq_galaxy")
                ),
                "",
            )

            runs: Dict[str, Dict[str, str]] = {}
            for row in reader:
                run_acc = str(row.get(run_col) or "").strip()
                if not _RUN_ACCESSION_RE.match(run_acc):
                    continue
                fq_value = str(row.get(fq_col) or "").strip() if fq_col else ""
                fq_parts = [part for part in re.split(r"[;,]", fq_value) if part]
                fq1 = fq_parts[0] if fq_parts else ""
                fq2 = fq_parts[1] if len(fq_parts) > 1 else ""
                runs[run_acc] = {
                    "sample": run_acc,
                    "fq1": fq1,
                    "fq2": fq2,
                    "layout": "paired" if fq2 else ("single" if fq1 else "unknown"),
                }
            return runs
    except OSError:
        return {}


# ---------------------------------------------------------------------------
# fastq-dl wrapper
# ---------------------------------------------------------------------------

_INSTALL_HINTS = {
    "fastq-dl": "conda install -c conda-forge -c bioconda fastq-dl",
}


@register_function(
    aliases=[
        "fastq_dl", "fastq-dl", "ena_download", "sra_download",
        "fastq_download", "SRA下载", "ENA下载",
    ],
    category="fastq",
    description=(
        "Download FASTQ files from ENA or SRA using fastq-dl. "
        "Accepts any accession level (BioProject, BioSample, Experiment, Run) "
        "and resolves all associated runs automatically. "
        "Results are stored in ``adata.obs['fastq_path']``."
    ),
    examples=[
        'adata = sa.fastq.fastq_dl(adata, accessions=["SRR1","SRR2"])',
    ],
    related=[
        "fastq.cutadapt", "fastq.fastqc", "fastq.multiqc",
    ],
    produces={"obs": ["fastq_path"]},
)
def fastq_dl(
    adata: AnnData,
    accessions: Union[str, Sequence[str]],
    output_dir: str = "fastq",
    provider: str = "ena",
    protocol: str = "ftp",
    group_by: Optional[str] = None,
    cpus: int = 4,
    connections: int = 8,
    max_attempts: int = 3,
    overwrite: bool = False,
    skip_compression: bool = False,
    gzip_level: int = 1,
    only_provider: bool = False,
    only_metadata: bool = False,
    ignore_md5: bool = False,
    prefix: Optional[str] = None,
    silent: bool = False,
    sleep: int = 10,
    sra_lite: bool = False,
    jobs: Optional[int] = None,
) -> AnnData:
    """Download FASTQ files and store paths in ``adata.obs['fastq_path']``.

    Parameters
    ----------
    adata
        AnnData object with ``.obs_names`` as sample IDs.
    accessions
        One or more ENA/SRA accessions. If a single string, all samples
        share this accession. If a list, must match ``len(adata.obs_names)``.
    output_dir
        Output directory for downloaded FASTQ files.
    provider
        Data provider: ``'ena'`` (default) or ``'sra'``.
    protocol
        ENA download protocol: ``'ftp'`` (default) or ``'https'``.
    group_by
        Group output by ``'experiment'`` or ``'sample'`` when a higher-level
        accession expands to multiple runs.
    cpus
        CPUs used for SRA conversion and compression.
    connections
        HTTP connections per file for SRA downloads.
    max_attempts
        Maximum download attempts per accession.
    overwrite
        Overwrite existing files (``--force``).
    skip_compression
        Skip compression of downloaded files (SRA provider).
    gzip_level
        Gzip compression level 1-9.
    only_provider
        Only attempt download from the specified provider; no fallback.
    only_metadata
        Only retrieve metadata, skip FASTQ download.
    ignore_md5
        Skip MD5 validation (ENA) or relax integrity checks (SRA).
    prefix
        Prefix for log file naming.
    silent
        Suppress non-critical output.
    sleep
        Minimum seconds to sleep between retries.
    sra_lite
        Prefer SRA Lite format (SRA provider).
    jobs
        Number of accessions to process concurrently.

    Returns
    -------
    AnnData
        Enriched with ``adata.obs['fastq_path']`` and ``adata.uns['output_dir']``.
    """
    n = len(adata.obs_names)
    if isinstance(accessions, str):
        acc_list = [accessions] * n
    else:
        acc_list = list(accessions)
        if len(acc_list) != n:
            raise ValueError(
                f"Number of accessions ({len(acc_list)}) must match "
                f"number of samples ({n})"
            )

    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    def _run_one(acc: str) -> Dict[str, Union[str, Dict]]:
        if not overwrite and not only_metadata:
            existing = _discover_all_fastqs(out_root, [acc])
            if existing and any(
                v.get("fq1") and Path(v["fq1"]).exists()
                for v in existing.values()
            ):
                print(
                    f"[fastq_dl] Skipping {acc}: files exist", flush=True,
                )
                return {"accession": acc, "runs": existing}

        cmd = [
            "fastq-dl", "--accession", acc,
            "--outdir", str(out_root),
            "--provider", provider,
            "--protocol", protocol,
            "--cpus", str(cpus),
            "--connections", str(connections),
            "--max-attempts", str(max_attempts),
            "--sleep", str(sleep),
        ]
        if group_by == "experiment":
            cmd.append("--group-by-experiment")
        elif group_by == "sample":
            cmd.append("--group-by-sample")
        if overwrite:
            cmd.append("--force")
        if skip_compression:
            cmd.append("--skip-compression")
        if only_provider:
            cmd.append("--only-provider")
        if only_metadata:
            cmd.append("--only-download-metadata")
        if ignore_md5:
            cmd.append("--ignore")
        if silent:
            cmd.append("--silent")
        if sra_lite:
            cmd.append("--sra-lite")
        if prefix:
            cmd.extend(["--prefix", prefix])
        if gzip_level != 1:
            cmd.extend(["--gzip-level", str(gzip_level)])

        run_cli_cmd(cmd)
        metadata_path = _discover_metadata_file(out_root, acc, prefix)
        runs = _parse_metadata_runs(metadata_path) if only_metadata else _discover_all_fastqs(out_root, [acc])
        return {"accession": acc, "runs": runs, "metadata": metadata_path}

    raw_results = run_threads(acc_list, _run_one, jobs)

    all_runs: Dict[str, Dict[str, str]] = {}
    metadata_files: Dict[str, str] = {}
    for result in raw_results:
        runs = result.get("runs")
        if isinstance(runs, dict):
            all_runs.update(runs)
        metadata_path = str(result.get("metadata") or "")
        if metadata_path:
            metadata_files[str(result["accession"])] = metadata_path

    fastq_paths: Dict[str, str] = {}
    sample_run_info: Dict[str, Dict[str, str]] = {}
    for i, sample in enumerate(adata.obs_names):
        acc = acc_list[i]
        result = next((r for r in raw_results if r["accession"] == acc), None)
        if result and result.get("runs"):
            run_keys = list(result["runs"].keys())
            if run_keys:
                run_info = result["runs"][run_keys[0]]
                sample_run_info[sample] = run_info
                fq1 = run_info.get("fq1", "")
                if fq1:
                    fastq_paths[sample] = fq1

    adata.obs["fastq_path"] = pd.Series(fastq_paths, dtype=str)
    adata.uns["output_dir"] = output_dir
    adata.uns["fastq_dl_runs"] = all_runs
    adata.uns["fastq_dl_run_info"] = sample_run_info
    if metadata_files:
        adata.uns["fastq_dl_metadata_files"] = metadata_files
    return adata
