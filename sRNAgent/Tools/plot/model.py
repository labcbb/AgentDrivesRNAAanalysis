"""Model-result plots for classification and Cox proportional-hazards analysis."""

from __future__ import annotations

from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from anndata import AnnData

from ..._registry import register_function
from .common import PALETTE, apply_style, save_figure


@register_function(aliases=["plot_classification_performance", "classification_plot", "分类模型性能图"], category="plot", description="Plot stored classification holdout/cross-validation performance metrics for each evaluated model.", examples=["sa.plot.classification_performance(adata)"], requires={"uns": ["classification"]}, produces={"uns": ["plots"]})
def classification_performance(adata: AnnData, *, metric: str = "roc_auc", output_dir: str = "results/plots") -> AnnData:
    """Render stored classification performance by model and evaluation set."""
    state = adata.uns.get("classification") or {}
    table = state.get("performance") if isinstance(state, dict) else None
    if not isinstance(table, pd.DataFrame) or table.empty:
        raise KeyError("adata.uns['classification']['performance'] is required")
    if metric not in table.columns:
        available = [c for c in table.columns if c not in {"model", "evaluation"}]
        metric = "balanced_accuracy" if "balanced_accuracy" in available else available[0]
    frame = table.dropna(subset=[metric]).copy()
    apply_style()
    fig, ax = plt.subplots(figsize=(5.6, 3.7))
    models = frame["model"].drop_duplicates().tolist()
    evaluations = frame["evaluation"].drop_duplicates().tolist()
    width = .72 / max(len(evaluations), 1)
    x = np.arange(len(models))
    for i, evaluation in enumerate(evaluations):
        vals = frame[frame["evaluation"] == evaluation].set_index("model")[metric].reindex(models)
        ax.bar(x - .36 + width / 2 + i * width, vals, width=width, color=PALETTE[i % len(PALETTE)], label=evaluation, edgecolor="white", linewidth=.5)
    ax.set(xticks=x, xticklabels=models, ylabel=metric.replace("_", " "), title="Classification performance")
    ax.legend(title="Evaluation", bbox_to_anchor=(1.02, 1), loc="upper left")
    save_figure(adata, fig, "classification_performance", category="classification", output_dir=output_dir, parameters={"metric": metric}, source="adata.uns['classification']['performance']")
    return adata


@register_function(aliases=["plot_cox_forest", "cox_forest", "Cox森林图"], category="plot", description="Plot univariate or multivariate Cox hazard ratios and confidence intervals from stored Cox results.", examples=["sa.plot.cox_forest(adata, result='multivariate')"], requires={"uns": ["cox"]}, produces={"uns": ["plots"]})
def cox_forest(adata: AnnData, *, result: str = "multivariate", top_n: int = 25, output_dir: str = "results/plots") -> AnnData:
    """Render stored Cox hazard ratios and confidence intervals."""
    state = adata.uns.get("cox") or {}
    key = f"{str(result).lower()}_results"
    table = state.get(key) if isinstance(state, dict) else None
    if not isinstance(table, pd.DataFrame) or table.empty:
        raise KeyError(f"adata.uns['cox'][{key!r}] is required")
    frame = table.copy().dropna(subset=["hazard_ratio", "ci_lower", "ci_upper"])
    if "p_value" in frame:
        frame = frame.nsmallest(int(top_n), "p_value")
    else:
        frame = frame.head(int(top_n))
    frame = frame.iloc[::-1]
    apply_style()
    fig, ax = plt.subplots(figsize=(6.2, max(3.2, len(frame) * .31 + 1.35)))
    y = np.arange(len(frame))
    hr = frame["hazard_ratio"].to_numpy(float)
    low, high = frame["ci_lower"].to_numpy(float), frame["ci_upper"].to_numpy(float)
    ax.errorbar(hr, y, xerr=np.vstack([hr - low, high - hr]), fmt="o", color=PALETTE[1], ecolor="#6C6C6C", elinewidth=.8, capsize=2.2, ms=4.2)
    ax.axvline(1, color="#555555", lw=.75, ls="--")
    ax.set_xscale("log"); ax.set(yticks=y, yticklabels=frame.index.astype(str), xlabel="Hazard ratio (95% CI)", title=f"{result.title()} Cox model")
    save_figure(adata, fig, f"cox_{result}_forest", category="cox", output_dir=output_dir, parameters={"result": result, "top_n": top_n}, source=f"adata.uns['cox']['{key}']")
    return adata


@register_function(aliases=["plot_cox_cv", "cox_cindex", "C-index图"], category="plot", description="Plot fold-level C-index values from stored Cox cross-validation results.", examples=["sa.plot.cox_cross_validation(adata)"], requires={"uns": ["cox"]}, produces={"uns": ["plots"]})
def cox_cross_validation(adata: AnnData, *, output_dir: str = "results/plots") -> AnnData:
    """Render fold-level Cox C-index values."""
    state = adata.uns.get("cox") or {}
    table = state.get("cross_validation") if isinstance(state, dict) else None
    if not isinstance(table, pd.DataFrame) or "c_index" not in table.columns:
        raise KeyError("adata.uns['cox']['cross_validation'] with c_index is required")
    apply_style()
    fig, ax = plt.subplots(figsize=(5.0, 3.5))
    x = table["fold"].to_numpy() if "fold" in table else np.arange(1, len(table) + 1)
    ax.plot(x, table["c_index"], color=PALETTE[1], marker="o", ms=5, lw=1.4)
    ax.axhline(table["c_index"].mean(), color=PALETTE[0], ls="--", lw=.8, label=f"mean = {table['c_index'].mean():.3f}")
    ax.set(xlabel="Cross-validation fold", ylabel="C-index", title="Cox cross-validation")
    ax.legend(loc="best")
    save_figure(adata, fig, "cox_cross_validation", category="cox", output_dir=output_dir, parameters={}, source="adata.uns['cox']['cross_validation']")
    return adata


__all__ = ["classification_performance", "cox_forest", "cox_cross_validation"]
