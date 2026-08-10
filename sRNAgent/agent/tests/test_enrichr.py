"""Tests for AnnData-backed GSEApy Enrichr enrichment."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import anndata as ad
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.Tools.target import enrichr as enrichr_function  # noqa: E402

enrichr_module = importlib.import_module("sRNAgent.Tools.target.enrichr")


def test_enrichr_normalizes_human_stores_results_and_reuses(monkeypatch, tmp_path):
    adata = ad.AnnData(X=np.array([[1]]), obs=pd.DataFrame(index=["S1"]), var=pd.DataFrame(index=["G1"]))
    calls = []

    class FakeGseapy:
        __version__ = "test"

        @staticmethod
        def enrichr(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(results=pd.DataFrame({"Term": ["Pathway"], "Adjusted P-value": [0.01]}))

    monkeypatch.setattr(enrichr_module, "_load_gseapy", lambda: FakeGseapy)
    result = enrichr_function(
        adata,
        ["TP53", "BRCA1", "TP53"],
        organism="Human",
        gene_sets="KEGG_2016",
    )

    state = result.uns[enrichr_module.ENRICHR_UNS_KEY]
    assert calls[0]["organism"] == "human"
    assert calls[0]["gene_list"] == ["TP53", "BRCA1"]
    assert state["last_run"]["n_terms"] == 1
    assert state["results"].loc[0, "Term"] == "Pathway"

    enrichr_function(result, ["TP53", "BRCA1"], organism="human")
    assert len(calls) == 1
    assert result.uns[enrichr_module.ENRICHR_UNS_KEY]["last_run"]["reused"] is True

    path = tmp_path / "enrichr.h5ad"
    result.write_h5ad(path)
    restored = ad.read_h5ad(path)
    assert restored.uns[enrichr_module.ENRICHR_UNS_KEY]["results"].loc[0, "Term"] == "Pathway"


def test_enrichr_rejects_empty_genes():
    adata = ad.AnnData(X=np.array([[1]]), obs=pd.DataFrame(index=["S1"]), var=pd.DataFrame(index=["G1"]))
    try:
        enrichr_function(adata, [])
    except ValueError as exc:
        assert "at least one" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected an empty gene list to fail")


def test_enrichr_skill_is_discoverable():
    from sRNAgent.skill_registry import SkillRegistry

    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()

    assert "enrichr-gene-enrichment" in registry.skill_metadata
