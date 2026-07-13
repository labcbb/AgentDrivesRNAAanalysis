"""MultiQC wrapper for aggregating QC reports from multiple tools.

Wraps `MultiQC <https://multiqc.info/>`_, a tool that scans directories for
log files from supported bioinformatics tools (FastQC, cutadapt, STAR, etc.)
and produces a single aggregated HTML report with summary plots and tables.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from anndata import AnnData

from ..._registry import register_function
from ..._utils import run_cli_cmd


@register_function(
    aliases=[
        "multiqc", "aggregate_qc", "merge_qc", "汇总报告",
        "qc_aggregation",
    ],
    category="fastq",
    description=(
        "Aggregate QC reports from multiple bioinformatics tools into a "
        "single HTML report using MultiQC. Scans directories containing "
        "FastQC HTML outputs (recorded in ``adata.obs['fastqc_html']``) "
        "for recognised log files and produces an interactive summary report."
    ),
    examples=[
        'sa.fastq.multiqc(adata, filename="my_report.html")',
        'sa.fastq.multiqc(adata, force=True, modules=["fastqc", "cutadapt"])',
    ],
    related=[
        "fastq.fastqc", "fastq.cutadapt",
    ],
    produces={"uns": ["multiqc_dir"]},
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
        Output format for parsed data: ``'tsv'`` (default), ``'json'``, or ``'yaml'``.
    data_dir
        Force (``True``) or suppress (``False``) creation of ``multiqc_data/``
        directory. ``None`` lets MultiQC decide based on other options.
    export_plots
        Export plots as standalone PNG files (``-p``).
    template
        Use a different report template (``-t``).
    dirs
        Prepend directory path to sample names (``-d``).
    dirs_depth
        Number of directory levels to prepend (``-dd``). Positive = from end,
        negative = from start.
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
        The input ``adata`` with ``adata.uns['multiqc_dir']`` set to the
        output directory path.
    """
    # Collect parent directories from fastqc_html entries
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

    # Run
    run_cli_cmd(cmd)

    # Store output directory in adata.uns
    adata.uns["multiqc_dir"] = str(out_path.resolve())
    report_name = filename or "multiqc_report.html"
    adata.uns["multiqc_html"] = str((out_path / report_name).resolve())
    data_path = out_path / "multiqc_data"
    if data_path.exists():
        adata.uns["multiqc_data_dir"] = str(data_path.resolve())

    return adata
