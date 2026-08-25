"""Regression tests for mirtop's quoted multi-sample counts TSV output."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.Tools.quant.mirtop import (  # noqa: E402
    _merge_sample_gffs,
    _merged_gff_covers_samples,
    _normalise_rna_sequence,
    _normalize_mirtop_counts_tsv,
    _parse_mirtop_counts,
)
from sRNAgent.Tools.reference.mirbase import prepare_mirtop_reference  # noqa: E402


def test_mirtop_read_sequence_is_normalized_to_rna_for_targeting():
    assert _normalise_rna_sequence("acgttgca") == "ACGUUGCA"
    assert _normalise_rna_sequence("ACGT-N") == ""


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


def test_merge_per_sample_gffs_preserves_coldata_for_all_samples(tmp_path: Path):
    sample_one = tmp_path / "S1.gff"
    sample_two = tmp_path / "S2.gff"
    sample_one.write_text(
        "## mirGFF3. VERSION 1.2\n"
        "## source-ontology: test\n"
        "## COLDATA: S1\n"
        "mir-1\ttest\tref_miRNA\t1\t7\t.\t+\t.\tRead ACGT;\n",
        encoding="utf-8",
    )
    sample_two.write_text(
        "## mirGFF3. VERSION 1.2\n"
        "## source-ontology: test\n"
        "## COLDATA: S2\n"
        "mir-2\ttest\tref_miRNA\t1\t7\t.\t+\t.\tRead TGCA;\n",
        encoding="utf-8",
    )

    merged = tmp_path / "mirtop.gff"
    _merge_sample_gffs({"S1": sample_one, "S2": sample_two}, merged)

    assert _merged_gff_covers_samples(merged, ["S1", "S2"])
    text = merged.read_text(encoding="utf-8")
    assert text.count("## COLDATA:") == 2
    assert text.count("## mirGFF3. VERSION 1.2") == 1
    assert "mir-1" in text and "mir-2" in text


def test_prepare_mirtop_reference_builds_valid_mature_parent_links(tmp_path: Path):
    hairpin = tmp_path / "hairpin_hsa.fa"
    hairpin.write_text(
        ">hsa-mir-a-1\nAAAACGTACCCC\n"
        ">hsa-mir-a-2\nGGGACGTATTT\n"
        ">hsa-mir-b\nTTTGGCAAAA\n",
        encoding="utf-8",
    )
    mature = tmp_path / "mature_hsa.fa"
    mature.write_text(
        ">hsa-miR-a-5p\nACGUA\n"
        ">hsa-miR-b-3p\nGGCAA\n",
        encoding="utf-8",
    )
    # This mirrors the miRBase genome GFF3: precursor rows only, so mirtop
    # would otherwise accept it but filter every mapped read as non-miRNA.
    source = tmp_path / "hsa.gff3"
    source.write_text(
        "##gff-version 3\n"
        "hsa-mir-a-1\tmirbase\tmiRNA_primary_transcript\t1\t12\t.\t+\t.\tID=hsa-mir-a-1;Name=hsa-mir-a-1\n"
        "hsa-mir-a-2\tmirbase\tmiRNA_primary_transcript\t1\t11\t.\t+\t.\tID=hsa-mir-a-2;Name=hsa-mir-a-2\n"
        "hsa-mir-b\tmirbase\tmiRNA_primary_transcript\t1\t10\t.\t+\t.\tID=hsa-mir-b;Name=hsa-mir-b\n",
        encoding="utf-8",
    )

    result = prepare_mirtop_reference(str(source), str(hairpin), str(mature))

    output = Path(result["gff3"])
    assert result["rebuilt"] is True
    assert result["mature_mappings"] == 3
    assert result["unmapped_mature_mirnas"] == 0
    assert output.name == "hsa.mirtop.gff3"

    primary_ids = set()
    mature_rows = []
    for line in output.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        attributes = dict(item.split("=", 1) for item in fields[8].split(";") if "=" in item)
        if fields[2] == "miRNA_primary_transcript":
            primary_ids.add(attributes["ID"])
        elif fields[2] == "miRNA":
            mature_rows.append((fields, attributes))

    assert len(mature_rows) == 3
    assert {attrs["Derives_from"] for _fields, attrs in mature_rows} <= primary_ids
    assert all(fields[0] == attrs["Derives_from"] for fields, attrs in mature_rows)
    assert sum(attrs["Name"] == "hsa-miR-a-5p" for _fields, attrs in mature_rows) == 2
