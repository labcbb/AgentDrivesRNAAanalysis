"""QC and alignment visualisations from recorded sample-level AnnData metadata."""

from __future__ import annotations

from typing import Optional, Sequence

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from anndata import AnnData

from ..._registry import register_function
from .common import PALETTE, apply_style, group_palette, require_group, save_figure


@register_function(aliases=["plot_qc_metrics", "qc_plot", "QC绘图"], category="plot", description="Plot available cutadapt, MultiQC, and Bowtie sample-level metrics, optionally grouped by an AnnData obs column.", examples=["sa.plot.qc_metrics(adata, group_col='group')"], produces={"uns": ["plots"]})
def qc_metrics(adata: AnnData, metrics: Optional[Sequence[str]] = None, *, group_col: Optional[str] = None, output_dir: str = "results/plots") -> AnnData:
    """Render available per-sample QC metrics with optional group comparisons."""
    require_group(adata, group_col)
    default = ["cutadapt_trim_rate", "multiqc_total_seqs", "multiqc_pct_gc", "multiqc_pct_dups", "bowtie_alignment_rate"]
    columns = [col for col in (list(metrics) if metrics else default) if col in adata.obs.columns and pd.api.types.is_numeric_dtype(adata.obs[col])]
    if not columns:
        raise KeyError("No requested numeric QC metrics are present in adata.obs")
    apply_style()
    fig, axes = plt.subplots(1, len(columns), figsize=(max(3.0 * len(columns), 4), 3.3), squeeze=False)
    palette = group_palette(adata, group_col)[1]
    for ax, column in zip(axes.flat, columns):
        frame = adata.obs[[column] + ([group_col] if group_col else [])].dropna()
        if group_col:
            sns.boxplot(data=frame, x=group_col, y=column, hue=group_col, palette=palette, legend=False, width=.55, fliersize=0, ax=ax)
            sns.stripplot(data=frame, x=group_col, y=column, color="#3D3D3D", size=3, alpha=.75, jitter=.16, ax=ax)
            ax.tick_params(axis="x", rotation=35)
        else:
            sns.stripplot(data=frame, y=column, color=PALETTE[1], size=4, alpha=.8, ax=ax)
            ax.set_xticks([])
        ax.set_title(column.replace("_", " "))
    save_figure(adata, fig, "qc_metrics", category="qc", output_dir=output_dir, parameters={"metrics": columns, "group_col": group_col}, source="adata.obs")
    return adata


@register_function(aliases=["plot_alignment", "alignment_plot", "比对率绘图"], category="plot", description="Plot Bowtie aligned and unaligned reads plus alignment rate from adata.obs.", examples=["sa.plot.alignment_summary(adata, group_col='condition')"], produces={"uns": ["plots"]})
def alignment_summary(adata: AnnData, *, group_col: Optional[str] = None, output_dir: str = "results/plots") -> AnnData:
    """Render Bowtie alignment rate by sample or declared group."""
    require_group(adata, group_col)
    rate = "bowtie_alignment_rate"
    if rate not in adata.obs.columns:
        raise KeyError("adata.obs['bowtie_alignment_rate'] is required; run alignment first")
    apply_style()
    fig, ax = plt.subplots(figsize=(5.6, 3.6))
    frame = adata.obs[[rate] + ([group_col] if group_col else [])].copy()
    if group_col:
        colors = group_palette(adata, group_col)[1]
        sns.boxplot(data=frame, x=group_col, y=rate, hue=group_col, palette=colors, legend=False, fliersize=0, width=.55, ax=ax)
        sns.stripplot(data=frame, x=group_col, y=rate, color="#3D3D3D", jitter=.15, size=3, ax=ax)
    else:
        ax.bar(adata.obs_names.astype(str), pd.to_numeric(frame[rate], errors="coerce"), color=PALETTE[3], edgecolor="#4D4D4D", linewidth=.4)
        ax.tick_params(axis="x", rotation=55)
    ax.set_ylabel("Alignment rate (%)")
    ax.set_title("Read alignment")
    save_figure(adata, fig, "alignment_summary", category="alignment", output_dir=output_dir, parameters={"group_col": group_col}, source="adata.obs['bowtie_alignment_rate']")
    return adata


__all__ = ["qc_metrics", "alignment_summary"]
