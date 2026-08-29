"""Compare observed isomiR targets with their parent mature-miRNA targets."""

from __future__ import annotations

import hashlib
import gzip
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import numpy as np
import pandas as pd
from anndata import AnnData

from ..._registry import register_function
from .candidates import TARGET_CANDIDATES_UNS_KEY, resolve_target_features
from .miranda import miranda
from .seed import seed_utr_matches


ISOMIR_PARENT_TARGET_COMPARISON_UNS_KEY = "isomir_parent_target_comparison"
ISOMIR_MIRANDA_UNS_KEY = "isomir_miranda_targets"
PARENT_MIRANDA_UNS_KEY = "parent_mirna_miranda_targets"
ISOMIR_SEED_UNS_KEY = "isomir_seed_utr_matches"
PARENT_SEED_UNS_KEY = "parent_mirna_seed_utr_matches"
_RNA_ALPHABET = re.compile(r"[ACGUN]+")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalise_sequence(value: object) -> str:
    sequence = "".join(str(value or "").split()).upper().replace("T", "U")
    return sequence if _RNA_ALPHABET.fullmatch(sequence or "") else ""


def _state_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError("result_key must be a non-empty AnnData uns key")
    return key


def _resolve_var_column(adata: AnnData, requested: str, alternatives: Sequence[str], label: str) -> str:
    if requested in adata.var.columns:
        return requested
    resolved = next((column for column in alternatives if column in adata.var.columns), None)
    if resolved:
        return resolved
    candidates = ", ".join([requested, *alternatives])
    raise KeyError(f"No {label} column was found in adata.var; checked: {candidates}")


def _read_fasta(path: Path) -> Dict[str, str]:
    records: Dict[str, str] = {}
    identifier = ""
    chunks: List[str] = []
    if path.suffix.lower() == ".gz":
        handle = gzip.open(path, "rt", encoding="utf-8")
    else:
        handle = path.open("rt", encoding="utf-8")
    with handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if identifier:
                    sequence = _normalise_sequence("".join(chunks))
                    if sequence:
                        records[identifier] = sequence
                identifier = line[1:].split(None, 1)[0]
                chunks = []
            else:
                chunks.append(line)
    if identifier:
        sequence = _normalise_sequence("".join(chunks))
        if sequence:
            records[identifier] = sequence
    return records


def _write_fasta(path: Path, records: Iterable[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wt", encoding="utf-8") as output:
        for identifier, sequence in records:
            output.write(f">{identifier}\n{sequence}\n")


def _isomir_features(
    adata: AnnData,
    *,
    sequence_col: str,
    parent_mirna_col: str,
    strict: bool,
    feature_ids: Sequence[str],
) -> List[Dict[str, str]]:
    missing: List[str] = []
    rows: List[Dict[str, str]] = []
    for feature_id in feature_ids:
        row = adata.var.loc[feature_id]
        rna_type = str(row.get("rna_type") or "").casefold()
        if rna_type and rna_type != "isomir":
            continue
        parent = str(row.get(parent_mirna_col) or "").strip()
        if parent.casefold() in {"", "nan", "none", "<na>"}:
            parent = ""
        sequence = _normalise_sequence(row.get(sequence_col))
        if len(sequence) < 8 or not parent:
            missing.append(str(feature_id))
            continue
        rows.append({"isomir_id": str(feature_id), "parent_mirna_id": parent, "sequence": sequence})
    if missing and strict:
        preview = ", ".join(missing[:8])
        raise ValueError(
            "Each compared isomiR needs an observed >=8 nt sequence and parent miRNA ID; "
            f"missing or invalid for: {preview}"
        )
    if not rows:
        raise ValueError("No valid isomiR features were available for parent-target comparison")
    return rows


def _mature_sequences(
    parent_ids: Sequence[str], mature_fasta: Path, *, strict: bool,
) -> tuple[Dict[str, str], List[str]]:
    records = _read_fasta(mature_fasta)
    lookup = {identifier.casefold(): sequence for identifier, sequence in records.items()}
    selected: Dict[str, str] = {}
    missing: List[str] = []
    for parent in dict.fromkeys(parent_ids):
        sequence = records.get(parent) or lookup.get(parent.casefold())
        if sequence and len(sequence) >= 8:
            selected[parent] = sequence
        else:
            missing.append(parent)
    if missing and strict:
        raise ValueError(
            "No mature miRBase sequence was found for parent miRNA(s): " + ", ".join(missing[:12])
        )
    return selected, missing


def _gene_evidence(
    frame: pd.DataFrame,
    query_id: str,
    *,
    score_threshold: float,
    energy_threshold: float,
) -> Dict[str, Dict[str, Any]]:
    if frame.empty:
        return {}
    required = {"mirna_id", "gene_name", "score", "energy", "transcript_id"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError("miRanda result is missing required columns: " + ", ".join(sorted(missing)))
    subset = frame.loc[frame["mirna_id"].astype(str) == str(query_id)].copy()
    subset["score"] = pd.to_numeric(subset["score"], errors="coerce")
    subset["energy"] = pd.to_numeric(subset["energy"], errors="coerce")
    subset = subset.loc[
        subset["gene_name"].notna()
        & subset["score"].ge(float(score_threshold))
        & subset["energy"].le(float(energy_threshold))
    ]
    evidence: Dict[str, Dict[str, Any]] = {}
    for gene, group in subset.groupby("gene_name", sort=True):
        name = str(gene).strip()
        if not name or name.casefold() == "nan":
            continue
        evidence[name] = {
            "max_score": float(group["score"].max()),
            "min_energy": float(group["energy"].min()),
            "transcripts": sorted({str(value) for value in group["transcript_id"] if str(value)}),
        }
    return evidence


def _seed_evidence(frame: pd.DataFrame, feature_id: str) -> Dict[str, Dict[str, Any]]:
    if frame.empty:
        return {}
    required = {"feature_id", "gene_name", "transcript_id", "utr_match_start"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError("Seed-match result is missing required columns: " + ", ".join(sorted(missing)))
    subset = frame.loc[frame["feature_id"].astype(str) == str(feature_id)]
    evidence: Dict[str, Dict[str, Any]] = {}
    for gene, group in subset.groupby("gene_name", sort=True):
        name = str(gene).strip()
        if not name or name.casefold() == "nan":
            continue
        sites = {
            (str(row.transcript_id), int(row.utr_match_start))
            for row in group[["transcript_id", "utr_match_start"]].itertuples(index=False)
        }
        evidence[name] = {"sites": sites, "site_count": len(sites)}
    return evidence


def _frame(rows: List[Dict[str, Any]], columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(columns))


@register_function(
    aliases=[
        "compare_isomir_parent_targets", "isomir_parent_target_comparison",
        "isomir_target_change", "isomir靶标变化", "isomir父mirna靶标比较",
    ],
    category="target",
    description=(
        "Compare exact-sequence isomiR and parent mature-miRNA targets with matched local miRanda thresholds "
        "and local 3' UTR seed support. Reads parent IDs from adata.var['mirna_id'], obtains parent sequences "
        "from miRBase mature FASTA, and records conserved, isomiR-gained, and parent-lost target genes."
    ),
    examples=[
        "adata = sa.target.compare_isomir_parent_targets(adata, 'references/mature_hsa.fa', 'references/human_3utr.fa')",
    ],
    related=["target.miranda", "target.seed_utr_matches", "reference.download_mirbase", "target.enrichr"],
    produces={"uns": [ISOMIR_PARENT_TARGET_COMPARISON_UNS_KEY]},
)
def compare_isomir_parent_targets(
    adata: AnnData,
    mature_fasta: str,
    utr_fasta: str,
    output_dir: str = "results/targets/isomir_parent_comparison",
    *,
    score_threshold: float = 140.0,
    energy_threshold: float = -20.0,
    sequence_col: str = "sequence",
    parent_mirna_col: str = "mirna_id",
    executable: str = "miranda",
    strict: bool = True,
    force: bool = False,
    result_key: str = ISOMIR_PARENT_TARGET_COMPARISON_UNS_KEY,
    feature_ids: Optional[str | Sequence[str]] = None,
    selection_key: str = TARGET_CANDIDATES_UNS_KEY,
) -> AnnData:
    """Compare isomiR targets to a same-method parent-miRNA baseline.

    The parent sequence is used only for the comparison baseline. Observed
    isomiR targets always derive from their own ``adata.var[sequence_col]``
    values, never from a parent-miRNA substitution.
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")
    if float(score_threshold) < 0:
        raise ValueError("score_threshold must be non-negative")
    mature_path = Path(mature_fasta).expanduser().resolve()
    if not mature_path.is_file():
        raise FileNotFoundError(f"miRBase mature FASTA not found: {mature_path}")
    utr_path = Path(utr_fasta).expanduser().resolve()
    if not utr_path.is_file():
        raise FileNotFoundError(f"3' UTR FASTA not found: {utr_path}")

    state_key = _state_key(result_key)
    sequence_col = _resolve_var_column(
        adata, sequence_col, ("isomir_sequence", "mature_sequence"), "isomiR sequence",
    )
    parent_mirna_col = _resolve_var_column(
        adata, parent_mirna_col, ("parent_mirna_id", "parent_mirna", "miRNA"), "parent miRNA ID",
    )
    selected_features = resolve_target_features(
        adata, feature_ids, selection_key=selection_key,
    )
    features = _isomir_features(
        adata,
        sequence_col=sequence_col,
        parent_mirna_col=parent_mirna_col,
        strict=bool(strict),
        feature_ids=selected_features,
    )
    parent_sequences, missing_parents = _mature_sequences(
        [row["parent_mirna_id"] for row in features], mature_path, strict=bool(strict),
    )
    features = [row for row in features if row["parent_mirna_id"] in parent_sequences]
    if not features:
        raise ValueError("No isomiR features have a resolvable parent mature-miRNA sequence")

    out_dir = Path(output_dir).expanduser().resolve()
    isomir_fasta = out_dir / "isomir_queries.fa"
    parent_fasta = out_dir / "parent_mirna_queries.fa"
    _write_fasta(isomir_fasta, ((row["isomir_id"], row["sequence"]) for row in features))
    _write_fasta(parent_fasta, parent_sequences.items())
    parent_var = pd.DataFrame(
        {
            "rna_type": "miRNA",
            "mirna_id": list(parent_sequences),
            "sequence": list(parent_sequences.values()),
        },
        index=list(parent_sequences),
    )
    parent_adata = AnnData(
        X=np.zeros((1, len(parent_var)), dtype=float),
        obs=pd.DataFrame(index=["parent_reference"]),
        var=parent_var,
    )
    isomir_ids = [row["isomir_id"] for row in features]
    parent_ids = list(parent_sequences)

    miranda(
        adata,
        str(isomir_fasta),
        str(utr_path),
        output_dir=str(out_dir / "isomir_miranda"),
        score_threshold=score_threshold,
        energy_threshold=energy_threshold,
        executable=executable,
        force=force,
        result_key=ISOMIR_MIRANDA_UNS_KEY,
        feature_ids=isomir_ids,
    )
    miranda(
        parent_adata,
        str(parent_fasta),
        str(utr_path),
        output_dir=str(out_dir / "parent_miranda"),
        score_threshold=score_threshold,
        energy_threshold=energy_threshold,
        executable=executable,
        force=force,
        result_key=PARENT_MIRANDA_UNS_KEY,
        feature_ids=parent_ids,
    )
    adata.uns[PARENT_MIRANDA_UNS_KEY] = parent_adata.uns[PARENT_MIRANDA_UNS_KEY]
    seed_utr_matches(
        adata,
        str(utr_path),
        sequence_col=sequence_col,
        mirna_id_col=parent_mirna_col,
        result_key=ISOMIR_SEED_UNS_KEY,
        feature_ids=isomir_ids,
    )
    seed_utr_matches(
        parent_adata,
        str(utr_path),
        result_key=PARENT_SEED_UNS_KEY,
        feature_ids=parent_ids,
    )
    adata.uns[PARENT_SEED_UNS_KEY] = parent_adata.uns[PARENT_SEED_UNS_KEY]

    isomir_miranda = adata.uns[ISOMIR_MIRANDA_UNS_KEY]["results"]
    parent_miranda = adata.uns[PARENT_MIRANDA_UNS_KEY]["results"]
    isomir_seed_results = adata.uns[ISOMIR_SEED_UNS_KEY]["results"]
    parent_seed_results = adata.uns[PARENT_SEED_UNS_KEY]["results"]

    rows: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    gene_sets: Dict[str, set[str]] = {"conserved": set(), "isomir_gained": set(), "parent_lost": set()}
    for feature in features:
        isomir_id = feature["isomir_id"]
        parent_id = feature["parent_mirna_id"]
        parent_sequence = parent_sequences[parent_id]
        isomir_targets = _gene_evidence(
            isomir_miranda, isomir_id, score_threshold=score_threshold, energy_threshold=energy_threshold,
        )
        parent_targets = _gene_evidence(
            parent_miranda, parent_id, score_threshold=score_threshold, energy_threshold=energy_threshold,
        )
        isomir_seed_sites = _seed_evidence(isomir_seed_results, isomir_id)
        parent_seed_sites = _seed_evidence(parent_seed_results, parent_id)
        isomir_targets = {gene: value for gene, value in isomir_targets.items() if gene in isomir_seed_sites}
        parent_targets = {gene: value for gene, value in parent_targets.items() if gene in parent_seed_sites}
        isomir_seed = feature["sequence"][1:8]
        parent_seed_sequence = parent_sequence[1:8]
        seed_status = "changed" if isomir_seed != parent_seed_sequence else "conserved"
        counts = {"conserved": 0, "isomir_gained": 0, "parent_lost": 0}
        for gene in sorted(set(isomir_targets) | set(parent_targets)):
            in_isomir = gene in isomir_targets
            in_parent = gene in parent_targets
            status = "conserved" if in_isomir and in_parent else "isomir_gained" if in_isomir else "parent_lost"
            counts[status] += 1
            gene_sets[status].add(gene)
            iso_sites = isomir_seed_sites.get(gene, {}).get("sites", set())
            parent_sites = parent_seed_sites.get(gene, {}).get("sites", set())
            rows.append({
                "isomir_id": isomir_id,
                "parent_mirna_id": parent_id,
                "gene_name": gene,
                "target_change": status,
                "isomir_sequence": feature["sequence"],
                "parent_sequence": parent_sequence,
                "isomir_seed": isomir_seed,
                "parent_seed": parent_seed_sequence,
                "seed_status": seed_status,
                "isomir_max_score": isomir_targets.get(gene, {}).get("max_score"),
                "parent_max_score": parent_targets.get(gene, {}).get("max_score"),
                "isomir_min_energy": isomir_targets.get(gene, {}).get("min_energy"),
                "parent_min_energy": parent_targets.get(gene, {}).get("min_energy"),
                "isomir_transcripts": "|".join(isomir_targets.get(gene, {}).get("transcripts", [])),
                "parent_transcripts": "|".join(parent_targets.get(gene, {}).get("transcripts", [])),
                "isomir_seed_sites": len(iso_sites),
                "parent_seed_sites": len(parent_sites),
                "shared_seed_sites": len(iso_sites & parent_sites),
            })
        summaries.append({
            "isomir_id": isomir_id,
            "parent_mirna_id": parent_id,
            "isomir_sequence": feature["sequence"],
            "parent_sequence": parent_sequence,
            "isomir_seed": isomir_seed,
            "parent_seed": parent_seed_sequence,
            "seed_status": seed_status,
            "n_conserved": counts["conserved"],
            "n_isomir_gained": counts["isomir_gained"],
            "n_parent_lost": counts["parent_lost"],
        })

    parameters = {
        "mature_fasta": str(mature_path),
        "utr_fasta": str(utr_path),
        "score_threshold": float(score_threshold),
        "energy_threshold": float(energy_threshold),
        "sequence_col": sequence_col,
        "parent_mirna_col": parent_mirna_col,
        "strict": bool(strict),
        "executable": executable,
        "result_key": state_key,
        "miranda_result_keys": {"isomir": ISOMIR_MIRANDA_UNS_KEY, "parent": PARENT_MIRANDA_UNS_KEY},
        "seed_result_keys": {"isomir": ISOMIR_SEED_UNS_KEY, "parent": PARENT_SEED_UNS_KEY},
    }
    signature = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode("utf-8")).hexdigest()
    adata.uns[state_key] = {
        "tool": "compare_isomir_parent_targets",
        "signature": signature,
        "parameters": parameters,
        "files": {"isomir_fasta": str(isomir_fasta), "parent_fasta": str(parent_fasta)},
        "results": _frame(rows, [
            "isomir_id", "parent_mirna_id", "gene_name", "target_change", "isomir_sequence", "parent_sequence",
            "isomir_seed", "parent_seed", "seed_status", "isomir_max_score", "parent_max_score",
            "isomir_min_energy", "parent_min_energy", "isomir_transcripts", "parent_transcripts",
            "isomir_seed_sites", "parent_seed_sites", "shared_seed_sites",
        ]),
        "summary": _frame(summaries, [
            "isomir_id", "parent_mirna_id", "isomir_sequence", "parent_sequence", "isomir_seed", "parent_seed",
            "seed_status", "n_conserved", "n_isomir_gained", "n_parent_lost",
        ]),
        "gene_sets": {name: sorted(genes) for name, genes in gene_sets.items()},
        "last_run": {
            "completed_at": _utc_now(),
            "n_compared_isomirs": len(features),
            "n_missing_parent_sequences": len(missing_parents),
        },
    }
    return adata


__all__ = [
    "ISOMIR_PARENT_TARGET_COMPARISON_UNS_KEY",
    "ISOMIR_MIRANDA_UNS_KEY",
    "PARENT_MIRANDA_UNS_KEY",
    "ISOMIR_SEED_UNS_KEY",
    "PARENT_SEED_UNS_KEY",
    "compare_isomir_parent_targets",
]
