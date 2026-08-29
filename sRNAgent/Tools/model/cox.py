"""AnnData-backed Cox proportional-hazards survival analysis.

The public :func:`cox` tool supports univariate and multivariate Cox models,
optional penalised (Lasso, Ridge, or ElasticNet) feature selection followed by
an unpenalised Cox refit, and event-stratified cross-validation.  Results and
the complete request are written to ``adata.uns['cox']``; fitted artefacts are
also saved as joblib files when ``output_dir`` is supplied.

``statsmodels`` is used for the inferential Cox PH fit.  Penalised selection is
implemented against the Breslow partial likelihood with ``scipy.optimize`` so
that the tool does not require an additional survival-analysis package.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import joblib
import numpy as np
import pandas as pd
from anndata import AnnData
from scipy.optimize import minimize
from scipy.special import logsumexp
from sklearn.model_selection import StratifiedKFold
from sklearn.preprocessing import StandardScaler
from statsmodels.duration.hazard_regression import PHReg

from ..._registry import register_function
from .featureselection import prepare_feature_matrix, validate_survival_endpoint


COX_UNS_KEY = "cox"
_SELECTIONS = {None, "none", "lasso", "ridge", "elasticnet", "elastic_net"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _features(adata: AnnData, features: Optional[str | Sequence[str]]) -> list[str]:
    if features is None:
        result = [str(x) for x in adata.var_names]
    elif isinstance(features, str):
        result = [features]
    else:
        result = [str(x) for x in features]
    result = list(dict.fromkeys(x.strip() for x in result if x.strip()))
    if not result:
        raise ValueError("features must contain at least one AnnData feature name")
    missing = [x for x in result if x not in adata.var_names]
    if missing:
        raise KeyError(f"Features are absent from adata.var_names: {missing}")
    return result


def _matrix(adata: AnnData, names: Sequence[str], layer: Optional[str]) -> tuple[np.ndarray, str]:
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
    if values is None:
        raise ValueError(f"AnnData {layer_name} matrix is empty")
    if hasattr(values, "toarray"):
        values = values.toarray()
    indices = adata.var_names.get_indexer(names)
    matrix = np.asarray(values, dtype=float)[:, indices]
    if not np.isfinite(matrix).all():
        raise ValueError("Selected expression matrix contains NaN or infinite values")
    return matrix, layer_name


def _survival(adata: AnnData, time_col: str, event_col: str) -> tuple[np.ndarray, np.ndarray, str]:
    for col in (time_col, event_col):
        if col not in adata.obs.columns:
            raise KeyError(f"{col!r} is not in adata.obs. Available: {list(adata.obs.columns)}")
    time = pd.to_numeric(adata.obs[time_col], errors="coerce").to_numpy(dtype=float)
    event_raw = pd.to_numeric(adata.obs[event_col], errors="coerce")
    if event_raw.isna().any() or not np.isfinite(time).all():
        raise ValueError("Survival time and event columns must be numeric and non-missing")
    if (time <= 0).any():
        raise ValueError("Survival times must be strictly positive")
    unique = set(np.unique(event_raw.to_numpy(dtype=float)))
    if not unique.issubset({0.0, 1.0}) or len(unique) < 2:
        raise ValueError("event_col must contain both 0 (censored) and 1 (event)")
    return time, event_raw.to_numpy(dtype=int), f"obs[{time_col!r}], obs[{event_col!r}]"


def _fit_phreg(x: np.ndarray, time: np.ndarray, event: np.ndarray, names: Sequence[str]) -> tuple[Any, pd.DataFrame]:
    if x.shape[1] == 0:
        raise ValueError("Cox model requires at least one feature")
    if np.linalg.matrix_rank(x) < x.shape[1]:
        # PHReg can fail or produce unstable estimates for exact collinearity.
        raise ValueError("Cox design matrix is rank deficient; remove duplicated or collinear features")
    try:
        fitted = PHReg(time, x, status=event, ties="breslow").fit(disp=0)
    except Exception as exc:
        raise RuntimeError(f"Cox proportional-hazards fit failed: {exc}") from exc
    params = np.asarray(fitted.params, dtype=float)
    se = np.asarray(fitted.bse, dtype=float)
    pvals = np.asarray(fitted.pvalues, dtype=float)
    ci = np.asarray(fitted.conf_int(), dtype=float)
    table = pd.DataFrame({
        "feature": list(names),
        "coef": params,
        "hazard_ratio": np.exp(params),
        "std_err": se,
        "p_value": pvals,
        "ci_lower": np.exp(ci[:, 0]),
        "ci_upper": np.exp(ci[:, 1]),
    }).set_index("feature")
    return fitted, table


def _penalised_objective(beta: np.ndarray, x: np.ndarray, time: np.ndarray, event: np.ndarray,
                         alpha: float, l1_ratio: float) -> float:
    order = np.argsort(time)
    xs, ts, es = x[order], time[order], event[order]
    eta = xs @ beta
    loglik = 0.0
    for i in range(len(ts)):
        if es[i]:
            loglik += eta[i] - logsumexp(eta[i:])
    penalty = alpha * (l1_ratio * np.sqrt(beta * beta + 1e-8).sum() + (1.0 - l1_ratio) * 0.5 * (beta * beta).sum())
    return float(-loglik + penalty)


def _select(x: np.ndarray, time: np.ndarray, event: np.ndarray, names: Sequence[str], method: str,
            alpha: float, l1_ratio: float, threshold: float, max_features: Optional[int]) -> tuple[list[str], pd.DataFrame]:
    scaler = StandardScaler()
    scaled = scaler.fit_transform(x)
    result = minimize(_penalised_objective, np.zeros(scaled.shape[1]), args=(scaled, time, event, alpha, l1_ratio), method="L-BFGS-B")
    if not result.success:
        raise RuntimeError(f"Penalised Cox feature selection failed: {result.message}")
    coef = np.asarray(result.x, dtype=float)
    selected = np.flatnonzero(np.abs(coef) >= float(threshold))
    if max_features is not None:
        if int(max_features) < 1:
            raise ValueError("max_features must be positive")
        selected = np.argsort(-np.abs(coef))[: int(max_features)]
    if selected.size == 0:
        selected = np.array([int(np.argmax(np.abs(coef)))])
    selected = np.sort(selected)
    coef_table = pd.DataFrame({"feature": list(names), "standardized_coef": coef, "selected": False}).set_index("feature")
    coef_table.loc[[names[i] for i in selected], "selected"] = True
    return [names[i] for i in selected], coef_table


def _cindex(time: np.ndarray, event: np.ndarray, risk: np.ndarray) -> float:
    concordant = permissible = ties = 0.0
    for i in range(len(time)):
        for j in range(i + 1, len(time)):
            if event[i] and time[i] < time[j]:
                permissible += 1
                delta = risk[i] - risk[j]
            elif event[j] and time[j] < time[i]:
                permissible += 1
                delta = risk[j] - risk[i]
            else:
                continue
            if delta > 0:
                concordant += 1
            elif delta == 0:
                ties += 1
    return float((concordant + 0.5 * ties) / permissible) if permissible else float("nan")


def _cross_validate(x: np.ndarray, time: np.ndarray, event: np.ndarray, names: Sequence[str], *, folds: int,
                    selection: Optional[str], alpha: float, l1_ratio: float, threshold: float,
                    max_features: Optional[int], random_state: int) -> pd.DataFrame:
    counts = pd.Series(event).value_counts()
    n_splits = min(int(folds), int(counts.min()))
    if n_splits < 2:
        raise ValueError("Cross-validation requires at least two censored and two event samples")
    rows = []
    splitter = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=random_state)
    for fold, (train, test) in enumerate(splitter.split(x, event), 1):
        fold_names = list(names)
        if selection:
            fold_names, _ = _select(x[train], time[train], event[train], names, selection, alpha, l1_ratio, threshold, max_features)
        indices = [names.index(name) for name in fold_names]
        fitted, _ = _fit_phreg(x[train][:, indices], time[train], event[train], fold_names)
        risk = x[test][:, indices] @ np.asarray(fitted.params)
        rows.append({"fold": fold, "c_index": _cindex(time[test], event[test], risk), "n_features": len(fold_names), "features": fold_names})
    return pd.DataFrame(rows)


@register_function(
    aliases=["cox", "coxph", "cox_model", "survival_cox", "单因素cox", "多因素cox", "生存cox"],
    category="model",
    description="Run validated univariate/multivariate Cox PH models on AnnData, with clinical categorical encoding, Lasso/Ridge/ElasticNet selection, and cross-validation.",
    examples=[
        "adata = sa.model.cox(adata, ['gene1', 'gene2'], 'os_time', 'os_event', analysis='both')",
        "adata = sa.model.cox(adata, features, 'time', 'event', selection='lasso', cross_validate=True)",
        "adata = sa.model.cox(adata, genes, 'time', 'event', obs_features=['Stage'], selection='elasticnet')",
    ],
    related=["model.classification"],
    produces={"uns": [COX_UNS_KEY]},
)
def cox(
    adata: AnnData,
    features: Optional[str | Sequence[str]],
    time_col: str,
    event_col: str,
    *,
    obs_features: Optional[str | Sequence[str]] = None,
    layer: Optional[str] = None,
    analysis: str = "multivariate",
    selection: Optional[str] = None,
    alpha: float = 0.1,
    l1_ratio: float = 0.5,
    selection_threshold: float = 1e-4,
    max_features: Optional[int] = None,
    missing_threshold: float = 0.20,
    imputation: str = "median",
    variance_threshold: float = 0.0,
    min_epv: float = 10.0,
    strict_epv: bool = False,
    cross_validate: bool = False,
    cv_folds: int = 5,
    output_dir: Optional[str] = "results/models/cox",
    random_state: int = 0,
    force: bool = False,
) -> AnnData:
    """Fit Cox models and write a reproducible summary into ``adata.uns``.

    ``analysis`` is ``'univariate'``, ``'multivariate'`` or ``'both'``.
    Penalised selection is applied before the multivariate refit and is
    performed inside each cross-validation training fold to avoid leakage.
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")
    analysis = str(analysis).lower().replace("-", "_")
    if analysis not in {"univariate", "multivariate", "both"}:
        raise ValueError("analysis must be 'univariate', 'multivariate', or 'both'")
    selection = None if selection is None else str(selection).lower().replace("-", "_")
    if selection not in _SELECTIONS:
        raise ValueError("selection must be one of None, 'lasso', 'ridge', or 'elasticnet'")
    if selection in {"none", "elastic_net"}:
        selection = None if selection == "none" else "elasticnet"
    if float(alpha) < 0 or not 0 <= float(l1_ratio) <= 1:
        raise ValueError("alpha must be non-negative and l1_ratio must be between 0 and 1")
    if selection == "lasso":
        l1_ratio = 1.0
    elif selection == "ridge":
        l1_ratio = 0.0
    if int(cv_folds) < 2:
        raise ValueError("cv_folds must be at least 2")

    names = _features(adata, features)
    x, names, layer_name, preprocessing = prepare_feature_matrix(
        adata, names, obs_features=obs_features, layer=layer,
        missing_threshold=missing_threshold, imputation=imputation,
        variance_threshold=variance_threshold,
    )
    time, event, survival_source = _survival(adata, time_col, event_col)
    endpoint_validation = validate_survival_endpoint(
        adata, time_col, event_col, n_features=len(names), min_epv=min_epv, strict_epv=strict_epv,
    )
    request = {"features": names, "time_col": time_col, "event_col": event_col, "layer": layer_name,
               "analysis": analysis, "selection": selection, "alpha": float(alpha), "l1_ratio": float(l1_ratio),
               "selection_threshold": float(selection_threshold), "max_features": max_features,
               "obs_features": list(obs_features) if obs_features is not None else [],
               "missing_threshold": float(missing_threshold), "imputation": imputation,
               "variance_threshold": float(variance_threshold), "min_epv": float(min_epv), "strict_epv": bool(strict_epv),
               "cross_validate": bool(cross_validate), "cv_folds": int(cv_folds), "random_state": int(random_state)}
    existing = adata.uns.get(COX_UNS_KEY)
    if isinstance(existing, Mapping) and not force and existing.get("request") == request:
        state = dict(existing)
        state["reused"] = True
        state["completed_at"] = _utc_now()
        adata.uns[COX_UNS_KEY] = state
        return adata

    out_dir = Path(output_dir).expanduser().resolve() if output_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {"request": request, "survival_source": survival_source, "input_layer": layer_name,
                             "preprocessing": preprocessing, "endpoint_validation": endpoint_validation, "reused": False}
    if analysis in {"univariate", "both"}:
        rows = []
        for i, name in enumerate(names):
            try:
                _, table = _fit_phreg(x[:, i:i + 1], time, event, [name])
                rows.append(table.reset_index())
            except (RuntimeError, ValueError):
                continue
        state["univariate_results"] = pd.concat(rows, ignore_index=True).set_index("feature") if rows else pd.DataFrame()

    selected_names = names
    if selection:
        selected_names, selection_table = _select(x, time, event, names, selection, float(alpha), float(l1_ratio), float(selection_threshold), max_features)
        state["selection_coefficients"] = selection_table
        state["selected_features"] = selected_names
    if analysis in {"multivariate", "both"}:
        indices = [names.index(name) for name in selected_names]
        fitted, table = _fit_phreg(x[:, indices], time, event, selected_names)
        state["multivariate_results"] = table
        state["selected_features"] = selected_names
        if out_dir:
            model_path = out_dir / "cox_model.joblib"
            joblib.dump({"result": fitted, "features": selected_names, "layer": layer_name, "time_col": time_col, "event_col": event_col}, model_path)
            state["model_path"] = str(model_path)
        if cross_validate:
            state["cross_validation"] = _cross_validate(x, time, event, names, folds=int(cv_folds), selection=selection,
                                                          alpha=float(alpha), l1_ratio=float(l1_ratio), threshold=float(selection_threshold),
                                                          max_features=max_features, random_state=int(random_state))
    elif cross_validate:
        raise ValueError("cross_validate requires analysis='multivariate' or 'both'")
    state["completed_at"] = _utc_now()
    adata.uns[COX_UNS_KEY] = state
    return adata


__all__ = ["cox", "COX_UNS_KEY"]
