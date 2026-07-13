"""cutadapt wrapper for adapter / quality trimming of FASTQ data.

Wraps `cutadapt <https://cutadapt.readthedocs.io/>`_, a widely used tool for
finding and removing adapter sequences, primers, poly-A tails, and other
types of unwanted sequence from high-throughput sequencing reads.

Key use cases for sRNA-seq:
  - **3' adapter trimming**: sRNA-seq libraries ligate adapters to both ends;
    the 3' adapter must be removed before mapping.
  - **Length filtering**: small RNAs (miRNAs, piRNAs) are 18-30 nt; discard
    reads outside that range.
  - **Quality trimming**: remove low-quality 3' bases.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import pandas as pd
from anndata import AnnData

from ..._registry import register_function
from ..._utils import run_cli_cmd, run_threads


def _parse_json_report(json_path: Path) -> dict:
    """Read a cutadapt JSON report and return its contents as a dict."""
    if json_path.exists():
        try:
            return json.loads(json_path.read_text())
        except Exception:
            return {}
    return {}


def _find_report(output_dir: Path, sample: str) -> Optional[Path]:
    """Locate a cutadapt JSON report for a sample."""
    candidates = [
        output_dir / f"{sample}.cutadapt.json",
        output_dir / "reports" / f"{sample}.cutadapt.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _build_adapter_args(
    adapters: Optional[Sequence[str]],
    adapter_file: Optional[str],
    flag: str,
) -> List[str]:
    """Convert adapter sequences or FASTA file to cutadapt CLI args."""
    args: List[str] = []
    if adapters:
        for a in adapters:
            args.extend([flag, a])
    if adapter_file:
        args.extend([flag, f"file:{adapter_file}"])
    return args


def _build_sample_input(
    fq1: str,
    fq2: Optional[str],
    paired: bool,
) -> Tuple[str, ...]:
    """Build the positional input arguments for cutadapt."""
    if paired and fq2:
        return (fq1, fq2)
    return (fq1,)


def _run_cutadapt(
    sample: str,
    fq1: str,
    fq2: Optional[str],
    output_dir: str,
    paired: bool,
    # Adapter options
    adapter_3: Optional[Sequence[str]],
    adapter_5: Optional[Sequence[str]],
    adapter_any: Optional[Sequence[str]],
    adapter_file_3: Optional[str],
    adapter_file_5: Optional[str],
    adapter_file_any: Optional[str],
    # Paired-end adapter options (R2)
    adapter_3_r2: Optional[Sequence[str]],
    adapter_5_r2: Optional[Sequence[str]],
    adapter_any_r2: Optional[Sequence[str]],
    # Adapter matching
    error_rate: Optional[float],
    min_overlap: int,
    no_indels: bool,
    times: int,
    # Quality trimming
    quality_cutoff: Optional[str],
    nextseq_trim: Optional[int],
    # Length trimming / filtering
    cut: Optional[Union[int, str]],
    cut_r2: Optional[Union[int, str]],
    min_length: Optional[int],
    max_length: Optional[int],
    max_n: Optional[Union[int, float]],
    trim_n: bool,
    # Poly-A
    poly_a: bool,
    # Read modification
    action: Optional[str],
    revcomp: bool,
    # Output options
    json_report: bool,
    report: Optional[str],
    info_file: Optional[str],
    quiet: bool,
    # Misc
    gc_content: Optional[float],
    extra_args: Optional[Sequence[str]],
    overwrite: bool,
) -> Dict[str, Union[str, dict]]:
    """Run cutadapt on a single sample.

    Parameters
    ----------
    sample
        Sample name.
    fq1
        Path to R1 FASTQ file.
    fq2
        Path to R2 FASTQ file (None for single-end).
    output_dir
        Root output directory; per-sample subdirectories are created.
    paired
        Whether the data is paired-end.
    overwrite
        Re-run cutadapt even if the output already exists.
    **kwargs
        All remaining cutadapt parameters (see the public
        :func:`cutadapt` function for descriptions).

    Returns
    -------
    dict
        Keys: ``sample``, ``fq1``, ``fq2`` (if paired), ``json``,
        and ``report`` (parsed JSON dict, if available).
    """
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    sample_dir = out_root / sample
    sample_dir.mkdir(parents=True, exist_ok=True)

    # Determine output paths
    fq1_out = str(sample_dir / f"{sample}_trimmed.fastq.gz")
    json_out = str(sample_dir / f"{sample}.cutadapt.json")

    # Guard: skip if output already exists (unless overwrite)
    if not overwrite and os.path.isfile(fq1_out) and os.path.getsize(fq1_out) > 0:
        print(f"[cutadapt] Skipping {sample}: output already exists at {fq1_out}", flush=True)
        result: Dict[str, Union[str, dict]] = {
            "sample": sample,
            "fq1": fq1_out,
            "json": json_out,
        }
        if paired:
            fq2_out = str(sample_dir / f"{sample}_trimmed_R2.fastq.gz")
            result["fq2"] = fq2_out
        report_data = _parse_json_report(Path(json_out))
        if report_data:
            result["report"] = report_data
        return result

    # Resolve executable — cutadapt should be on PATH
    cmd = ["cutadapt"]

    # Adapter arguments
    cmd.extend(_build_adapter_args(adapter_3, adapter_file_3, "-a"))
    cmd.extend(_build_adapter_args(adapter_5, adapter_file_5, "-g"))
    cmd.extend(_build_adapter_args(adapter_any, adapter_file_any, "-b"))

    if paired:
        cmd.extend(_build_adapter_args(adapter_3_r2, None, "-A"))
        cmd.extend(_build_adapter_args(adapter_5_r2, None, "-G"))
        cmd.extend(_build_adapter_args(adapter_any_r2, None, "-B"))

    # Adapter matching parameters
    if error_rate is not None:
        cmd.extend(["-e", str(error_rate)])
    if min_overlap != 3:
        cmd.extend(["-O", str(min_overlap)])
    if no_indels:
        cmd.append("--no-indels")
    if times > 1:
        cmd.extend(["-n", str(times)])

    # Quality trimming
    if quality_cutoff is not None:
        cmd.extend(["-q", quality_cutoff])
    if nextseq_trim is not None:
        cmd.extend(["--nextseq-trim", str(nextseq_trim)])

    # Length trimming
    if cut is not None:
        cmd.extend(["-u", str(cut)])
    if paired and cut_r2 is not None:
        cmd.extend(["-U", str(cut_r2)])

    # Length filtering
    if min_length is not None:
        cmd.extend(["-m", str(min_length)])
    if max_length is not None:
        cmd.extend(["-M", str(max_length)])
    if max_n is not None:
        cmd.extend(["--max-n", str(max_n)])
    if trim_n:
        cmd.append("--trim-n")

    # Poly-A
    if poly_a:
        cmd.append("--poly-a")

    # Read modification
    if action is not None:
        cmd.extend(["--action", action])
    if revcomp:
        cmd.append("--revcomp")

    # Output options
    cmd.extend(["-o", fq1_out])
    if paired:
        fq2_out = str(sample_dir / f"{sample}_trimmed_R2.fastq.gz")
        cmd.extend(["-p", fq2_out])

    # Reports
    if json_report:
        cmd.extend(["--json", json_out])
    if report is not None:
        cmd.extend(["--report", report])
    if info_file is not None:
        cmd.extend(["--info-file", info_file])
    if quiet:
        cmd.append("--quiet")

    # Misc
    if gc_content is not None:
        cmd.extend(["--gc-content", str(gc_content)])

    # Extra user-supplied arguments
    if extra_args:
        cmd.extend(extra_args)

    # Input files
    cmd.extend(_build_sample_input(fq1, fq2, paired and fq2 is not None))

    # Run
    run_cli_cmd(cmd)

    result: Dict[str, Union[str, dict]] = {
        "sample": sample,
        "fq1": fq1_out,
        "json": json_out,
    }
    if paired:
        result["fq2"] = fq2_out

    report_data = _parse_json_report(Path(json_out))
    if report_data:
        result["report"] = report_data

    return result


@register_function(
    aliases=[
        "cutadapt", "trim_adapter", "adapter_trimming",
        "接头修剪", "adapter_trim", "trim",
    ],
    category="fastq",
    description=(
        "Trim adapters, primers, poly-A tails, and low-quality bases from "
        "FASTQ reads using cutadapt. "
        "Reads input paths from ``adata.obs[\"fastq_path\"]`` (set by "
        "``fastq_dl``) and writes trimmed paths to "
        "``adata.obs[\"trimmed_path\"]``. "
        "Supports single-end data, multiple adapters, length filtering, "
        "and JSON report output."
    ),
    examples=[
        (
            'sa.fastq.cutadapt(adata, adapter_3="TGGAATTCTCGGGTGCCAAGG", '
            'min_length=18, max_length=30, quality_cutoff="20")'
        ),
        (
            'sa.fastq.cutadapt(adata, adapter_3="TGGAATTCTCGGGTGCCAAGG", '
            'output_dir="trimmed", jobs=2)'
        ),
        (
            'sa.fastq.cutadapt(adata, adapter_3="ACGT", '
            'quality_cutoff="15,10", json_report=True)'
        ),
    ],
    related=[
        "fastq.fastqc", "fastq.fastq_dl",
    ],
    produces={"obs": ["trimmed_path", "cutadapt_json", "cutadapt_report"]},
)
def cutadapt(
    adata: AnnData,
    output_dir: str = "trimmed",
    # Adapter options — 3' (most common for sRNA-seq)
    adapter_3: Optional[Union[str, List[str]]] = None,
    adapter_5: Optional[Union[str, List[str]]] = None,
    adapter_any: Optional[Union[str, List[str]]] = None,
    adapter_file_3: Optional[str] = None,
    adapter_file_5: Optional[str] = None,
    adapter_file_any: Optional[str] = None,
    # Paired-end R2 adapters
    adapter_3_r2: Optional[Union[str, List[str]]] = None,
    adapter_5_r2: Optional[Union[str, List[str]]] = None,
    adapter_any_r2: Optional[Union[str, List[str]]] = None,
    # Adapter matching parameters
    error_rate: Optional[float] = None,
    min_overlap: int = 3,
    no_indels: bool = False,
    times: int = 1,
    # Quality trimming
    quality_cutoff: Optional[str] = None,
    nextseq_trim: Optional[int] = None,
    # Fixed-length trimming
    cut: Optional[Union[int, str]] = None,
    cut_r2: Optional[Union[int, str]] = None,
    # Length filtering
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
    max_n: Optional[Union[int, float]] = None,
    trim_n: bool = False,
    # Poly-A
    poly_a: bool = False,
    # Read modification
    action: Optional[str] = None,
    revcomp: bool = False,
    # Output / reporting
    json_report: bool = True,
    report: Optional[str] = None,
    info_file: Optional[str] = None,
    quiet: bool = False,
    # Misc
    gc_content: Optional[float] = None,
    extra_args: Optional[Sequence[str]] = None,
    jobs: Optional[int] = None,
    overwrite: bool = False,
) -> AnnData:
    """Trim adapters and low-quality bases from FASTQ reads with cutadapt.

    Operates on an :class:`~anndata.AnnData` object whose ``obs``
    contains a ``fastq_path`` column (set by :func:`fastq_dl`).
    After trimming, each sample's trimmed FASTQ path is stored back
    in ``adata.obs[\"trimmed_path\"]``.

    Parameters
    ----------
    adata
        AnnData object with ``adata.obs[\"fastq_path\"]`` containing
        paths to the input FASTQ files (one per observation/sample).
    output_dir
        Output directory. Per-sample subdirectories are created.
    adapter_3
        3' adapter sequence(s) to trim (``-a``). This is the most common
        option for sRNA-seq 3' adapter removal.
    adapter_5
        5' adapter sequence(s) to trim (``-g``).
    adapter_any
        Adapter sequence(s) found at either end (``-b``).
    adapter_file_3
        FASTA file containing 3' adapter sequences (``-a file:...``).
    adapter_file_5
        FASTA file containing 5' adapter sequences (``-g file:...``).
    adapter_file_any
        FASTA file containing anywhere adapters (``-b file:...``).
    adapter_3_r2
        3' adapter(s) for R2 in paired-end mode (``-A``).
    adapter_5_r2
        5' adapter(s) for R2 in paired-end mode (``-G``).
    adapter_any_r2
        Anywhere adapter(s) for R2 in paired-end mode (``-B``).
    error_rate
        Maximum allowed error rate (0-1). Default 0.1 if not set.
    min_overlap
        Minimum adapter overlap length. Default 3.
    no_indels
        Disallow indels in adapter matching.
    times
        Maximum number of times to trim each read. Default 1.
    quality_cutoff
        Quality trimming cutoff. A single number (3' only) or
        ``\"<5prime>,<3prime>\"`` (both ends). Passed to ``-q``.
    nextseq_trim
        NextSeq/NovaSeq quality trimming cutoff (``--nextseq-trim``).
    cut
        Remove fixed number of bases from the beginning (positive) or
        end (negative). Passed to ``-u``.
    cut_r2
        Same as *cut* but for R2 (``-U``). Only for paired-end.
    min_length
        Discard reads shorter than this (``-m``). For sRNA-seq, typically 18.
    max_length
        Discard reads longer than this (``-M``). For sRNA-seq, typically 30
        (miRNA) or 36 (total sRNA).
    max_n
        Discard reads with more than this many N bases. If between 0 and 1,
        treated as a fraction of read length.
    trim_n
        Trim flanking N bases.
    poly_a
        Trim poly-A / poly-T tails (``--poly-a``).
    action
        Action when adapter is found: ``'trim'`` (default), ``'retain'``,
        ``'mask'``, ``'lowercase'``, ``'crop'``, or ``'none'``.
    revcomp
        Also look for reverse-complement of adapters (``--revcomp``).
    json_report
        Write JSON report. Default ``True``.
    report
        Report style: ``'full'`` (default) or ``'minimal'``.
    info_file
        Path for detailed per-read info TSV (``--info-file``).
    quiet
        Suppress non-critical output (``--quiet``).
    gc_content
        Expected GC content (percent) for better estimates (``--gc-content``).
    extra_args
        Additional arguments passed directly to cutadapt.
    jobs
        Number of samples to process concurrently. Default 1 (sequential).
    overwrite
        Re-run even if output files exist.

    Returns
    -------
    AnnData
        The input ``adata`` with ``adata.obs[\"trimmed_path\"]`` added
        (path to the trimmed FASTQ for each sample).

    Examples
    --------
    >>> import sRNAgent as sa

    >>> # Single sRNA-seq sample: trim 3' adapter and length-filter
    >>> adata = sa.fastq.cutadapt(
    ...     adata,
    ...     adapter_3="TGGAATTCTCGGGTGCCAAGG",
    ...     min_length=18, max_length=30,
    ...     quality_cutoff="20",
    ...     output_dir="trimmed",
    ... )
    >>> adata.obs["trimmed_path"]
    S1    trimmed/S1/S1_trimmed.fastq.gz
    Name: trimmed_path, dtype: object

    >>> # Multiple samples with batch processing
    >>> adata = sa.fastq.cutadapt(adata, output_dir="trimmed", jobs=2)
    >>> adata.obs["trimmed_path"]
    S1    trimmed/S1/S1_trimmed.fastq.gz
    S2    trimmed/S2/S2_trimmed.fastq.gz
    Name: trimmed_path, dtype: object
    """
    # Validate that fastq_path exists in obs
    if "fastq_path" not in adata.obs:
        raise KeyError(
            "adata.obs must contain a 'fastq_path' column. "
            "Run fastq_dl first to populate it."
        )

    # Build sample list from adata
    sample_list = [
        (name, str(adata.obs.loc[name, "fastq_path"]))
        for name in adata.obs_names
    ]

    # Process adapters into lists
    def _to_list(val):
        if val is None:
            return None
        return [val] if isinstance(val, str) else list(val)

    al3 = _to_list(adapter_3)
    al5 = _to_list(adapter_5)
    ala = _to_list(adapter_any)
    al3_r2 = _to_list(adapter_3_r2)
    al5_r2 = _to_list(adapter_5_r2)
    ala_r2 = _to_list(adapter_any_r2)

    def _process_one(sample_tuple) -> Dict:
        name, r1 = sample_tuple
        return _run_cutadapt(
            sample=name,
            fq1=str(r1),
            fq2=None,
            output_dir=output_dir,
            paired=False,
            # Adapter options
            adapter_3=al3,
            adapter_5=al5,
            adapter_any=ala,
            adapter_file_3=adapter_file_3,
            adapter_file_5=adapter_file_5,
            adapter_file_any=adapter_file_any,
            adapter_3_r2=al3_r2,
            adapter_5_r2=al5_r2,
            adapter_any_r2=ala_r2,
            # Adapter matching
            error_rate=error_rate,
            min_overlap=min_overlap,
            no_indels=no_indels,
            times=times,
            # Quality trimming
            quality_cutoff=quality_cutoff,
            nextseq_trim=nextseq_trim,
            # Length trimming / filtering
            cut=cut,
            cut_r2=cut_r2,
            min_length=min_length,
            max_length=max_length,
            max_n=max_n,
            trim_n=trim_n,
            # Poly-A
            poly_a=poly_a,
            # Read modification
            action=action,
            revcomp=revcomp,
            # Output
            json_report=json_report,
            report=report,
            info_file=info_file,
            quiet=quiet,
            # Misc
            gc_content=gc_content,
            extra_args=extra_args,
            overwrite=overwrite,
        )

    results = run_threads(sample_list, _process_one, jobs)

    # Write cutadapt outputs back to adata.obs
    trimmed_map = {r["sample"]: r["fq1"] for r in results}
    adata.obs["trimmed_path"] = pd.Series(
        trimmed_map, index=adata.obs_names, dtype="object"
    )
    json_map = {r["sample"]: r.get("json", "") for r in results}
    adata.obs["cutadapt_json"] = pd.Series(
        json_map, index=adata.obs_names, dtype="object"
    )
    report_map = {r["sample"]: r.get("report", {}) for r in results}
    adata.obs["cutadapt_report"] = pd.Series(
        report_map, index=adata.obs_names, dtype="object"
    )

    return adata
