"""Tests for deterministic and auditable candidate prioritization."""

from __future__ import annotations

import sys
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.Tools.model.candidate_prioritization import (  # noqa: E402
    CANDIDATE_PRIORITIZATION_UNS_KEY,
    candidate_prioritization,
)


def _adata(tmp_path: Path) -> ad.AnnData:
    values = np.array([
        [4.0, 3.0, 2.0], [4.1, 3.1, 2.1], [4.0, 3.0, 2.2],
        [4.1, 3.0, 2.0], [4.0, 3.1, 2.1], [4.1, 3.0, 2.2],
    ])
    adata = ad.AnnData(
        X=values,
        obs=pd.DataFrame(
            {"batch": ["A", "A", "A", "B", "B", "B"], "library_size": [100, 110, 90, 105, 95, 100]},
            index=[f"S{i}" for i in range(6)],
        ),
        var=pd.DataFrame(
            {"rna_type": ["miRNA", "miRNA", "miRNA"], "multi_mapping_risk": [0.1, 0.2, 0.1]},
            index=["miR-good", "miR-fdr-fail", "miR-effect-fail"],
        ),
    )
    adata.uns["de_results"] = pd.DataFrame(
        {
            "adj_p_value": [0.01, 0.20, 0.01],
            "log_fc": [1.2, 1.5, 0.2],
            "ave_expr": [4.0, 3.0, 2.0],
        },
        index=adata.var_names,
    )
    adata.uns["candidate_replication"] = pd.DataFrame(
        {"feature": ["miR-good"], "log_fc": [0.8], "adj_p_value": [0.01]},
    )
    adata.uns["classification"] = {
        "feature_names": ["miR-good"],
        "details": {"logistic_regression": {"cross_validation": {"roc_auc": 0.78}}},
    }
    adata.uns["cox"] = {
        "request": {"features": ["miR-good"], "obs_features": ["age", "stage"]},
        "selected_features": ["miR-good"],
        "multivariate_results": pd.DataFrame({"coef": [0.5], "p_value": [0.02]}, index=["miR-good"]),
        "cross_validation": pd.DataFrame({"fold": [1, 2], "c_index": [0.66, 0.68]}),
    }
    target_path = tmp_path / "miR-good.starbase.tsv"
    pd.DataFrame(
        {"miRNAname": ["miR-good", "miR-good"], "geneName": ["TP53", "PTEN"], "clipExpNum": [2, 1], "degraExpNum": [1, 0]},
    ).to_csv(target_path, sep="\t", index=False)
    adata.uns["starbase_mirna_targets"] = {"last_run": {"records": [{"miRNA": "miR-good", "tsv": str(target_path)}]}}
    adata.uns["enrichr"] = {"results": pd.DataFrame({"Term": ["Cancer pathway"]})}
    return adata


def test_candidate_prioritization_audits_all_candidates_and_gates_failures(tmp_path):
    adata = _adata(tmp_path)
    result = candidate_prioritization(adata, output_dir=str(tmp_path / "priorities"))

    state = result.uns[CANDIDATE_PRIORITIZATION_UNS_KEY]
    audit = state["audit"].set_index("candidate")
    assert set(audit.index) == {"miR-good", "miR-fdr-fail", "miR-effect-fail"}
    assert audit.loc["miR-good", "eligible"]
    assert not audit.loc["miR-fdr-fail", "eligible"]
    assert "DE FDR exceeds" in audit.loc["miR-fdr-fail", "exclusion_reasons"]
    assert not audit.loc["miR-effect-fail", "eligible"]
    assert "log-fold-change" in audit.loc["miR-effect-fail", "exclusion_reasons"]
    assert state["recommended"]["candidate"].tolist() == ["miR-good"]
    assert Path(state["artifacts"]["audit_csv"]).exists()
    assert Path(state["artifacts"]["recommended_csv"]).exists()
    assert Path(state["artifacts"]["manifest"]).exists()

    candidate_prioritization(result, output_dir=str(tmp_path / "priorities"))
    assert result.uns[CANDIDATE_PRIORITIZATION_UNS_KEY]["reused"] is True


def test_candidate_prioritization_gates_a_model_used_without_cross_validation(tmp_path):
    adata = _adata(tmp_path)
    adata.uns["classification"] = {"feature_names": ["miR-good"], "details": {"logistic_regression": {"all_samples": {"roc_auc": 0.99}}}}
    del adata.uns["cox"]

    result = candidate_prioritization(adata, candidates=["miR-good"], output_dir=str(tmp_path / "priorities"))
    row = result.uns[CANDIDATE_PRIORITIZATION_UNS_KEY]["audit"].iloc[0]

    assert not row["eligible"]
    assert "model performance lacks cross-validation" in row["exclusion_reasons"]


def test_candidate_prioritization_marks_partial_replication_stability_as_a_gap(tmp_path):
    adata = _adata(tmp_path)
    adata.uns["candidate_replication"] = pd.DataFrame(
        {"feature": ["miR-good", "miR-good"], "log_fc": [0.8, -0.3], "adj_p_value": [0.01, 0.01]},
    )

    result = candidate_prioritization(adata, candidates=["miR-good"], output_dir=str(tmp_path / "priorities"))
    row = result.uns[CANDIDATE_PRIORITIZATION_UNS_KEY]["audit"].iloc[0]

    assert row["eligible"]
    assert "replication direction is not fully stable" in row["evidence_gaps"]
