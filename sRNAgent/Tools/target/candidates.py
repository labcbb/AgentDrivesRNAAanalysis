"""Deterministic candidate selection for miRNA/isomiR target prediction."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd
from anndata import AnnData
from scipy import sparse

from ..._registry import register_function


TARGET_CANDIDATES_UNS_KEY = "target_candidate_selection"
_P_COLUMNS = ("adj_p_value", "adj_p", "padj", "fdr", "FDR")
_EFFECT_COLUMNS = ("log_fc", "logFC", "log2FoldChange")
_FEATURE_COLUMNS = ("feature", "candidate", "mirna_id", "miRNA", "miRNAname")


def _as_frame(value: Any, label: str) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if isinstance(value, Mapping) or isinstance(value, list):
        return pd.DataFrame(value)
    raise TypeError(f"{label} must be a pandas DataFrame, mapping, or list of records")


def _first_column(frame: pd.DataFrame, names: Sequence[str]) -> Optional[str]:
    return next((name for name in names if name in frame.columns), None)


def _as_feature_ids(value: str | Sequence[str]) -> list[str]:
    raw = [value] if isinstance(value, str) else value
    selected: list[str] = []
    for item in raw:
        selected.extend(part.strip() for part in str(item).split(",") if part.strip())
    return list(dict.fromkeys(selected))


def _feature_column(frame: pd.DataFrame) -> pd.Series:
    column = _first_column(frame, _FEATURE_COLUMNS)
    return frame[column].astype(str) if column else pd.Series(frame.index.astype(str), index=frame.index)


def _mean_cpm(adata: AnnData, counts_layer: str) -> pd.Series:
    if counts_layer not in adata.layers:
        raise KeyError(
            f"adata.layers[{counts_layer!r}] is required to calculate arithmetic mean CPM; "
            "do not derive it from logCPM."
        )
    counts = adata.layers[counts_layer]
    counts = counts.toarray() if sparse.issparse(counts) else np.asarray(counts)
    if counts.ndim != 2 or counts.shape != adata.shape:
        raise ValueError(f"adata.layers[{counts_layer!r}] must have AnnData shape {adata.shape}")
    counts = np.asarray(counts, dtype=float)
    library_sizes = counts.sum(axis=1, keepdims=True)
    library_sizes[library_sizes <= 0] = 1.0
    return pd.Series((counts / library_sizes * 1e6).mean(axis=0), index=adata.var_names, name="mean_cpm")


def resolve_target_features(
    adata: AnnData,
    feature_ids: Optional[str | Sequence[str]] = None,
    *,
    selection_key: str = TARGET_CANDIDATES_UNS_KEY,
    allow_all_features: bool = False,
) -> list[str]:
    """Resolve an explicit or persisted target-candidate set.

    This guard is intentionally shared by all expensive target predictors.
    Passing ``allow_all_features=True`` is an explicit bulk-computation opt-in.
    """
    if feature_ids is not None:
        selected = _as_feature_ids(feature_ids)
        source = "explicit feature_ids"
    else:
        state = adata.uns.get(selection_key)
        selected = _as_feature_ids(state.get("features") or []) if isinstance(state, Mapping) else []
        source = f"adata.uns[{selection_key!r}]"
    if not selected and allow_all_features:
        selected = adata.var_names.astype(str).tolist()
        source = "explicit allow_all_features=True"
    if not selected:
        raise ValueError(
            "No target candidates were supplied. Run target.select_target_candidates first, "
            "pass feature_ids explicitly, or set allow_all_features=True after explicit user approval."
        )
    missing = [feature for feature in selected if feature not in adata.var_names]
    if missing:
        raise KeyError(f"Target candidates are absent from adata.var_names: {', '.join(missing[:5])}")
    return selected


@register_function(
    aliases=[
        "select_target_candidates", "target_candidate_selection", "select_mirna_target_candidates",
        "靶标候选筛选", "靶标预测候选", "isomir靶标候选",
    ],
    category="target",
    description=(
        "Deterministically select a documented miRNA/isomiR target-prediction subset from DE and raw counts. "
        "Default filters are FDR < 0.05, logFC > 1, and arithmetic mean CPM > 5, then retain the top 5 ranked "
        "candidates; the result is stored in "
        "adata.uns['target_candidate_selection'] for downstream starBase, miRanda, and seed scanning."
    ),
    examples=[
        "adata = sa.target.select_target_candidates(adata)",
        "adata = sa.target.select_target_candidates(isomir_adata, direction='downregulated')",
        "adata = sa.target.select_target_candidates(adata, candidates=['hsa-miR-21-5p'])",
    ],
    produces={"uns": [TARGET_CANDIDATES_UNS_KEY]},
)
def select_target_candidates(
    adata: AnnData,
    candidates: Optional[str | Sequence[str]] = None,
    *,
    de_key: str = "de_results",
    counts_layer: str = "counts",
    candidate_type: str = "auto",
    direction: str = "upregulated",
    adj_p_max: float = 0.05,
    log_fc_min: float = 1.0,
    mean_cpm_min: float = 5.0,
    top_n: Optional[int] = 5,
    result_key: str = TARGET_CANDIDATES_UNS_KEY,
) -> AnnData:
    """Persist target candidates without letting an LLM choose or rank them.

    An explicit ``candidates`` list is a documented user override and does not
    require DE. Otherwise, raw counts and DE are both mandatory. Automatic
    selection is capped at the top five candidates by default to prevent an
    expensive whole-UTR prediction for every significant isomiR. Pass
    ``top_n=None`` only after explicit approval for a larger ranked set.
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")
    key = str(result_key or "").strip()
    if not key:
        raise ValueError("result_key must be a non-empty AnnData uns key")
    kind = str(candidate_type or "auto").lower().replace("-", "")
    if kind not in {"auto", "mirna", "isomir"}:
        raise ValueError("candidate_type must be 'auto', 'mirna', or 'isomir'")
    if str(direction).lower() not in {"upregulated", "downregulated"}:
        raise ValueError("direction must be 'upregulated' or 'downregulated'")
    if top_n is not None:
        if isinstance(top_n, bool) or int(top_n) != top_n or int(top_n) < 1:
            raise ValueError("top_n must be a positive integer or None")
        top_n = int(top_n)

    if candidates is not None:
        features = resolve_target_features(adata, candidates)
        table = pd.DataFrame(index=features)
        table["feature_id"] = features
        table["selection_reason"] = "explicit user candidate list"
        source = "explicit_candidates"
        mean_cpm_source = None
        eligible_count = len(features)
        selection_limit = None
    else:
        if de_key not in adata.uns:
            raise KeyError(f"adata.uns[{de_key!r}] is required; run differential analysis first")
        de = _as_frame(adata.uns[de_key], "differential-expression results")
        p_column = _first_column(de, _P_COLUMNS)
        effect_column = _first_column(de, _EFFECT_COLUMNS)
        if p_column is None or effect_column is None:
            raise KeyError("DE results require an adjusted-p-value column and log_fc/logFC/log2FoldChange")
        de = de.copy()
        de["feature_id"] = _feature_column(de)
        de["adj_p_value"] = pd.to_numeric(de[p_column], errors="coerce")
        de["log_fc"] = pd.to_numeric(de[effect_column], errors="coerce")
        de["mean_cpm"] = de["feature_id"].map(_mean_cpm(adata, counts_layer))
        if "rna_type" in adata.var.columns:
            accepted = {"mirna", "isomir"} if kind == "auto" else {"mirna" if kind == "mirna" else "isomir"}
            allowed = set(
                adata.var.index[adata.var["rna_type"].astype(str).str.casefold().isin(accepted)].astype(str)
            )
            de = de.loc[de["feature_id"].isin(allowed)]
        direction_filter = de["log_fc"] >= float(log_fc_min) if str(direction).lower() == "upregulated" else de["log_fc"] <= -float(log_fc_min)
        table = de.loc[
            (de["adj_p_value"] < float(adj_p_max)) & direction_filter & (de["mean_cpm"] > float(mean_cpm_min))
        ].copy()
        table = table.loc[table["feature_id"].isin(adata.var_names.astype(str))]
        table = table.sort_values(["adj_p_value", "log_fc"], ascending=[True, str(direction).lower() == "downregulated"], kind="stable")
        table = table.drop_duplicates("feature_id", keep="first")
        eligible_count = len(table)
        if top_n is not None:
            table = table.head(top_n).copy()
        features = table["feature_id"].astype(str).tolist()
        if top_n is None:
            table["selection_reason"] = "DE FDR, effect-size, and arithmetic mean-CPM gate"
        else:
            table["selection_reason"] = (
                "DE FDR, effect-size, and arithmetic mean-CPM gate; "
                f"ranked top {top_n}"
            )
        source = de_key
        mean_cpm_source = counts_layer
        selection_limit = top_n

    adata.uns[key] = {
        "tool": "select_target_candidates",
        "source": source,
        "features": features,
        "table": table,
        "parameters": {
            "de_key": de_key,
            "counts_layer": mean_cpm_source,
            "candidate_type": kind,
            "direction": str(direction).lower(),
            "adj_p_value_max": float(adj_p_max),
            "log_fc_min": float(log_fc_min),
            "mean_cpm_min": float(mean_cpm_min),
            "top_n": selection_limit,
        },
        "n_eligible_before_top_n": eligible_count,
        "n_features": len(features),
    }
    return adata


__all__ = ["TARGET_CANDIDATES_UNS_KEY", "resolve_target_features", "select_target_candidates"]
