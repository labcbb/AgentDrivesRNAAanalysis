"""Tests for local miRNA/isomiR seed validation against predicted-target UTRs."""
from __future__ import annotations

import sys
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.Tools.target.seed import find_seed_matches, seed_utr_matches  # noqa: E402


def test_find_seed_matches_keeps_overlapping_sites_and_normalizes_rna():
    assert find_seed_matches("AAAAAAAAT", "TTTTTTTT") == [0, 1]
    assert find_seed_matches("AUGCAAAU", "CCCATTTGCAGGG") == [3]


def test_seed_utr_matches_can_validate_cached_starbase_target_genes(tmp_path):
    adata = ad.AnnData(
        X=np.array([[1.0]]),
        obs=pd.DataFrame(index=["S1"]),
        var=pd.DataFrame(
            {
                "rna_type": ["isoMiR"],
                "mirna_id": ["hsa-miR-21-5p"],
                "sequence": ["AUGCAAAU"],
            },
            index=["hsa-miR-21-5p|iso_3p"],
        ),
    )
    target_tsv = tmp_path / "targets.tsv"
    pd.DataFrame(
        {
            "miRNAname": ["hsa-miR-21-5p", "hsa-miR-21-5p"],
            "geneName": ["PDCD4", "OTHER"],
        }
    ).to_csv(target_tsv, sep="\t", index=False)
    adata.uns["starbase_mirna_targets"] = {
        "last_run": {"records": [{"miRNA": "hsa-miR-21-5p", "tsv": str(target_tsv)}]}
    }
    utr_fasta = tmp_path / "utr.fa"
    utr_fasta.write_text(
        ">ENST0001|gene=PDCD4\nCCCATTTGCAGGG\n"
        ">ENST0002|gene=UNPREDICTED\nATTTGCA\n",
        encoding="utf-8",
    )

    result = seed_utr_matches(
        adata, str(utr_fasta), target_key="starbase_mirna_targets",
        restrict_to_cached_targets=True,
        feature_ids=["hsa-miR-21-5p|iso_3p"],
    )

    matches = result.uns["seed_utr_matches"]["results"]
    summary = result.uns["seed_utr_matches"]["summary"]
    assert matches[["gene_name", "transcript_id", "utr_match_start"]].to_dict("records") == [
        {"gene_name": "PDCD4", "transcript_id": "ENST0001", "utr_match_start": 3}
    ]
    assert matches.loc[0, "seed_sequence"] == "UGCAAAU"
    assert summary.loc[0, "predicted_genes"] == 2
    assert summary.loc[0, "matched_transcripts"] == 1


def test_isomir_sequence_scans_utr_directly_without_parent_or_starbase(tmp_path):
    adata = ad.AnnData(
        X=np.array([[1.0]]),
        obs=pd.DataFrame(index=["S1"]),
        var=pd.DataFrame(
            {"rna_type": ["isomiR"], "sequence": ["AUGCAAAU"]},
            index=["isomiR|observed_variant"],
        ),
    )
    utr_fasta = tmp_path / "utr.fa"
    utr_fasta.write_text(
        ">ENST0009|gene=UNPREDICTED\nATTTGCA\n",
        encoding="utf-8",
    )

    result = seed_utr_matches(adata, str(utr_fasta), feature_ids=["isomiR|observed_variant"])

    state = result.uns["seed_utr_matches"]
    assert state["parameters"]["mode"] == "direct_all_utr_scan"
    assert state["results"]["gene_name"].tolist() == ["UNPREDICTED"]
    assert state["results"]["feature_id"].tolist() == ["isomiR|observed_variant"]
    assert state["results"]["mirna_id"].tolist() == [""]


def test_seed_utr_matches_rejects_an_unselected_whole_matrix_scan(tmp_path):
    adata = ad.AnnData(
        X=np.array([[1.0]]),
        obs=pd.DataFrame(index=["S1"]),
        var=pd.DataFrame({"sequence": ["AUGCAAAU"]}, index=["iso-1"]),
    )
    utr_fasta = tmp_path / "utr.fa"
    utr_fasta.write_text(">TX1|gene=GENE1\nATTTGCA\n", encoding="utf-8")

    try:
        seed_utr_matches(adata, str(utr_fasta))
    except ValueError as exc:
        assert "select_target_candidates" in str(exc)
    else:
        raise AssertionError("whole-matrix seed scans must require a candidate set")
