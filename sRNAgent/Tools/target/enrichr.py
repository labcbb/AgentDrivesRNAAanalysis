"""AnnData-compatible gene-set enrichment through GSEApy Enrichr."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd
from anndata import AnnData

from ..._registry import register_function


ENRICHR_UNS_KEY = "enrichr"
DEFAULT_GENE_SET = "KEGG_2016"
_ORGANISM_ALIASES = {
    "human": "human",
    "homo sapiens": "human",
    "homo_sapiens": "human",
    "hs": "human",
    "mouse": "mouse",
    "mus musculus": "mouse",
    "mus_musculus": "mouse",
    "mm": "mouse",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalise_genes(genes: str | Sequence[str]) -> List[str]:
    values = [genes] if isinstance(genes, str) else list(genes)
    cleaned: List[str] = []
    for value in values:
        cleaned.extend(part.strip() for part in str(value).split(",") if part.strip())
    result = list(dict.fromkeys(cleaned))
    if not result:
        raise ValueError("genes must contain at least one non-empty gene identifier")
    return result


def _normalise_organism(organism: str) -> str:
    value = str(organism or "").strip().lower()
    if not value:
        raise ValueError("organism must be provided")
    return _ORGANISM_ALIASES.get(value, value)


def _serialise_gene_sets(gene_sets: str | Sequence[str] | Mapping[str, Any]) -> Any:
    if isinstance(gene_sets, str):
        return gene_sets.strip()
    if isinstance(gene_sets, Mapping):
        return {str(key): gene_sets[key] for key in sorted(gene_sets, key=str)}
    return [str(value) for value in gene_sets]


def _run_signature(
    genes: Sequence[str],
    gene_sets: str | Sequence[str] | Mapping[str, Any],
    organism: str,
    background: Optional[str | int | Sequence[str]],
    cutoff: float,
) -> str:
    if isinstance(background, str) or background is None or isinstance(background, int):
        serialised_background: Any = background
    else:
        serialised_background = [str(value) for value in background]
    payload = {
        "genes": list(genes),
        "gene_sets": _serialise_gene_sets(gene_sets),
        "organism": organism,
        "background": serialised_background,
        "cutoff": float(cutoff),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_gseapy():
    try:
        import gseapy as gp
    except ImportError as exc:
        raise ImportError(
            "GSEApy is required for Enrichr analysis. Install the project's gseapy dependency first."
        ) from exc
    return gp


@register_function(
    aliases=[
        "enrichr", "enrichr_enrichment", "gene_set_enrichment", "pathway_enrichment",
        "基因富集", "通路富集", "Enrichr富集分析",
    ],
    category="target",
    description=(
        "Run GSEApy Enrichr over a supplied gene list and store the result table and full query metadata in "
        "adata.uns['enrichr']. The default is human KEGG_2016. Human/Mouse capitalization and common aliases "
        "are normalized for current GSEApy compatibility. Reuses the stored result for an unchanged query unless "
        "force=True."
    ),
    examples=[
        "adata = sa.target.enrichr(adata, ['TP53', 'BRCA1', 'EGFR'])",
        "adata = sa.target.enrichr(adata, genes, gene_sets='KEGG_2021_Human', organism='Human')",
    ],
    related=["target.starbase_mirna_targets", "download.download_msigdb"],
    produces={"uns": [ENRICHR_UNS_KEY]},
)
def enrichr(
    adata: AnnData,
    genes: str | Sequence[str],
    *,
    gene_sets: str | Sequence[str] | Mapping[str, Any] = DEFAULT_GENE_SET,
    organism: str = "human",
    background: Optional[str | int | Sequence[str]] = None,
    cutoff: float = 0.05,
    force: bool = False,
) -> AnnData:
    """Run Enrichr enrichment and store the result in ``adata.uns``.

    ``genes`` accepts one symbol or a sequence. The default query is human
    ``KEGG_2016``; pass any Enrichr library name for a different collection.
    Common human and mouse aliases are normalized before the GSEApy call.
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")
    if float(cutoff) < 0 or float(cutoff) > 1:
        raise ValueError("cutoff must be between 0 and 1")

    selected_genes = _normalise_genes(genes)
    normalized_organism = _normalise_organism(organism)
    signature = _run_signature(selected_genes, gene_sets, normalized_organism, background, float(cutoff))
    existing = adata.uns.get(ENRICHR_UNS_KEY)
    if isinstance(existing, Mapping) and not force and existing.get("signature") == signature and "results" in existing:
        state = dict(existing)
        last_run = dict(state.get("last_run") or {})
        last_run["reused"] = True
        last_run["completed_at"] = _utc_now()
        state["last_run"] = last_run
        adata.uns[ENRICHR_UNS_KEY] = state
        return adata

    gp = _load_gseapy()
    try:
        run = gp.enrichr(
            gene_list=selected_genes,
            gene_sets=gene_sets,
            organism=normalized_organism,
            outdir=None,
            background=background,
            cutoff=float(cutoff),
            no_plot=True,
            verbose=False,
        )
    except Exception as exc:  # noqa: BLE001 - retain remote Enrichr failure context
        raise RuntimeError(f"Enrichr query failed: {exc}") from exc
    results = getattr(run, "results", None)
    if not isinstance(results, pd.DataFrame):
        raise RuntimeError("GSEApy Enrichr completed without returning a results DataFrame")

    result_table = results.copy()
    parameters: Dict[str, Any] = {
        "gene_sets": _serialise_gene_sets(gene_sets),
        "organism": normalized_organism,
        "background": background if isinstance(background, (str, int)) or background is None else list(background),
        "cutoff": float(cutoff),
    }
    adata.uns[ENRICHR_UNS_KEY] = {
        "tool": "gseapy.enrichr",
        "gseapy_version": str(getattr(gp, "__version__", "unknown")),
        "signature": signature,
        "input_genes": selected_genes,
        "parameters": parameters,
        "results": result_table,
        "last_run": {
            "reused": False,
            "n_input_genes": len(selected_genes),
            "n_terms": len(result_table),
            "completed_at": _utc_now(),
        },
    }
    return adata
