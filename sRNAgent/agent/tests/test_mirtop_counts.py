"""Regression tests for mirtop's quoted multi-sample counts TSV output."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.Tools.quant.mirtop import (  # noqa: E402
    _normalize_mirtop_counts_tsv,
    _parse_mirtop_counts,
)


def test_normalize_mirtop_quoted_sample_columns(tmp_path: Path):
    counts = tmp_path / "mirtop.tsv"
    counts.write_text(
        "UID\tRead\tmiRNA\tVariant\tiso_5p\tiso_3p\tiso_add3p\tiso_snp\t\"S1\tS2\"\n"
        "iso-1\tACGT\thsa-miR-1\tiso_3p:-1\t0\t-1\t0\t0\t\"3\t7\"\n",
        encoding="utf-8",
    )

    assert _normalize_mirtop_counts_tsv(counts) is True
    text = counts.read_text(encoding="utf-8")
    assert '"S1' not in text
    assert '"3' not in text

    sample_df, _id_col, _mirna, _meta = _parse_mirtop_counts(counts, ["S1", "S2"])
    assert list(sample_df.columns) == ["S1", "S2"]
    assert sample_df.loc["iso-1", "S1"] == 3
    assert sample_df.loc["iso-1", "S2"] == 7
