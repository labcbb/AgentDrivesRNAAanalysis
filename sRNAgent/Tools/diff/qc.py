"""QC filtering and dimensionality reduction for miRNA expression data.

Provides two AnnData-centric tools:
- ``filter_low_expression`` — remove features with mean raw count ≤ 1
- ``pca_logcpm`` — PCA on logcpm-normalised expression via scanpy
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
from anndata import AnnData
from sklearn.decomposition import PCA

from ..._registry import register_function


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@register_function(
    aliases=[
        "filter_low_expression", "filter_low_counts",
        "filter_expression", "低表达过滤",
    ],
    category="diff",
    description=(
        "Filter out features (miRNAs) whose mean raw count across all "
        "samples is ≤ 1. Uses ``adata.X`` (or ``adata.layers['counts']`` "
        "if present) as the raw count source. The filter is applied "
        "consistently to ``adata.X``, all ``adata.layers``, and "
        "``adata.var`` — no layer mismatch is introduced.\n\n"
        "After filtering, a summary of how many features were removed "
        "and how many passed is printed. The full raw count matrix (before "
        "filtering) is backed up in ``adata.uns['raw_counts']``."
    ),
    examples=[
        'adata = sa.diff.filter_low_expression(adata)',
        'adata = sa.diff.filter_low_expression(adata, min_mean=1)',
    ],
    related=[
        "diff.pca_logcpm",
    ],
    produces={
        "var": [],
        "layers": [],
        "uns": ["raw_counts"],
    },
)
def filter_low_expression(
    adata: AnnData,
    min_mean: float = 1.0,
) -> AnnData:
    """Filter features with mean raw count ≤ *min_mean*.

    Parameters
    ----------
    adata
        AnnData object with raw counts in ``adata.X`` (or
        ``adata.layers['counts']``).
    min_mean
        Minimum mean expression threshold. Features with mean count
        across all samples ≤ *min_mean* are removed. Default 1.0.

    Returns
    -------
    AnnData
        The input ``adata`` subsetted in-place to retain only features
        with mean count > *min_mean*. The full pre-filtering count matrix
        is saved in ``adata.uns['raw_counts']``.
    """
    # Prefer layers["counts"] as the raw count source; fall back to X
    if "counts" in adata.layers:
        counts = adata.layers["counts"]
    else:
        counts = adata.X

    if counts is None:
        raise ValueError(
            "adata.X is None and no 'counts' layer found — "
            "cannot compute mean expression."
        )

    # Backup full raw count matrix before filtering (stored in uns,
    # not affected by _inplace_subset_var)
    raw = np.asarray(counts, dtype=np.float64)
    if hasattr(raw, "toarray"):
        raw = raw.toarray()
    adata.uns["raw_counts"] = raw

    # Compute per-feature mean
    mean_expr = np.asarray(counts.mean(axis=0)).ravel()
    mask = mean_expr > min_mean

    n_before = adata.n_vars
    n_keep = int(mask.sum())
    n_removed = n_before - n_keep

    print(
        f"[filter_low_expression] min_mean={min_mean}: "
        f"{n_keep}/{n_before} features kept "
        f"({n_removed} removed)",
        flush=True,
    )

    # Subset in-place — this consistently filters X, all layers, and var
    adata._inplace_subset_var(mask)

    return adata


@register_function(
    aliases=[
        "pca_logcpm", "pca_expression", "pca_on_logcpm",
        "logcpm主成分分析",
    ],
    category="diff",
    description=(
        "Run PCA on the logcpm-normalised expression layer "
        "(``adata.layers['logcpm']``) using sklearn's PCA. "
        "Results are stored in ``adata.obsm['X_pca']`` (cell coordinates), "
        "``adata.uns['pca']`` (explained variance ratio, etc.), and "
        "``adata.varm['PCs']`` (loadings).\n\n"
        "The logcpm layer must already exist — run "
        "``sa.quant.normalize_cpm(adata)`` or ``quantify_mirna`` first."
    ),
    examples=[
        'adata = sa.diff.pca_logcpm(adata)',
        'adata = sa.diff.pca_logcpm(adata, n_comps=30)',
        (
            'sa.quant.normalize_cpm(adata)\n'
            'adata = sa.diff.pca_logcpm(adata)'
        ),
    ],
    related=[
        "quant.normalize_cpm", "diff.filter_low_expression",
    ],
    produces={
        "obsm": ["X_pca"],
        "uns": ["pca"],
        "varm": ["PCs"],
    },
)
def pca_logcpm(
    adata: AnnData,
    n_comps: int = 50,
    svd_solver: str = "arpack",
    random_state: Optional[int] = 0,
) -> AnnData:
    """Run PCA on the logcpm-normalised expression layer.

    Parameters
    ----------
    adata
        AnnData object with ``adata.layers['logcpm']`` populated.
    n_comps
        Number of principal components to compute. Default 50.
    svd_solver
        SVD solver passed to ``sklearn.decomposition.PCA``.
        Default ``"arpack"``.
    random_state
        Random state for reproducibility. Default 0.
        Pass ``None`` for non-deterministic behaviour.

    Returns
    -------
    AnnData
        The input ``adata`` with PCA results stored in ``.obsm``,
        ``.uns``, and ``.varm``.
    """
    if "logcpm" not in adata.layers:
        raise KeyError(
            "adata.layers['logcpm'] not found. "
            "Run sa.quant.normalize_cpm(adata) first."
        )

    mat = adata.layers["logcpm"]
    # Ensure 2D dense array
    if hasattr(mat, "toarray"):
        mat = mat.toarray()
    mat = np.asarray(mat, dtype=np.float64)

    pca = PCA(n_components=n_comps, svd_solver=svd_solver, random_state=random_state)
    pca_result = pca.fit_transform(mat)

    adata.obsm["X_pca"] = pca_result
    adata.uns["pca"] = {
        "variance": pca.explained_variance_,
        "variance_ratio": pca.explained_variance_ratio_,
        "total_variance": pca.explained_variance_.sum(),
    }
    # Loadings (features × PCs)
    adata.varm["PCs"] = pca.components_.T

    top5_ratio = pca.explained_variance_ratio_[:5].sum()
    print(
        f"[pca_logcpm] PCA done: {n_comps} components, "
        f"top 5 explain {top5_ratio*100:.1f}% variance",
        flush=True,
    )

    return adata
