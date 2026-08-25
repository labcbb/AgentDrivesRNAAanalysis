"""Deterministic guards for target-prediction candidate selection."""
from __future__ import annotations

from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd

from sRNAgent.Tools.target.candidates import select_target_candidates
from sRNAgent.agent.plan_orchestrator import _apply_skill_plan_contracts, _load_skill_plan_contracts
from sRNAgent.skill_registry import SkillRegistry


def _adata() -> ad.AnnData:
    adata = ad.AnnData(
        X=np.array([[1.0, 1.0], [1.0, 1.0]]),
        obs=pd.DataFrame(index=["S1", "S2"]),
        var=pd.DataFrame({"rna_type": ["isoMiR", "isoMiR"]}, index=["iso-pass", "iso-fdr-fail"]),
    )
    adata.layers["counts"] = np.array([[100, 10], [100, 10]], dtype=float)
    adata.layers["logcpm"] = np.log2(adata.layers["counts"] + 0.5)
    adata.uns["de_results"] = pd.DataFrame(
        {"adj_p_value": [0.01, 0.20], "log_fc": [1.5, 3.0]},
        index=adata.var_names,
    )
    return adata


def test_target_candidate_selector_requires_fdr_effect_and_arithmetic_cpm():
    result = select_target_candidates(_adata(), candidate_type="isomir")

    state = result.uns["target_candidate_selection"]
    assert state["features"] == ["iso-pass"]
    assert state["table"].loc["iso-pass", "mean_cpm"] > 5
    assert state["parameters"]["counts_layer"] == "counts"


def test_target_candidate_selector_defaults_to_top_five_ranked_de_isomirs():
    feature_ids = [f"iso-{index}" for index in range(1, 8)]
    adata = ad.AnnData(
        X=np.ones((2, len(feature_ids))),
        obs=pd.DataFrame(index=["S1", "S2"]),
        var=pd.DataFrame({"rna_type": "isoMiR"}, index=feature_ids),
    )
    adata.layers["counts"] = np.full((2, len(feature_ids)), 100.0)
    adata.uns["de_results"] = pd.DataFrame(
        {
            "adj_p_value": [0.01, 0.001, 0.02, 0.003, 0.04, 0.005, 0.03],
            "log_fc": [1.2, 1.5, 1.8, 2.1, 3.0, 1.1, 2.5],
        },
        index=feature_ids,
    )

    result = select_target_candidates(adata, candidate_type="isomir")

    state = result.uns["target_candidate_selection"]
    assert state["features"] == ["iso-2", "iso-4", "iso-6", "iso-1", "iso-3"]
    assert state["n_eligible_before_top_n"] == 7
    assert state["n_features"] == 5
    assert state["parameters"]["top_n"] == 5


def test_explicit_target_candidates_are_not_truncated_by_the_default_limit():
    adata = _adata()
    adata.var = pd.DataFrame({"rna_type": "isoMiR"}, index=["iso-1", "iso-2"])

    result = select_target_candidates(adata, candidates=["iso-1", "iso-2"])

    state = result.uns["target_candidate_selection"]
    assert state["features"] == ["iso-1", "iso-2"]
    assert state["parameters"]["top_n"] is None


def test_target_skill_contract_requires_selection_and_confirmation_before_prediction():
    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()
    contracts = _load_skill_plan_contracts(
        registry,
        "对 isomiR 做 miRanda 靶标预测和 seed 分析",
        step_skills=["mirna-target-prediction"],
    )
    planned = _apply_skill_plan_contracts(
        [{"id": "1", "title": "预测 isomiR 靶标", "goal": "运行 miRanda 与 seed", "skill": "mirna-target-prediction"}],
        contracts,
        user_query="对 isomiR 做 miRanda 靶标预测和 seed 分析",
        extra_context="",
    )

    assert [step["workflowContract"] for step in planned[:2]] == [
        "select-target-candidates-before-prediction",
        "confirm-target-candidates-before-prediction",
    ]
    selection, confirmation, prediction = planned
    assert confirmation["depends_on"] == [selection["id"]]
    assert prediction["depends_on"] == [confirmation["id"]]
    assert "sample_mirtop_gff" not in prediction["inputs"]
    assert prediction["executionContracts"][0]["id"] == "target-prediction-execution-contract"
    assert "前 5 个 candidate" in selection["goal"]
