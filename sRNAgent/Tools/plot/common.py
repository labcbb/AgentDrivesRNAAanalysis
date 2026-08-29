"""Shared publication-style plotting helpers and AnnData plot provenance."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from anndata import AnnData


PLOT_UNS_KEY = "plots"
PALETTE = ("#A1A0A5", "#db7094", "#e79db6", "#becfe9", "#a3c0c8", "#c5d2b8", "#8babd2", "#ff9fa0", "#ffc080")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def apply_style() -> None:
    """Apply one restrained, journal-ready visual language to all plot tools."""
    plt.rcParams.update({
        "font.family": "sans-serif", "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans", "sans-serif"],
        "font.size": 8, "axes.labelsize": 9, "axes.titlesize": 10, "xtick.labelsize": 7,
        "ytick.labelsize": 7, "legend.fontsize": 7, "axes.linewidth": 0.8,
        "axes.spines.top": False, "axes.spines.right": False, "axes.grid": False,
        "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none", "figure.dpi": 140,
    })


def group_palette(adata: AnnData, group_col: Optional[str]) -> tuple[Optional[list[str]], dict[str, str]]:
    if group_col is None:
        return None, {}
    if group_col not in adata.obs.columns:
        raise KeyError(f"group_col {group_col!r} is absent from adata.obs")
    levels = sorted(adata.obs[group_col].dropna().astype(str).unique().tolist())
    colors = {level: PALETTE[i % len(PALETTE)] for i, level in enumerate(levels)}
    sample_colors = [colors.get(str(value), "#D9D9D9") for value in adata.obs[group_col]]
    return sample_colors, colors


def resolve_matrix(adata: AnnData, layer: Optional[str] = None) -> tuple[np.ndarray, str]:
    chosen = layer
    if chosen is None:
        for candidate in ("logcpm", "voom_E", "CPM", "counts"):
            if candidate in adata.layers:
                chosen = candidate
                break
    values = adata.X if chosen is None or str(chosen).lower() in {"x", "adata.x"} else adata.layers[chosen]
    if hasattr(values, "toarray"):
        values = values.toarray()
    return np.asarray(values, dtype=float), "X" if chosen is None else str(chosen)


def save_figure(adata: AnnData, fig: plt.Figure, name: str, *, category: str, output_dir: str = "results/plots",
                parameters: Optional[dict[str, Any]] = None, source: Optional[str] = None) -> dict[str, Any]:
    """Save vector and raster versions and register the exact plotting provenance."""
    out_dir = Path(output_dir).expanduser().resolve() / category
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / name
    fig.savefig(stem.with_suffix(".png"), dpi=350, bbox_inches="tight", facecolor="white")
    fig.savefig(stem.with_suffix(".pdf"), bbox_inches="tight", facecolor="white")
    fig.savefig(stem.with_suffix(".svg"), bbox_inches="tight", facecolor="white")
    plt.close(fig)
    record = {"category": category, "path_png": str(stem.with_suffix(".png")), "path_pdf": str(stem.with_suffix(".pdf")),
              "path_svg": str(stem.with_suffix(".svg")), "source": source or "", "parameters": parameters or {}, "created_at": utc_now()}
    plots = dict(adata.uns.get(PLOT_UNS_KEY) or {})
    plots[name] = record
    adata.uns[PLOT_UNS_KEY] = plots
    return record


def require_group(adata: AnnData, group_col: Optional[str]) -> None:
    if group_col is not None and group_col not in adata.obs.columns:
        raise KeyError(f"group_col {group_col!r} is absent from adata.obs; available: {list(adata.obs.columns)}")


__all__ = ["PLOT_UNS_KEY", "PALETTE", "apply_style", "group_palette", "resolve_matrix", "save_figure", "require_group"]
