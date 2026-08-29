"""Tests for deterministic isomiR-parent target comparison."""

from __future__ import annotations

import importlib
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd

from sRNAgent.Tools.target import compare_isomir_parent_targets


miranda_module = importlib.import_module("sRNAgent.Tools.target.miranda")


def test_compare_isomir_parent_targets_records_gain_loss_and_conservation(tmp_path, monkeypatch):
    adata = ad.AnnData(
        X=np.array([[1.0]]),
        obs=pd.DataFrame(index=["S1"]),
        var=pd.DataFrame(
            {
                "rna_type": ["isoMiR"],
                "mirna_id": ["hsa-miR-parent"],
                "sequence": ["AUGCAAAU"],
            },
            index=["iso-1"],
        ),
    )
    mature = tmp_path / "mature.fa"
    mature.write_text(">hsa-miR-parent\nAAGCAAAU\n", encoding="utf-8")
    utr = tmp_path / "utr.fa"
    utr.write_text(
        ">TXSH|gene=SHARED\nATTTGCAATTTGCT\n"
        ">TXGAIN|gene=GAINED\nATTTGCA\n"
        ">TXLOST|gene=LOST\nATTTGCT\n",
        encoding="utf-8",
    )

    def fake_run(command):
        query_ids = [line[1:] for line in Path(command[1]).read_text(encoding="utf-8").splitlines() if line.startswith(">")]
        output = Path(command[command.index("-out") + 1])
        if query_ids == ["iso-1"]:
            text = (
                "Read Sequence: iso-1\n>> TXSH|gene=SHARED\nScores for this duplex: 150\nEnergy: -21\n"
                ">> TXGAIN|gene=GAINED\nScores for this duplex: 160\nEnergy: -22\n"
            )
        else:
            assert query_ids == ["hsa-miR-parent"]
            text = (
                "Read Sequence: hsa-miR-parent\n>> TXSH|gene=SHARED\nScores for this duplex: 155\nEnergy: -21\n"
                ">> TXLOST|gene=LOST\nScores for this duplex: 165\nEnergy: -22\n"
            )
        output.write_text(text, encoding="utf-8")

    monkeypatch.setattr(miranda_module, "run_cli_cmd", fake_run)
    result = compare_isomir_parent_targets(
        adata,
        str(mature),
        str(utr),
        output_dir=str(tmp_path / "comparison"),
        feature_ids=["iso-1"],
    )

    state = result.uns["isomir_parent_target_comparison"]
    summary = state["summary"].iloc[0]
    assert summary["seed_status"] == "changed"
    assert summary[["n_conserved", "n_isomir_gained", "n_parent_lost"]].to_dict() == {
        "n_conserved": 1,
        "n_isomir_gained": 1,
        "n_parent_lost": 1,
    }
    assert state["gene_sets"] == {
        "conserved": ["SHARED"],
        "isomir_gained": ["GAINED"],
        "parent_lost": ["LOST"],
    }
    changes = state["results"].set_index("gene_name")["target_change"].to_dict()
    assert changes == {"GAINED": "isomir_gained", "LOST": "parent_lost", "SHARED": "conserved"}
    assert "isomir_miranda_targets" in result.uns
    assert "parent_mirna_miranda_targets" in result.uns
    assert "isomir_seed_utr_matches" in result.uns
    assert "parent_mirna_seed_utr_matches" in result.uns
    output = tmp_path / "comparison.h5ad"
    result.write_h5ad(output)
    restored = ad.read_h5ad(output)
    assert restored.uns["isomir_parent_target_comparison"]["gene_sets"]["isomir_gained"] == ["GAINED"]


def test_compare_isomir_parent_targets_requires_parent_sequence(tmp_path):
    adata = ad.AnnData(
        X=np.array([[1.0]]),
        obs=pd.DataFrame(index=["S1"]),
        var=pd.DataFrame(
            {"rna_type": ["isoMiR"], "mirna_id": ["missing-parent"], "sequence": ["AUGCAAAU"]},
            index=["iso-1"],
        ),
    )
    mature = tmp_path / "mature.fa"
    mature.write_text(">hsa-miR-parent\nAAGCAAAU\n", encoding="utf-8")
    utr = tmp_path / "utr.fa"
    utr.write_text(">TX|gene=GENE\nATTTGCA\n", encoding="utf-8")

    try:
        compare_isomir_parent_targets(adata, str(mature), str(utr), feature_ids=["iso-1"])
    except ValueError as exc:
        assert "No mature miRBase sequence" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected unresolved parent sequence to fail")
