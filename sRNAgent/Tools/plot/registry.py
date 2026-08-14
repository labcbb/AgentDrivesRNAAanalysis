"""Result-driven plot discovery and safe batch generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from anndata import AnnData


@dataclass(frozen=True)
class PlotDefinition:
    name: str
    category: str
    level: str
    available: Callable[[AnnData], tuple[bool, str]]
    render: Callable[..., AnnData]


def _has(path: str) -> Callable[[AnnData], tuple[bool, str]]:
    parts = path.split(".")
    def check(adata: AnnData) -> tuple[bool, str]:
        current = adata
        for part in parts:
            if part == "uns": current = adata.uns
            elif part == "obs": current = adata.obs
            elif part == "var": current = adata.var
            elif part == "layers": current = adata.layers
            elif part == "obsm": current = adata.obsm
            elif isinstance(current, dict) or hasattr(current, "__contains__"):
                if part not in current: return False, f"missing {path}"
                current = current[part]
        return True, ""
    return check


def _fragment(adata: AnnData) -> tuple[bool, str]:
    return ("CPM" in adata.layers and "type" in adata.var.columns, "missing fragmentomics CPM/type")


def _target(adata: AnnData) -> tuple[bool, str]:
    state = adata.uns.get("starbase_mirna_targets")
    records = ((state or {}).get("last_run") or {}).get("records") if isinstance(state, dict) else None
    return (bool(records), "missing cached starBase records")


def definitions() -> dict[str, PlotDefinition]:
    from . import differential, expression, fragmentomics, model, qc, target
    return {
        "qc_metrics": PlotDefinition("qc_metrics", "qc", "standard", _has("obs"), qc.qc_metrics),
        "alignment_summary": PlotDefinition("alignment_summary", "alignment", "standard", _has("obs.bowtie_alignment_rate"), qc.alignment_summary),
        "pca": PlotDefinition("pca", "expression", "minimal", _has("obsm.X_pca"), expression.pca),
        "sample_correlation": PlotDefinition("sample_correlation", "expression", "standard", _has("layers"), expression.sample_correlation),
        "rna_composition": PlotDefinition("rna_composition", "expression", "standard", _has("var.rna_type"), expression.rna_composition),
        "volcano": PlotDefinition("volcano", "differential", "minimal", _has("uns.de_results"), differential.volcano),
        "de_heatmap": PlotDefinition("de_heatmap", "differential", "minimal", _has("uns.de_results"), differential.de_heatmap),
        "enrichment_dotplot": PlotDefinition("enrichment_dotplot", "enrichment", "standard", _has("uns.enrichr"), differential.enrichment_dotplot),
        "fragment_profile": PlotDefinition("fragment_profile", "fragmentomics", "standard", _fragment, fragmentomics.fragment_profile),
        "fragment_heatmap": PlotDefinition("fragment_heatmap", "fragmentomics", "standard", _fragment, fragmentomics.fragment_heatmap),
        "mirna_target_network": PlotDefinition("mirna_target_network", "target", "standard", _target, target.target_network),
        "classification_performance": PlotDefinition("classification_performance", "classification", "minimal", _has("uns.classification"), model.classification_performance),
        "cox_multivariate_forest": PlotDefinition("cox_multivariate_forest", "cox", "minimal", _has("uns.cox.multivariate_results"), model.cox_forest),
        "cox_cross_validation": PlotDefinition("cox_cross_validation", "cox", "standard", _has("uns.cox.cross_validation"), model.cox_cross_validation),
        "candidate_priorities": PlotDefinition("candidate_priorities", "candidate_prioritization", "minimal", _has("uns.candidate_prioritization.audit"), model.candidate_priorities),
    }


def available_plots(adata: AnnData) -> dict[str, dict[str, str | bool]]:
    return {name: {"available": ok, "category": item.category, "level": item.level, "reason": reason} for name, item in definitions().items() for ok, reason in [item.available(adata)]}


def generate_plots(adata: AnnData, plots: Optional[Sequence[str]] = None, *, scope: str = "standard", group_col: Optional[str] = None, output_dir: str = "results/plots") -> dict[str, object]:
    chosen = definitions()
    names = list(plots) if plots is not None else [name for name, item in chosen.items() if scope == "all" or item.level in {"minimal", scope}]
    generated, skipped, failed = [], {}, {}
    for name in names:
        if name not in chosen:
            skipped[name] = "unknown plot"
            continue
        item = chosen[name]
        ok, reason = item.available(adata)
        if not ok:
            skipped[name] = reason
            continue
        try:
            kwargs = {"output_dir": output_dir}
            if name in {"qc_metrics", "alignment_summary", "pca", "sample_correlation", "de_heatmap", "fragment_profile", "fragment_heatmap"}:
                kwargs["group_col"] = group_col
            item.render(adata, **kwargs)
            generated.append(name)
        except Exception as exc:  # Batch generation reports independent failures without rerunning analyses.
            failed[name] = str(exc)
    return {"generated": generated, "skipped": skipped, "failed": failed}


__all__ = ["PlotDefinition", "available_plots", "generate_plots", "definitions"]
