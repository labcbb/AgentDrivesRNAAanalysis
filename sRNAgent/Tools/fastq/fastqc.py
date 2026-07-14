"""FastQC wrapper for quality control reports on FASTQ / BAM / SAM data.

Wraps `FastQC <https://www.bioinformatics.babraham.ac.uk/projects/fastqc/>`_,
a widely used QC tool that analyses raw sequencing data and produces interactive
HTML reports with per-module quality metrics (per-base quality, GC content, N
content, sequence duplication, overrepresented sequences, adapter content, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from anndata import AnnData
import pandas as pd

from ..._registry import register_function
from ..._utils import run_cli_cmd, run_threads


def _discover_qc_outputs(
    input_files: List[str],
    output_dir: str,
    extract: bool,
) -> Dict[str, Dict[str, str]]:
    """Discover FastQC output files for the given inputs.

    FastQC naming::
        <input_basename>_fastqc.html
        <input_basename>_fastqc.zip
    When ``--extract`` is used, also creates::
        <input_basename>_fastqc/
    """
    out_root = Path(output_dir)
    results: Dict[str, Dict[str, str]] = {}

    for fpath in input_files:
        inp = Path(fpath)
        base = inp.name.replace(".gz", "").replace(".fastq", "").replace(".fq", "")
        base = base.replace(".sam", "").replace(".bam", "")

        html = out_root / f"{base}_fastqc.html"
        zipf = out_root / f"{base}_fastqc.zip"
        data_dir = out_root / f"{base}_fastqc" if extract else None

        entry: Dict[str, str] = {
            "input": fpath,
            "html": str(html) if html.exists() else "",
            "zip": str(zipf) if zipf.exists() else "",
        }
        if data_dir and data_dir.is_dir():
            entry["data_dir"] = str(data_dir)

        results[base] = entry

    return results


def _run_fastqc_one(
    input_files: List[str],
    output_dir: str,
    # FastQC options
    format: Optional[str],
    threads: int,
    contaminants: Optional[str],
    adapters: Optional[str],
    limits: Optional[str],
    kmers: int,
    casava: bool,
    nano: bool,
    nofilter: bool,
    extract: bool,
    nogroup: bool,
    quiet: bool,
    java_path: Optional[str],
    temp_dir: Optional[str],
) -> Dict[str, Union[str, Dict[str, str]]]:
    """Run FastQC on a batch of input files."""
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    # Guard: check if all expected outputs already exist
    existing = _discover_qc_outputs(input_files, output_dir, extract)
    all_done = all(
        v.get("html") and Path(v["html"]).exists()
        for v in existing.values()
    )
    if all_done:
        print(
            f"[fastqc] Skipping {len(input_files)} file(s): "
            f"HTML reports already exist in {output_dir}",
            flush=True,
        )
        return {
            "output_dir": output_dir,
            "files": existing,
        }

    # Resolve executable — fastqc should be on PATH
    cmd = ["fastqc"]

    # Output directory
    cmd.extend(["-o", str(out_root)])

    # Threads
    cmd.extend(["-t", str(threads)])

    # Format
    if format is not None:
        cmd.extend(["-f", format])

    # Contaminants / adapters
    if contaminants is not None:
        cmd.extend(["-c", contaminants])
    if adapters is not None:
        cmd.extend(["-a", adapters])
    if limits is not None:
        cmd.extend(["-l", limits])

    # Kmer length
    if kmers != 7:
        cmd.extend(["-k", str(kmers)])

    # Modes
    if casava:
        cmd.append("--casava")
    if nano:
        cmd.append("--nano")
    if nofilter:
        cmd.append("--nofilter")

    # Extraction
    if extract:
        cmd.append("--extract")
    else:
        cmd.append("--noextract")

    # Grouping
    if nogroup:
        cmd.append("--nogroup")

    # Quiet
    if quiet:
        cmd.append("--quiet")

    # Java path
    if java_path is not None:
        cmd.extend(["-j", java_path])

    # Temp directory
    if temp_dir is not None:
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
        cmd.extend(["-d", temp_dir])

    # Input files
    cmd.extend(input_files)

    # Run
    run_cli_cmd(cmd)

    # Discover outputs
    outputs = _discover_qc_outputs(input_files, output_dir, extract)

    return {
        "output_dir": output_dir,
        "files": outputs,
    }


@register_function(
    aliases=[
        "fastqc", "qc_report", "quality_control", "质控报告",
        "fastq_qc", "read_quality",
    ],
    category="fastq",
    description=(
        "Generate FastQC quality control reports for FASTQ, BAM, or SAM files "
        "from an AnnData object. Reads input paths from ``adata.obs['trimmed_path']`` "
        "(preferred) or ``adata.obs['fastq_path']``, runs FastQC, and writes "
        "the resulting HTML and zip paths back to ``adata.obs['fastqc_html']`` "
        "and ``adata.obs['fastqc_zip']`` respectively. "
        "Produces interactive HTML reports with per-module quality metrics "
        "including per-base quality, GC content, N content, sequence duplication, "
        "overrepresented sequences, and adapter content."
    ),
    examples=[
        'sa.fastq.fastqc(adata, output_dir="qc")',
        'sa.fastq.fastqc(adata, output_dir="qc", threads=4)',
        'sa.fastq.fastqc(adata, output_dir="qc", contaminants="contam.txt")',
    ],
    related=[
        "fastq.cutadapt", "fastq.fastq_dl",
    ],
    produces={"obs": ["fastqc_html", "fastqc_zip"]},
)
def fastqc(
    adata: AnnData,
    output_dir: str = "fastqc_out",
    format: Optional[str] = None,
    threads: int = 2,
    contaminants: Optional[str] = None,
    adapters: Optional[str] = None,
    limits: Optional[str] = None,
    kmers: int = 7,
    casava: bool = False,
    nano: bool = False,
    nofilter: bool = False,
    extract: bool = True,
    nogroup: bool = False,
    quiet: bool = False,
    java_path: Optional[str] = None,
    temp_dir: Optional[str] = None,
    jobs: Optional[int] = None,
    overwrite: bool = False,
) -> AnnData:
    """Generate FastQC quality control reports.

    Parameters
    ----------
    adata
        AnnData object. Input FASTQ / BAM / SAM paths are read from
        ``adata.obs['trimmed_path']`` if it exists, otherwise from
        ``adata.obs['fastq_path']``. Results are stored in
        ``adata.obs['fastqc_html']`` and ``adata.obs['fastqc_zip']``.
    output_dir
        Output directory for FastQC reports (HTML + zip).
    format
        Force file format: ``'fastq'``, ``'bam'``, ``'sam'``, ``'bam_mapped'``,
        or ``'sam_mapped'``. Auto-detected when not set.
    threads
        Number of files to process simultaneously (FastQC's ``-t`` flag).
        FastQC uses ~250 MB RAM per thread. Default 2.
    contaminants
        File with contaminant sequences (tab-separated: ``name\\tsequence``).
    adapters
        File with adapter sequences (tab-separated: ``name\\tsequence``).
    limits
        File with custom warn/error limits for FastQC modules.
    kmers
        Kmer length for the Kmer Content module (2–10). Default 7.
    casava
        Input is from raw Casava output; group files by sample and exclude
        filtered reads.
    nano
        Input is from Nanopore in fast5 format. Pass directories to process
        all fast5 files.
    nofilter
        With ``casava=True``, do not remove poor-quality flagged reads.
    extract
        Unzip the output zip file after creation. Default ``True``.
    nogroup
        Disable base grouping for reads longer than 50 bp. May crash on
        very long reads — use with caution.
    quiet
        Suppress progress messages; report only errors.
    java_path
        Full path to the Java binary (default: ``java`` on PATH).
    temp_dir
        Directory for FastQC temporary files.
    jobs
        Number of files to process concurrently. Each file uses *threads*
        internal FastQC threads. Default 1 (sequential).
    overwrite
        Re-run even if HTML reports already exist.

    Returns
    -------
    AnnData
        The input ``adata`` with ``fastqc_html`` and ``fastqc_zip`` columns
        added to ``adata.obs``, containing paths to the generated reports.

    Examples
    --------
    >>> import sRNAgent as sa

    >>> # Basic usage — reads from adata.obs['trimmed_path'] or fastq_path
    >>> adata = sa.fastq.fastqc(adata, output_dir="qc")

    >>> # Custom threads and contaminants file
    >>> adata = sa.fastq.fastqc(
    ...     adata,
    ...     output_dir="qc",
    ...     threads=4,
    ...     contaminants="my_contaminants.txt",
    ... )

    >>> # Nanopore mode
    >>> adata = sa.fastq.fastqc(
    ...     adata,
    ...     nano=True,
    ...     output_dir="nanopore_qc",
    ... )
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")

    # Determine input path column — prefer trimmed_path over fastq_path
    if "trimmed_path" in adata.obs.columns:
        path_col = "trimmed_path"
    elif "fastq_path" in adata.obs.columns:
        path_col = "fastq_path"
    else:
        raise KeyError(
            "adata.obs must contain 'trimmed_path' or 'fastq_path'"
        )

    file_list = adata.obs[path_col].dropna().tolist()
    if not file_list:
        raise ValueError(f"No non-null paths found in adata.obs['{path_col}']")

    # Optionally remove already-processed files when not overwriting
    if not overwrite:
        existing_html = adata.obs.get("fastqc_html", pd.Series([None] * len(adata.obs)))
        existing_zip = adata.obs.get("fastqc_zip", pd.Series([None] * len(adata.obs)))
        # Build lookup: path -> (html, zip)
        path_to_idx = {
            row[path_col]: idx
            for idx, row in adata.obs.iterrows()
            if pd.notna(row.get(path_col))
        }
        # Keep only files whose HTML does not already exist on disk
        filtered = []
        for p in file_list:
            idx = path_to_idx.get(p)
            if idx is not None:
                html_val = existing_html.get(idx)
                zip_val = existing_zip.get(idx)
                if pd.notna(html_val) and Path(str(html_val)).exists() \
                        and pd.notna(zip_val) and Path(str(zip_val)).exists():
                    continue  # already complete, skip
            filtered.append(p)
        file_list = filtered
        if not file_list:
            print("[fastqc] All files already processed. Returning adata unchanged.", flush=True)
            return adata

    def _run_one(fpath: str) -> Dict:
        return _run_fastqc_one(
            input_files=[fpath],
            output_dir=output_dir,
            format=format,
            threads=threads,
            contaminants=contaminants,
            adapters=adapters,
            limits=limits,
            kmers=kmers,
            casava=casava,
            nano=nano,
            nofilter=nofilter,
            extract=extract,
            nogroup=nogroup,
            quiet=quiet,
            java_path=java_path,
            temp_dir=temp_dir,
        )

    results = run_threads(file_list, _run_one, jobs)

    # Collate results back into adata.obs
    html_cols: List[Optional[str]] = [None] * len(adata.obs)
    zip_cols: List[Optional[str]] = [None] * len(adata.obs)

    path_to_idx = {
        row[path_col]: idx
        for idx, row in adata.obs.iterrows()
        if pd.notna(row.get(path_col))
    }

    for r in results:
        if r and "files" in r:
            for base_name, qc_info in r["files"].items():
                inp_path = qc_info.get("input", "")
                idx = path_to_idx.get(inp_path)
                if idx is None:
                    continue
                html_val = qc_info.get("html") or None
                zip_val = qc_info.get("zip") or None
                if html_val:
                    html_cols[idx] = html_val
                if zip_val:
                    zip_cols[idx] = zip_val

    adata.obs["fastqc_html"] = html_cols
    adata.obs["fastqc_zip"] = zip_cols

    return adata
