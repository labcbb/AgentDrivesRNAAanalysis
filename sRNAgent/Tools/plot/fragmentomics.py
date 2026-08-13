"""Plots specific to the independent fragmentomics AnnData modality."""

from __future__ import annotations

from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from anndata import AnnData

from ..._registry import register_function
from .common import PALETTE, apply_style, group_palette, require_group, save_figure


def _fragment_matrix(adata: AnnData, feature_type: str) -> tuple[np.ndarray, list[str]]:
    if "type" not in adata.var.columns or "CPM" not in adata.layers:
        raise KeyError("Fragmentomics plotting needs adata.var['type'] and adata.layers['CPM']")
    mask = adata.var["type"].astype(str).to_numpy() == feature_type
    if not mask.any():
        raise ValueError(f"No {feature_type} features are present in this fragmentomics AnnData")
    values = np.asarray(adata.layers["CPM"], dtype=float)[:, mask]
    return values, adata.var.index[mask].astype(str).tolist()


@register_function(aliases=["plot_fragment_profile", "fragmentomics_plot", "片段组学绘图"], category="plot", description="Plot grouped fragmentomics feature profiles (FSD, FSC, RCD, EDM, or BPM) from the dedicated fragmentomics AnnData.", examples=["sa.plot.fragment_profile(fragmentomics_adata, feature_type='FSD', group_col='group')"], produces={"uns": ["plots"]})
def fragment_profile(adata: AnnData, *, feature_type: str = "FSD", group_col: Optional[str] = None, top_n: int = 30, output_dir: str = "results/plots") -> AnnData:
    """Render the dominant values for one fragmentomics feature family."""
    require_group(adata, group_col)
    feature_type = str(feature_type).upper()
    values, names = _fragment_matrix(adata, feature_type)
    totals = values.sum(axis=0)
    idx = np.argsort(-totals)[: int(top_n)]
    frame = pd.DataFrame(values[:, idx], index=adata.obs_names, columns=[names[i] for i in idx])
    apply_style()
    fig, ax = plt.subplots(figsize=(max(6, len(idx) * .28), 3.8))
    if group_col:
        levels = sorted(adata.obs[group_col].dropna().astype(str).unique())
        colors = group_palette(adata, group_col)[1]
        for level in levels:
            mask = adata.obs[group_col].astype(str).to_numpy() == level
            ax.plot(frame.columns, frame.iloc[mask].mean(axis=0), marker="o", ms=2.5, lw=1.4, color=colors[level], label=level)
        ax.legend(title=group_col, bbox_to_anchor=(1.02, 1), loc="upper left")
    else:
        ax.plot(frame.columns, frame.mean(axis=0), marker="o", ms=2.5, lw=1.5, color=PALETTE[1])
    ax.set(title=f"{feature_type} profile", xlabel="Feature", ylabel="CPM")
    ax.tick_params(axis="x", rotation=60)
    save_figure(adata, fig, f"fragment_{feature_type.lower()}_profile", category="fragmentomics", output_dir=output_dir, parameters={"feature_type": feature_type, "group_col": group_col, "top_n": top_n}, source="adata.layers['CPM']")
    return adata


@register_function(aliases=["plot_fragment_heatmap", "fragmentomics_heatmap", "片段组学热图"], category="plot", description="Plot a selected fragmentomics feature-type heatmap with optional sample-group annotations.", examples=["sa.plot.fragment_heatmap(fragmentomics_adata, feature_type='BPM_START', group_col='group')"], produces={"uns": ["plots"]})
def fragment_heatmap(adata: AnnData, *, feature_type: str = "FSD", group_col: Optional[str] = None, top_n: int = 40, output_dir: str = "results/plots") -> AnnData:
    """Render a clustered heatmap for one fragmentomics feature family."""
    require_group(adata, group_col)
    values, names = _fragment_matrix(adata, str(feature_type).upper())
    idx = np.argsort(-values.sum(axis=0))[: int(top_n)]
    data = np.log1p(values[:, idx]).T
    data = (data - data.mean(axis=1, keepdims=True)) / np.where(data.std(axis=1, keepdims=True) == 0, 1, data.std(axis=1, keepdims=True))
    colors = group_palette(adata, group_col)[0] if group_col else None
    apply_style()
    grid = sns.clustermap(pd.DataFrame(data, index=[names[i] for i in idx], columns=adata.obs_names), cmap="vlag", center=0, col_colors=colors, figsize=(7, max(4, len(idx) * .14 + 2)), xticklabels=False)
    grid.ax_heatmap.set_xlabel("Samples")
    save_figure(adata, grid.fig, f"fragment_{str(feature_type).lower()}_heatmap", category="fragmentomics", output_dir=output_dir, parameters={"feature_type": feature_type, "group_col": group_col, "top_n": top_n}, source="adata.layers['CPM']")
    return adata


__all__ = ["fragment_profile", "fragment_heatmap"]
