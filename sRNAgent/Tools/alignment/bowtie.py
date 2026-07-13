"""Bowtie wrapper for short-read alignment and index building.

Wraps `Bowtie <https://bowtie-bio.sourceforge.net/>`_, a fast, short-read
aligner commonly used for sRNA-seq analysis. Supports single-end and
paired-end alignment with configurable mismatch tolerance, seed length, and
multi-mapping reporting.

Key sRNA-seq use cases:
  - **Stringent mapping** (``-v 0 -m 1``): uniquely mapped, perfect match
  - **Permissive mapping** (``-v 1 -k 10``): allow 1 mismatch, up to 10 hits
  - **Seed-based mapping** (``-n 1 -l 18``): seed-based for short reads
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import pandas as pd
from anndata import AnnData

from ..._registry import register_function
from ..._utils import run_cli_cmd, run_threads


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _discover_sam(output_dir: str, sample: str) -> str:
    """Locate the SAM output for a sample, or return the expected path."""
    expected = str(Path(output_dir) / f"{sample}.sam")
    return expected


# ---------------------------------------------------------------------------
# bowtie-build
# ---------------------------------------------------------------------------

@register_function(
    aliases=[
        "bowtie_build", "bowtie-build", "build_index", "index",
        "构建索引",
    ],
    category="alignment",
    description=(
        "Build a Bowtie genome index from FASTA reference sequences. "
        "Produces .ebwt / .ebwtl index files needed for alignment."
    ),
    examples=[
        'sa.alignment.bowtie_build("genome.fa", "mm10")',
        'sa.alignment.bowtie_build(["chr1.fa", "chr2.fa"], "my_index", threads=4)',
    ],
    related=["alignment.bowtie"],
)
def bowtie_build(
    reference: Union[str, List[str]],
    index_basename: str,
    offrate: Optional[int] = None,
    threads: int = 1,
    verbose: bool = False,
    extra_args: Optional[Sequence[str]] = None,
) -> Dict[str, str]:
    """Build a Bowtie index from reference FASTA sequences.

    Parameters
    ----------
    reference
        One or more FASTA file paths containing reference sequences.
    index_basename
        Basename for the output index files (e.g., ``"mm10"``).
    offrate
        Override index offrate. Smaller values = faster alignment but
        more memory (``-o``). Default uses Bowtie's built-in default.
    threads
        Threads for index building (``-p``).
    verbose
        Print verbose output (``--verbose``).
    extra_args
        Additional arguments passed directly to bowtie-build.

    Returns
    -------
    dict
        ``{"index_basename": "<path>", "directory": "<dir>"}``
    """
    ref_list = [reference] if isinstance(reference, str) else list(reference)
    basename = str(Path(index_basename))

    cmd = ["bowtie-build"]

    if offrate is not None:
        cmd.extend(["-o", str(offrate)])
    if threads > 1:
        cmd.extend(["-p", str(threads)])
    if verbose:
        cmd.append("--verbose")
    if extra_args:
        cmd.extend(extra_args)

    cmd.extend(ref_list)
    cmd.append(basename)

    run_cli_cmd(cmd)

    return {
        "index_basename": str(Path(basename).resolve()),
        "directory": str(Path(basename).parent.resolve()),
    }


# ---------------------------------------------------------------------------
# bowtie aligner
# ---------------------------------------------------------------------------

def _run_bowtie_one(
    sample: str,
    fq1: str,
    fq2: Optional[str],
    index_basename: str,
    output_dir: str,
    # Input format
    input_format: str,
    trim5: Optional[int],
    trim3: Optional[int],
    skip: Optional[int],
    upto: Optional[int],
    # Alignment
    seed_mismatches: Optional[int],
    total_mismatches: Optional[int],
    seed_len: Optional[int],
    maqerr: Optional[int],
    nomaqround: bool,
    minins: Optional[int],
    maxins: Optional[int],
    fr: bool,
    rf: bool,
    ff: bool,
    nofw: bool,
    norc: bool,
    tryhard: bool,
    # Reporting
    k: Optional[int],
    report_all: bool,
    m: Optional[int],
    M: Optional[int],
    best: bool,
    strata: bool,
    # Output / SAM
    sam_out: bool,
    no_unal: bool,
    mapq: Optional[int],
    quiet: bool,
    # Performance
    threads: int,
    offrate: Optional[int],
    reorder: bool,
    mm: bool,
    shmem: bool,
    # Misc
    extra_args: Optional[Sequence[str]],
) -> Dict[str, str]:
    """Run bowtie on a single sample."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sam_path = out_dir / f"{sample}.sam"

    # Skip if output already exists
    if sam_path.exists() and sam_path.stat().st_size > 0:
        print(f"[bowtie] Skipping {sample}: {sam_path} already exists", flush=True)
        log_out = str(sample_dir / f"{sample}.bowtie.log")
        return {"sample": sample, "sam": str(sam_path), "log": log_out, "metrics": {}}

    cmd = ["bowtie"]

    # Input format
    fmt_map = {"fastq": "-q", "fasta": "-f", "raw": "-r"}
    cmd.append(fmt_map.get(input_format, "-q"))

    # Trim
    if trim5 is not None:
        cmd.extend(["-5", str(trim5)])
    if trim3 is not None:
        cmd.extend(["-3", str(trim3)])
    if skip is not None:
        cmd.extend(["-s", str(skip)])
    if upto is not None:
        cmd.extend(["-u", str(upto)])

    # Alignment: -v mode takes precedence over -n mode
    if total_mismatches is not None:
        cmd.extend(["-v", str(total_mismatches)])
    elif seed_mismatches is not None:
        cmd.extend(["-n", str(seed_mismatches)])

    if seed_len is not None:
        cmd.extend(["-l", str(seed_len)])
    if maqerr is not None:
        cmd.extend(["-e", str(maqerr)])
    if nomaqround:
        cmd.append("--nomaqround")

    # Paired-end insert size
    if minins is not None:
        cmd.extend(["-I", str(minins)])
    if maxins is not None:
        cmd.extend(["-X", str(maxins)])

    # Mate orientation
    if fr:
        cmd.append("--fr")
    elif rf:
        cmd.append("--rf")
    elif ff:
        cmd.append("--ff")

    # Strand
    if nofw:
        cmd.append("--nofw")
    if norc:
        cmd.append("--norc")

    if tryhard:
        cmd.append("-y")

    # Reporting
    if k is not None:
        cmd.extend(["-k", str(k)])
    if report_all:
        cmd.append("-a")
    if m is not None:
        cmd.extend(["-m", str(m)])
    if M is not None:
        cmd.extend(["-M", str(M)])
    if best:
        cmd.append("--best")
    if strata:
        cmd.append("--strata")

    # SAM output
    if sam_out:
        cmd.append("-S")
    if no_unal:
        cmd.append("--no-unal")
    if mapq is not None:
        cmd.extend(["--mapq", str(mapq)])

    # Performance
    if threads > 1:
        cmd.extend(["-p", str(threads)])
    if offrate is not None:
        cmd.extend(["-o", str(offrate)])
    if reorder:
        cmd.append("--reorder")
    if mm:
        cmd.append("--mm")
    if shmem:
        cmd.append("--shmem")

    if quiet:
        cmd.append("--quiet")

    # Index
    cmd.extend(["-x", index_basename])

    # Input files
    is_paired = fq2 is not None and str(fq2).strip() != ""
    if is_paired:
        cmd.extend(["-1", fq1, "-2", fq2])
    else:
        cmd.append(fq1)

    if extra_args:
        cmd.extend(extra_args)

    # Redirect SAM output to file
    cmd.extend([str(sam_path)])

    # Run with log capture
    log_out = str(sample_dir / f"{sample}.bowtie.log")
    print(">>", " ".join(str(c) for c in cmd), flush=True)
    log_metrics: Dict[str, Union[str, float, int, None]] = {}
    with open(log_out, "w") as log_f:
        proc = subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                print(line, end="", flush=True)
                log_f.write(line)
        finally:
            proc.stdout.close()
        ret = proc.wait()
    if ret != 0:
        raise RuntimeError(f"bowtie failed for {sample} (exit code {ret})")

    if not sam_path.exists():
        raise RuntimeError(f"bowtie failed to produce {sam_path}")

    # Parse bowtie alignment metrics from log
    with open(log_out) as f:
        log_text = f.read()

    m_total = re.search(r"# reads processed:\s+(\d+)", log_text)
    m_aligned = re.search(
        r"# reads with at least one reported alignment:\s+(\d+)\s+\((\d+\.?\d*)%\)",
        log_text,
    )
    m_failed = re.search(
        r"# reads that failed to align:\s+(\d+)\s+\((\d+\.?\d*)%\)", log_text,
    )
    m_suppressed = re.search(
        r"# reads with alignments suppressed due to -m:\s+(\d+)", log_text,
    )
    m_reported = re.search(r"Reported\s+(\d+)\s+alignments", log_text)

    if m_total:
        log_metrics["total_reads"] = int(m_total.group(1))
    if m_aligned:
        log_metrics["aligned_reads"] = int(m_aligned.group(1))
        log_metrics["alignment_rate"] = float(m_aligned.group(2))
    if m_failed:
        log_metrics["unaligned_reads"] = int(m_failed.group(1))
    if m_suppressed:
        log_metrics["suppressed_reads"] = int(m_suppressed.group(1))
    if m_reported:
        log_metrics["reported_alignments"] = int(m_reported.group(1))

    return {
        "sample": sample,
        "sam": str(sam_path),
        "log": log_out,
        "metrics": log_metrics,
    }


@register_function(
    aliases=[
        "bowtie", "align", "bowtie_align", "比对",
        "shotgun_align",
    ],
    category="alignment",
    description=(
        "Align sRNA-seq or short-read FASTQ reads to a reference genome "
        "using Bowtie. Supports configurable mismatch tolerance, seed "
        "length, multi-mapping reporting, and SAM output."
    ),
    examples=[
        '>>> result = sa.alignment.bowtie(',
        '...     adata,',
        '...     index_basename="mm10",',
        '...     total_mismatches=0,',
        '...     m=1,',
        '...     best=True,',
        '...     output_dir="aligned",',
        '... )',
        '>>> result.obs["sam_path"]',
        '>>> result.uns["genome_index"]',
    ],
    related=[
        "alignment.bowtie_build",
    ],
    produces={
        "obs": [
            "sam_path", "bowtie_log",
            "bowtie_total_reads", "bowtie_aligned_reads",
            "bowtie_alignment_rate", "bowtie_unaligned_reads",
            "bowtie_suppressed_reads", "bowtie_reported_alignments",
        ],
        "uns": ["genome_index"],
    },
)
def bowtie(
    adata: AnnData,
    index_basename: str = "index",
    output_dir: str = "aligned",
    # Input format
    input_format: str = "fastq",
    trim5: Optional[int] = None,
    trim3: Optional[int] = None,
    skip: Optional[int] = None,
    upto: Optional[int] = None,
    # Alignment
    seed_mismatches: Optional[int] = None,
    total_mismatches: Optional[int] = None,
    seed_len: Optional[int] = None,
    maqerr: Optional[int] = None,
    nomaqround: bool = False,
    minins: Optional[int] = None,
    maxins: Optional[int] = None,
    fr: bool = True,
    rf: bool = False,
    ff: bool = False,
    nofw: bool = False,
    norc: bool = False,
    tryhard: bool = False,
    # Reporting
    k: Optional[int] = None,
    report_all: bool = False,
    m: Optional[int] = None,
    M: Optional[int] = None,
    best: bool = False,
    strata: bool = False,
    # Output / SAM
    sam_out: bool = True,
    no_unal: bool = False,
    mapq: Optional[int] = None,
    quiet: bool = False,
    # Performance
    threads: int = 1,
    offrate: Optional[int] = None,
    reorder: bool = False,
    mm: bool = False,
    shmem: bool = False,
    # Execution
    jobs: Optional[int] = None,
    extra_args: Optional[Sequence[str]] = None,
) -> AnnData:
    """Align sRNA-seq or short-read FASTQ reads with Bowtie.

    Reads input FASTQ paths from ``adata.obs["trimmed_path"]`` (or
    ``adata.obs["fastq_path"]`` as fallback). Writes output SAM paths to
    ``adata.obs["sam_path"]`` and stores the index basename in
    ``adata.uns["genome_index"]``.

    Parameters
    ----------
    adata
        Annotated data matrix with FASTQ paths in ``.obs["trimmed_path"]``
        (or ``.obs["fastq_path"]`` as fallback).
    index_basename
        Bowtie index basename (e.g., ``"mm10"``, ``"hg38"``).
    output_dir
        Output directory for SAM files.
    input_format
        Input format: ``'fastq'`` (default), ``'fasta'``, or ``'raw'``.
    trim5
        Trim N bases from 5' end before alignment (``-5``).
    trim3
        Trim N bases from 3' end before alignment (``-3``).
    skip
        Skip the first N reads/pairs (``-s``).
    upto
        Only align the first N reads/pairs (``-u``).
    seed_mismatches
        Max mismatches in seed (``-n``, 0-3). Mutually exclusive with
        *total_mismatches*.
    total_mismatches
        Max total mismatches allowed (``-v``, 0-3). Takes precedence over
        *seed_mismatches*. For stringent sRNA-seq: ``0``.
    seed_len
        Seed length in bases (``-l``). For sRNA-seq, set close to read
        length (e.g., ``18``, ``22``).
    maqerr
        Max total quality at mismatched positions (``-e``). Default 70.
    nomaqround
        Disable Maq-style quality rounding.
    minins
        Minimum insert size for paired-end (``-I``). Default 0.
    maxins
        Maximum insert size for paired-end (``-X``). Default 250.
    fr
        Forward-reverse mate orientation (``--fr``, default).
    rf
        Reverse-forward mate orientation (``--rf``).
    ff
        Forward-forward mate orientation (``--ff``).
    nofw
        Skip forward strand alignment (``--nofw``).
    norc
        Skip reverse-complement strand alignment (``--norc``).
    tryhard
        Try harder to find valid alignments (``-y``).
    k
        Report up to K valid alignments per read (``-k``).
    report_all
        Report all valid alignments (``-a``).
    m
        Suppress reads with >M reportable alignments (``-m``). Use ``1``
        for unique mapping only.
    M
        Like *m* but report one random alignment if ceiling exceeded (``-M``).
    best
        Guarantee best alignments are reported first (``--best``).
        Recommended for sRNA-seq.
    strata
        Report only alignments in the best stratum (``--strata``).
        Requires ``best=True``.
    sam_out
        Output in SAM format (``-S``). Default ``True``.
    no_unal
        Suppress SAM records for unaligned reads (``--no-unal``).
    mapq
        MAPQ score for non-repetitive alignments (``--mapq``).
    quiet
        Print nothing except alignments (``--quiet``).
    threads
        Parallel search threads per bowtie invocation (``-p``). Default 1.
    offrate
        Override index offrate (``-o``). Higher = less memory, slower.
    reorder
        Guarantee output order matches input order (``--reorder``).
    mm
        Use memory-mapped I/O for index loading (``--mm``).
    shmem
        Use shared memory for index loading (``--shmem``).
    jobs
        Number of samples to process concurrently. Default 1 (sequential).
    extra_args
        Additional arguments passed directly to bowtie.

    Returns
    -------
    AnnData
        The input ``adata`` with updated annotations:

        - ``adata.obs["sam_path"]`` -- path to each sample's SAM file
        - ``adata.uns["genome_index"]`` -- the index basename used

    Examples
    --------
    >>> import sRNAgent as sa

    >>> # Stringent sRNA-seq: perfect match, unique only
    >>> result = sa.alignment.bowtie(
    ...     adata,
    ...     index_basename="mm10",
    ...     total_mismatches=0,
    ...     m=1,
    ...     best=True,
    ...     output_dir="aligned",
    ... )
    >>> result.obs["sam_path"]
    >>> result.uns["genome_index"]

    >>> # Permissive: 1 mismatch, up to 10 hits
    >>> result = sa.alignment.bowtie(
    ...     adata,
    ...     index_basename="hg38",
    ...     total_mismatches=1,
    ...     k=10,
    ...     best=True,
    ... )
    """
    # Build sample list from adata.obs
    sample_list: List[Tuple[str, str, Optional[str]]] = []
    for sample_name in adata.obs_names:
        # Resolve FASTQ path -- prefer trimmed_path, fall back to fastq_path
        if "trimmed_path" in adata.obs.columns:
            fq_path = adata.obs.loc[sample_name, "trimmed_path"]
            if not pd.isna(fq_path) and str(fq_path).strip() != "":
                sample_list.append((sample_name, str(fq_path), None))
                continue

        if "fastq_path" in adata.obs.columns:
            fq_path = adata.obs.loc[sample_name, "fastq_path"]
            if not pd.isna(fq_path) and str(fq_path).strip() != "":
                sample_list.append((sample_name, str(fq_path), None))
                continue

        raise KeyError(
            f"Sample '{sample_name}' has neither 'trimmed_path' nor "
            f"'fastq_path' in adata.obs"
        )

    def _run_one(item: Tuple[str, str, Optional[str]]) -> Dict[str, str]:
        name, r1, r2 = item
        return _run_bowtie_one(
            sample=name,
            fq1=str(r1),
            fq2=str(r2) if (r2 is not None and str(r2).strip() != "") else None,
            index_basename=index_basename,
            output_dir=output_dir,
            input_format=input_format,
            trim5=trim5,
            trim3=trim3,
            skip=skip,
            upto=upto,
            seed_mismatches=seed_mismatches,
            total_mismatches=total_mismatches,
            seed_len=seed_len,
            maqerr=maqerr,
            nomaqround=nomaqround,
            minins=minins,
            maxins=maxins,
            fr=fr,
            rf=rf,
            ff=ff,
            nofw=nofw,
            norc=norc,
            tryhard=tryhard,
            k=k,
            report_all=report_all,
            m=m,
            M=M,
            best=best,
            strata=strata,
            sam_out=sam_out,
            no_unal=no_unal,
            mapq=mapq,
            quiet=quiet,
            threads=threads,
            offrate=offrate,
            reorder=reorder,
            mm=mm,
            shmem=shmem,
            extra_args=extra_args,
        )

    results = run_threads(sample_list, _run_one, jobs)

    # Write SAM paths and bowtie metrics back to adata.obs
    for result in results:
        sample_name = result["sample"]
        adata.obs.loc[sample_name, "sam_path"] = result["sam"]
        adata.obs.loc[sample_name, "bowtie_log"] = result.get("log", "")

        metrics = result.get("metrics", {})
        for key, val in metrics.items():
            adata.obs.loc[sample_name, f"bowtie_{key}"] = val

    # Store genome index in adata.uns
    adata.uns["genome_index"] = index_basename

    return adata
