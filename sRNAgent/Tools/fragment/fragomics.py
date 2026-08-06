"""Fragmentomics feature extraction for small RNA-seq."""

from __future__ import annotations

import copy
import gzip
from collections import Counter
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import pysam
from anndata import AnnData

from ..._registry import register_function
from ..._utils import run_threads

FASTQ_COLUMNS: Sequence[str] = ("trimmed_path", "clean_fastq_path")
GENOME_FASTA_KEYS: Sequence[str] = ("genome_fasta", "reference_fasta", "fasta_path")
_READ_PROGRESS_INTERVAL = 250_000


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt")
    return open(path, "rt")


def _iter_fastq_sequences(path: Path) -> Iterator[str]:
    with _open_text(path) as handle:
        while True:
            header = handle.readline()
            if not header:
                break
            seq = handle.readline()
            plus = handle.readline()
            qual = handle.readline()
            if not seq or not plus or not qual:
                raise ValueError(f"Malformed FASTQ file: {path}")
            if not header.startswith("@") or not plus.startswith("+"):
                raise ValueError(f"Malformed FASTQ record in {path}")
            yield seq.strip().upper()


def _reverse_complement(seq: str) -> str:
    table = str.maketrans("ACGTNacgtn", "TGCANtgcan")
    return seq.translate(table)[::-1]


def _pad_fetch(fasta: pysam.FastaFile, chrom: str, start: int, end: int) -> str:
    chrom_len = fasta.get_reference_length(chrom)
    left_pad = max(0, -start)
    right_pad = max(0, end - chrom_len)
    fetch_start = max(0, start)
    fetch_end = min(chrom_len, end)
    seq = fasta.fetch(chrom, fetch_start, fetch_end).upper()
    return ("N" * left_pad) + seq + ("N" * right_pad)


def _breakpoint_motif(
    fasta: pysam.FastaFile,
    chrom: str,
    breakpoint: int,
    motif_k: int,
    *,
    reverse: bool,
) -> str:
    left = motif_k // 2
    right = motif_k - left
    motif = _pad_fetch(fasta, chrom, breakpoint - left, breakpoint + right)
    return _reverse_complement(motif) if reverse else motif


def _detect_fastq_column(adata: AnnData) -> str:
    for col in FASTQ_COLUMNS:
        if col in adata.obs.columns:
            return col
    raise KeyError(
        "adata.obs must contain a QC-completed FASTQ column: "
        "'trimmed_path' or 'clean_fastq_path'. Run sa.fastq.cutadapt() first."
    )


def _detect_genome_fasta(adata: AnnData, genome_fasta: Optional[str]) -> str:
    if genome_fasta:
        return genome_fasta
    for key in GENOME_FASTA_KEYS:
        value = adata.uns.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise KeyError(
        "Genome FASTA path not found. Provide genome_fasta=... or set "
        "adata.uns['genome_fasta']."
    )


def _load_fasta_reference_lengths(path: Path) -> Dict[str, int]:
    with pysam.FastaFile(str(path)) as fasta:
        return {
            str(name): int(length)
            for name, length in zip(fasta.references, fasta.lengths)
        }


def _validate_genome_aligned_bam(path: Path, fasta_refs: Dict[str, int]) -> None:
    if path.suffix.lower() != ".bam":
        raise ValueError(
            f"Fragmentomics requires coordinate-sorted whole-genome BAM input; "
            f"SAM/other formats are not accepted: {path}"
        )
    if not path.exists():
        raise FileNotFoundError(f"BAM file not found: {path}")
    with pysam.AlignmentFile(str(path), "rb") as bam:
        header = bam.header.to_dict()
    sort_order = str(header.get("HD", {}).get("SO", "")).lower()
    if sort_order != "coordinate":
        raise ValueError(
            f"BAM must be coordinate-sorted for fragmentomics analysis: {path}"
        )
    sq_entries = header.get("SQ") or []
    if not sq_entries:
        raise ValueError(
            f"BAM header does not contain reference sequences (SQ); whole-genome BAM is required: {path}"
        )

    missing_refs: List[str] = []
    length_mismatches: List[str] = []
    matched_refs = 0
    for entry in sq_entries:
        name = str(entry.get("SN") or "").strip()
        length = entry.get("LN")
        if not name:
            continue
        fasta_len = fasta_refs.get(name)
        if fasta_len is None:
            missing_refs.append(name)
            continue
        matched_refs += 1
        if length is not None and int(length) != int(fasta_len):
            length_mismatches.append(f"{name} (bam={int(length)}, fasta={int(fasta_len)})")

    if matched_refs == 0:
        raise ValueError(
            "BAM header references do not match the provided genome FASTA. "
            f"Fragmentomics requires whole-genome alignment coordinates: {path}"
        )
    if missing_refs:
        preview = ", ".join(missing_refs[:5])
        raise ValueError(
            "BAM contains references absent from the provided genome FASTA; "
            "this usually indicates transcriptome/local-reference rather than whole-genome alignment: "
            f"{preview}"
        )
    if length_mismatches:
        preview = ", ".join(length_mismatches[:3])
        raise ValueError(
            "BAM reference lengths do not match the provided genome FASTA; "
            "whole-genome coordinate consistency is required: "
            f"{preview}"
        )


def _has_srna_expression(adata: AnnData) -> bool:
    if adata.n_vars > 0:
        return True
    if "counts" in adata.layers:
        return True
    x = getattr(adata, "X", None)
    return bool(x is not None and getattr(x, "shape", (0, 0))[1] > 0)


def _replace_adata_inplace(target: AnnData, source: AnnData) -> AnnData:
    layers = {
        key: np.asarray(source.layers[key]).copy()
        for key in source.layers.keys()
        if key is not None
    }
    obsm = {
        key: value.copy() if hasattr(value, "copy") else copy.deepcopy(value)
        for key, value in source.obsm.items()
    }
    varm = {
        key: value.copy() if hasattr(value, "copy") else copy.deepcopy(value)
        for key, value in source.varm.items()
    }
    target._init_as_actual(
        X=np.asarray(source.X).copy(),
        obs=source.obs.copy(),
        var=source.var.copy(),
        uns=copy.deepcopy(source.uns),
        obsm=obsm,
        varm=varm,
        layers=layers,
    )
    return target


def _normalise_within_type(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    totals = result.groupby("feature_type")["raw_value"].transform("sum")
    totals = totals.replace(0, np.nan)
    result["cpm"] = (result["raw_value"] / totals).fillna(0.0) * 1_000_000.0
    return result


def _rows_from_counter(counter: Counter, feature_type: str) -> List[Tuple[str, str, float]]:
    rows: List[Tuple[str, str, float]] = []
    for feature, count in sorted(counter.items(), key=lambda item: str(item[0])):
        rows.append((feature_type, str(feature), float(count)))
    return rows


def _emit_fragomics_log(sample: str, phase: str, message: str) -> None:
    print(f"[fragomics] sample={sample} phase={phase} {message}", flush=True)


def _analyse_sample(
    sample: str,
    *,
    fastq_path: Path,
    bam_path: Path,
    genome_fasta: Path,
    motif_k: int,
    region_size: int,
    output_dir: Path,
    max_reads: Optional[int],
) -> Dict[str, object]:
    _emit_fragomics_log(sample, "fastq", "start FSD/EDM extraction")
    fsd = Counter()
    edm_5p = Counter()
    edm_3p = Counter()

    for seq in _iter_fastq_sequences(fastq_path):
        if not seq:
            continue
        fsd[f"len={len(seq)}"] += 1
        if len(seq) >= motif_k:
            edm_5p[seq[:motif_k]] += 1
            edm_3p[seq[-motif_k:]] += 1

    _emit_fragomics_log(sample, "bam", "start FSC/RCD/BPM extraction")
    fsc = Counter()
    rcd = Counter()
    bpm_start = Counter()
    bpm_end = Counter()

    with pysam.FastaFile(str(genome_fasta)) as fasta, pysam.AlignmentFile(str(bam_path), "rb") as bam:
        processed = 0
        for read in bam.fetch(until_eof=True):
            if read.is_unmapped or read.is_secondary or read.is_supplementary:
                continue
            chrom = bam.get_reference_name(read.reference_id)
            if chrom is None:
                continue
            read_len = read.query_length or read.infer_read_length() or 0
            if read_len <= 0:
                continue
            fsc[f"{chrom}|len={read_len}"] += 1

            bin_start = (read.reference_start // region_size) * region_size
            bin_end = bin_start + region_size
            rcd[f"{chrom}:{bin_start + 1}-{bin_end}"] += 1

            start_pos = int(read.reference_start)
            end_pos = max(start_pos, int(read.reference_end or start_pos + 1) - 1)
            bpm_start[_breakpoint_motif(
                fasta, chrom, start_pos, motif_k, reverse=bool(read.is_reverse)
            )] += 1
            bpm_end[_breakpoint_motif(
                fasta, chrom, end_pos, motif_k, reverse=bool(read.is_reverse)
            )] += 1

            processed += 1
            if processed % _READ_PROGRESS_INTERVAL == 0:
                _emit_fragomics_log(
                    sample,
                    "bam",
                    f"processed_reads={processed} current_chrom={chrom}",
                )
            if max_reads is not None and processed >= max_reads:
                break

    rows: List[Tuple[str, str, float]] = []
    rows.extend(_rows_from_counter(fsd, "FSD"))
    rows.extend(_rows_from_counter(fsc, "FSC"))
    rows.extend(_rows_from_counter(rcd, "RCD"))
    rows.extend(_rows_from_counter(edm_5p, "EDM_5P"))
    rows.extend(_rows_from_counter(edm_3p, "EDM_3P"))
    rows.extend(_rows_from_counter(bpm_start, "BPM_START"))
    rows.extend(_rows_from_counter(bpm_end, "BPM_END"))

    frame = pd.DataFrame(rows, columns=["feature_type", "feature", "raw_value"])
    frame = _normalise_within_type(frame)
    sample_dir = output_dir / sample
    sample_dir.mkdir(parents=True, exist_ok=True)
    table_path = sample_dir / f"{sample}.fragmentomics.tsv"
    _emit_fragomics_log(sample, "write", f"write_table={table_path}")
    frame.to_csv(table_path, sep="\t", index=False)
    _emit_fragomics_log(sample, "done", f"feature_rows={len(frame)}")
    return {
        "sample": sample,
        "table_path": str(table_path),
        "frame": frame,
    }


def _build_fragmentomics_adata(
    source: AnnData,
    sample_results: List[Dict[str, object]],
    *,
    output_dir: Path,
    genome_fasta: str,
    motif_k: int,
    region_size: int,
    fastq_column: str,
) -> AnnData:
    sample_names = list(source.obs_names)
    combined = pd.concat(
        [
            result["frame"].assign(sample=result["sample"])  # type: ignore[union-attr]
            for result in sample_results
        ],
        ignore_index=True,
    )
    combined["feature_key"] = (
        combined["feature_type"].astype(str) + "::" + combined["feature"].astype(str)
    )
    meta = (
        combined[["feature_key", "feature_type", "feature"]]
        .drop_duplicates()
        .sort_values(["feature_type", "feature"])
        .set_index("feature_key")
    )

    raw_wide = combined.pivot_table(
        index="feature_key",
        columns="sample",
        values="raw_value",
        aggfunc="sum",
        fill_value=0.0,
    )
    cpm_wide = combined.pivot_table(
        index="feature_key",
        columns="sample",
        values="cpm",
        aggfunc="sum",
        fill_value=0.0,
    )

    meta["type"] = meta["feature_type"].astype(str)
    meta["modality"] = "fragmentomics"
    meta = meta.drop(columns=["feature_type"])
    raw_matrix = (
        raw_wide.reindex(index=meta.index, columns=sample_names, fill_value=0.0)
        .to_numpy(dtype=np.float64)
        .T
    )
    cpm_matrix = (
        cpm_wide.reindex(index=meta.index, columns=sample_names, fill_value=0.0)
        .to_numpy(dtype=np.float64)
        .T
    )

    frag_adata = AnnData(X=raw_matrix, obs=source.obs.copy(), var=meta.copy())
    frag_adata.layers["counts"] = raw_matrix.copy()
    frag_adata.layers["CPM"] = cpm_matrix.copy()
    frag_adata.obs["fragomics_table"] = [
        next(str(result["table_path"]) for result in sample_results if result["sample"] == sample)
        for sample in sample_names
    ]
    sample_tables = {str(result["sample"]): str(result["table_path"]) for result in sample_results}

    raw_export = meta.reset_index().rename(columns={"index": "feature_key"})
    raw_export = raw_export.join(raw_wide.reindex(index=meta.index, columns=sample_names, fill_value=0.0), on="feature_key")
    cpm_export = meta.reset_index().rename(columns={"index": "feature_key"})
    cpm_export = cpm_export.join(cpm_wide.reindex(index=meta.index, columns=sample_names, fill_value=0.0), on="feature_key")

    raw_tsv = output_dir / "fragmentomics_raw.tsv"
    cpm_tsv = output_dir / "fragmentomics_cpm.tsv"
    raw_export.to_csv(raw_tsv, sep="\t", index=False)
    cpm_export.to_csv(cpm_tsv, sep="\t", index=False)

    frag_adata.uns["genome_fasta"] = genome_fasta
    frag_adata.uns["modality"] = "fragmentomics"
    frag_adata.uns["fragomics_output_dir"] = str(output_dir)
    frag_adata.uns["fragomics_raw_tsv"] = str(raw_tsv)
    frag_adata.uns["fragomics_cpm_tsv"] = str(cpm_tsv)
    frag_adata.uns["fragomics_sample_tables"] = sample_tables
    frag_adata.uns["fragomics_params"] = {
        "motif_k": int(motif_k),
        "region_size": int(region_size),
        "fastq_column": fastq_column,
    }
    return frag_adata


@register_function(
    aliases=[
        "fragomics",
        "fragmentomics",
        "fragment_analysis",
        "smallrna_fragmentomics",
        "片段组学",
    ],
    category="fragment",
    description=(
        "Extract small-RNA fragmentomics features from QC-completed FASTQ files, "
        "coordinate-sorted whole-genome BAM files, and a reference genome FASTA. Computes "
        "FSD, FSC, RCD, EDM, and BPM features, writes per-sample tables plus "
        "merged raw/CPM matrices, and returns an independent fragmentomics AnnData."
    ),
    examples=[
        'sa.fragment.fragomics(adata, genome_fasta="ref/GRCh38.primary_assembly.genome.fa")',
        'sa.fragment.fragomics(adata, genome_fasta="ref/genome.fa", jobs=8, motif_k=6)',
    ],
    related=[
        "fastq.cutadapt",
        "alignment.bowtie",
        "reference.download_genome",
    ],
    produces={
        "obs": ["fragomics_table"],
        "var": ["type", "feature", "modality"],
        "layers": ["counts", "CPM"],
        "uns": [
            "fragomics_output_dir",
            "fragomics_raw_tsv",
            "fragomics_cpm_tsv",
            "fragomics_sample_tables",
            "fragomics_params",
        ],
    },
)
def fragomics(
    adata: AnnData,
    genome_fasta: Optional[str] = None,
    output_dir: str = "fragmentomics_out",
    *,
    motif_k: int = 6,
    region_size: int = 5_000_000,
    jobs: int = 4,
    max_reads: Optional[int] = None,
) -> AnnData:
    """Compute fragmentomics features for small RNA-seq samples.

    Parameters
    ----------
    adata
        AnnData object whose observations are samples. Must contain a
        QC-completed FASTQ path column (``trimmed_path`` or ``clean_fastq_path``)
        and ``adata.obs['bam_path']`` with coordinate-sorted whole-genome BAM files
        whose header references match the provided genome FASTA.
    genome_fasta
        Reference genome FASTA path. If omitted, uses ``adata.uns['genome_fasta']``.
    output_dir
        Output directory for per-sample feature tables and merged matrices.
    motif_k
        Motif size for EDM and BPM features. Default 6.
    region_size
        Genomic window size for RCD features. Default 5,000,000 bp.
    jobs
        Number of per-sample worker threads.
    max_reads
        Optional cap on aligned reads processed per sample, useful for quick tests.

    Returns
    -------
    AnnData
        An independent fragmentomics AnnData. The input sRNA AnnData is used
        only for sample-level input paths and is never modified or combined
        with fragmentomics results.
    """
    if motif_k <= 0:
        raise ValueError("motif_k must be a positive integer.")
    if region_size <= 0:
        raise ValueError("region_size must be a positive integer.")
    if adata.n_obs == 0:
        raise ValueError("adata has no samples.")
    if "bam_path" not in adata.obs.columns:
        raise KeyError(
            "adata.obs must contain 'bam_path'. Run sa.alignment.bowtie() first."
        )

    fastq_column = _detect_fastq_column(adata)
    fasta_path = Path(_detect_genome_fasta(adata, genome_fasta)).expanduser().resolve()
    if not fasta_path.exists():
        raise FileNotFoundError(f"Genome FASTA not found: {fasta_path}")
    fasta_refs = _load_fasta_reference_lengths(fasta_path)

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs: Dict[str, Dict[str, Path]] = {}
    for sample in adata.obs_names:
        fastq_path = Path(str(adata.obs.at[sample, fastq_column])).expanduser().resolve()
        bam_path = Path(str(adata.obs.at[sample, "bam_path"])).expanduser().resolve()
        if not fastq_path.exists():
            raise FileNotFoundError(f"FASTQ file not found for sample {sample}: {fastq_path}")
        _validate_genome_aligned_bam(bam_path, fasta_refs)
        inputs[str(sample)] = {
            "fastq": fastq_path,
            "bam": bam_path,
        }

    effective_jobs = max(1, min(int(jobs or 1), len(inputs)))
    print(
        "[fragomics] start "
        f"samples={len(inputs)} jobs={effective_jobs} "
        f"features=FSD/FSC/RCD/EDM/BPM motif_k={motif_k} region_size={region_size}",
        flush=True,
    )

    def _worker(sample: str) -> Dict[str, object]:
        item = inputs[sample]
        _emit_fragomics_log(sample, "queued", "worker_started")
        return _analyse_sample(
            sample,
            fastq_path=item["fastq"],
            bam_path=item["bam"],
            genome_fasta=fasta_path,
            motif_k=motif_k,
            region_size=region_size,
            output_dir=out_dir,
            max_reads=max_reads,
        )

    sample_results = run_threads(list(inputs.keys()), _worker, jobs)
    print(
        f"[fragomics] merge samples_done={len(sample_results)} output_dir={out_dir}",
        flush=True,
    )
    frag_adata = _build_fragmentomics_adata(
        adata,
        sample_results,
        output_dir=out_dir,
        genome_fasta=str(fasta_path),
        motif_k=motif_k,
        region_size=region_size,
        fastq_column=fastq_column,
    )
    print(
        "[fragomics] done "
        f"raw_tsv={frag_adata.uns.get('fragomics_raw_tsv', '')} "
        f"cpm_tsv={frag_adata.uns.get('fragomics_cpm_tsv', '')}",
        flush=True,
    )

    return frag_adata
