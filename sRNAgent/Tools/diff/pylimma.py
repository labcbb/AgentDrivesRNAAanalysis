"""Limma-voom differential expression analysis via pylimma (AnnData mode).

Wraps the standard limma-voom pipeline:

    voom → lm_fit → contrasts_fit → e_bayes → top_table

Results for all features are stored in ``adata.uns["de_results"]``,
and comparison metadata in ``adata.uns["de_params"]``.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
from anndata import AnnData

import pylimma

from ..._registry import register_function


# Common column names that may store group information
_GROUP_COL_CANDIDATES = [
    "group", "Group", "condition", "Condition",
    "treatment", "Treatment", "sample_group",
]


def _detect_group_col(adata: AnnData, group_col: Optional[str] = None) -> str:
    """Auto-detect the group column in ``adata.obs``.

    If *group_col* is given, it is returned directly after validation.
    Otherwise the first matching column from ``_GROUP_COL_CANDIDATES``
    is used.

    Raises
    ------
    KeyError
        If no suitable column is found.
    """
    if group_col is not None:
        if group_col not in adata.obs.columns:
            raise KeyError(
                f"Specified group_col='{group_col}' not found in "
                f"adata.obs. Available: {list(adata.obs.columns)}"
            )
        return group_col

    for col in _GROUP_COL_CANDIDATES:
        if col in adata.obs.columns:
            return col

    raise KeyError(
        "Could not auto-detect a group column in adata.obs. "
        "Please specify `group_col` explicitly. "
        f"Candidates checked: {_GROUP_COL_CANDIDATES}"
    )


def _resolve_design_column(
    design_cols: list[str],
    group_name: str,
) -> str:
    """Find the design-matrix column that matches *group_name*.

    pylimma's ``model_matrix`` may produce column names like
    ``"groupCtrl"``, ``"group[Ctrl]"``, or just ``"Ctrl"`` depending
    on the formula and backend.  This function does a substring match
    so the caller does not need to guess the prefix/suffix convention.
    """
    for col in design_cols:
        # Exact match
        if col == group_name:
            return col
    for col in design_cols:
        # group_name is contained in col
        if group_name in col:
            return col
    for col in design_cols:
        # col is contained in group_name (unlikely but safe)
        if col in group_name:
            return col
    raise ValueError(
        f"Cannot find a design-matrix column matching group "
        f"'{group_name}'. Available columns: {design_cols}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@register_function(
    aliases=[
        "de_analysis", "differential_expression", "limma_voom",
        "差异分析", "pylimma_de",
    ],
    category="diff",
    description=(
        "Run limma-voom differential expression analysis on AnnData "
        "using pylimma.  Raw counts are read from "
        "``adata.layers['counts']`` (fallback: ``adata.X``).\n\n"
        "**Group column detection** — if ``group_col`` is not specified, "
        "the function auto-detects common column names "
        "``['group', 'Condition', 'treatment', …]`` in ``adata.obs``.\n\n"
        "**Control group** — if ``control_group`` is given, that group "
        "is used as the baseline; otherwise the first group "
        "(alphabetically) is treated as the control.\n\n"
        "**Multi-group** — when more than 2 groups exist, only the "
        "first two (alphabetically) are compared (a warning is printed).\n\n"
        "Results for **all** features are stored in "
        "``adata.uns['de_results']``, with comparison metadata in "
        "``adata.uns['de_params']``."
    ),
    examples=[
        'adata = sa.diff.de_analysis(adata)',
        'adata = sa.diff.de_analysis(adata, group_col="condition")',
        'adata = sa.diff.de_analysis(adata, control_group="Ctrl")',
    ],
    related=[
        "diff.filter_low_expression", "quant.normalize_cpm",
    ],
    produces={
        "uns": ["de_results", "de_params"],
        "layers": ["voom_E", "voom_weights"],
    },
)
def de_analysis(
    adata: AnnData,
    group_col: Optional[str] = None,
    control_group: Optional[str] = None,
) -> AnnData:
    """Differential expression analysis using limma-voom (pylimma).

    Parameters
    ----------
    adata
        AnnData object. Raw counts must be in ``adata.layers['counts']``
        (or ``adata.X`` as fallback).
    group_col
        Column name in ``adata.obs`` that holds group labels.
        Auto-detected if not given.
    control_group
        Group label to use as the baseline (control) in the contrast.
        If not given, the first group alphabetically is used.

    Returns
    -------
    AnnData
        The input ``adata`` with:
        - ``adata.uns["de_results"]`` — DataFrame with DE results for
          all features (columns: ``log_fc`` (log2 fold change, CPM-based),
          ``ave_expr``, ``t``, ``p_value``, ``adj_p_value``, ``b``).
        - ``adata.uns["de_params"]`` — dict with comparison metadata.
        - ``adata.layers["voom_E"]``, ``adata.layers["voom_weights"]``
          (from ``voom``).
    """
    # ── 1. Ensure raw counts ──
    if "counts" in adata.layers:
        counts_source = "layers['counts']"
        raw = np.asarray(adata.layers["counts"], dtype=np.float64)
    else:
        counts_source = "X"
        raw = np.asarray(adata.X, dtype=np.float64)
        if raw.ndim != 2:
            raise ValueError(
                f"adata.X has shape {raw.shape}, expected 2-D."
            )

    if hasattr(raw, "toarray"):
        raw = raw.toarray()

    # Write counts back to X (pylimma reads X for voom)
    adata.X = raw

    n_features = adata.n_vars
    print(
        f"[de_analysis] Raw counts from {counts_source}: "
        f"{adata.n_obs} samples × {n_features} features",
        flush=True,
    )

    # ── 2. Detect / validate group column ──
    col = _detect_group_col(adata, group_col)
    print(f"[de_analysis] Using group column: '{col}'", flush=True)

    unique_groups = sorted(
        str(g) for g in adata.obs[col].dropna().unique()
    )
    if len(unique_groups) < 2:
        raise ValueError(
            f"Group column '{col}' has {len(unique_groups)} unique "
            f"value(s); need at least 2 for DE analysis."
        )

    if len(unique_groups) > 2:
        print(
            f"[de_analysis] ⚠️  {len(unique_groups)} groups detected: "
            f"{unique_groups}. Only the first two will be compared "
            f"({unique_groups[0]} vs {unique_groups[1]}).",
            flush=True,
        )

    # ── 3. Determine treatment / control ──
    if control_group is not None:
        if control_group not in unique_groups:
            raise ValueError(
                f"control_group='{control_group}' not found in "
                f"group column '{col}'. Available: {unique_groups}"
            )
        group_control = control_group
        # Treatment = the first other group
        group_treatment = [g for g in unique_groups if g != control_group][0]
    else:
        group_control = unique_groups[0]
        group_treatment = unique_groups[1]

    print(
        f"[de_analysis] Contrast: {group_treatment} vs {group_control} "
        f"(treatment - control)",
        flush=True,
    )

    # ── 4. Design matrix ──
    design_raw = pylimma.model_matrix(f"~0+{col}", adata.obs)

    # Wrap in DataFrame with explicit column names
    # (model_matrix returns a plain ndarray — use group names directly)
    group_names = sorted(adata.obs[col].dropna().unique())
    design = pd.DataFrame(design_raw, columns=group_names, index=adata.obs_names)

    design_cols = list(design.columns)
    print(f"[de_analysis] Design columns: {design_cols}", flush=True)

    # ── 5. Resolve design column names for treatment and control ──
    col_treat = _resolve_design_column(design_cols, group_treatment)
    col_ctrl = _resolve_design_column(design_cols, group_control)

    contrast_formula = f"{col_treat} - {col_ctrl}"
    print(f"[de_analysis] Contrast formula: {contrast_formula}", flush=True)

    contrast_matrix = pylimma.make_contrasts(contrast_formula, levels=design)

    # ── 6. limma-voom pipeline ──
    pylimma.voom(adata, design=design.values)
    pylimma.lm_fit(
        adata,
        design=design.values,
        layer="voom_E",
        weights_layer="voom_weights",
    )
    pylimma.contrasts_fit(adata, contrasts=contrast_matrix.values)
    pylimma.e_bayes(adata)

    # ── 7. Extract results for ALL features ──
    de_results = pylimma.top_table(
        adata, coef=0, number=n_features, sort_by="p_value",
    )

    # ── 8. Store results ──
    adata.uns["de_results"] = de_results
    adata.uns["de_params"] = {
        "group_col": col,
        "groups": unique_groups,
        "treatment": group_treatment,
        "control": group_control,
        "contrast_formula": contrast_formula,
        "n_samples": adata.n_obs,
        "n_features": n_features,
    }

    print(
        f"[de_analysis] Done. Results stored in "
        f"adata.uns['de_results'] ({len(de_results)} features) "
        f"and adata.uns['de_params'].",
        flush=True,
    )

    return adata
