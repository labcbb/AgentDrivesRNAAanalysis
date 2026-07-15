"""MultiQC wrapper for aggregating QC reports from multiple tools.

Wraps `MultiQC <https://multiqc.info/>`_, a tool that scans directories for
log files from supported bioinformatics tools (FastQC, cutadapt, STAR, etc.)
and produces a single aggregated HTML report with summary plots and tables.

After aggregation, per-sample quality metrics are extracted from the MultiQC
data (``multiqc_data.json`` and ``multiqc_fastqc.txt``) and stored in
``adata.obs`` columns prefixed with ``multiqc_``.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import pandas as pd
from anndata import AnnData

from ..._registry import register_function
from ..._utils import run_cli_cmd


# ---------------------------------------------------------------------------
# QC data extraction helpers
# ---------------------------------------------------------------------------

# Mapping: multiqc column name → adata.obs column suffix (+ type + description)
_QC_METRICS: list[tuple[str, str, type, str]] = [
    ("total_sequences", "total_seqs", float, "Total Sequences"),
    ("avg_sequence_length", "avg_length", float, "Average Read Length (bp)"),
    ("median_sequence_length", "med_length", float, "Median Read Length (bp)"),
    ("percent_gc", "pct_gc", float, "GC Content (%)"),
    ("percent_duplicates", "pct_dups", float, "Duplicate Reads (%)"),
    ("percent_fails", "pct_fails", float, "FastQC Module Failures (%)"),
]

# Additional metrics from report_saved_raw_data.multiqc_fastqc
# (not in report_general_stats_data)
_RAW_QC_METRICS: list[tuple[str, str, str]] = [
    ("total_deduplicated_percentage", "pct_unique", "Unique Reads (%)"),
    ("Total Bases", "total_bases", "Total Bases (Mbp)"),
]

_FASTQC_MODULES: list[str] = [
    "basic_statistics", "per_base_sequence_quality",
    "per_sequence_quality_scores", "per_base_sequence_content",
    "per_sequence_gc_content", "per_base_n_content",
    "sequence_length_distribution", "sequence_duplication_levels",
    "overrepresented_sequences", "adapter_content",
]


def _build_sample_map(adata: AnnData, data_dir: Path) -> dict[str, str]:
    """Build a mapping from MultiQC sample names → adata.obs index.

    Strategy:
    1. Try exact match.
    2. Try extracting the stem from ``fastqc_html`` paths (removing ``_fastqc`` suffix).
    3. Try suffix/prefix matching.
    """
    # Collect candidate stems from fastqc_html paths
    html_stems: list[str] = []
    for p in adata.obs["fastqc_html"]:
        if not p:
            continue
        stem = Path(p).stem  # e.g. "SRR1_trimmed_fastqc"
        # Remove trailing _fastqc
        if stem.endswith("_fastqc"):
            stem = stem[:-7]
        html_stems.append(stem)

    obs_names = list(adata.obs_names)
    mapping: dict[str, str] = {}

    # Try reading multiqc sample names from fastqc.txt first
    fastqc_txt = data_dir / "multiqc_fastqc.txt"
    mqc_samples: list[str] = []
    if fastqc_txt.exists():
        with fastqc_txt.open() as f:
            reader = csv.DictReader(f, delimiter="\t")
            if reader.fieldnames and "Sample" in reader.fieldnames:
                mqc_samples = [row["Sample"] for row in reader]

    if mqc_samples:
        for mqc_name in mqc_samples:
            mqc_lower = mqc_name.lower()
            # Exact
            for on in obs_names:
                if on.lower() == mqc_lower:
                    mapping[mqc_name] = on
                    break
            else:
                # html_stem suffix match
                for stem, on in zip(html_stems, obs_names):
                    stem_lower = stem.lower()
                    if mqc_lower in stem_lower or stem_lower in mqc_lower:
                        mapping[mqc_name] = on
                        break

    return mapping


def _extract_general_stats(
    data: dict, mapping: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Extract per-sample general QC stats from parsed multiqc_data.json.

    Returns ``{adata_obs_name: {metric_key: value, ...}}``.
    """
    result: dict[str, dict[str, Any]] = {}
    stats_list = data.get("report_general_stats_data", [])
    if not stats_list:
        return result

    stats = stats_list[0]  # First (usually only) stats group
    for mqc_name, sample_stats in stats.items():
        adata_name = mapping.get(mqc_name)
        if adata_name is None:
            continue
        row: dict[str, Any] = {}
        for mqc_key, obs_suffix, _, _ in _QC_METRICS:
            val = sample_stats.get(mqc_key)
            if val is not None:
                row[obs_suffix] = val
        if row:
            result[adata_name] = row

    return result


def _extract_fastqc_modules(
    fastqc_txt: Path, mapping: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Extract per-module FastQC pass/fail/warn status.

    Returns ``{adata_obs_name: {module_name: "pass"|"warn"|"fail", ...}}``.
    """
    result: dict[str, dict[str, str]] = {}
    if not fastqc_txt.exists():
        return result

    with fastqc_txt.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            mqc_name = row.get("Sample", "")
            adata_name = mapping.get(mqc_name)
            if adata_name is None:
                continue
            modules: dict[str, str] = {}
            for mod in _FASTQC_MODULES:
                status = row.get(mod, "")
                if status in ("pass", "warn", "fail"):
                    modules[f"fastqc_{mod}"] = status
            if modules:
                result[adata_name] = modules

    return result


def _extract_raw_qc(
    data: dict, mapping: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Extract per-sample QC metrics from report_saved_raw_data.multiqc_fastqc.

    Reads fields defined in ``_RAW_QC_METRICS`` (not available in
    ``report_general_stats_data``, e.g. ``total_deduplicated_percentage``,
    ``Total Bases``).

    Returns ``{adata_obs_name: {obs_suffix: value, ...}}``.
    """
    result: dict[str, dict[str, Any]] = {}
    raw = data.get("report_saved_raw_data", {}).get("multiqc_fastqc", {})
    if not raw:
        return result

    for mqc_name, sample_raw in raw.items():
        adata_name = mapping.get(mqc_name)
        if adata_name is None:
            continue
        row: dict[str, Any] = {}
        for raw_key, obs_suffix, _ in _RAW_QC_METRICS:
            val = sample_raw.get(raw_key)
            if val is not None:
                # Parse "Total Bases" like "94.8 Mbp" → 94.8
                if isinstance(val, str) and raw_key == "Total Bases":
                    try:
                        val = float(val.split()[0])
                    except (ValueError, IndexError):
                        val = None
                else:
                    try:
                        val = float(val)
                    except (ValueError, TypeError):
                        val = None
            if val is not None:
                row[obs_suffix] = val
        if row:
            result[adata_name] = row

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@register_function(
    aliases=[
        "multiqc", "aggregate_qc", "merge_qc", "汇总报告",
        "qc_aggregation",
    ],
    category="fastq",
    description=(
        "Aggregate QC reports from multiple bioinformatics tools into a "
        "single HTML report using MultiQC, then extract per-sample quality "
        "metrics into ``adata.obs`` columns (prefixed ``multiqc_``).\n\n"
        "Scans directories containing FastQC HTML outputs (recorded in "
        "``adata.obs['fastqc_html']``) for recognised log files and produces "
        "an interactive summary report. After aggregation, per-sample metrics "
        "such as GC content, read length, duplicate rate, and per-module "
        "FastQC pass/fail/warn status are stored back into ``adata.obs``."
    ),
    examples=[
        'adata = sa.fastq.multiqc(adata, filename="my_report.html")',
        'print(adata.obs[["multiqc_total_seqs", "multiqc_pct_gc"]])',
    ],
    related=[
        "fastq.fastqc", "fastq.cutadapt",
    ],
    produces={
        "obs": [
            "multiqc_total_seqs", "multiqc_avg_length", "multiqc_med_length",
            "multiqc_pct_gc", "multiqc_pct_dups", "multiqc_pct_fails",
            "multiqc_pct_unique", "multiqc_total_bases",
            "multiqc_fastqc_basic_statistics",
            "multiqc_fastqc_per_base_sequence_quality",
            "multiqc_fastqc_per_sequence_quality_scores",
            "multiqc_fastqc_per_base_sequence_content",
            "multiqc_fastqc_per_sequence_gc_content",
            "multiqc_fastqc_per_base_n_content",
            "multiqc_fastqc_sequence_length_distribution",
            "multiqc_fastqc_sequence_duplication_levels",
            "multiqc_fastqc_overrepresented_sequences",
            "multiqc_fastqc_adapter_content",
        ],
        "uns": ["multiqc_dir", "multiqc_html", "multiqc_data_dir"],
    },
)
def multiqc(
    adata: AnnData,
    output_dir: str = ".",
    filename: Optional[str] = None,
    force: bool = False,
    modules: Optional[Union[str, List[str]]] = None,
    exclude: Optional[Union[str, List[str]]] = None,
    data_format: Optional[str] = None,
    data_dir: Optional[bool] = None,
    export_plots: bool = False,
    template: Optional[str] = None,
    dirs: bool = False,
    dirs_depth: Optional[int] = None,
    ignore: Optional[Union[str, List[str]]] = None,
    file_list: Optional[str] = None,
    pdf: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    cl_config: Optional[str] = None,
    extra_args: Optional[Sequence[str]] = None,
) -> AnnData:
    """Aggregate QC reports with MultiQC.

    Parameters
    ----------
    adata
        AnnData object whose ``obs['fastqc_html']`` column contains paths to
        FastQC HTML files. The parent directories of each non-empty path are
        collected and passed to MultiQC as scan targets.
    output_dir
        Output directory for the report and data files. Default: current dir.
    filename
        Custom report filename (e.g., ``"my_report.html"``).
    force
        Overwrite existing report files (``-f``).
    modules
        Only run the specified modules (e.g., ``["fastqc", "cutadapt"]``).
    exclude
        Run all modules except those listed.
    data_format
        Output format for parsed data: ``'tsv'`` (default), ``'json'``, or
        ``'yaml'``.
    data_dir
        Force (``True``) or suppress (``False``) creation of
        ``multiqc_data/`` directory. ``None`` lets MultiQC decide.
    export_plots
        Export plots as standalone PNG files (``-p``).
    template
        Use a different report template (``-t``).
    dirs
        Prepend directory path to sample names (``-d``).
    dirs_depth
        Number of directory levels to prepend (``-dd``).
    ignore
        Ignore files/directories matching glob pattern(s) (``-x``).
    file_list
        Path to a file containing a list of file paths to search.
    pdf
        Generate a PDF report (requires Pandoc + LaTeX).
    verbose
        Increase verbosity (``-v``).
    quiet
        Suppress output (``-q``).
    cl_config
        Additional configuration as a YAML string (``--cl-config``).
    extra_args
        Additional arguments passed directly to multiqc.

    Returns
    -------
    AnnData
        The input ``adata`` with per-sample QC metrics written to
        ``adata.obs`` (prefix ``multiqc_``) and report paths stored in
        ``adata.uns``.
    """
    # ── Collect input directories from fastqc_html ──
    dir_list = sorted(
        set(
            str(Path(p).parent)
            for p in adata.obs["fastqc_html"]
            if p
        )
    )

    if not dir_list:
        raise ValueError(
            "No valid FastQC HTML paths found in adata.obs['fastqc_html']. "
            "Run sa.fastq.fastqc() first."
        )

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    cmd = ["multiqc"]

    # Output directory
    cmd.extend(["-o", str(out_path)])

    # Force overwrite
    if force:
        cmd.append("-f")

    # Custom filename
    if filename is not None:
        cmd.extend(["-n", filename])

    # Modules
    if modules is not None:
        mods = [modules] if isinstance(modules, str) else list(modules)
        for m in mods:
            cmd.extend(["-m", m])

    # Exclude modules
    if exclude is not None:
        exc = [exclude] if isinstance(exclude, str) else list(exclude)
        for e in exc:
            cmd.extend(["-e", e])

    # Data format
    if data_format is not None:
        cmd.extend(["-k", data_format])

    # Data directory
    if data_dir is True:
        cmd.append("--data-dir")
    elif data_dir is False:
        cmd.append("--no-data-dir")

    # Export plots
    if export_plots:
        cmd.append("-p")

    # Template
    if template is not None:
        cmd.extend(["-t", template])

    # Dirs / dirs-depth
    if dirs:
        cmd.append("-d")
    if dirs_depth is not None:
        cmd.extend(["-dd", str(dirs_depth)])

    # Ignore patterns
    if ignore is not None:
        ign = [ignore] if isinstance(ignore, str) else list(ignore)
        for i in ign:
            cmd.extend(["-x", i])

    # File list
    if file_list is not None:
        cmd.extend(["--file-list", file_list])

    # PDF
    if pdf:
        cmd.append("--pdf")

    # Verbose / quiet
    if verbose:
        cmd.append("-v")
    if quiet:
        cmd.append("-q")

    # CL config
    if cl_config is not None:
        cmd.extend(["--cl-config", cl_config])

    # Extra arguments
    if extra_args:
        cmd.extend(extra_args)

    # Input directories
    cmd.extend(dir_list)

    # ── Run MultiQC ──
    run_cli_cmd(cmd)

    # ── Store paths in adata.uns ──
    adata.uns["multiqc_dir"] = str(out_path.resolve())
    report_name = filename or "multiqc_report.html"
    adata.uns["multiqc_html"] = str((out_path / report_name).resolve())
    data_dir_path = out_path / "multiqc_data"
    if data_dir_path.exists():
        adata.uns["multiqc_data_dir"] = str(data_dir_path.resolve())

    # ── Extract per-sample QC metrics ──
    data_json = data_dir_path / "multiqc_data.json"
    fastqc_txt = data_dir_path / "multiqc_fastqc.txt"

    if data_dir_path.exists() and data_json.exists():
        with data_json.open() as f:
            mqc_data = json.load(f)

        sample_map = _build_sample_map(adata, data_dir_path)

        if sample_map:
            # General stats
            stats = _extract_general_stats(mqc_data, sample_map)
            if stats:
                for obs_suffix, _, _, _ in _QC_METRICS:
                    col = f"multiqc_{obs_suffix}"
                    series = {}
                    for on in adata.obs_names:
                        series[on] = stats.get(on, {}).get(obs_suffix, None)
                    adata.obs[col] = pd.Series(series, dtype="object")

            # FastQC module pass/fail/warn
            modules_data = _extract_fastqc_modules(fastqc_txt, sample_map)
            if modules_data:
                all_module_keys: set[str] = set()
                for v in modules_data.values():
                    all_module_keys.update(v.keys())
                for mod_key in sorted(all_module_keys):
                    series = {}
                    for on in adata.obs_names:
                        series[on] = modules_data.get(on, {}).get(mod_key, None)
                    adata.obs[f"multiqc_{mod_key}"] = pd.Series(
                        series, dtype="object"
                    )

            # Raw QC metrics from report_saved_raw_data
            raw_qc = _extract_raw_qc(mqc_data, sample_map)
            if raw_qc:
                for obs_suffix, _ in _RAW_QC_METRICS:
                    col = f"multiqc_{obs_suffix}"
                    series = {}
                    for on in adata.obs_names:
                        series[on] = raw_qc.get(on, {}).get(obs_suffix, None)
                    if any(v is not None for v in series.values()):
                        adata.obs[col] = pd.Series(series, dtype="float64")

    return adata
