"""Expression-level PCA, correlation, composition, and abundance plots."""

from __future__ import annotations

from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from anndata import AnnData

from ..._registry import register_function
from .common import PALETTE, apply_style, group_palette, require_group, resolve_matrix, save_figure


@register_function(aliases=["plot_pca", "pca_plot", "PCA绘图"], category="plot", description="Plot stored AnnData PCA coordinates, colouring samples by an optional obs group column.", examples=["sa.plot.pca(adata, group_col='group')"], requires={"obsm": ["X_pca"]}, produces={"uns": ["plots"]})
def pca(adata: AnnData, *, group_col: Optional[str] = None, output_dir: str = "results/plots") -> AnnData:
    """Render stored first and second principal components."""
    require_group(adata, group_col)
    if "X_pca" not in adata.obsm:
        raise KeyError("adata.obsm['X_pca'] is missing; run sa.diff.pca_logcpm first")
    coords = np.asarray(adata.obsm["X_pca"], dtype=float)
    if coords.shape[1] < 2:
        raise ValueError("PCA requires at least two components")
    variance = np.asarray((adata.uns.get("pca") or {}).get("variance_ratio", []), dtype=float)
    apply_style()
    fig, ax = plt.subplots(figsize=(4.6, 4.0))
    if group_col:
        colors = group_palette(adata, group_col)[1]
        for level, color in colors.items():
            mask = adata.obs[group_col].astype(str).to_numpy() == level
            ax.scatter(coords[mask, 0], coords[mask, 1], s=30, color=color, edgecolor="white", linewidth=.45, label=level)
        ax.legend(title=group_col, bbox_to_anchor=(1.02, 1), loc="upper left")
    else:
        ax.scatter(coords[:, 0], coords[:, 1], s=30, color=PALETTE[1], edgecolor="white", linewidth=.45)
    pc1 = f"PC1 ({variance[0] * 100:.1f}%)" if variance.size else "PC1"
    pc2 = f"PC2 ({variance[1] * 100:.1f}%)" if variance.size > 1 else "PC2"
    ax.set(xlabel=pc1, ylabel=pc2, title="Principal component analysis")
    save_figure(adata, fig, "pca", category="expression", output_dir=output_dir, parameters={"group_col": group_col}, source="adata.obsm['X_pca']")
    return adata


@register_function(aliases=["plot_sample_correlation", "correlation_heatmap", "样本相关性热图"], category="plot", description="Plot sample correlation heatmap from an expression layer, with optional group annotations.", examples=["sa.plot.sample_correlation(adata, group_col='condition')"], produces={"uns": ["plots"]})
def sample_correlation(adata: AnnData, *, layer: Optional[str] = None, group_col: Optional[str] = None, output_dir: str = "results/plots") -> AnnData:
    """Render a clustered sample correlation matrix."""
    require_group(adata, group_col)
    matrix, layer_name = resolve_matrix(adata, layer)
    corr = pd.DataFrame(matrix, index=adata.obs_names).T.corr()
    apply_style()
    sample_colors, _ = group_palette(adata, group_col)
    grid = sns.clustermap(corr, cmap="vlag", vmin=-1, vmax=1, center=0, row_colors=sample_colors, col_colors=sample_colors,
                          figsize=(6.2, 5.7), linewidths=.1, cbar_kws={"label": "Pearson r"})
    grid.ax_heatmap.set_xlabel("")
    grid.ax_heatmap.set_ylabel("")
    save_figure(adata, grid.fig, "sample_correlation", category="expression", output_dir=output_dir, parameters={"layer": layer_name, "group_col": group_col}, source=f"adata.layers[{layer_name!r}]")
    return adata


@register_function(aliases=["plot_rna_composition", "rna_composition", "RNA组成图"], category="plot", description="Plot per-sample composition by adata.var['rna_type'] from the selected count layer.", examples=["sa.plot.rna_composition(adata)"], produces={"uns": ["plots"]})
def rna_composition(adata: AnnData, *, layer: Optional[str] = "counts", output_dir: str = "results/plots") -> AnnData:
    """Render per-sample composition of annotated RNA types."""
    if "rna_type" not in adata.var.columns:
        raise KeyError("adata.var['rna_type'] is required for RNA composition plotting")
    matrix, layer_name = resolve_matrix(adata, layer)
    types = adata.var["rna_type"].fillna("unannotated").astype(str)
    frame = pd.DataFrame(matrix, index=adata.obs_names, columns=types).T.groupby(level=0).sum().T
    fractions = frame.div(frame.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    apply_style()
    fig, ax = plt.subplots(figsize=(max(5.5, len(fractions) * .38), 3.8))
    bottom = np.zeros(len(fractions))
    for i, kind in enumerate(fractions.columns):
        ax.bar(fractions.index.astype(str), fractions[kind], bottom=bottom, color=PALETTE[i % len(PALETTE)], width=.78, label=kind)
        bottom += fractions[kind].to_numpy()
    ax.set(ylabel="Fraction of reads", ylim=(0, 1), title="Small-RNA composition")
    ax.tick_params(axis="x", rotation=55)
    ax.legend(bbox_to_anchor=(1.02, 1), loc="upper left", title="RNA type")
    save_figure(adata, fig, "rna_composition", category="expression", output_dir=output_dir, parameters={"layer": layer_name}, source="adata.var['rna_type']")
    return adata


__all__ = ["pca", "sample_correlation", "rna_composition"]
