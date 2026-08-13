"""Differential-expression and Enrichr visualisations."""

from __future__ import annotations

from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from anndata import AnnData

from ..._registry import register_function
from .common import PALETTE, apply_style, require_group, resolve_matrix, save_figure


def _de(adata: AnnData) -> pd.DataFrame:
    result = adata.uns.get("de_results")
    if not isinstance(result, pd.DataFrame):
        raise KeyError("adata.uns['de_results'] is required; run differential analysis first")
    return result.copy()


@register_function(aliases=["plot_volcano", "volcano", "火山图"], category="plot", description="Plot a publication-style volcano plot from adata.uns['de_results'].", examples=["sa.plot.volcano(adata)"], requires={"uns": ["de_results"]}, produces={"uns": ["plots"]})
def volcano(adata: AnnData, *, fdr: float = .05, abs_logfc: float = 1.0, label_top: int = 8, output_dir: str = "results/plots") -> AnnData:
    """Render a labelled DE volcano plot."""
    de = _de(adata)
    pcol = next((c for c in ("adj_p_value", "padj", "fdr", "p_value") if c in de.columns), None)
    fc_col = next((c for c in ("log_fc", "logFC", "log2FoldChange") if c in de.columns), None)
    if not pcol or not fc_col:
        raise KeyError("DE table must contain log_fc/logFC and adjusted or raw p-value columns")
    x = pd.to_numeric(de[fc_col], errors="coerce")
    p = pd.to_numeric(de[pcol], errors="coerce").clip(lower=1e-300)
    significant = (p <= fdr) & (x.abs() >= abs_logfc)
    colors = np.where(significant & (x > 0), PALETTE[1], np.where(significant & (x < 0), PALETTE[6], PALETTE[0]))
    apply_style()
    fig, ax = plt.subplots(figsize=(5.2, 4.1))
    ax.scatter(x, -np.log10(p), c=colors, s=13, alpha=.82, linewidths=0)
    ax.axvline(-abs_logfc, color="#666666", ls="--", lw=.7); ax.axvline(abs_logfc, color="#666666", ls="--", lw=.7); ax.axhline(-np.log10(fdr), color="#666666", ls="--", lw=.7)
    top = de.assign(_p=p, _fc=x).loc[significant].nsmallest(int(label_top), "_p")
    for feature, row in top.iterrows():
        ax.annotate(str(feature), (row["_fc"], -np.log10(row["_p"])), xytext=(3, 3), textcoords="offset points", fontsize=6)
    ax.set(xlabel="log2 fold change", ylabel=f"-log10({pcol})", title="Differential expression")
    save_figure(adata, fig, "volcano", category="differential", output_dir=output_dir, parameters={"fdr": fdr, "abs_logfc": abs_logfc}, source="adata.uns['de_results']")
    return adata


@register_function(aliases=["plot_de_heatmap", "de_heatmap", "差异热图"], category="plot", description="Plot a top differentially expressed feature heatmap, optionally annotated by a group column.", examples=["sa.plot.de_heatmap(adata, group_col='condition')"], requires={"uns": ["de_results"]}, produces={"uns": ["plots"]})
def de_heatmap(adata: AnnData, *, top_n: int = 30, layer: Optional[str] = None, group_col: Optional[str] = None, output_dir: str = "results/plots") -> AnnData:
    """Render a z-scored heatmap of top DE features."""
    require_group(adata, group_col)
    de = _de(adata)
    pcol = next((c for c in ("adj_p_value", "padj", "fdr", "p_value") if c in de.columns), None)
    if not pcol:
        raise KeyError("DE table has no p-value column")
    names = [str(x) for x in de.assign(_p=pd.to_numeric(de[pcol], errors="coerce")).nsmallest(int(top_n), "_p").index if str(x) in adata.var_names]
    if not names:
        raise ValueError("No DE features are present in adata.var_names")
    matrix, layer_name = resolve_matrix(adata, layer)
    values = matrix[:, adata.var_names.get_indexer(names)].T
    values = (values - values.mean(axis=1, keepdims=True)) / np.where(values.std(axis=1, keepdims=True) == 0, 1, values.std(axis=1, keepdims=True))
    colors = None
    if group_col:
        from .common import group_palette
        colors, _ = group_palette(adata, group_col)
    apply_style()
    grid = sns.clustermap(pd.DataFrame(values, index=names, columns=adata.obs_names), cmap="vlag", center=0, col_colors=colors, figsize=(7, max(4, top_n * .13 + 2)), xticklabels=False, yticklabels=True)
    grid.ax_heatmap.set_xlabel("Samples")
    grid.ax_heatmap.set_ylabel("Features")
    save_figure(adata, grid.fig, "de_heatmap", category="differential", output_dir=output_dir, parameters={"top_n": top_n, "layer": layer_name, "group_col": group_col}, source="adata.uns['de_results']")
    return adata


@register_function(aliases=["plot_enrichment", "enrichment_dotplot", "富集气泡图"], category="plot", description="Plot Enrichr enrichment terms from stored AnnData results.", examples=["sa.plot.enrichment_dotplot(adata)"], requires={"uns": ["enrichr"]}, produces={"uns": ["plots"]})
def enrichment_dotplot(adata: AnnData, *, top_n: int = 15, output_dir: str = "results/plots") -> AnnData:
    """Render top stored Enrichr terms as a dot plot."""
    state = adata.uns.get("enrichr") or {}
    table = state.get("results") if isinstance(state, dict) else None
    if not isinstance(table, pd.DataFrame) or table.empty:
        raise KeyError("adata.uns['enrichr']['results'] is required")
    pcol = next((c for c in ("Adjusted P-value", "Adjusted P-value", "P-value") if c in table.columns), None)
    term_col = next((c for c in ("Term", "term") if c in table.columns), None)
    overlap = next((c for c in ("Overlap", "Genes") if c in table.columns), None)
    if not pcol or not term_col:
        raise KeyError("Enrichr results lack Term or p-value columns")
    frame = table.assign(_p=pd.to_numeric(table[pcol], errors="coerce")).nsmallest(int(top_n), "_p").iloc[::-1]
    size = np.full(len(frame), 42.0)
    if overlap:
        size = frame[overlap].astype(str).str.extract(r"(\d+)")[0].fillna(1).astype(float).to_numpy() * 10 + 20
    apply_style()
    fig, ax = plt.subplots(figsize=(6.0, max(3.2, len(frame) * .31 + 1.3)))
    points = ax.scatter(-np.log10(frame["_p"].clip(lower=1e-300)), frame[term_col].astype(str), s=size, c=PALETTE[1], edgecolor="white", linewidth=.5)
    ax.set(xlabel=f"-log10({pcol})", ylabel="", title="Pathway enrichment")
    save_figure(adata, fig, "enrichment_dotplot", category="enrichment", output_dir=output_dir, parameters={"top_n": top_n}, source="adata.uns['enrichr']['results']")
    return adata


__all__ = ["volcano", "de_heatmap", "enrichment_dotplot"]
