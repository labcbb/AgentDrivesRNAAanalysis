"""AnnData-backed supervised classification models."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import joblib
import numpy as np
import pandas as pd
from anndata import AnnData
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold, cross_val_predict, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.svm import SVC

from ..._registry import register_function
from .featureselection import prepare_feature_matrix, select_features


CLASSIFICATION_UNS_KEY = "classification"
_MODEL_ALIASES = {
    "svm": "svm",
    "support_vector_machine": "svm",
    "random_forest": "random_forest",
    "randomforest": "random_forest",
    "rf": "random_forest",
    "xgboost": "xgboost",
    "xgb": "xgboost",
    "logistic_regression": "logistic_regression",
    "logistic": "logistic_regression",
    "lr": "logistic_regression",
}
_ALL_MODELS = ("svm", "random_forest", "xgboost", "logistic_regression")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalise_features(features: str | Sequence[str]) -> List[str]:
    values = [features] if isinstance(features, str) else list(features)
    result = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    if not result:
        raise ValueError("features must contain at least one AnnData feature name")
    return result


def _normalise_models(model: Optional[str | Sequence[str]]) -> List[str]:
    if model is None:
        return list(_ALL_MODELS)
    raw = [model] if isinstance(model, str) else list(model)
    selected: List[str] = []
    for value in raw:
        name = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        if name == "all":
            selected.extend(_ALL_MODELS)
            continue
        resolved = _MODEL_ALIASES.get(name)
        if resolved is None:
            raise ValueError(f"Unsupported model {value!r}. Choose from {list(_ALL_MODELS)} or 'all'.")
        selected.append(resolved)
    return list(dict.fromkeys(selected))


def _resolve_groups(adata: AnnData, group_col: str | Sequence[Any]) -> tuple[np.ndarray, str]:
    if isinstance(group_col, str):
        if group_col not in adata.obs.columns:
            raise KeyError(f"group_col {group_col!r} is not in adata.obs. Available: {list(adata.obs.columns)}")
        values = adata.obs[group_col]
        source = group_col
    else:
        values = list(group_col)
        if len(values) != adata.n_obs:
            raise ValueError("A sequence passed as group_col must have one label per AnnData observation")
        source = "provided_labels"
    labels = pd.Series(values, index=adata.obs_names)
    if labels.isna().any() or (labels.astype(str).str.strip() == "").any():
        raise ValueError("Classification group labels cannot contain missing or empty values")
    result = labels.astype(str).to_numpy()
    if len(np.unique(result)) < 2:
        raise ValueError("Classification requires at least two groups")
    return result, source


def _resolve_matrix(adata: AnnData, feature_names: Sequence[str], layer: Optional[str]) -> tuple[np.ndarray, str]:
    missing = [name for name in feature_names if name not in adata.var_names]
    if missing:
        raise KeyError(f"Features are absent from adata.var_names: {missing}")
    if layer is None:
        for candidate in ("logcpm", "voom_E", "counts"):
            if candidate in adata.layers:
                layer = candidate
                break
    if layer is None or str(layer).lower() in {"x", "adata.x"}:
        values = adata.X
        layer_name = "X"
    else:
        if layer not in adata.layers:
            raise KeyError(f"adata.layers[{layer!r}] is missing. Available: {list(adata.layers.keys())}")
        values = adata.layers[layer]
        layer_name = str(layer)
    if values is None:
        raise ValueError(f"AnnData {layer_name} matrix is empty")
    if hasattr(values, "toarray"):
        values = values.toarray()
    indices = adata.var_names.get_indexer(feature_names)
    matrix = np.asarray(values, dtype=float)[:, indices]
    if matrix.shape != (adata.n_obs, len(feature_names)):
        raise ValueError("Feature matrix shape does not match AnnData observations and selected features")
    if np.isinf(matrix).any():
        raise ValueError("Feature matrix contains infinite values")
    return matrix, layer_name


def _classifier(model_name: str, n_classes: int, random_state: int) -> Any:
    if model_name == "svm":
        estimator = SVC(kernel="rbf", probability=True, class_weight="balanced", random_state=random_state)
    elif model_name == "random_forest":
        estimator = RandomForestClassifier(
            n_estimators=500,
            class_weight="balanced",
            n_jobs=-1,
            random_state=random_state,
        )
    elif model_name == "logistic_regression":
        estimator = LogisticRegression(
            max_iter=2000,
            class_weight="balanced",
            random_state=random_state,
        )
    elif model_name == "xgboost":
        try:
            from xgboost import XGBClassifier
        except ImportError as exc:
            raise ImportError("XGBoost is required for model='xgboost'. Install xgboost first.") from exc
        params: Dict[str, Any] = {
            "n_estimators": 200,
            "max_depth": 3,
            "learning_rate": 0.05,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "tree_method": "hist",
            "n_jobs": 1,
            "random_state": random_state,
            "eval_metric": "logloss" if n_classes == 2 else "mlogloss",
        }
        if n_classes == 2:
            params["objective"] = "binary:logistic"
        else:
            params.update({"objective": "multi:softprob", "num_class": n_classes})
        estimator = XGBClassifier(**params)
    else:  # pragma: no cover - _normalise_models prevents this
        raise ValueError(f"Unsupported model {model_name!r}")
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        ("classifier", estimator),
    ])


def _metrics(y_true: np.ndarray, predicted: np.ndarray, probabilities: Optional[np.ndarray], labels: np.ndarray) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "accuracy": float(accuracy_score(y_true, predicted)),
        "balanced_accuracy": float(balanced_accuracy_score(y_true, predicted)),
        "precision_weighted": float(precision_score(y_true, predicted, average="weighted", zero_division=0)),
        "recall_weighted": float(recall_score(y_true, predicted, average="weighted", zero_division=0)),
        "f1_weighted": float(f1_score(y_true, predicted, average="weighted", zero_division=0)),
        "confusion_matrix": confusion_matrix(y_true, predicted, labels=labels).astype(int).tolist(),
    }
    if probabilities is not None:
        try:
            if len(labels) == 2:
                result["roc_auc"] = float(roc_auc_score(y_true, probabilities[:, 1]))
            else:
                result["roc_auc_ovr_weighted"] = float(
                    roc_auc_score(y_true, probabilities, multi_class="ovr", average="weighted", labels=labels)
                )
        except ValueError:
            result["roc_auc"] = None
    return result


def _cross_validate(model: Any, matrix: np.ndarray, labels: np.ndarray, *, folds: int, random_state: int) -> Dict[str, Any]:
    smallest_group = int(pd.Series(labels).value_counts().min())
    if smallest_group < 2:
        raise ValueError("Cross-validation requires at least two samples in every class")
    n_splits = min(int(folds), smallest_group)
    if n_splits < 2:
        raise ValueError("cv_folds must allow at least two stratified folds")
    splitter = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=random_state)
    predicted = cross_val_predict(model, matrix, labels, cv=splitter, method="predict")
    probabilities = cross_val_predict(model, matrix, labels, cv=splitter, method="predict_proba")
    result = _metrics(labels, predicted, probabilities, np.unique(labels))
    result["folds"] = n_splits
    return result


def _performance_rows(details: Mapping[str, Mapping[str, Any]]) -> pd.DataFrame:
    rows = []
    for model_name, result in details.items():
        for evaluation, metrics in result.items():
            if not isinstance(metrics, Mapping):
                continue
            row = {"model": model_name, "evaluation": evaluation}
            row.update({key: value for key, value in metrics.items() if not isinstance(value, (list, dict))})
            rows.append(row)
    return pd.DataFrame(rows)


@register_function(
    aliases=[
        "classification", "classification_model", "classifier", "svm_classification",
        "random_forest_classification", "xgboost_classification", "分类模型", "分类建模",
    ],
    category="model",
    description=(
        "Train SVM, random forest, XGBoost, and/or logistic-regression classifiers from selected AnnData features "
        "and group labels. With model=None, evaluate all four models for comparison; a specified model evaluates only "
        "that model. Supports clinical covariates with categorical encoding, missing-value preprocessing, supervised "
        "feature selection, stratified train/test splitting, and stratified cross-validation. Performance, selected "
        "features, labels, and persisted final-model paths are stored in adata.uns['classification']."
    ),
    examples=[
        "adata = sa.model.classification(adata, ['hsa-miR-490-3p'], 'group')",
        "adata = sa.model.classification(adata, top5, 'group', split_data=True, cross_validate=True)",
        "adata = sa.model.classification(adata, top5, 'group', model='xgboost')",
        "adata = sa.model.classification(adata, genes, 'group', obs_features=['Stage'], feature_selection='mutual_info', selection_top_k=20)",
    ],
    related=["diff.de_analysis", "target.enrichr"],
    produces={"uns": [CLASSIFICATION_UNS_KEY]},
)
def classification(
    adata: AnnData,
    features: str | Sequence[str],
    group_col: str | Sequence[Any],
    *,
    obs_features: Optional[str | Sequence[str]] = None,
    layer: Optional[str] = None,
    feature_selection: Optional[str] = None,
    selection_top_k: Optional[int] = None,
    missing_threshold: float = 0.20,
    imputation: str = "median",
    variance_threshold: float = 0.0,
    split_data: bool = False,
    test_size: float = 0.2,
    model: Optional[str | Sequence[str]] = None,
    cross_validate: bool = False,
    cv_folds: int = 5,
    output_dir: str = "results/models/classification",
    random_state: int = 0,
    force: bool = False,
) -> AnnData:
    """Train requested classifiers and save models for future prediction.

    The default uses all samples to train every supported model. Enable
    ``split_data`` for a stratified holdout evaluation and/or ``cross_validate``
    for stratified out-of-fold metrics. The saved artifact for each model holds
    its preprocessing pipeline, label encoder, selected features, and labels.
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")
    if not 0 < float(test_size) < 1:
        raise ValueError("test_size must be between 0 and 1")
    if int(cv_folds) < 2:
        raise ValueError("cv_folds must be at least 2")

    feature_names = _normalise_features(features)
    selected_models = _normalise_models(model)
    raw_labels, group_source = _resolve_groups(adata, group_col)
    matrix, feature_names, layer_name, preprocessing = prepare_feature_matrix(
        adata, feature_names, obs_features=obs_features, layer=layer,
        missing_threshold=missing_threshold, imputation=imputation,
        variance_threshold=variance_threshold,
    )
    if feature_selection:
        matrix, feature_names, selection_table = select_features(
            matrix, feature_names, raw_labels, method=feature_selection,
            top_k=selection_top_k, de_results=adata.uns.get("de_results"),
            random_state=int(random_state),
        )
        preprocessing["selection_table"] = selection_table
    encoder = LabelEncoder()
    labels = encoder.fit_transform(raw_labels)
    class_codes = np.unique(labels)
    if len(class_codes) < 2:
        raise ValueError("Classification requires at least two encoded classes")

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = adata.uns.get(CLASSIFICATION_UNS_KEY)
    request = {
        "features": feature_names,
        "group_source": group_source,
        "group_labels": raw_labels.tolist(),
        "obs_features": list(obs_features) if obs_features is not None else [],
        "layer": layer_name,
        "feature_selection": feature_selection,
        "selection_top_k": selection_top_k,
        "missing_threshold": float(missing_threshold),
        "imputation": imputation,
        "variance_threshold": float(variance_threshold),
        "split_data": bool(split_data),
        "test_size": float(test_size),
        "models": selected_models,
        "cross_validate": bool(cross_validate),
        "cv_folds": int(cv_folds),
        "output_dir": str(out_dir),
        "random_state": int(random_state),
    }
    cached_paths = dict(existing.get("model_paths") or {}) if isinstance(existing, Mapping) else {}
    cache_is_complete = bool(cached_paths) and all(Path(path).exists() for path in cached_paths.values())
    if isinstance(existing, Mapping) and not force and existing.get("request") == request and cache_is_complete:
        state = dict(existing)
        state["reused"] = True
        state["completed_at"] = _utc_now()
        adata.uns[CLASSIFICATION_UNS_KEY] = state
        return adata

    train_idx: Optional[np.ndarray] = None
    test_idx: Optional[np.ndarray] = None
    if split_data:
        smallest_group = int(pd.Series(labels).value_counts().min())
        if smallest_group < 2:
            raise ValueError("Stratified train/test split requires at least two samples in every class")
        train_idx, test_idx = train_test_split(
            np.arange(adata.n_obs),
            test_size=float(test_size),
            stratify=labels,
            random_state=int(random_state),
        )

    details: Dict[str, Dict[str, Any]] = {}
    model_paths: Dict[str, str] = {}
    for model_name in selected_models:
        estimator = _classifier(model_name, len(class_codes), int(random_state))
        evaluations: Dict[str, Any] = {}
        if train_idx is not None and test_idx is not None:
            estimator.fit(matrix[train_idx], labels[train_idx])
            predicted = estimator.predict(matrix[test_idx])
            probabilities = estimator.predict_proba(matrix[test_idx])
            evaluations["holdout"] = _metrics(labels[test_idx], predicted, probabilities, class_codes)
            evaluations["holdout"].update({
                "n_train": int(len(train_idx)),
                "n_test": int(len(test_idx)),
                "train_samples": adata.obs_names[train_idx].astype(str).tolist(),
                "test_samples": adata.obs_names[test_idx].astype(str).tolist(),
            })
        if cross_validate:
            evaluations["cross_validation"] = _cross_validate(
                estimator, matrix, labels, folds=int(cv_folds), random_state=int(random_state),
            )

        # Fit the deployable model on all available samples after evaluation.
        estimator.fit(matrix, labels)
        predicted_all = estimator.predict(matrix)
        probabilities_all = estimator.predict_proba(matrix)
        evaluations["all_samples"] = _metrics(labels, predicted_all, probabilities_all, class_codes)
        artifact = {
            "pipeline": estimator,
            "label_encoder": encoder,
            "features": feature_names,
            "layer": layer_name,
            "model": model_name,
            "class_labels": encoder.classes_.astype(str).tolist(),
        }
        path = out_dir / f"{model_name}.joblib"
        joblib.dump(artifact, path)
        model_paths[model_name] = str(path)
        details[model_name] = evaluations

    adata.uns[CLASSIFICATION_UNS_KEY] = {
        "request": request,
        "input_layer": layer_name,
        "class_labels": encoder.classes_.astype(str).tolist(),
        "feature_names": feature_names,
        "preprocessing": preprocessing,
        "feature_selection_note": (
            "Features are supplied by the caller. If they were selected using all samples and their labels "
            "(for example, global DE top features), holdout and cross-validation metrics may be optimistically biased."
        ),
        "model_paths": model_paths,
        "performance": _performance_rows(details),
        "details": details,
        "reused": False,
        "completed_at": _utc_now(),
    }
    return adata
