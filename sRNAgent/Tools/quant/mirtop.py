"""mirtop wrapper for isoMiR (isomiR) quantification and variant analysis.

Wraps `mirtop <https://github.com/miRTop/mirtop>`_ to annotate isomiR
variants from BAM files aligned against a miRBase hairpin reference, then
export per-sample variant-type distributions and a cross-sample counts
matrix at a configurable granularity (variant, miRNA, or hairpin).

The wrapper runs three mirtop subcommands:

1. ``mirtop gff --sps <species> --gtf <precursor.gff3>
   --hairpin <hairpin.fa> --out <dir> <bam1> <bam2> ...`` -- one
   invocation over all BAMs writes a merged ``mirtop.gff`` plus one
   per-sample ``<bamstem>.gff``.
2. ``mirtop counts --gff <merged.gff> --out <dir>`` -- writes
   ``<dir>/mirtop.tsv`` (the name derives from the GFF basename).
3. ``mirtop stats -o <dir> <merged.gff>`` -- writes ``mirtop_stats.txt``
   and ``mirtop_stats.log`` with the per-sample variant-type distribution.

Note: ``mirtop counts`` accepts a single ``--gff``, so the per-sample
GFFs must first be combined into one merged GFF by a single ``mirtop
gff`` invocation over all BAMs -- per-sample parallel invocations cannot
feed ``mirtop counts``. The agent runtime supervises output artifacts and
subprocess lifecycle generically, without mirtop-specific UI hooks.
"""

from __future__ import annotations

import csv
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from anndata import AnnData

from ..._registry import register_function
from ..._utils import run_cli_cmd
from ..alignment.bowtie import normalize_rna_fasta_to_dna
from .tRAX import store_count_matrix


# ---------------------------------------------------------------------------
# Internal helpers -- BAM resolution / CLI plumbing
# ---------------------------------------------------------------------------


def _resolve_bam_paths(adata: AnnData, bam_col: str) -> Dict[str, str]:
    """Return ``{sample: bam_path}`` from ``adata.obs``, falling back to ``sam_path``."""
    if bam_col in adata.obs.columns:
        bam_paths = {
            str(sample): str(adata.obs.loc[sample, bam_col])
            for sample in adata.obs_names
            if str(adata.obs.loc[sample, bam_col]).strip()
        }
    elif "sam_path" in adata.obs.columns:
        bam_paths = {}
        for sample in adata.obs_names:
            sam = str(adata.obs.loc[sample, "sam_path"]).strip()
            if not sam:
                continue
            bam = str(Path(sam).with_suffix(".bam"))
            if Path(bam).exists():
                bam_paths[str(sample)] = bam
    else:
        raise KeyError(
            f"adata.obs must contain {bam_col!r}. Run sa.alignment.bowtie() "
            "against a miRBase hairpin FASTA reference first."
        )

    missing = [
        f"{sample}: {path}"
        for sample, path in bam_paths.items()
        if not Path(path).exists()
    ]
    if missing:
        raise FileNotFoundError("BAM files not found: " + "; ".join(missing))
    if not bam_paths:
        raise ValueError("No BAM paths found in AnnData")
    return bam_paths


def _ensure_bam_index(bam: str, *, create_index: bool) -> None:
    """Create ``.bai`` for ``bam`` if missing and ``create_index`` is True."""
    bai_candidates = [Path(f"{bam}.bai"), Path(bam).with_suffix(".bai")]
    if any(candidate.exists() for candidate in bai_candidates):
        return
    if not create_index:
        return
    samtools = shutil.which("samtools") or "samtools"
    run_cli_cmd([samtools, "index", bam])


def _find_mirtop_binary() -> str:
    binary = shutil.which("mirtop")
    if not binary:
        raise FileNotFoundError(
            "mirtop not found in PATH. Install with `pip install mirtop`."
        )
    return binary


def _resolve_reference_files(gff: str, hairpin: str) -> Tuple[Path, Path]:
    gff_path = Path(gff).expanduser().resolve()
    hairpin_path = Path(hairpin).expanduser().resolve()
    if not gff_path.is_file():
        raise FileNotFoundError(f"Precursor GFF3 not found: {gff_path}")
    if not hairpin_path.is_file():
        raise FileNotFoundError(f"Hairpin FASTA not found: {hairpin_path}")
    return gff_path, hairpin_path


# ---------------------------------------------------------------------------
# Internal helpers -- counts parsing / aggregation
# ---------------------------------------------------------------------------


def _compute_log_cpm(count_matrix) -> np.ndarray:
    """``log2(CPM + 1)`` from a raw count matrix (samples x features)."""
    arr = np.asarray(count_matrix, dtype=np.float64)
    lib_sizes = arr.sum(axis=1, keepdims=True)
    lib_sizes = np.where(lib_sizes == 0, 1.0, lib_sizes)
    cpm = arr / lib_sizes * 1e6
    return np.log2(cpm + 1.0)


_MIRTOP_META_COLS = [
    "UID", "Read", "miRNA", "Variant",
    "iso_5p", "iso_3p", "iso_add3p", "iso_snp",
    "iso_5p_nt", "iso_3p_nt", "iso_add3p_nt", "iso_snp_nt",
]


def _resolve_counts_tsv(out_dir: Path) -> Path:
    """Find the counts TSV produced by ``mirtop counts`` in ``out_dir``.

    mirtop names it ``<gff-basename>.tsv``, so a merged ``mirtop.gff``
    yields ``mirtop.tsv``; a ``mirtop_counts.gff`` yields
    ``mirtop_counts.tsv``. All known names plus a generic fallback are
    probed.
    """
    candidates = [
        out_dir / "mirtop_counts.tsv",
        out_dir / "mirtop.tsv",
        out_dir / "mirtop.count.tsv",
        out_dir / "counts.tsv",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    tsvs = sorted(out_dir.glob("*.tsv"))
    if tsvs:
        return tsvs[0]
    raise FileNotFoundError(
        f"No counts TSV produced by `mirtop counts` in: {out_dir}"
    )


def _find_counts_tsv(out_dir: Path) -> Optional[Path]:
    """Return the counts TSV in ``out_dir`` if present, else None."""
    try:
        return _resolve_counts_tsv(out_dir)
    except FileNotFoundError:
        return None


def _normalize_mirtop_counts_tsv(counts_path: Path) -> bool:
    """Expand mirtop's incorrectly quoted multi-sample count field in-place.

    mirtop 0.4.30 constructs its DataFrame with ``samples=["S1\\tS2..."]``
    instead of one column per sample.  Pandas then quotes that one tab-bearing
    field in the TSV.  Parse it as CSV first, split only that final field, and
    atomically rewrite a real tabular TSV.
    """
    rows: List[List[str]] = []
    changed = False
    try:
        with counts_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.reader(handle, delimiter="\t"):
                if row and "\t" in row[-1]:
                    row = [*row[:-1], *row[-1].split("\t")]
                    changed = True
                rows.append(row)
    except OSError:
        return False
    if not changed:
        return False

    temporary = counts_path.with_suffix(f"{counts_path.suffix}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerows(rows)
        os.replace(temporary, counts_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def _counts_tsv_covers_samples(tsv: Path, sample_names: List[str]) -> bool:
    """True if the counts TSV header contains every sample name."""
    if not tsv.is_file():
        return False
    try:
        with open(tsv, encoding="utf-8") as handle:
            header = handle.readline().rstrip("\n").split("\t")
    except OSError:
        return False
    return all(sample in header for sample in sample_names)


def _merged_gff_covers_samples(gff: Path, sample_names: List[str]) -> bool:
    """True if the merged GFF's ``## COLDATA:`` line covers every sample.

    mirtop writes the sample names (BAM stems) into the GFF header, e.g.
    ``## COLDATA: S1.sorted,S2.sorted``; fuzzy matching is used so an
    ``obs_names`` entry like ``S1`` matches a ``S1.sorted`` COLDATA tag.
    """
    if not gff.is_file():
        return False
    try:
        with open(gff, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("## COLDATA:"):
                    coldata = [
                        item.strip()
                        for item in line.split("COLDATA:")[1].strip().split(",")
                    ]
                    return all(
                        any(sample in item or item in sample for item in coldata)
                        for sample in sample_names
                    )
                if not line.startswith("#"):
                    break
    except OSError:
        return False
    return False


def _parse_mirtop_counts(
    counts_path: Path,
    sample_names: List[str],
) -> Tuple[pd.DataFrame, str, Optional[pd.Series]]:
    """Read a mirtop counts TSV.

    Stock mirtop output has columns ``UID, Read, miRNA, Variant,
    iso_5p, iso_3p, iso_add3p, iso_snp`` followed by one column per
    sample (named by BAM stem).

    Returns
    -------
    sample_df
        DataFrame with feature IDs (UIDs) as rows and sample columns
        only, column names aligned to ``sample_names``.
    id_col
        Name of the feature-ID column (``"UID"`` for stock output).
    mirna_series
        ``miRNA`` column values indexed by UID, or None when absent
        (used for miRNA-level aggregation).
    meta_df
        Per-feature metadata (``Read``, ``miRNA``, ``Variant``,
        ``iso_5p``, ``iso_3p``, ``iso_add3p``, ``iso_snp``) indexed by
        UID, or None when absent. ``Variant`` holds the isomiR variant
        type, e.g. ``iso_5p`` / ``iso_3p;iso_add3p``.
    """
    df = pd.read_csv(counts_path, sep="\t")
    id_col = next(
        (candidate for candidate in ("UID", "name", "uid") if candidate in df.columns),
        None,
    )
    if id_col is None:
        raise ValueError(
            f"No feature ID column (UID/name) found in mirtop counts: {list(df.columns)}"
        )

    mirna_col = "miRNA" if "miRNA" in df.columns else None
    sample_cols = [col for col in df.columns if col not in _MIRTOP_META_COLS]
    if not sample_cols:
        raise ValueError(
            f"No sample columns found in mirtop counts: {list(df.columns)}"
        )

    # Map mirtop sample columns (BAM stems) back to adata.obs_names.
    rename: Dict[str, str] = {}
    sample_set = set(sample_names)
    for col in sample_cols:
        if col in sample_set:
            continue
        for sample in sample_names:
            if sample == col or sample in col or col in sample:
                rename[col] = sample
                break

    sample_df = df[sample_cols].copy()
    sample_df.index = df[id_col].astype(str)
    if rename:
        sample_df = sample_df.rename(columns=rename)

    mirna_series = df[mirna_col].copy() if mirna_col else None
    if mirna_series is not None:
        mirna_series.index = df[id_col].astype(str)

    meta_cols = [col for col in _MIRTOP_META_COLS if col in df.columns]
    meta_df: Optional[pd.DataFrame] = None
    if meta_cols:
        meta_df = df[meta_cols].copy()
        meta_df.index = df[id_col].astype(str)
    return sample_df, id_col, mirna_series, meta_df


def _aggregate_by_granularity(
    counts_df: pd.DataFrame,
    granularity: str,
    *,
    mirna_data: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """Aggregate mirtop variant-level counts at the requested granularity.

    ``"variant"`` keeps each isomiR UID as a feature. ``"miRNA"`` sums
    over variants sharing the same mature miRNA name -- preferably via the
    TSV's ``miRNA`` column, falling back to splitting UIDs on ``|``.
    """
    if granularity == "variant":
        return counts_df
    if granularity == "miRNA":
        if mirna_data is not None:
            grouped = counts_df.groupby(mirna_data.reindex(counts_df.index)).sum()
            return grouped
        new_index = [str(idx).split("|")[0] for idx in counts_df.index]
        grouped = counts_df.copy()
        grouped.index = new_index
        return grouped.groupby(level=0).sum()
    if granularity == "hairpin":
        raise NotImplementedError(
            "hairpin-level aggregation requires a precursor->hairpin mapping; "
            "mirtop counts does not emit hairpin IDs by default."
        )
    raise ValueError(
        f"Unknown granularity {granularity!r}; "
        "expected 'variant', 'miRNA', or 'hairpin'."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@register_function(
    aliases=[
        "mirtop",
        "mirtop_quant",
        "quantify_isomir",
        "isomir_quant",
        "isoMiR\u5b9a\u91cf",
    ],
    category="quant",
    description=(
        "Quantify isoMiR (isomiR) variants from BAM files using mirtop. "
        "Runs a single `mirtop gff` over all BAMs to build the merged "
        "isomiR GFF, then "
        "`mirtop counts` to summarise reads at the requested granularity "
        "(variant, miRNA, or hairpin), and `mirtop stats` for the "
        "per-sample variant-type distribution. Counts merge into "
        "adata.layers['counts']; log2(CPM+1) is stored in "
        "adata.layers['logcpm'] when normalize=True."
    ),
    examples=[
        (
            'adata = sa.quant.mirtop(\n'
            '    adata,\n'
            '    gff="ref/hairpin_hsa.gff3",\n'
            '    hairpin="ref/hairpin_hsa.fa",\n'
            '    species="hsa",\n'
            '    output_dir="mirtop_out",\n'
            '    # 默认 granularity="variant"：每个 isomiR 一个特征，不聚合\n'
            ')'
        ),
    ],
    related=[
        "alignment.bowtie",
        "quant.quantify_mirna",
        "quant.feature_count",
    ],
    produces={
        "obs": ["mirtop_gff", "mirtop_dir"],
        "var": [
            "mirna_id", "rna_type", "variant_type", "reads",
            "iso_5p", "iso_3p", "iso_add3p", "iso_snp",
        ],
        "layers": ["counts", "logcpm"],
        "uns": ["mirtop_result"],
    },
)
def mirtop_quant(
    adata: AnnData,
    gff: str,
    hairpin: str,
    output_dir: str = "results/quantification/mirtop",
    *,
    bam_col: str = "bam_path",
    species: str = "hsa",
    granularity: str = "variant",
    normalize: bool = True,
    create_index: bool = True,
    rna_type: str = "isoMiR",
    extra_args: Optional[Sequence[str]] = None,
    overwrite: bool = False,
) -> AnnData:
    """Quantify isoMiR (isomiR) variants from BAM files using mirtop.

    The pipeline runs in three stages:

    1. **Merged GFF** -- one ``mirtop gff --sps <species> --gtf <gff>
       --hairpin <hairpin> --out <out> <bam1> <bam2> ...`` call over all
       BAMs writes the merged ``mirtop.gff`` plus per-sample
       ``<bamstem>.gff``. Idempotent: skipped when ``mirtop.gff`` already
       covers every sample.
    2. **Combined counts** -- ``mirtop counts --gff <merged.gff> --out
       <out>`` writes ``<out>/mirtop.tsv``. Idempotent: skipped when the
       TSV already covers every sample.
    3. **Combined stats** -- ``mirtop stats -o <out> <merged.gff>``
       writes ``mirtop_stats.txt``/``mirtop_stats.log``; the content is
       streamed and stored in ``adata.uns['mirtop_result']``.

    Parameters
    ----------
    adata
        AnnData with ``adata.obs['bam_path']`` (set by
        :func:`sa.alignment.bowtie`).
    gff
        Path to miRBase **precursor** GFF3 (``*-hairpin.gff3``). Passed
        to ``mirtop gff --gtf``.
    hairpin
        Path to miRBase **hairpin** FASTA (``hairpin.fa``). RNA references
        containing ``U`` are normalized to a sibling ``*.dna.fa`` before the
        mirtop command, matching the Bowtie index reference.
    output_dir
        Output directory for mirtop artefacts.
    bam_col
        Column in ``adata.obs`` holding sorted BAM paths.
    species
        Three-letter miRBase species code (``"hsa"``, ``"mmu"``, ...).
        Passed to ``mirtop gff --sps``.
    granularity
        Aggregation level for counts. Default ``"variant"`` — **no
        aggregation**: every isomiR UID is its own feature. ``"miRNA"``
        sums over variants sharing a mature miRNA name; ``"hairpin"`` is
        not implemented.
    normalize
        When True (default), store ``log2(CPM + 1)`` in
        ``adata.layers['logcpm']``.
    create_index
        Create ``.bai`` indexes for BAMs that lack them.
    rna_type
        Label stored in ``adata.var['rna_type']``. Default ``"isoMiR"``.
    extra_args
        Extra arguments appended to the ``mirtop gff`` command.
    overwrite
        Re-run ``mirtop gff``/``mirtop counts`` even when outputs already
        cover every sample.

    Returns
    -------
    AnnData
        The input ``adata`` with:
        - ``adata.obs['mirtop_gff']`` -- per-sample GFF path
        - ``adata.obs['mirtop_dir']`` -- output directory
        - ``adata.layers['counts']`` -- merged count matrix
        - ``adata.layers['logcpm']`` -- log2(CPM+1) (if ``normalize``)
        - ``adata.var['mirna_id']`` -- feature IDs
        - ``adata.var['rna_type']`` -- ``"isoMiR"``
        - ``adata.var['variant_type']`` -- isomiR variant type per feature
          (``iso_5p`` / ``iso_3p`` / ``iso_add3p`` / ``iso_snp`` or a
          combination; empty at aggregated granularities)
        - ``adata.var['reads']`` + ``iso_*`` columns -- per-feature total
          reads and per-variant-type counts (variant granularity)
        - ``adata.uns['mirtop_result']`` -- output paths and stats log
          (including source/normalized hairpin provenance)
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")

    binary = _find_mirtop_binary()
    gff_path, source_hairpin_path = _resolve_reference_files(gff, hairpin)
    hairpin_path, hairpin_normalized = normalize_rna_fasta_to_dna(source_hairpin_path)
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    bam_paths = _resolve_bam_paths(adata, bam_col)
    sample_names = [str(sample) for sample in adata.obs_names if str(sample) in bam_paths]
    if not sample_names:
        raise ValueError("No BAM paths found in AnnData")

    for sample in sample_names:
        _ensure_bam_index(bam_paths[sample], create_index=create_index)

    merged_gff = out_dir / "mirtop.gff"

    # Step 1: merged `mirtop gff` over all BAMs (idempotent).
    if not overwrite and _merged_gff_covers_samples(merged_gff, sample_names):
        print(
            f"[mirtop] Skipping `mirtop gff`: {merged_gff} already covers "
            f"all {len(sample_names)} samples",
            flush=True,
        )
    else:
        cmd = [
            binary,
            "gff",
            "--sps",
            species,
            "--gtf",
            str(gff_path),
            "--hairpin",
            str(hairpin_path),
            "--out",
            str(out_dir),
        ]
        if extra_args:
            cmd.extend(list(extra_args))
        cmd.extend(bam_paths[sample] for sample in sample_names)

        run_cli_cmd(cmd)

    if not merged_gff.exists():
        raise FileNotFoundError(f"`mirtop gff` did not produce {merged_gff}")

    # Per-sample GFFs are written by the same invocation (one per BAM stem).
    sample_gffs = {
        sample: str(out_dir / f"{Path(bam_paths[sample]).stem}.gff")
        for sample in sample_names
    }

    # Step 2: combined `mirtop counts` (idempotent).
    existing_tsv = _find_counts_tsv(out_dir)
    if existing_tsv is not None and _normalize_mirtop_counts_tsv(existing_tsv):
        print(f"[mirtop] Normalized quoted sample columns in {existing_tsv}", flush=True)
    if (
        existing_tsv is not None
        and not overwrite
        and _counts_tsv_covers_samples(existing_tsv, sample_names)
    ):
        print(
            f"[mirtop] Skipping `mirtop counts`: {existing_tsv} already "
            f"covers all {len(sample_names)} samples",
            flush=True,
        )
    else:
        counts_cmd = [binary, "counts", "--gff", str(merged_gff), "--out", str(out_dir)]
        run_cli_cmd(counts_cmd)

    # Step 3: combined `mirtop stats` -- tolerate crashes on empty GFFs.
    try:
        run_cli_cmd([binary, "stats", "-o", str(out_dir), str(merged_gff)])
    except RuntimeError:
        print(
            "[mirtop] `mirtop stats` failed (likely an empty GFF with no "
            "isoMiRs); continuing without stats",
            flush=True,
        )
    stats_txt = out_dir / "mirtop_stats.txt"
    stats_text = stats_txt.read_text(encoding="utf-8") if stats_txt.is_file() else ""
    if stats_text:
        print(stats_text, flush=True)

    # Parse the combined counts TSV.
    counts_tsv = _resolve_counts_tsv(out_dir)
    if _normalize_mirtop_counts_tsv(counts_tsv):
        print(f"[mirtop] Normalized quoted sample columns in {counts_tsv}", flush=True)
    sample_df, id_col, mirna_series, meta_df = _parse_mirtop_counts(counts_tsv, sample_names)

    if sample_df.shape[0] == 0:
        print(
            f"[mirtop] WARNING: counts TSV is empty ({counts_tsv}); "
            "no isoMiRs detected.",
            flush=True,
        )
        aggregated = sample_df
    else:
        aggregated = _aggregate_by_granularity(
            sample_df,
            granularity,
            mirna_data=mirna_series,
        )

    # Align columns to adata.obs_names order, padding missing samples with 0.
    for sample in sample_names:
        if sample not in aggregated.columns:
            aggregated[sample] = 0.0
    aggregated = aggregated[sample_names]
    matrix = aggregated.T.to_numpy(dtype=np.float64)

    # Build var. At variant granularity (default) each feature is an isomiR
    # UID, so the per-feature metadata from the counts TSV (isomiR variant
    # type, reads, per-type counts) is carried into adata.var.
    var = pd.DataFrame(index=aggregated.index)
    if granularity == "variant" and meta_df is not None:
        meta = meta_df.reindex(aggregated.index)
        if "miRNA" in meta.columns:
            var["mirna_id"] = meta["miRNA"].astype(str)
        else:
            var["mirna_id"] = [str(idx) for idx in aggregated.index]
        if "Variant" in meta.columns:
            var["variant_type"] = meta["Variant"].astype(str)
        if "Read" in meta.columns:
            var["reads"] = pd.to_numeric(meta["Read"], errors="coerce").fillna(0)
        for col in ("iso_5p", "iso_3p", "iso_add3p", "iso_snp"):
            if col in meta.columns:
                var[col] = pd.to_numeric(meta[col], errors="coerce").fillna(0)
    else:
        # miRNA/hairpin granularity: features are aggregated, so there is
        # no single variant type per feature; per-type counts are summed.
        var["mirna_id"] = [str(idx) for idx in aggregated.index]
        var["variant_type"] = ""
        if meta_df is not None and mirna_series is not None:
            type_cols = [
                col for col in ("iso_5p", "iso_3p", "iso_add3p", "iso_snp")
                if col in meta_df.columns
            ]
            if type_cols:
                summed = meta_df[type_cols].groupby(
                    mirna_series.reindex(meta_df.index)
                ).sum(numeric_only=True)
                for col in type_cols:
                    var[col] = summed[col].reindex(aggregated.index).fillna(0)
    var["id_column"] = id_col

    adata = adata[sample_names].copy()
    adata.obs["mirtop_gff"] = [sample_gffs[sample] for sample in sample_names]
    adata.obs["mirtop_dir"] = str(out_dir)

    quantified = store_count_matrix(adata, matrix, var, rna_type=rna_type)

    if normalize and matrix.size:
        quantified.layers["logcpm"] = _compute_log_cpm(quantified.layers["counts"])

    quantified.uns["mirtop_result"] = {
        "output_dir": str(out_dir),
        "species": species,
        "granularity": granularity,
        "gff": str(gff_path),
        "hairpin": str(hairpin_path),
        "source_hairpin": str(source_hairpin_path),
        "hairpin_rna_to_dna_normalized": hairpin_normalized,
        "rna_type": rna_type,
        "files": {
            "merged_gff": str(merged_gff),
            "counts_tsv": str(counts_tsv),
            "stats_txt": str(stats_txt),
            "stats_log": str(out_dir / "mirtop_stats.log"),
            "sample_gffs": sample_gffs,
        },
        "stats_log": stats_text,
        "id_column": id_col,
    }
    return quantified


mirtop = mirtop_quant
quantify_isomir = mirtop_quant
