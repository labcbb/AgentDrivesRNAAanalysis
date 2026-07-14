"""featureCounts wrapper for quantifying reads over genomic features.

Wraps `featureCounts <https://subread.sourceforge.net/>`_ from the
Subread package, a fast read summarization tool that counts aligned
reads (BAM) against genomic features (GTF/GFF3).

Designed for small RNA quantification (miRNA, piRNA, etc.) with
flexible feature type and attribute selection.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

import pandas as pd
import pysam
from anndata import AnnData

from ..._registry import register_function
from ..._utils import run_cli_cmd


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_chromosomes(bam_path: str, annotation: str) -> None:
    """Check that BAM reference names and GTF/GFF chromosome names match.

    Prints a warning if the overlap is poor or empty.
    """
    # Read BAM header chromosomes
    bam_chrs: set[str] = set()
    with pysam.AlignmentFile(bam_path, "rb") as bam:
        for ref in bam.references:
            bam_chrs.add(ref)

    # Read GTF/GFF chromosome names (first column)
    anno_chrs: set[str] = set()
    with open(annotation) as f:
        for line in f:
            if line.startswith("#") or line.strip() == "":
                continue
            cols = line.split("\t")
            if len(cols) >= 1:
                anno_chrs.add(cols[0].strip())

    overlap = bam_chrs & anno_chrs
    if not overlap:
        print(
            f"[featureCounts] ⚠️  No common chromosomes between BAM and"
            f" annotation!\n"
            f"  BAM chrs (first 5): {sorted(bam_chrs)[:5]}\n"
            f"  Annotation chrs (first 5): {sorted(anno_chrs)[:5]}",
            flush=True,
        )
    elif len(overlap) < min(len(bam_chrs), len(anno_chrs)):
        print(
            f"[featureCounts] Chromosome overlap: {len(overlap)}/"
            f"{len(anno_chrs)} annotation chrs in BAM",
            flush=True,
        )


def _parse_feature_counts(counts_path: Path) -> pd.DataFrame:
    """Parse the featureCounts output matrix into a DataFrame.

    featureCounts output format::

        Geneid\tChr\tStart\tEnd\tStrand\tLength\t{sample1}\t{sample2}\t...
        hsa-let-7a-5p\tchr1\t123\t456\t+\t22\t10\t15\t...
    """
    # featureCounts adds a header line starting with #
    with open(counts_path) as f:
        first = f.readline().strip()
        if not first.startswith("#"):
            raise ValueError(
                f"Expected featureCounts header (#), got: {first[:60]}"
            )

    df = pd.read_csv(counts_path, sep="\t", comment="#")
    # The first column is Geneid; last columns are sample counts
    if "Geneid" not in df.columns:
        raise ValueError(
            f"Missing 'Geneid' column in featureCounts output: {df.columns.tolist()}"
        )
    gene_col = "Geneid"

    # Extract count columns (everything after Length)
    cols = list(df.columns)
    try:
        len_idx = cols.index("Length")
    except ValueError:
        raise ValueError(f"Cannot find 'Length' column in: {cols}")
    count_cols = cols[len_idx + 1 :]

    # Build feature index from Geneid
    df = df.set_index(gene_col)
    counts = df[count_cols]
    # Clean sample names: featureCounts uses file basenames
    counts.columns = [
        Path(c).stem.replace(".sorted", "").replace(".bam", "")
        for c in counts.columns
    ]
    return counts


def _parse_summary(summary_path: Path) -> Dict[str, Dict[str, int]]:
    """Parse the featureCounts .summary file."""
    result: Dict[str, Dict[str, int]] = {}
    with open(summary_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            status = parts[0]
            for i, val in enumerate(parts[1:], start=1):
                sample = f"sample_{i}"
                if sample not in result:
                    result[sample] = {}
                result[sample][status] = int(val)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@register_function(
    aliases=[
        "feature_count", "featureCounts", "count_features",
        "定量特征",
    ],
    category="quant",
    description=(
        "Count aligned reads (BAM) over genomic features "
        "(GTF/GFF3) using featureCounts.\n\n"
        "Reads BAM paths from ``adata.obs['bam_path']`` (set by ``bowtie``).\n"
        "After counting, the count matrix is stored in ``adata.X`` and "
        "the raw output path in ``adata.obs['fc_counts_csv']``.\n\n"
        "Designed for sRNA-seq — default ``-t miRNA -g Name -s 1`` fits "
        "TruSeq Small RNA stranded protocol.\n"
        "Also supports piRNA or other small RNA features by adjusting "
        "``feature_type`` and ``attr_type``."
    ),
    examples=[
        (
            'adata = sa.quant.feature_count(\n'
            '    adata,\n'
            '    annotation="ref/gencode.v50.primary_assembly.annotation.gtf.gz",\n'
            '    feature_type="miRNA", attr_type="Name",\n'
            '    strand=1, threads=6, output_dir="fc_out",\n'
            ')'
        ),
        (
            'adata = sa.quant.feature_count(\n'
            '    adata,\n'
            '    annotation="ref/piRNA.gff3",\n'
            '    feature_type="piRNA", attr_type="ID",\n'
            '    strand=0, output_dir="fc_out",\n'
            ')'
        ),
    ],
    related=[
        "alignment.bowtie",
    ],
    produces={
        "obs": ["fc_counts_csv", "fc_summary_csv"],
        "uns": ["fc_annotation"],
    },
)
def feature_count(
    adata: AnnData,
    annotation: str,
    output_dir: str = "fc_out",
    *,
    feature_type: str = "miRNA",
    attr_type: str = "Name",
    strand: int = 1,
    allow_overlap: bool = False,
    threads: int = 4,
    extra_args: Optional[Sequence[str]] = None,
) -> AnnData:
    """Count aligned reads over genomic features with featureCounts.

    Parameters
    ----------
    adata
        AnnData object with ``adata.obs['bam_path']`` pointing to sorted
        BAM files (output of :func:`sa.alignment.bowtie`).
    annotation
        Path to GTF or GFF3 annotation file.
    output_dir
        Output directory for featureCounts results.
    feature_type
        Feature type to count (``-t`` in featureCounts).
        Matches the third column in GTF/GFF3. Default ``"miRNA"``.
    attr_type
        Attribute key for feature ID (``-g`` in featureCounts).
        Matches a key in the 9th column attributes. Default ``"Name"``.
    strand
        Strand specificity: 0=unstranded, 1=stranded (read1 sense),
        2=reverse stranded (read1 antisense).
        TruSeq Small RNA is **stranded (1)**. Default 1.
    allow_overlap
        Allow reads to overlap multiple features (``-O``).
    threads
        Number of CPU threads (``-T``).
    extra_args
        Additional arguments passed directly to featureCounts.

    Returns
    -------
    AnnData
        The input ``adata`` with:
        - ``adata.obs['fc_counts_csv']`` — path to the raw count matrix
        - ``adata.obs['fc_summary_csv']`` — path to the summary stats
        - ``adata.X`` — count matrix (samples × features)
        - ``adata.var`` — feature annotations
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Collect BAM files from adata
    if "bam_path" not in adata.obs:
        # Fallback: try sam_path
        if "sam_path" in adata.obs:
            # Check if .bam counterparts exist
            bam_paths = []
            for p in adata.obs["sam_path"]:
                b = str(Path(p).with_suffix(".bam"))
                if Path(b).exists():
                    bam_paths.append(b)
                else:
                    bam_paths.append(p)
        else:
            raise KeyError(
                "adata.obs must contain 'bam_path' or 'sam_path'. "
                "Run sa.alignment.bowtie() first."
            )
    else:
        bam_paths = list(adata.obs["bam_path"])

    # Validate BAM vs annotation chromosomes (first BAM only)
    first_bam = next((p for p in bam_paths if Path(p).exists()), None)
    if first_bam:
        _validate_chromosomes(first_bam, annotation)

    # Build command
    cmd = ["featureCounts"]

    cmd.extend(["-T", str(threads)])
    cmd.extend(["-t", feature_type])
    cmd.extend(["-g", attr_type])
    cmd.extend(["-s", str(strand)])
    cmd.extend(["-a", annotation])

    # Count output prefix → featureCounts generates {prefix}.txt
    prefix = str(out_path / "feature_counts")
    cmd.extend(["-o", f"{prefix}.txt"])

    if allow_overlap:
        cmd.append("-O")

    if extra_args:
        cmd.extend(extra_args)

    # BAM files go last
    cmd.extend(bam_paths)

    # Run
    run_cli_cmd(cmd)

    # Parse results
    counts_path = Path(f"{prefix}.txt")
    summary_path = Path(f"{prefix}.txt.summary")

    if not counts_path.exists():
        raise RuntimeError(
            f"featureCounts failed to produce output at {counts_path}"
        )

    counts_df = _parse_feature_counts(counts_path)
    sample_names = list(adata.obs_names)

    # Map featureCounts sample names to adata obs_names (by BAM filename stem)
    bam_stems = {Path(p).stem: i for i, p in enumerate(bam_paths)}
    # Remove .sorted / .bam suffixes for matching
    fc_cols = list(counts_df.columns)

    # Build matrix aligned to adata.obs_names order
    n_features = len(counts_df)
    matrix = [[0.0] * n_features for _ in range(len(sample_names))]

    col_map: dict[str, int] = {}
    for fc_name in fc_cols:
        # Try matching fc_name to adata obs_names
        for i, on in enumerate(sample_names):
            if on == fc_name or on in fc_name or fc_name in on:
                col_map[fc_name] = i
                break

    for j, fc_name in enumerate(fc_cols):
        i = col_map.get(fc_name)
        if i is not None:
            matrix[i][j] = float(counts_df.iloc[j])

    # Store results
    adata.obs["fc_counts_csv"] = str(counts_path)
    if summary_path.exists():
        adata.obs["fc_summary_csv"] = str(summary_path)

    # Assign count matrix and feature metadata
    adata.X = matrix
    adata.var = pd.DataFrame(index=counts_df.index)
    adata.var["feature_id"] = counts_df.index.tolist()
    adata.uns["fc_annotation"] = annotation

    return adata
