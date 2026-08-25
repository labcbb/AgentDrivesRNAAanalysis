"""Tests for cached, sequential starBase target retrieval."""

from __future__ import annotations

import sys
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.Tools.target import starbase as starbase_module  # noqa: E402
from sRNAgent.Tools.target.candidates import select_target_candidates  # noqa: E402


SAMPLE_RESPONSE = "\n".join([
    "#please cite:",
    "MIMAT0000076\thsa-miR-21-5p\tENSG00000150593\tPDCD4\tprotein_coding\tchr10\t1\t2\t1\t3\t+\t16\t0\tAGO2\t1\t0\t0\t1\t1\t1\t1\t0.9612\t6.347\t23\tMCF7",
])


def test_starbase_explicit_target_query_is_cached_in_tsv_and_uns(tmp_path, monkeypatch):
    adata = ad.AnnData(
        X=np.array([[1]]),
        obs=pd.DataFrame(index=["S1"]),
        var=pd.DataFrame({"rna_type": ["miRNA"]}, index=["hsa-miR-21-5p"]),
    )
    calls = []

    def fake_fetch(params, timeout):
        calls.append((dict(params), timeout))
        return SAMPLE_RESPONSE

    monkeypatch.setattr(starbase_module, "_fetch_starbase", fake_fetch)
    result = starbase_module.starbase_mirna_targets(
        adata,
        mirnas="hsa-miR-21-5p",
        output_dir=str(tmp_path),
        request_interval=0,
    )

    record = result.uns[starbase_module.STARBASE_UNS_KEY]["last_run"]["records"][0]
    stored = pd.read_csv(record["tsv"], sep="\t")
    assert calls[0][0]["assembly"] == "hg38"
    assert calls[0][0]["program"] == "None"
    assert record["n_targets"] == 1
    assert {"miRNAname", "geneName", "pancancerNum", "TDMDScore"}.issubset(stored.columns)

    starbase_module.starbase_mirna_targets(
        result,
        mirnas="hsa-miR-21-5p",
        output_dir=str(tmp_path),
        request_interval=0,
    )
    assert len(calls) == 1
    assert result.uns[starbase_module.STARBASE_UNS_KEY]["last_run"]["records"][0]["reused"] is True


def test_starbase_selects_significant_mirnas_from_de_results(tmp_path, monkeypatch):
    adata = ad.AnnData(
        X=np.array([[1, 1]]),
        obs=pd.DataFrame(index=["S1"]),
        var=pd.DataFrame({"rna_type": ["miRNA", "miRNA"]}, index=["hsa-miR-21-5p", "hsa-miR-1-3p"]),
    )
    adata.uns["de_results"] = pd.DataFrame(
        {"adj_p_value": [0.01, 0.2], "log_fc": [1.5, 2.0]},
        index=["hsa-miR-21-5p", "hsa-miR-1-3p"],
    )
    adata.layers["counts"] = np.array([[100, 100]], dtype=float)
    select_target_candidates(adata)
    queried = []
    monkeypatch.setattr(
        starbase_module,
        "_fetch_starbase",
        lambda params, timeout: queried.append(params["miRNA"]) or SAMPLE_RESPONSE,
    )

    starbase_module.starbase_mirna_targets(adata, output_dir=str(tmp_path), request_interval=0)

    assert queried == ["hsa-miR-21-5p"]


def test_starbase_rejects_an_observed_isomir_candidate(tmp_path):
    adata = ad.AnnData(
        X=np.array([[1]]),
        obs=pd.DataFrame(index=["S1"]),
        var=pd.DataFrame(
            {"rna_type": ["isoMiR"], "mirna_id": ["hsa-miR-21-5p"]},
            index=["hsa-miR-21-5p|iso_3p"],
        ),
    )
    adata.uns["target_candidate_selection"] = {"features": ["hsa-miR-21-5p|iso_3p"]}

    try:
        starbase_module.starbase_mirna_targets(adata, output_dir=str(tmp_path))
    except ValueError as exc:
        assert "cannot represent observed isomiR" in str(exc)
    else:
        raise AssertionError("starBase must not substitute an isomiR parent sequence")


def test_unified_target_prediction_skill_is_discoverable():
    from sRNAgent.skill_registry import SkillRegistry

    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()

    assert "mirna-target-prediction" in registry.skill_metadata
    assert "starbase-mirna-targets" not in registry.skill_metadata
    skill = registry.load_full_skill("mirna-target-prediction")
    assert skill is not None
    assert "sa.reference.download_mirbase" in skill.body
    assert "sa.target.starbase_mirna_targets" in skill.body
    assert "sa.target.miranda" in skill.body
    assert "sa.target.seed_utr_matches" in skill.body
    assert "enrichr-gene-enrichment" in skill.body
    assert "adj_p_value < 0.05" in skill.body
    assert "log_fc > 1" in skill.body
    assert "mean CPM across all samples `> 5`" in skill.body
    assert "Candidate Gate Before Target Prediction" in skill.body
    assert "never silently launch a prediction over all isomiRs" in " ".join(skill.body.split())
    assert "does **not** require\nsample-level mirtop GFF files" in skill.body
