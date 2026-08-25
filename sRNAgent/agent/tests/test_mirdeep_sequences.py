"""Tests for miRBase mature-sequence annotations written during quantification."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.Tools.quant.mirdeep2 import _load_mature_sequences  # noqa: E402


def test_load_mature_sequences_uses_identifier_and_rna_alphabet(tmp_path):
    fasta = tmp_path / "mature_hsa.fa"
    fasta.write_text(
        ">hsa-miR-1-5p MIMAT0000001 Homo sapiens miR-1-5p\n"
        "UGGAAUGUAAAGAAGUAUGUAU\n"
        ">hsa-miR-2-3p\n"
        "ACGTTGCA\n",
        encoding="utf-8",
    )

    sequences = _load_mature_sequences(str(fasta))

    assert sequences["hsa-miR-1-5p"] == "UGGAAUGUAAAGAAGUAUGUAU"
    assert sequences["hsa-miR-2-3p"] == "ACGUUGCA"
