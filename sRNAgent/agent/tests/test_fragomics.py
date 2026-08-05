"""Tests for fragmentomics tool."""

from __future__ import annotations

import gzip
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import anndata as ad  # noqa: E402
import mudata as md  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pysam  # noqa: E402
import pytest  # noqa: E402
import sRNAgent as sa  # noqa: E402


def _write_fastq_gz(path: Path, records: list[tuple[str, str]]) -> None:
    with gzip.open(path, "wt") as handle:
        for name, seq in records:
            handle.write(f"@{name}\n{seq}\n+\n{'I' * len(seq)}\n")


def _write_bam(path: Path, *, sorted_bam: bool) -> None:
    header = {
        "HD": {"VN": "1.0", "SO": "coordinate" if sorted_bam else "unknown"},
        "SQ": [{"SN": "chr1", "LN": 64}],
    }
    with pysam.AlignmentFile(str(path), "wb", header=header) as bam:
        for idx, start in enumerate([5, 15], start=1):
            segment = pysam.AlignedSegment()
            segment.query_name = f"read{idx}"
            segment.query_sequence = "ACGTAC"
            segment.flag = 0
            segment.reference_id = 0
            segment.reference_start = start
            segment.mapping_quality = 60
            segment.cigar = ((0, 6),)
            segment.query_qualities = pysam.qualitystring_to_array("IIIIII")
            bam.write(segment)


def _write_non_genome_bam(path: Path) -> None:
    header = {
        "HD": {"VN": "1.0", "SO": "coordinate"},
        "SQ": [{"SN": "miR-1", "LN": 70}],
    }
    with pysam.AlignmentFile(str(path), "wb", header=header) as bam:
        segment = pysam.AlignedSegment()
        segment.query_name = "read1"
        segment.query_sequence = "ACGTAC"
        segment.flag = 0
        segment.reference_id = 0
        segment.reference_start = 5
        segment.mapping_quality = 60
        segment.cigar = ((0, 6),)
        segment.query_qualities = pysam.qualitystring_to_array("IIIIII")
        bam.write(segment)


def _make_input_adata(tmp: Path, *, with_counts: bool, sorted_bam: bool = True) -> ad.AnnData:
    fastq = tmp / "sample1.trimmed.fastq.gz"
    bam = tmp / ("sample1.sorted.bam" if sorted_bam else "sample1.unsorted.bam")
    fasta = tmp / "genome.fa"

    _write_fastq_gz(fastq, [("r1", "ACGTAC"), ("r2", "TTTTTT")])
    fasta.write_text(">chr1\n" + "ACGT" * 16 + "\n")
    pysam.faidx(str(fasta))
    _write_bam(bam, sorted_bam=sorted_bam)

    obs = pd.DataFrame(
        {
            "trimmed_path": [str(fastq)],
            "bam_path": [str(bam)],
        },
        index=["sample1"],
    )
    if with_counts:
        adata = ad.AnnData(
            X=np.array([[12.0]], dtype=float),
            obs=obs,
            var=pd.DataFrame(index=["mir1"]),
        )
        adata.layers["counts"] = adata.X.copy()
    else:
        adata = ad.AnnData(obs=obs)
    adata.uns["genome_fasta"] = str(fasta)
    return adata


def test_fragomics_returns_fragmentomics_anndata_when_input_has_no_counts():
    with tempfile.TemporaryDirectory() as tmpdir:
        adata = _make_input_adata(Path(tmpdir), with_counts=False)

        result = sa.fragment.fragomics(adata, output_dir=str(Path(tmpdir) / "frag"), jobs=1)

        assert isinstance(result, ad.AnnData)
        assert "counts" in result.layers
        assert "CPM" in result.layers
        assert "type" in result.var.columns
        assert "feature" in result.var.columns
        assert "fragomics_raw_tsv" in result.uns
        assert Path(result.uns["fragomics_raw_tsv"]).exists()
        assert "FSD" in set(result.var["type"].astype(str))
        assert "RCD" in set(result.var["type"].astype(str))


def test_fragomics_returns_mudata_when_input_already_has_srna_counts():
    with tempfile.TemporaryDirectory() as tmpdir:
        adata = _make_input_adata(Path(tmpdir), with_counts=True)

        result = sa.fragment.fragomics(adata, output_dir=str(Path(tmpdir) / "frag"), jobs=1)

        assert isinstance(result, md.MuData)
        assert set(result.mod.keys()) == {"srna", "fragmentomics"}
        assert "counts" in result.mod["fragmentomics"].layers
        assert "CPM" in result.mod["fragmentomics"].layers
        assert "counts" in result.mod["srna"].layers


def test_fragomics_rejects_unsorted_bam():
    with tempfile.TemporaryDirectory() as tmpdir:
        adata = _make_input_adata(Path(tmpdir), with_counts=False, sorted_bam=False)

        with pytest.raises(ValueError, match="coordinate-sorted"):
            sa.fragment.fragomics(adata, output_dir=str(Path(tmpdir) / "frag"), jobs=1)


def test_fragomics_rejects_non_genome_bam():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        adata = _make_input_adata(tmp, with_counts=False)
        bam = tmp / "sample1.transcriptome.bam"
        _write_non_genome_bam(bam)
        adata.obs["bam_path"] = [str(bam)]

        with pytest.raises(ValueError, match="whole-genome|transcriptome|provided genome FASTA"):
            sa.fragment.fragomics(adata, output_dir=str(tmp / "frag"), jobs=1)


def test_fragomics_emits_progress_logs(capsys: pytest.CaptureFixture[str]):
    with tempfile.TemporaryDirectory() as tmpdir:
        adata = _make_input_adata(Path(tmpdir), with_counts=False)

        sa.fragment.fragomics(
            adata,
            output_dir=str(Path(tmpdir) / "frag"),
            jobs=1,
            max_reads=1,
        )

        out = capsys.readouterr().out
        assert "[fragomics] start" in out
        assert "sample=sample1 phase=queued worker_started" in out
        assert "sample=sample1 phase=fastq start FSD/EDM extraction" in out
        assert "sample=sample1 phase=bam start FSC/RCD/BPM extraction" in out
        assert "[fragomics] done" in out
