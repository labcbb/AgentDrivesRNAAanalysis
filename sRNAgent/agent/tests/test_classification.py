"""Tests for persisted AnnData classification models."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.Tools.model import classification

classification_module = importlib.import_module("sRNAgent.Tools.model.classification")


def _adata() -> ad.AnnData:
    rng = np.random.default_rng(7)
    labels = np.array(["normal"] * 15 + ["tumor"] * 15)
    values = rng.normal(0, 0.3, size=(30, 5))
    values[labels == "tumor"] += np.array([2.0, 1.3, -1.2, 0.8, -0.7])
    return ad.AnnData(
        X=values,
        obs=pd.DataFrame({"group": labels}, index=[f"S{i}" for i in range(30)]),
        var=pd.DataFrame(index=[f"miR-{i}" for i in range(5)]),
    )


def test_classification_compares_all_models_and_persists_artifacts(tmp_path):
    adata = _adata()
    result = classification(
        adata,
        list(adata.var_names),
        "group",
        split_data=True,
        cross_validate=True,
        cv_folds=3,
        output_dir=str(tmp_path),
    )

    state = result.uns[classification_module.CLASSIFICATION_UNS_KEY]
    assert set(state["model_paths"]) == {"svm", "random_forest", "xgboost", "logistic_regression"}
    assert set(state["performance"]["evaluation"]) == {"holdout", "cross_validation", "all_samples"}
    assert set(state["performance"]["model"]) == set(state["model_paths"])
    assert all(Path(path).exists() for path in state["model_paths"].values())
    assert state["details"]["svm"]["holdout"]["n_test"] == 6

    h5ad = tmp_path / "classified.h5ad"
    result.write_h5ad(h5ad)
    restored = ad.read_h5ad(h5ad)
    assert restored.uns[classification_module.CLASSIFICATION_UNS_KEY]["performance"].shape[0] == 12


def test_classification_runs_only_requested_model_and_reuses(tmp_path):
    adata = _adata()
    result = classification(adata, ["miR-0", "miR-1"], "group", model="logistic", output_dir=str(tmp_path))
    state = result.uns[classification_module.CLASSIFICATION_UNS_KEY]
    assert list(state["model_paths"]) == ["logistic_regression"]
    assert set(state["performance"]["evaluation"]) == {"all_samples"}

    classification(result, ["miR-0", "miR-1"], "group", model="logistic", output_dir=str(tmp_path))
    assert result.uns[classification_module.CLASSIFICATION_UNS_KEY]["reused"] is True


def test_classification_requires_known_features():
    try:
        classification(_adata(), ["missing"], "group")
    except KeyError as exc:
        assert "missing" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected a missing feature to fail")
