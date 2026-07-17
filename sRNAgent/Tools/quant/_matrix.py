"""Shared AnnData expression-matrix helpers for quantification tools."""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
from anndata import AnnData


def _infer_rna_type(var: pd.DataFrame) -> str:
    if "rna_type" in var.columns and len(set(var["rna_type"].astype(str))) == 1:
        return str(var["rna_type"].astype(str).iloc[0])
    if "mirna_id" in var.columns:
        return "miRNA"
    if "trna_id" in var.columns or "trax_feature_id" in var.columns:
        return "tRNA"
    if "reference_name" in var.columns:
        return "smallRNA"
    return "unknown"


def store_count_matrix(
    adata: AnnData,
    matrix,
    var: pd.DataFrame,
    *,
    rna_type: str,
    counts_layer: str = "counts",
) -> AnnData:
    """Store raw expression counts in one shared layer and append by RNA type.

    Existing features with the same ``rna_type`` are replaced. Features with a
    different ``rna_type`` are retained and the new features are appended.
    """
    counts = np.asarray(matrix, dtype=np.float64)
    if counts.ndim != 2:
        raise ValueError("matrix must be 2-dimensional")
    if counts.shape[0] != adata.n_obs:
        raise ValueError(
            f"matrix has {counts.shape[0]} rows, but adata has {adata.n_obs} observations"
        )
    if counts.shape[1] != var.shape[0]:
        raise ValueError(
            f"matrix has {counts.shape[1]} columns, but var has {var.shape[0]} rows"
        )

    new_var = var.copy()
    new_var["rna_type"] = rna_type

    if adata.n_vars == 0:
        combined_counts = counts
        combined_var = new_var
    else:
        old_counts = (
            np.asarray(adata.layers[counts_layer], dtype=np.float64)
            if counts_layer in adata.layers
            else np.asarray(adata.X, dtype=np.float64)
        )
        old_var = adata.var.copy()
        if "rna_type" not in old_var.columns:
            old_var["rna_type"] = _infer_rna_type(old_var)

        keep = old_var["rna_type"].astype(str).to_numpy() != rna_type
        kept_counts = old_counts[:, keep]
        kept_var = old_var.loc[keep].copy()
        combined_counts = np.concatenate([kept_counts, counts], axis=1)
        combined_var = pd.concat([kept_var, new_var], axis=0)

    result = AnnData(X=combined_counts, obs=adata.obs.copy(), var=combined_var)
    result.layers[counts_layer] = combined_counts.copy()
    result.uns.update(adata.uns)

    for key in adata.obsm.keys():
        result.obsm[key] = adata.obsm[key].copy()

    return result
