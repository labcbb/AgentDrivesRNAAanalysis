"""AnnData-compatible gene-set enrichment through GSEApy Enrichr."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd
from anndata import AnnData

from ..._registry import register_function


ENRICHR_UNS_KEY = "enrichr"
DEFAULT_GENE_SET = None
DEFAULT_GMT_DIR = Path("references/msigdb")
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


def _result_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError("result_key must be a non-empty AnnData uns key")
    return key


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


def _resolve_local_gene_sets(gene_sets: Any) -> tuple[Any, List[str]]:
    """Resolve local GMT/dict inputs without allowing an Enrichr API library."""
    if gene_sets is None:
        files = sorted(DEFAULT_GMT_DIR.expanduser().glob("*.symbols.gmt"))
        if not files:
            files = sorted(DEFAULT_GMT_DIR.expanduser().glob("*.gmt"))
        preferred = [path for path in files if "kegg" in path.name.lower()]
        preferred += [path for path in files if "go.bp" in path.name.lower() and path not in preferred]
        files = preferred or files
        if not files:
            raise FileNotFoundError(
                f"No local MSigDB GMT found under {DEFAULT_GMT_DIR}. "
                "Run sa.download.download_msigdb(...) first or pass gene_sets='/path/to/file.gmt'."
            )
        gene_sets = str(files[0])

    if isinstance(gene_sets, Mapping):
        return gene_sets, ["<mapping>"]
    values = [gene_sets] if isinstance(gene_sets, (str, Path)) else list(gene_sets)
    paths = [Path(value).expanduser() for value in values]
    if not paths or any(path.suffix.lower() != ".gmt" for path in paths):
        raise ValueError(
            "Local enrichment requires a .gmt file path or a gene-set mapping; "
            "Enrichr library names such as 'KEGG_2016' are not accepted."
        )
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Local GMT file not found: {', '.join(missing)}")
    resolved = [str(path.resolve()) for path in paths]
    return resolved, resolved


@register_function(
    aliases=[
        "enrichr", "enrichr_enrichment", "gene_set_enrichment", "pathway_enrichment",
        "基因富集", "通路富集", "Enrichr富集分析",
    ],
    category="target",
    description=(
        "Run GSEApy's local GMT Enrichr-compatible enrichment over a supplied gene list and store the result table "
        "and full query metadata in adata.uns['enrichr']. A downloaded GMT under references/msigdb is used by "
        "default. Human/Mouse capitalization and common aliases are retained as metadata. Reuses the stored result "
        "for an unchanged query unless "
        "force=True."
    ),
    examples=[
        "adata = sa.target.enrichr(adata, ['TP53', 'BRCA1', 'EGFR'], gene_sets='references/msigdb/c2.cp.kegg_legacy.symbols.gmt')",
        "adata = sa.target.enrichr(adata, genes, gene_sets='references/msigdb/custom.symbols.gmt', organism='Human')",
    ],
    related=[
        "target.starbase_mirna_targets",
        "target.miranda",
        "target.seed_utr_matches",
        "download.download_msigdb",
    ],
    produces={"uns": [ENRICHR_UNS_KEY]},
)
def enrichr(
    adata: AnnData,
    genes: str | Sequence[str],
    *,
    gene_sets: Optional[str | Sequence[str] | Mapping[str, Any]] = DEFAULT_GENE_SET,
    organism: str = "human",
    background: Optional[str | int | Sequence[str]] = None,
    cutoff: float = 0.05,
    force: bool = False,
    result_key: str = ENRICHR_UNS_KEY,
) -> AnnData:
    """Run GSEApy's offline Enrichr-compatible enrichment and store the result in ``adata.uns``.

    ``genes`` accepts one symbol or a sequence. ``gene_sets`` must be a local
    ``.gmt`` path or a gene-set mapping. If omitted, a downloaded GMT under
    ``references/msigdb`` is selected. Enrichr library names are deliberately
    rejected so this function cannot access the online Enrichr service. Set
    ``result_key`` to preserve multiple enrichment contrasts in one AnnData.
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")
    if float(cutoff) < 0 or float(cutoff) > 1:
        raise ValueError("cutoff must be between 0 and 1")
    state_key = _result_key(result_key)

    selected_genes = _normalise_genes(genes)
    normalized_organism = _normalise_organism(organism)
    local_gene_sets, gene_set_metadata = _resolve_local_gene_sets(gene_sets)
    signature = _run_signature(selected_genes, gene_set_metadata, normalized_organism, background, float(cutoff))
    existing = adata.uns.get(state_key)
    if isinstance(existing, Mapping) and not force and existing.get("signature") == signature and "results" in existing:
        state = dict(existing)
        last_run = dict(state.get("last_run") or {})
        last_run["reused"] = True
        last_run["completed_at"] = _utc_now()
        state["last_run"] = last_run
        adata.uns[state_key] = state
        return adata

    gp = _load_gseapy()
    try:
        run = gp.enrichr(
            gene_list=selected_genes,
            gene_sets=local_gene_sets,
            organism=normalized_organism,
            outdir=None,
            background=background,
            cutoff=float(cutoff),
            no_plot=True,
            verbose=False,
        )
    except Exception as exc:  # noqa: BLE001 - retain local GSEApy failure context
        raise RuntimeError(f"Local GMT enrichment failed: {exc}") from exc
    results = getattr(run, "results", None)
    if not isinstance(results, pd.DataFrame):
        raise RuntimeError("GSEApy Enrichr completed without returning a results DataFrame")

    result_table = results.copy()
    parameters: Dict[str, Any] = {
        "gene_sets": gene_set_metadata,
        "organism": normalized_organism,
        "background": background if isinstance(background, (str, int)) or background is None else list(background),
        "cutoff": float(cutoff),
        "result_key": state_key,
    }
    adata.uns[state_key] = {
        "tool": "gseapy.enrichr.local_gmt",
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
