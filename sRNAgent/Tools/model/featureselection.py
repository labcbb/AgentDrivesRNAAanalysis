"""Shared feature preparation and selection utilities for model tools.

This module keeps preprocessing identical between Cox and classification
models.  AnnData ``var_names`` are treated as numeric measurements, while
columns in ``adata.obs`` can be supplied as clinical covariates.  Clinical
categoricals are one-hot encoded with the first (sorted) level as the
reference, missing values are imputed, and unusable columns are removed
before any supervised selection is performed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence
import warnings

import numpy as np
import pandas as pd
from anndata import AnnData
from sklearn.feature_selection import f_classif, mutual_info_classif
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from ..._registry import register_function


FEATURE_SELECTION_UNS_KEY = "feature_selection"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _as_names(values: Optional[str | Sequence[str]]) -> list[str]:
    if values is None:
        return []
    raw = [values] if isinstance(values, str) else list(values)
    return list(dict.fromkeys(str(value).strip() for value in raw if str(value).strip()))


def _read_var_matrix(adata: AnnData, names: Sequence[str], layer: Optional[str]) -> tuple[pd.DataFrame, str]:
    missing = [name for name in names if name not in adata.var_names]
    if missing:
        raise KeyError(f"Features are absent from adata.var_names: {missing}")
    chosen = layer
    if chosen is None:
        for candidate in ("logcpm", "voom_E", "counts"):
            if candidate in adata.layers:
                chosen = candidate
                break
    if chosen is None or str(chosen).lower() in {"x", "adata.x"}:
        values, layer_name = adata.X, "X"
    else:
        if chosen not in adata.layers:
            raise KeyError(f"adata.layers[{chosen!r}] is missing. Available: {list(adata.layers.keys())}")
        values, layer_name = adata.layers[chosen], str(chosen)
    if hasattr(values, "toarray"):
        values = values.toarray()
    matrix = np.asarray(values, dtype=float)[:, adata.var_names.get_indexer(names)]
    if not np.isfinite(matrix).all():
        matrix = np.where(np.isfinite(matrix), matrix, np.nan)
    return pd.DataFrame(matrix, index=adata.obs_names, columns=list(names)), layer_name


def _encode_and_impute(
    frame: pd.DataFrame,
    *,
    missing_threshold: float,
    imputation: str,
    variance_threshold: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not 0 <= float(missing_threshold) <= 1:
        raise ValueError("missing_threshold must be between 0 and 1")
    if float(variance_threshold) < 0:
        raise ValueError("variance_threshold must be non-negative")
    missing_rate = frame.isna().mean()
    kept = missing_rate[missing_rate <= float(missing_threshold)].index.tolist()
    dropped_missing = [str(x) for x in frame.columns if x not in kept]
    frame = frame.loc[:, kept].copy()
    if frame.shape[1] == 0:
        raise ValueError("No features remain after missing-value filtering")

    numeric = [col for col in frame.columns if pd.api.types.is_numeric_dtype(frame[col]) and not pd.api.types.is_bool_dtype(frame[col])]
    categorical = [col for col in frame.columns if col not in numeric]
    imputation = str(imputation).lower().replace("-", "_")
    if imputation not in {"median", "mean", "most_frequent", "iterative"}:
        raise ValueError("imputation must be 'median', 'mean', 'most_frequent', or 'iterative'")

    if numeric:
        numeric_frame = frame[numeric].apply(pd.to_numeric, errors="coerce")
        if imputation == "iterative":
            from sklearn.experimental import enable_iterative_imputer  # noqa: F401
            from sklearn.impute import IterativeImputer
            numeric_values = IterativeImputer(random_state=0).fit_transform(numeric_frame)
        else:
            strategy = imputation if imputation in {"mean", "median", "most_frequent"} else "median"
            numeric_values = SimpleImputer(strategy=strategy).fit_transform(numeric_frame)
        numeric_out = pd.DataFrame(numeric_values, index=frame.index, columns=numeric)
    else:
        numeric_out = pd.DataFrame(index=frame.index)

    references: dict[str, str] = {}
    if categorical:
        cat_frame = frame[categorical].astype("object")
        cat_frame = pd.DataFrame(
            SimpleImputer(strategy="most_frequent").fit_transform(cat_frame),
            index=frame.index,
            columns=categorical,
        )
        categories = [sorted(cat_frame[col].astype(str).unique().tolist()) for col in categorical]
        for col, levels in zip(categorical, categories):
            references[str(col)] = levels[0]
        try:
            encoder = OneHotEncoder(categories=categories, drop="first", handle_unknown="ignore", sparse_output=False, dtype=float)
        except TypeError:  # sklearn < 1.2
            encoder = OneHotEncoder(categories=categories, drop="first", handle_unknown="ignore", sparse=False, dtype=float)
        cat_values = encoder.fit_transform(cat_frame.astype(str))
        cat_names = encoder.get_feature_names_out(categorical).tolist()
        categorical_out = pd.DataFrame(cat_values, index=frame.index, columns=cat_names)
    else:
        categorical_out = pd.DataFrame(index=frame.index)

    result = pd.concat([numeric_out, categorical_out], axis=1)
    variances = result.var(axis=0, ddof=0)
    keep_variance = variances > float(variance_threshold)
    dropped_variance = variances.index[~keep_variance].astype(str).tolist()
    result = result.loc[:, keep_variance]
    if result.shape[1] == 0:
        raise ValueError("No features remain after variance filtering")
    metadata = {
        "missing_rate": missing_rate.to_dict(),
        "dropped_missing": dropped_missing,
        "numeric_features": [str(x) for x in numeric],
        "categorical_features": [str(x) for x in categorical],
        "reference_levels": references,
        "variance": variances.to_dict(),
        "dropped_variance": dropped_variance,
        "imputation": imputation,
        "missing_threshold": float(missing_threshold),
        "variance_threshold": float(variance_threshold),
    }
    return result, metadata


def prepare_feature_matrix(
    adata: AnnData,
    features: Optional[str | Sequence[str]] = None,
    *,
    obs_features: Optional[str | Sequence[str]] = None,
    layer: Optional[str] = None,
    missing_threshold: float = 0.20,
    imputation: str = "median",
    variance_threshold: float = 0.0,
) -> tuple[np.ndarray, list[str], str, dict[str, Any]]:
    """Build a numeric model matrix from expression and clinical features."""
    var_names = _as_names(features)
    if features is None:
        var_names = [str(x) for x in adata.var_names]
    obs_names = _as_names(obs_features)
    missing_obs = [name for name in obs_names if name not in adata.obs.columns]
    if missing_obs:
        raise KeyError(f"Clinical features are absent from adata.obs: {missing_obs}")
    parts: list[pd.DataFrame] = []
    layer_name = "obs_only" if not var_names else "X"
    if var_names:
        expression, layer_name = _read_var_matrix(adata, var_names, layer)
        parts.append(expression)
    if obs_names:
        parts.append(adata.obs.loc[:, obs_names].copy())
    if not parts:
        raise ValueError("Provide expression features, obs_features, or both")
    raw = pd.concat(parts, axis=1)
    transformed, metadata = _encode_and_impute(raw, missing_threshold=missing_threshold, imputation=imputation, variance_threshold=variance_threshold)
    metadata.update({"input_features": var_names, "input_obs_features": obs_names, "output_features": transformed.columns.tolist()})
    return transformed.to_numpy(dtype=float), transformed.columns.tolist(), layer_name, metadata


def validate_survival_endpoint(
    adata: AnnData,
    time_col: str,
    event_col: str,
    *,
    n_features: Optional[int] = None,
    min_epv: float = 10.0,
    strict_epv: bool = False,
) -> dict[str, Any]:
    """Validate Cox endpoints and report event-per-variable adequacy."""
    for col in (time_col, event_col):
        if col not in adata.obs.columns:
            raise KeyError(f"{col!r} is not in adata.obs. Available: {list(adata.obs.columns)}")
    time = pd.to_numeric(adata.obs[time_col], errors="coerce")
    raw = adata.obs[event_col]
    if time.isna().any():
        raise ValueError(f"{time_col!r} contains missing or non-numeric values")
    invalid_time = int((time <= 0).sum())
    if invalid_time:
        raise ValueError(f"{time_col!r} contains {invalid_time} non-positive values; clean or filter them before Cox modeling")
    if pd.api.types.is_bool_dtype(raw):
        event = raw.astype(int)
    else:
        event = pd.to_numeric(raw, errors="coerce")
    if event.isna().any() or not set(event.astype(int).unique()).issubset({0, 1}) or not set(event.astype(int).unique()) == {0, 1}:
        raise ValueError(f"{event_col!r} must contain both binary values 0 and 1 (or False and True)")
    events = int(event.sum())
    variables = int(n_features or 0)
    epv = float(events / variables) if variables else None
    warning = None
    if epv is not None and epv < float(min_epv):
        warning = f"EPV={epv:.2f} is below the recommended minimum of {float(min_epv):g}"
        if strict_epv:
            raise ValueError(warning)
        warnings.warn(warning, UserWarning, stacklevel=2)
    return {"time_col": time_col, "event_col": event_col, "n_samples": int(len(time)), "n_events": events,
            "n_censored": int(len(event) - events), "n_features": variables, "epv": epv, "epv_warning": warning,
            "valid": True}


def clean_survival_data(adata: AnnData, time_col: str, event_col: str) -> tuple[AnnData, dict[str, Any]]:
    """Return a copy after dropping non-positive times and invalid events."""
    if time_col not in adata.obs.columns or event_col not in adata.obs.columns:
        raise KeyError("time_col and event_col must be present in adata.obs")
    time = pd.to_numeric(adata.obs[time_col], errors="coerce")
    event = adata.obs[event_col]
    if pd.api.types.is_bool_dtype(event):
        event = event.astype(int)
    else:
        event = pd.to_numeric(event, errors="coerce")
    keep = time.gt(0) & event.isin([0, 1])
    cleaned = adata[keep.to_numpy()].copy()
    report = {"removed_samples": int((~keep).sum()), "kept_samples": int(keep.sum()), "removed_obs": adata.obs_names[~keep].astype(str).tolist()}
    return cleaned, report


def select_features(
    matrix: np.ndarray,
    feature_names: Sequence[str],
    target: Sequence[Any],
    *,
    method: str = "variance",
    top_k: Optional[int] = None,
    variance_threshold: float = 0.0,
    de_results: Optional[pd.DataFrame] = None,
    de_pvalue: float = 0.05,
    random_state: int = 0,
) -> tuple[np.ndarray, list[str], pd.DataFrame]:
    """Select features for classification or a caller-provided DE table."""
    x = np.asarray(matrix, dtype=float)
    names = [str(x) for x in feature_names]
    if x.ndim != 2 or x.shape[1] != len(names):
        raise ValueError("matrix columns must match feature_names")
    method = str(method).lower().replace("-", "_")
    scores = np.var(x, axis=0)
    pvalues = np.full(x.shape[1], np.nan)
    if method in {"de", "differential", "differential_expression"}:
        if de_results is None:
            raise ValueError("de_results is required for method='de'")
        pcol = next((c for c in ("adj_p_value", "padj", "fdr", "p_value", "P.Value") if c in de_results.columns), None)
        if pcol is None:
            raise KeyError("de_results must contain adj_p_value, padj, fdr, or p_value")
        pvalues = pd.to_numeric(de_results.reindex(names)[pcol], errors="coerce").to_numpy()
        scores = -np.log10(np.clip(pvalues, 1e-300, 1))
        keep = np.isfinite(pvalues) & (pvalues <= float(de_pvalue))
    elif method in {"mutual_info", "mutual_information"}:
        scores = mutual_info_classif(x, np.asarray(target), random_state=random_state)
        keep = scores > 0
    elif method in {"f_classif", "anova", "univariate"}:
        scores, pvalues = f_classif(x, np.asarray(target))
        keep = np.isfinite(pvalues) & (pvalues <= float(de_pvalue))
    elif method in {"variance", "filter"}:
        keep = scores > float(variance_threshold)
    elif method in {"all", "none"}:
        keep = np.ones(x.shape[1], dtype=bool)
    else:
        raise ValueError("method must be variance, de, univariate, f_classif, mutual_info, or none")
    eligible = np.flatnonzero(keep)
    if top_k is not None:
        if int(top_k) < 1:
            raise ValueError("top_k must be positive")
        eligible = eligible[np.argsort(-np.nan_to_num(scores[eligible], nan=-np.inf))[: int(top_k)]]
    if eligible.size == 0:
        raise ValueError("Feature selection removed every feature")
    eligible = np.sort(eligible)
    table = pd.DataFrame({"feature": names, "score": scores, "p_value": pvalues, "selected": False}).set_index("feature")
    table.loc[[names[i] for i in eligible], "selected"] = True
    return x[:, eligible], [names[i] for i in eligible], table


@register_function(
    aliases=["feature_selection", "select_features", "特征选择", "特征预处理"],
    category="model",
    description="Prepare AnnData expression/clinical features, encode categoricals, impute missing values, filter low variance, and optionally select features.",
    examples=["adata = sa.model.feature_selection(adata, features, obs_features=['Stage'], method='de')"],
    produces={"uns": [FEATURE_SELECTION_UNS_KEY]},
)
def feature_selection(
    adata: AnnData,
    features: Optional[str | Sequence[str]] = None,
    *,
    obs_features: Optional[str | Sequence[str]] = None,
    layer: Optional[str] = None,
    method: str = "variance",
    top_k: Optional[int] = None,
    missing_threshold: float = 0.20,
    imputation: str = "median",
    variance_threshold: float = 0.0,
    target_col: Optional[str] = None,
    de_pvalue: float = 0.05,
) -> AnnData:
    """Run shared preparation/selection and record the matrix metadata."""
    matrix, names, layer_name, metadata = prepare_feature_matrix(
        adata, features, obs_features=obs_features, layer=layer, missing_threshold=missing_threshold,
        imputation=imputation, variance_threshold=variance_threshold,
    )
    normalised_method = str(method).lower().replace("-", "_")
    if normalised_method not in {"variance", "filter", "none", "all"}:
        if normalised_method not in {"de", "differential", "differential_expression"}:
            if target_col is None or target_col not in adata.obs.columns:
                raise KeyError("target_col in adata.obs is required for supervised feature selection")
            target = adata.obs[target_col]
        else:
            target = np.zeros(adata.n_obs, dtype=int)
        matrix, names, table = select_features(matrix, names, target, method=normalised_method, top_k=top_k, de_results=adata.uns.get("de_results"), de_pvalue=de_pvalue)
        metadata["selection_table"] = table
    adata.obsm["X_feature_selection"] = matrix
    adata.uns[FEATURE_SELECTION_UNS_KEY] = {"feature_names": names, "input_layer": layer_name, "metadata": metadata, "completed_at": _utc_now()}
    return adata


__all__ = ["feature_selection", "prepare_feature_matrix", "select_features", "validate_survival_endpoint", "clean_survival_data", "FEATURE_SELECTION_UNS_KEY"]
