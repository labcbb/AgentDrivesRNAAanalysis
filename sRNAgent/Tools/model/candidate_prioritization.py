"""Deterministic, auditable small-RNA candidate prioritization.

This module deliberately consumes completed analysis records instead of
calling an LLM or refitting models.  It makes the evidence used for every
rank explicit, preserves candidates that fail a gate in the audit table, and
only exposes eligible candidates in the recommendation table.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd
from anndata import AnnData
from scipy.stats import spearmanr

from ..._registry import register_function


CANDIDATE_PRIORITIZATION_UNS_KEY = "candidate_prioritization"
_WEIGHTS = {"D": 0.30, "R": 0.25, "C": 0.20, "B": 0.15, "Q": 0.10}
_P_COLUMNS = ("adj_p_value", "adj_p", "padj", "fdr", "FDR", "p_value", "P.Value")
_EFFECT_COLUMNS = ("log_fc", "logFC", "log2FoldChange")
_EXPRESSION_COLUMNS = ("ave_expr", "mean_expression", "mean_expr", "baseMean")
_FEATURE_COLUMNS = ("feature", "candidate", "mirna_id", "miRNA", "miRNAname")
_DEPTH_COLUMNS = ("library_size", "total_reads", "raw_reads", "mapped_reads", "reads")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _as_frame(value: Any, *, name: str) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if isinstance(value, Mapping):
        return pd.DataFrame(value)
    if isinstance(value, list):
        return pd.DataFrame(value)
    raise TypeError(f"{name} must be a pandas DataFrame, mapping, or list of records")


def _first_column(frame: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    return next((column for column in candidates if column in frame.columns), None)


def _clip(value: Any, lower: float = 0.0, upper: float = 1.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return float(np.clip(numeric, lower, upper)) if np.isfinite(numeric) else 0.0


def _numeric(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return numeric if np.isfinite(numeric) else float("nan")


def _feature_series(frame: pd.DataFrame) -> pd.Series:
    column = _first_column(frame, _FEATURE_COLUMNS)
    if column is not None:
        return frame[column].astype(str)
    return pd.Series(frame.index.astype(str), index=frame.index)


def _normalise_candidates(adata: AnnData, de: pd.DataFrame, candidates: Optional[str | Sequence[str]], candidate_type: str) -> list[str]:
    if candidates is None:
        values = _feature_series(de).tolist()
    elif isinstance(candidates, str):
        values = [part.strip() for part in candidates.split(",") if part.strip()]
    else:
        values = [str(value).strip() for value in candidates if str(value).strip()]
    values = list(dict.fromkeys(values))
    if not values:
        raise ValueError("No candidate features were found")
    kind = str(candidate_type).lower().replace("-", "")
    if kind not in {"auto", "mirna", "isomir"}:
        raise ValueError("candidate_type must be 'auto', 'mirna', or 'isomir'")
    if kind == "mirna" and "rna_type" in adata.var.columns:
        mirnas = set(adata.var.index[adata.var["rna_type"].astype(str).str.lower() == "mirna"].astype(str))
        if mirnas:
            values = [value for value in values if value in mirnas]
    if not values:
        raise ValueError("No requested candidates remain after candidate_type filtering")
    return values


def _expression_frame(adata: AnnData, candidates: Sequence[str], layer: Optional[str]) -> tuple[pd.DataFrame, str]:
    chosen = layer
    if chosen is None:
        chosen = next((name for name in ("logcpm", "voom_E", "counts") if name in adata.layers), None)
    if chosen is None or str(chosen).lower() in {"x", "adata.x"}:
        values, layer_name = adata.X, "X"
    else:
        if chosen not in adata.layers:
            raise KeyError(f"adata.layers[{chosen!r}] is missing. Available: {list(adata.layers.keys())}")
        values, layer_name = adata.layers[chosen], str(chosen)
    if hasattr(values, "toarray"):
        values = values.toarray()
    array = np.asarray(values, dtype=float)
    present = [candidate for candidate in candidates if candidate in adata.var_names]
    frame = pd.DataFrame(index=adata.obs_names)
    if present:
        frame = pd.DataFrame(array[:, adata.var_names.get_indexer(present)], index=adata.obs_names, columns=present)
    return frame, layer_name


def _frame_digest(frame: pd.DataFrame) -> str:
    stable = frame.copy()
    stable.columns = stable.columns.astype(str)
    stable.index = stable.index.astype(str)
    payload = stable.to_csv(index=True, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _value_digest(value: Any) -> str:
    if isinstance(value, pd.DataFrame):
        return _frame_digest(value)
    try:
        text = json.dumps(value, sort_keys=True, default=str, ensure_ascii=True, separators=(",", ":"))
    except TypeError:
        text = repr(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _de_lookup(de: pd.DataFrame) -> pd.DataFrame:
    table = de.copy()
    table["_candidate"] = _feature_series(table).astype(str)
    return table.drop_duplicates("_candidate", keep="first").set_index("_candidate", drop=False)


def _replication_rows(value: Any, candidates: Sequence[str]) -> dict[str, pd.DataFrame]:
    if value is None:
        return {}
    frame = _as_frame(value, name="replication results")
    if frame.empty:
        return {}
    frame["_candidate"] = _feature_series(frame).astype(str)
    return {candidate: frame.loc[frame["_candidate"] == candidate].copy() for candidate in candidates}


def _replication_evidence(rows: pd.DataFrame, *, primary_direction: float, fdr_cutoff: float) -> tuple[float, dict[str, Any], list[str], list[str]]:
    if rows.empty:
        return 0.0, {}, ["replication evidence unavailable"], []
    direction_col = _first_column(rows, _EFFECT_COLUMNS)
    consistency_col = _first_column(rows, ("direction_consistency", "direction_stability"))
    significance_col = _first_column(rows, ("significant_fraction", "replication_rate"))
    heterogeneity_col = _first_column(rows, ("heterogeneity", "i2", "I2"))
    p_col = _first_column(rows, _P_COLUMNS)
    evidence: dict[str, Any] = {"n_replication_rows": int(len(rows))}
    gaps: list[str] = []
    exclusion: list[str] = []

    directions = pd.to_numeric(rows[direction_col], errors="coerce").dropna() if direction_col else pd.Series(dtype=float)
    if consistency_col:
        consistency = _clip(pd.to_numeric(rows[consistency_col], errors="coerce").mean())
    elif not directions.empty and primary_direction != 0:
        consistency = float((np.sign(directions) == np.sign(primary_direction)).mean())
    else:
        consistency = float("nan")
        gaps.append("replication direction unavailable")
    if np.isfinite(consistency):
        evidence["direction_consistency"] = consistency
        if consistency < 1.0:
            gaps.append("replication direction is not fully stable")
        if consistency == 0.0:
            exclusion.append("cross-cohort direction conflict")

    if significance_col:
        significant_fraction = _clip(pd.to_numeric(rows[significance_col], errors="coerce").mean())
    elif p_col:
        significant_fraction = float((pd.to_numeric(rows[p_col], errors="coerce") <= fdr_cutoff).mean())
    else:
        significant_fraction = float("nan")
        gaps.append("replication significance unavailable")
    if np.isfinite(significant_fraction):
        evidence["significant_fraction"] = significant_fraction

    if heterogeneity_col:
        heterogeneity = _numeric(pd.to_numeric(rows[heterogeneity_col], errors="coerce").mean())
        heterogeneity_score = 1.0 / (1.0 + max(0.0, heterogeneity)) if np.isfinite(heterogeneity) else float("nan")
    elif len(directions) >= 2:
        heterogeneity = float(directions.std(ddof=0) / (abs(directions.mean()) + 1e-12))
        heterogeneity_score = 1.0 / (1.0 + heterogeneity)
    else:
        heterogeneity = heterogeneity_score = float("nan")
        gaps.append("replication heterogeneity unavailable")
    if np.isfinite(heterogeneity):
        evidence["heterogeneity"] = heterogeneity

    components = [value for value in (consistency, significant_fraction, heterogeneity_score) if np.isfinite(value)]
    return float(np.mean(components)) if components else 0.0, evidence, gaps, exclusion


def _classification_evidence(state: Any, candidate: str, minimum_auc: float) -> tuple[float, dict[str, Any], list[str], list[str]]:
    if not isinstance(state, Mapping) or candidate not in {str(value) for value in state.get("feature_names") or []}:
        return 0.0, {}, ["validated classification evidence unavailable"], []
    details = state.get("details") or {}
    aucs: list[float] = []
    for model, result in details.items() if isinstance(details, Mapping) else []:
        cv = result.get("cross_validation") if isinstance(result, Mapping) else None
        if not isinstance(cv, Mapping):
            continue
        auc = _numeric(cv.get("roc_auc", cv.get("roc_auc_ovr_weighted")))
        if np.isfinite(auc):
            aucs.append(auc)
    if not aucs:
        return 0.0, {}, ["classification used this candidate without cross-validation"], ["model performance lacks cross-validation"]
    auc = max(aucs)
    evidence = {"classification_cv_auc": auc}
    exclusion = [] if auc >= minimum_auc else [f"classification CV AUC below {minimum_auc:g}"]
    return _clip((auc - 0.5) / 0.5), evidence, [], exclusion


def _cox_evidence(state: Any, candidate: str, minimum_cindex: float) -> tuple[float, dict[str, Any], list[str], list[str]]:
    if not isinstance(state, Mapping):
        return 0.0, {}, ["validated Cox evidence unavailable"], []
    request = state.get("request") or {}
    selected = {str(value) for value in state.get("selected_features") or request.get("features") or []}
    tables = [state.get("multivariate_results"), state.get("univariate_results")]
    table = next((item for item in tables if isinstance(item, pd.DataFrame) and candidate in item.index.astype(str)), None)
    if candidate not in selected and table is None:
        return 0.0, {}, ["validated Cox evidence unavailable"], []
    evidence: dict[str, Any] = {}
    gaps: list[str] = []
    if table is not None:
        row = table.loc[candidate]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        p_value = _numeric(row.get("p_value"))
        coefficient = _numeric(row.get("coef"))
        if np.isfinite(p_value):
            evidence["cox_p_value"] = p_value
        if np.isfinite(coefficient):
            evidence["cox_coef"] = coefficient
        if not (state.get("request") or {}).get("obs_features"):
            gaps.append("Cox association is not adjusted for recorded clinical covariates")
    else:
        gaps.append("candidate-specific Cox coefficient unavailable")

    cv = state.get("cross_validation")
    if not isinstance(cv, pd.DataFrame) or "c_index" not in cv.columns:
        return 0.0, evidence, gaps + ["Cox model used this candidate without cross-validation"], ["model performance lacks cross-validation"]
    cindex = _numeric(pd.to_numeric(cv["c_index"], errors="coerce").mean())
    if not np.isfinite(cindex):
        return 0.0, evidence, gaps + ["Cox cross-validation C-index unavailable"], ["model performance lacks cross-validation"]
    evidence["cox_cv_c_index"] = cindex
    exclusion = [] if cindex >= minimum_cindex else [f"Cox CV C-index below {minimum_cindex:g}"]
    if not (state.get("request") or {}).get("obs_features"):
        # An unadjusted association remains auditable but does not count as
        # clinical value in the composite score.
        return 0.0, evidence, gaps, exclusion
    return _clip((cindex - 0.5) / 0.5), evidence, gaps, exclusion


def _target_evidence(adata: AnnData, candidate: str, target_key: str, enrichr_key: str) -> tuple[float, dict[str, Any], list[str]]:
    state = adata.uns.get(target_key)
    records = ((state or {}).get("last_run") or {}).get("records") if isinstance(state, Mapping) else []
    frames: list[pd.DataFrame] = []
    for record in records or []:
        if str(record.get("miRNA") or "") != candidate:
            continue
        path = Path(str(record.get("tsv") or ""))
        if path.exists():
            frame = pd.read_csv(path, sep="\t", dtype=str)
            frames.append(frame)
    if not frames:
        return 0.0, {}, ["starBase CLIP/degradome target evidence unavailable"]
    targets = pd.concat(frames, ignore_index=True)
    clip = pd.to_numeric(targets.get("clipExpNum"), errors="coerce") if "clipExpNum" in targets else pd.Series(dtype=float)
    degradome = pd.to_numeric(targets.get("degraExpNum"), errors="coerce") if "degraExpNum" in targets else pd.Series(dtype=float)
    target_count = int(len(targets))
    clip_score = _clip(float((clip > 0).mean())) if not clip.empty else 0.0
    degradome_score = _clip(float((degradome > 0).mean())) if not degradome.empty else 0.0
    abundance_score = _clip(target_count / 10.0)
    score = 0.45 * clip_score + 0.35 * degradome_score + 0.20 * abundance_score
    evidence = {"starbase_target_count": target_count, "clip_supported_fraction": clip_score, "degradome_supported_fraction": degradome_score}
    gaps = ["target-gene inverse-correlation evidence unavailable"]
    enrichr = adata.uns.get(enrichr_key)
    if isinstance(enrichr, Mapping) and isinstance(enrichr.get("results"), pd.DataFrame):
        evidence["enrichment_terms_available"] = int(len(enrichr["results"]))
    else:
        gaps.append("target-pathway enrichment evidence unavailable")
    return score, evidence, gaps


def _isomir_evidence(adata: AnnData, candidate: str, seed_col: str, target_difference_col: str) -> tuple[float, dict[str, Any], list[str]]:
    if candidate not in adata.var_names:
        return 0.0, {}, ["isomiR annotation unavailable"]
    row = adata.var.loc[candidate]
    seed = _numeric(row.get(seed_col))
    target_difference = _numeric(row.get(target_difference_col))
    evidence: dict[str, Any] = {}
    components: list[float] = []
    gaps: list[str] = []
    if np.isfinite(seed):
        evidence["seed_change_evidence"] = _clip(seed)
        components.append(_clip(seed))
    else:
        gaps.append(f"isomiR seed evidence missing from var[{seed_col!r}]")
    if np.isfinite(target_difference):
        evidence["target_difference_evidence"] = _clip(target_difference)
        components.append(_clip(target_difference))
    else:
        gaps.append(f"isomiR target-difference evidence missing from var[{target_difference_col!r}]")
    return float(np.mean(components)) if components else 0.0, evidence, gaps


def _technical_evidence(adata: AnnData, expression: pd.DataFrame, candidate: str, *, batch_col: Optional[str], depth_col: Optional[str], mapping_risk_col: str) -> tuple[float, dict[str, Any], list[str]]:
    if candidate not in expression.columns:
        return 0.0, {}, ["candidate is absent from the expression matrix"]
    values = expression[candidate]
    coverage = float(values.notna().mean() and (values.fillna(0) > 0).mean())
    evidence: dict[str, Any] = {"sample_coverage": coverage}
    components = [coverage]
    gaps: list[str] = []
    chosen_depth = depth_col or next((column for column in _DEPTH_COLUMNS if column in adata.obs.columns), None)
    if chosen_depth:
        depth = pd.to_numeric(adata.obs[chosen_depth], errors="coerce")
        valid = depth.notna() & values.notna()
        correlation = spearmanr(depth[valid], values[valid]).statistic if valid.sum() >= 3 else float("nan")
        if np.isfinite(correlation):
            evidence["depth_spearman_rho"] = float(correlation)
            components.append(1.0 - abs(float(correlation)))
        else:
            gaps.append("sequencing-depth independence unavailable")
    else:
        gaps.append("sequencing-depth column unavailable")
    if batch_col:
        if batch_col not in adata.obs.columns:
            raise KeyError(f"batch_col {batch_col!r} is not in adata.obs")
        grouped = values.groupby(adata.obs[batch_col].astype(str))
        overall_var = float(values.var(ddof=0))
        between = float(np.var(grouped.mean(), ddof=0)) if len(grouped) >= 2 else float("nan")
        if overall_var > 0 and np.isfinite(between):
            batch_fraction = _clip(between / overall_var)
            evidence["batch_variance_fraction"] = batch_fraction
            components.append(1.0 - batch_fraction)
        else:
            gaps.append("batch robustness unavailable")
    else:
        gaps.append("batch column unavailable")
    if mapping_risk_col in adata.var.columns and candidate in adata.var_names:
        risk = _numeric(adata.var.loc[candidate, mapping_risk_col])
        if np.isfinite(risk):
            evidence["multi_mapping_risk"] = _clip(risk)
            components.append(1.0 - _clip(risk))
    else:
        gaps.append(f"multi-mapping risk missing from var[{mapping_risk_col!r}]")
    return float(np.mean(components)), evidence, gaps


def _json_safe(value: Any) -> Any:
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


@register_function(
    aliases=["candidate_prioritization", "candidate_ranking", "biomarker_prioritization", "候选优先级", "候选排序", "生物标志物排序"],
    category="model",
    description=(
        "Deterministically integrate completed DE, replication, classification, Cox, starBase, Enrichr, and technical-QC evidence "
        "into an auditable small-RNA candidate priority table. Scores use fixed D/R/C/B/Q weights; hard gates keep failed "
        "candidates in the audit table but exclude them from the recommended list."
    ),
    examples=[
        "adata = sa.model.candidate_prioritization(adata)",
        "adata = sa.model.candidate_prioritization(adata, replication_key='candidate_replication', batch_col='batch')",
        "adata = sa.model.candidate_prioritization(isomir_adata, candidate_type='isomir', seed_change_col='seed_change_score')",
    ],
    related=["diff.de_analysis", "model.classification", "model.cox", "target.starbase_mirna_targets", "target.enrichr"],
    requires={"uns": ["de_results"]},
    produces={"uns": [CANDIDATE_PRIORITIZATION_UNS_KEY]},
)
def candidate_prioritization(
    adata: AnnData,
    candidates: Optional[str | Sequence[str]] = None,
    *,
    candidate_type: str = "auto",
    de_key: str = "de_results",
    replication_key: str = "candidate_replication",
    classification_key: str = "classification",
    cox_key: str = "cox",
    target_key: str = "starbase_mirna_targets",
    enrichr_key: str = "enrichr",
    layer: Optional[str] = None,
    batch_col: Optional[str] = None,
    depth_col: Optional[str] = None,
    mapping_risk_col: str = "multi_mapping_risk",
    seed_change_col: str = "seed_change_score",
    target_difference_col: str = "target_difference_score",
    fdr_cutoff: float = 0.05,
    min_abs_logfc: float = 0.5,
    min_expression: float = 1.0,
    min_coverage: float = 0.70,
    min_classification_auc: float = 0.60,
    min_cox_cindex: float = 0.60,
    output_dir: str = "results/candidate_prioritization",
    force: bool = False,
) -> AnnData:
    """Rank candidates from existing results with fixed scoring and hard gates.

    ``replication_key`` can contain a long table with feature/candidate,
    ``log_fc`` and adjusted-p-value columns, or per-candidate summary columns
    ``direction_consistency``, ``significant_fraction``, and
    ``heterogeneity``.  A conflicting observed direction is a hard exclusion.
    Missing evidence is recorded as a gap and receives no score; it is never
    silently treated as positive evidence.
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")
    for name, value in {
        "fdr_cutoff": fdr_cutoff,
        "min_coverage": min_coverage,
        "min_classification_auc": min_classification_auc,
        "min_cox_cindex": min_cox_cindex,
    }.items():
        if not 0 < float(value) <= 1:
            raise ValueError(f"{name} must be in (0, 1]")
    if float(min_abs_logfc) < 0 or float(min_expression) < 0:
        raise ValueError("min_abs_logfc and min_expression must be non-negative")
    if de_key not in adata.uns:
        raise KeyError(f"adata.uns[{de_key!r}] is required; run differential analysis first")

    de = _as_frame(adata.uns[de_key], name=f"adata.uns[{de_key!r}]")
    p_col = _first_column(de, _P_COLUMNS)
    effect_col = _first_column(de, _EFFECT_COLUMNS)
    if p_col is None or effect_col is None:
        raise KeyError("DE results must contain an adjusted p-value/FDR column and a log-fold-change column")
    expression_col = _first_column(de, _EXPRESSION_COLUMNS)
    selected = _normalise_candidates(adata, de, candidates, candidate_type)
    expression, layer_name = _expression_frame(adata, selected, layer)
    de_table = _de_lookup(de)
    replication_value = adata.uns.get(replication_key)
    replication = _replication_rows(replication_value, selected)
    classification_state = adata.uns.get(classification_key)
    cox_state = adata.uns.get(cox_key)
    request = {
        "candidates": selected,
        "candidate_type": candidate_type,
        "de_key": de_key,
        "replication_key": replication_key,
        "classification_key": classification_key,
        "cox_key": cox_key,
        "target_key": target_key,
        "enrichr_key": enrichr_key,
        "layer": layer_name,
        "batch_col": batch_col,
        "depth_col": depth_col,
        "mapping_risk_col": mapping_risk_col,
        "seed_change_col": seed_change_col,
        "target_difference_col": target_difference_col,
        "fdr_cutoff": float(fdr_cutoff),
        "min_abs_logfc": float(min_abs_logfc),
        "min_expression": float(min_expression),
        "min_coverage": float(min_coverage),
        "min_classification_auc": float(min_classification_auc),
        "min_cox_cindex": float(min_cox_cindex),
    }
    fingerprints = {
        "de": _frame_digest(de),
        "expression": _frame_digest(expression),
        "replication": _value_digest(replication_value),
        "classification": _value_digest(classification_state),
        "cox": _value_digest(cox_state),
        "targets": _value_digest(adata.uns.get(target_key)),
        "enrichr": _value_digest(adata.uns.get(enrichr_key)),
    }
    existing = adata.uns.get(CANDIDATE_PRIORITIZATION_UNS_KEY)
    if isinstance(existing, Mapping) and not force and existing.get("request") == request and existing.get("input_fingerprints") == fingerprints:
        state = dict(existing)
        state["reused"] = True
        state["completed_at"] = _utc_now()
        adata.uns[CANDIDATE_PRIORITIZATION_UNS_KEY] = state
        return adata

    rows: list[dict[str, Any]] = []
    kind = str(candidate_type).lower().replace("-", "")
    for candidate in selected:
        de_row = de_table.loc[candidate] if candidate in de_table.index else pd.Series(dtype=object)
        if isinstance(de_row, pd.DataFrame):
            de_row = de_row.iloc[0]
        fdr = _numeric(de_row.get(p_col))
        effect = _numeric(de_row.get(effect_col))
        mean_expression = _numeric(de_row.get(expression_col)) if expression_col else float("nan")
        if not np.isfinite(mean_expression) and candidate in expression.columns:
            mean_expression = _numeric(expression[candidate].mean())
        fdr_score = _clip(-np.log10(max(fdr, 1e-300)) / -np.log10(float(fdr_cutoff))) if np.isfinite(fdr) else 0.0
        effect_score = _clip(abs(effect) / max(float(min_abs_logfc), 1e-12)) if np.isfinite(effect) else 0.0
        expression_score = _clip(mean_expression / max(float(min_expression), 1e-12)) if np.isfinite(mean_expression) else 0.0
        d_score = float(np.mean([fdr_score, effect_score, expression_score]))
        evidence = {"fdr": fdr, "log_fc": effect, "mean_expression": mean_expression}
        gaps: list[str] = []
        exclusions: list[str] = []
        if not np.isfinite(fdr):
            gaps.append("DE FDR unavailable")
            exclusions.append("DE FDR unavailable")
        elif fdr > float(fdr_cutoff):
            exclusions.append(f"DE FDR exceeds {float(fdr_cutoff):g}")
        if not np.isfinite(effect):
            gaps.append("DE effect size unavailable")
            exclusions.append("DE effect size unavailable")
        elif abs(effect) < float(min_abs_logfc):
            exclusions.append(f"absolute log-fold-change below {float(min_abs_logfc):g}")
        if not np.isfinite(mean_expression):
            gaps.append("mean expression unavailable")
            exclusions.append("mean expression unavailable")
        elif mean_expression < float(min_expression):
            exclusions.append(f"mean expression below {float(min_expression):g}")

        r_score, r_evidence, r_gaps, r_exclusions = _replication_evidence(
            replication.get(candidate, pd.DataFrame()), primary_direction=effect, fdr_cutoff=float(fdr_cutoff),
        )
        c_classification, classification_evidence, classification_gaps, classification_exclusions = _classification_evidence(
            classification_state, candidate, float(min_classification_auc),
        )
        c_cox, cox_evidence, cox_gaps, cox_exclusions = _cox_evidence(cox_state, candidate, float(min_cox_cindex))
        clinical_components = [score for score, source in ((c_classification, classification_evidence), (c_cox, cox_evidence)) if source]
        c_score = float(np.mean(clinical_components)) if clinical_components else 0.0
        if kind == "isomir" or (kind == "auto" and candidate in adata.var_names and "isomir" in str(adata.var.loc[candidate].get("rna_type", "")).lower()):
            b_score, b_evidence, b_gaps = _isomir_evidence(adata, candidate, seed_change_col, target_difference_col)
        else:
            b_score, b_evidence, b_gaps = _target_evidence(adata, candidate, target_key, enrichr_key)
        q_score, q_evidence, q_gaps = _technical_evidence(
            adata, expression, candidate, batch_col=batch_col, depth_col=depth_col, mapping_risk_col=mapping_risk_col,
        )
        coverage = _numeric(q_evidence.get("sample_coverage"))
        if np.isfinite(coverage) and coverage < float(min_coverage):
            exclusions.append(f"sample coverage below {float(min_coverage):g}")

        evidence.update({f"R_{key}": value for key, value in r_evidence.items()})
        evidence.update({f"C_{key}": value for key, value in classification_evidence.items()})
        evidence.update({f"C_{key}": value for key, value in cox_evidence.items()})
        evidence.update({f"B_{key}": value for key, value in b_evidence.items()})
        evidence.update({f"Q_{key}": value for key, value in q_evidence.items()})
        gaps.extend(r_gaps + classification_gaps + cox_gaps + b_gaps + q_gaps)
        exclusions.extend(r_exclusions + classification_exclusions + cox_exclusions)
        total = sum(_WEIGHTS[letter] * score for letter, score in (("D", d_score), ("R", r_score), ("C", c_score), ("B", b_score), ("Q", q_score)))
        all_dimensions = 5
        observed_dimensions = 1 + int(bool(r_evidence)) + int(bool(classification_evidence or cox_evidence)) + int(bool(b_evidence)) + int(bool(q_evidence))
        rows.append({
            "candidate": candidate,
            "priority_score": float(total),
            "D_differential": d_score,
            "R_reproducibility": r_score,
            "C_clinical": c_score,
            "B_biological": b_score,
            "Q_technical": q_score,
            "evidence_coverage": observed_dimensions / all_dimensions,
            "eligible": not exclusions,
            "exclusion_reasons": "; ".join(dict.fromkeys(exclusions)),
            "evidence_gaps": "; ".join(dict.fromkeys(gaps)),
            **_json_safe(evidence),
        })

    audit = pd.DataFrame(rows).sort_values(["eligible", "priority_score", "candidate"], ascending=[False, False, True], kind="stable").reset_index(drop=True)
    audit.insert(0, "rank", np.arange(1, len(audit) + 1))
    recommended = audit.loc[audit["eligible"]].copy().reset_index(drop=True)
    recommended["recommendation_rank"] = np.arange(1, len(recommended) + 1)
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    audit_path = output / "candidate_priority_audit.csv"
    recommended_path = output / "candidate_recommendations.csv"
    audit.to_csv(audit_path, index=False)
    recommended.to_csv(recommended_path, index=False)
    manifest = {
        "tool": "candidate_prioritization",
        "formula": "0.30*D + 0.25*R + 0.20*C + 0.15*B + 0.10*Q",
        "weights": _WEIGHTS,
        "request": request,
        "input_fingerprints": fingerprints,
        "n_candidates": int(len(audit)),
        "n_recommended": int(len(recommended)),
        "audit_path": str(audit_path),
        "recommended_path": str(recommended_path),
        "completed_at": _utc_now(),
    }
    manifest_path = output / "candidate_priority_manifest.json"
    manifest_path.write_text(json.dumps(_json_safe(manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    adata.uns[CANDIDATE_PRIORITIZATION_UNS_KEY] = {
        "formula": manifest["formula"],
        "weights": _WEIGHTS,
        "request": request,
        "input_fingerprints": fingerprints,
        "audit": audit,
        "recommended": recommended,
        "artifacts": {"audit_csv": str(audit_path), "recommended_csv": str(recommended_path), "manifest": str(manifest_path)},
        "reused": False,
        "completed_at": manifest["completed_at"],
    }
    return adata


__all__ = ["candidate_prioritization", "CANDIDATE_PRIORITIZATION_UNS_KEY"]
