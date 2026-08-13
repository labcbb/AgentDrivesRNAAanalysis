"""starBase/ENCORI miRNA target retrieval with AnnData-backed caching."""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd
from anndata import AnnData

from ..._registry import register_function


STARBASE_MIRNA_TARGET_URL = "https://rnasysu.com/encori/api/miRNATarget/"
STARBASE_UNS_KEY = "starbase_mirna_targets"
SUPPORTED_ASSEMBLIES = {"hg38", "mmu10"}

# The service currently returns these 25 fields without a header. Older
# deployments returned the first 22 fields only, which is handled below.
STARBASE_COLUMNS = [
    "miRNAid", "miRNAname", "geneID", "geneName", "geneType", "chromosome",
    "narrowStart", "narrowEnd", "broadStart", "broadEnd", "strand",
    "clipExpNum", "degraExpNum", "RBP", "PITA", "RNA22", "miRmap", "microT",
    "miRanda", "PicTar", "TargetScan", "TDMDScore", "phyloP", "pancancerNum",
    "cellline/tissue",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _as_mirna_list(mirnas: str | Sequence[str]) -> List[str]:
    raw = [mirnas] if isinstance(mirnas, str) else list(mirnas)
    values = []
    for value in raw:
        values.extend(part.strip() for part in str(value).split(",") if part.strip())
    return list(dict.fromkeys(values))


def _as_frame(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if isinstance(value, Mapping):
        return pd.DataFrame(value)
    if isinstance(value, list):
        return pd.DataFrame(value)
    raise TypeError("Differential-expression results must be a pandas DataFrame, mapping, or list of records")


def _select_de_mirnas(
    adata: AnnData,
    *,
    de_key: str,
    adj_p_max: float,
    abs_logfc_min: float,
    top_n: Optional[int],
) -> List[str]:
    if de_key not in adata.uns:
        raise KeyError(
            f"adata.uns[{de_key!r}] is missing. Supply mirnas explicitly, or run/store miRNA DE results first."
        )
    frame = _as_frame(adata.uns[de_key])
    if frame.empty:
        return []
    if "feature" in frame.columns:
        feature = frame["feature"].astype(str)
    elif "mirna_id" in frame.columns:
        feature = frame["mirna_id"].astype(str)
    else:
        feature = pd.Series(frame.index.astype(str), index=frame.index)
    frame = frame.assign(_feature=feature)
    if "rna_type" in adata.var.columns:
        mirna_names = set(adata.var.index[adata.var["rna_type"].astype(str).str.lower() == "mirna"].astype(str))
        if "mirna_id" in adata.var.columns:
            mirna_names.update(adata.var.loc[adata.var["rna_type"].astype(str).str.lower() == "mirna", "mirna_id"].astype(str))
        frame = frame[frame["_feature"].isin(mirna_names)]
    p_col = next((name for name in ("adj_p_value", "adj_p", "padj", "FDR", "fdr") if name in frame.columns), None)
    if p_col is not None:
        frame = frame[pd.to_numeric(frame[p_col], errors="coerce") <= float(adj_p_max)]
    logfc_col = next((name for name in ("log_fc", "logFC", "log2FoldChange") if name in frame.columns), None)
    if logfc_col is not None and abs_logfc_min > 0:
        frame = frame[pd.to_numeric(frame[logfc_col], errors="coerce").abs() >= float(abs_logfc_min)]
    if p_col is not None:
        frame = frame.sort_values(p_col, kind="stable")
    if top_n is not None:
        frame = frame.head(int(top_n))
    return list(dict.fromkeys(frame["_feature"].astype(str).tolist()))


def _select_var_mirnas(adata: AnnData, feature_filters: Mapping[str, Any]) -> List[str]:
    frame = adata.var.copy()
    if "rna_type" in frame.columns:
        frame = frame[frame["rna_type"].astype(str).str.lower() == "mirna"]
    for column, expected in feature_filters.items():
        if column not in frame.columns:
            raise KeyError(f"adata.var has no column {column!r}")
        choices = expected if isinstance(expected, (list, tuple, set)) else [expected]
        frame = frame[frame[column].isin(choices)]
    if "mirna_id" in frame.columns:
        return list(dict.fromkeys(frame["mirna_id"].astype(str).tolist()))
    return frame.index.astype(str).tolist()


def _parse_starbase_response(payload: str) -> pd.DataFrame:
    content = "\n".join(
        line for line in str(payload or "").splitlines() if line.strip() and not line.startswith("#")
    )
    if not content:
        return pd.DataFrame(columns=STARBASE_COLUMNS)
    first = content.splitlines()[0].split("\t")
    has_header = first and first[0] == "miRNAid"
    frame = pd.read_csv(io.StringIO(content), sep="\t", header=0 if has_header else None, dtype=str)
    if not has_header:
        columns = STARBASE_COLUMNS[:frame.shape[1]]
        if frame.shape[1] > len(columns):
            columns += [f"extra_{index}" for index in range(len(columns) + 1, frame.shape[1] + 1)]
        frame.columns = columns
    return frame


def _fetch_starbase(params: Mapping[str, Any], timeout: int) -> str:
    url = f"{STARBASE_MIRNA_TARGET_URL}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "sRNAgent/0.1 starBase target client"})
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed HTTPS API endpoint
            return response.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - preserve HTTP and network context for the user
        raise RuntimeError(f"starBase target API request failed for {params.get('miRNA')}: {exc}") from exc


def _query_signature(params: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(sorted((str(key), str(value)) for key, value in params.items())), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@register_function(
    aliases=[
        "starbase_mirna_targets", "starbase_target", "encori_target", "mirna_target_prediction",
        "miRNA靶标预测", "starBase靶标分析",
    ],
    category="target",
    description=(
        "Retrieve miRNA-mRNA target sites from the starBase/ENCORI miRNATarget HTTPS API. "
        "Targets are fetched sequentially to avoid burdening the remote service, saved as local TSV files, "
        "and cached with request parameters in adata.uns['starbase_mirna_targets']. With no explicit mirnas, "
        "selects significant miRNAs from adata.uns['de_results']."
    ),
    examples=[
        "adata = sa.target.starbase_mirna_targets(adata, mirnas='hsa-miR-21-5p')",
        "adata = sa.target.starbase_mirna_targets(adata, adj_p_max=0.05, abs_logfc_min=1)",
    ],
    related=["diff.de_analysis", "reference.download_mirtarbase"],
    produces={"uns": [STARBASE_UNS_KEY]},
)
def starbase_mirna_targets(
    adata: AnnData,
    mirnas: Optional[str | Sequence[str]] = None,
    output_dir: str = "results/targets/starbase",
    *,
    assembly: str = "hg38",
    gene_type: str = "mRNA",
    clip_exp_num: int = 5,
    degra_exp_num: int = 1,
    pancancer_num: int = 10,
    program_num: int = 5,
    program: Optional[str] = None,
    target: str = "all",
    cell_type: str = "all",
    de_key: str = "de_results",
    adj_p_max: float = 0.05,
    abs_logfc_min: float = 0.0,
    top_n: Optional[int] = None,
    feature_filters: Optional[Mapping[str, Any]] = None,
    timeout: int = 60,
    request_interval: float = 0.5,
    force: bool = False,
) -> AnnData:
    """Fetch and cache starBase miRNA target predictions for selected miRNAs.

    Explicit ``mirnas`` take priority. Otherwise, ``feature_filters`` selects
    miRNAs from ``adata.var``; otherwise significant miRNAs are selected from
    ``adata.uns[de_key]``. API requests are intentionally serial.
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")
    assembly = str(assembly).lower().strip()
    if assembly not in SUPPORTED_ASSEMBLIES:
        raise ValueError(f"assembly must be one of {sorted(SUPPORTED_ASSEMBLIES)}, got {assembly!r}")
    if request_interval < 0:
        raise ValueError("request_interval must be non-negative")
    if mirnas is not None:
        selected = _as_mirna_list(mirnas)
        selection_source = "explicit"
    elif feature_filters:
        selected = _select_var_mirnas(adata, feature_filters)
        selection_source = "feature_filters"
    else:
        selected = _select_de_mirnas(
            adata,
            de_key=de_key,
            adj_p_max=adj_p_max,
            abs_logfc_min=abs_logfc_min,
            top_n=top_n,
        )
        selection_source = "significant_de"
    if not selected:
        raise ValueError("No miRNAs matched the requested target-selection criteria")

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    state = dict(adata.uns.get(STARBASE_UNS_KEY) or {})
    queries = dict(state.get("queries") or {})
    run_records: List[Dict[str, Any]] = []
    for position, mirna in enumerate(selected):
        params = {
            "assembly": assembly,
            "geneType": gene_type,
            "miRNA": mirna,
            "clipExpNum": int(clip_exp_num),
            "degraExpNum": int(degra_exp_num),
            "pancancerNum": int(pancancer_num),
            "programNum": int(program_num),
            "program": "None" if program is None else str(program),
            "target": str(target),
            "cellType": str(cell_type),
        }
        signature = _query_signature(params)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", mirna).strip("_") or "mirna"
        tsv_path = out_dir / f"{safe_name}.{signature[:12]}.starbase.tsv"
        cached = queries.get(signature) if isinstance(queries.get(signature), Mapping) else {}
        reused = bool(not force and tsv_path.exists())
        if not reused:
            if position:
                time.sleep(float(request_interval))
            payload = _fetch_starbase(params, timeout=int(timeout))
            frame = _parse_starbase_response(payload)
            frame.to_csv(tsv_path, sep="\t", index=False)
        else:
            frame = pd.read_csv(tsv_path, sep="\t", dtype=str)
        record = {
            "miRNA": mirna,
            "parameters": params,
            "tsv": str(tsv_path),
            "n_targets": int(len(frame)),
            "columns": [str(column) for column in frame.columns],
            "fetched_at": str(cached.get("fetched_at") or _utc_now()),
            "reused": reused,
        }
        queries[signature] = {key: value for key, value in record.items() if key != "reused"}
        run_records.append(record)

    state.update({
        "api": "starBase/ENCORI miRNATarget",
        "api_url": STARBASE_MIRNA_TARGET_URL,
        "queries": queries,
        "last_run": {
            "selection_source": selection_source,
            "selected_miRNAs": selected,
            "feature_filters": dict(feature_filters or {}),
            "de_key": de_key if selection_source == "significant_de" else "",
            "adj_p_max": float(adj_p_max),
            "abs_logfc_min": float(abs_logfc_min),
            "records": run_records,
            "completed_at": _utc_now(),
        },
    })
    adata.uns[STARBASE_UNS_KEY] = state
    return adata
