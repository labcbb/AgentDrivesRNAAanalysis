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
    gmt = tmp_path / "kegg.symbols.gmt"
    gmt.write_text("Pathway\tdescription\tTP53\tBRCA1\n", encoding="utf-8")

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
        gene_sets=str(gmt),
    )

    state = result.uns[enrichr_module.ENRICHR_UNS_KEY]
    assert calls[0]["organism"] == "human"
    assert calls[0]["gene_list"] == ["TP53", "BRCA1"]
    assert calls[0]["gene_sets"] == [str(gmt.resolve())]
    assert state["tool"] == "gseapy.enrichr.local_gmt"
    assert state["last_run"]["n_terms"] == 1
    assert state["results"].loc[0, "Term"] == "Pathway"

    enrichr_function(result, ["TP53", "BRCA1"], organism="human", gene_sets=str(gmt))
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


def test_enrichr_rejects_online_library_name_before_loading_gseapy():
    adata = ad.AnnData(X=np.array([[1]]), obs=pd.DataFrame(index=["S1"]), var=pd.DataFrame(index=["G1"]))
    try:
        enrichr_function(adata, ["TP53"], gene_sets="KEGG_2016")
    except ValueError as exc:
        assert "Local enrichment requires" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected an Enrichr library name to be rejected")


def test_enrichr_uses_downloaded_local_gmt_by_default(monkeypatch, tmp_path):
    adata = ad.AnnData(X=np.array([[1]]), obs=pd.DataFrame(index=["S1"]), var=pd.DataFrame(index=["G1"]))
    gmt = tmp_path / "c2.cp.kegg_legacy.v2026.1.Hs.symbols.gmt"
    gmt.write_text("Pathway\tdescription\tTP53\n", encoding="utf-8")
    calls = []

    class FakeGseapy:
        __version__ = "test"

        @staticmethod
        def enrichr(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(results=pd.DataFrame({"Term": ["Pathway"]}))

    monkeypatch.setattr(enrichr_module, "DEFAULT_GMT_DIR", tmp_path)
    monkeypatch.setattr(enrichr_module, "_load_gseapy", lambda: FakeGseapy)

    enrichr_function(adata, ["TP53"])

    assert calls[0]["gene_sets"] == [str(gmt.resolve())]


def test_enrichr_preserves_named_contrasts(monkeypatch, tmp_path):
    adata = ad.AnnData(X=np.array([[1]]), obs=pd.DataFrame(index=["S1"]), var=pd.DataFrame(index=["G1"]))
    gmt = tmp_path / "kegg.symbols.gmt"
    gmt.write_text("Pathway\tdescription\tGAINED\tLOST\n", encoding="utf-8")

    class FakeGseapy:
        __version__ = "test"

        @staticmethod
        def enrichr(**_kwargs):
            return SimpleNamespace(results=pd.DataFrame({"Term": ["Pathway"]}))

    monkeypatch.setattr(enrichr_module, "_load_gseapy", lambda: FakeGseapy)
    enrichr_function(adata, ["GAINED"], gene_sets=str(gmt), result_key="isomir_gained_enrichr")
    enrichr_function(adata, ["LOST"], gene_sets=str(gmt), result_key="parent_lost_enrichr")

    assert adata.uns["isomir_gained_enrichr"]["input_genes"] == ["GAINED"]
    assert adata.uns["parent_lost_enrichr"]["input_genes"] == ["LOST"]
    assert "enrichr" not in adata.uns


def test_enrichr_skill_is_discoverable():
    from sRNAgent.skill_registry import SkillRegistry

    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()

    assert "enrichr-gene-enrichment" in registry.skill_metadata
    skill = registry.load_full_skill("enrichr-gene-enrichment")
    assert skill is not None
    assert "mirna-target-prediction" in skill.body
