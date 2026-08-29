"""Validate miRNA/isomiR seed matches in 3' UTR sequences."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import pandas as pd
from anndata import AnnData
from Bio.Seq import Seq

from ..._registry import register_function
from .candidates import TARGET_CANDIDATES_UNS_KEY, resolve_target_features


SEED_UTR_UNS_KEY = "seed_utr_matches"
STARBASE_UNS_KEY = "starbase_mirna_targets"
_RNA_TYPES = {"mirna", "isomir"}


def _dna_sequence(value: object) -> str:
    """Normalize RNA/DNA input for a DNA 3' UTR FASTA comparison."""
    return "".join(str(value or "").split()).upper().replace("U", "T")


def _result_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError("result_key must be a non-empty AnnData uns key")
    return key


def find_seed_matches(mirna_seq: str, utr_seq: str) -> List[int]:
    """Return all zero-based 7-mer seed-complement positions in a 3' UTR.

    The seed is miRNA positions 2-8 (one-based). RNA ``U`` and DNA ``T`` are
    treated equivalently because extracted UTR FASTA records use DNA alphabet.
    Overlapping matches are retained.
    """
    mirna = _dna_sequence(mirna_seq)
    utr = _dna_sequence(utr_seq)
    if len(mirna) < 8:
        raise ValueError("miRNA/isomiR sequence must contain at least 8 nucleotides for a 7-mer seed")
    if not re.fullmatch(r"[ACGTN]+", mirna) or not re.fullmatch(r"[ACGTN]+", utr):
        raise ValueError("miRNA/isomiR and 3' UTR sequences must use A/C/G/T/U/N alphabet")

    seed = mirna[1:8]
    seed_rc = str(Seq(seed).reverse_complement())
    matches: List[int] = []
    start = 0
    while True:
        position = utr.find(seed_rc, start)
        if position < 0:
            break
        matches.append(position)
        start = position + 1
    return matches


def _iter_fasta(path: Path) -> Iterator[Tuple[str, str]]:
    header = ""
    chunks: List[str] = []
    with path.open("rt", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if header:
                    yield header, _dna_sequence("".join(chunks))
                header = line[1:].split(None, 1)[0]
                chunks = []
            elif header:
                chunks.append(line)
    if header:
        yield header, _dna_sequence("".join(chunks))


def _utr_header(header: str) -> Tuple[str, str]:
    """Parse the ``transcript|gene=<gene>`` headers made by extract_3utr."""
    tokens = str(header).split("|")
    transcript = tokens[0].strip()
    gene = ""
    for token in tokens[1:]:
        key, separator, value = token.partition("=")
        if separator and key.lower() in {"gene", "gene_name", "symbol"}:
            gene = value.strip()
            break
    return transcript, gene


def _target_edges(adata: AnnData, target_key: str) -> Dict[str, set[str]]:
    state = adata.uns.get(target_key)
    if not isinstance(state, Mapping):
        raise KeyError(f"adata.uns[{target_key!r}] is missing; run starbase_mirna_targets first")
    last_run = state.get("last_run") if isinstance(state.get("last_run"), Mapping) else {}
    records = last_run.get("records") if isinstance(last_run.get("records"), list) else []
    if not records:
        records = list((state.get("queries") or {}).values()) if isinstance(state.get("queries"), Mapping) else []

    edges: Dict[str, set[str]] = {}
    seen_paths: set[Path] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        path = Path(str(record.get("tsv") or "")).expanduser()
        if not path.is_file() or path.resolve() in seen_paths:
            continue
        seen_paths.add(path.resolve())
        frame = pd.read_csv(path, sep="\t", dtype=str)
        gene_column = "geneName" if "geneName" in frame.columns else "geneID" if "geneID" in frame.columns else None
        if gene_column is None:
            continue
        default_mirna = str(record.get("miRNA") or "").strip()
        mirna_column = "miRNAname" if "miRNAname" in frame.columns else None
        for _, row in frame.iterrows():
            mirna = str(row.get(mirna_column) if mirna_column else default_mirna).strip()
            gene = str(row.get(gene_column) or "").strip()
            if mirna and gene and gene.lower() != "nan":
                edges.setdefault(mirna.casefold(), set()).add(gene.casefold())
    if not edges:
        raise KeyError("No readable starBase target TSV records were found; run starbase_mirna_targets first")
    return edges


def _feature_rows(
    adata: AnnData,
    sequence_col: str,
    mirna_id_col: str,
    feature_ids: Sequence[str],
) -> List[Dict[str, str]]:
    if sequence_col not in adata.var.columns:
        raise KeyError(f"adata.var[{sequence_col!r}] is missing; quantify miRNA/isomiR with sequence annotation first")
    rows: List[Dict[str, str]] = []
    for feature_id in feature_ids:
        row = adata.var.loc[feature_id]
        rna_type = str(row.get("rna_type") or "").casefold()
        if rna_type and rna_type not in _RNA_TYPES:
            continue
        sequence = _dna_sequence(row.get(sequence_col))
        if len(sequence) < 8 or not re.fullmatch(r"[ACGTN]+", sequence):
            continue
        parent_mirna = str(row.get(mirna_id_col) or "").strip()
        if parent_mirna.lower() == "nan":
            parent_mirna = ""
        rows.append({"feature_id": str(feature_id), "mirna_id": parent_mirna, "sequence": sequence})
    if not rows:
        raise ValueError(f"No miRNA/isomiR features with an >=8 nt sequence were found in adata.var[{sequence_col!r}]")
    return rows


@register_function(
    aliases=["seed_utr_matches", "mirna_seed_matches", "validate_seed_targets", "miRNA_seed_3utr", "miRNA种子匹配"],
    category="target",
    description=(
        "Find quantified miRNA/isomiR 7-mer seed-complement sites in a local 3' UTR FASTA. "
        "By default scans all UTRs from each observed sequence, so isomiR does not require a parent miRNA "
        "or starBase. Set restrict_to_cached_targets=True for optional starBase validation."
    ),
    examples=[
        "adata = sa.target.seed_utr_matches(adata, 'references/human_3utr.fa', feature_ids=['hsa-miR-21-5p'])",
        "isomir_adata = sa.target.seed_utr_matches(isomir_adata, 'references/human_3utr.fa')",
        "validated = sa.target.seed_utr_matches(adata, 'references/human_3utr.fa', target_key='starbase_mirna_targets', restrict_to_cached_targets=True)",
    ],
    related=[
        "target.starbase_mirna_targets",
        "target.miranda",
        "reference.extract_three_prime_utr",
    ],
    produces={"uns": [SEED_UTR_UNS_KEY]},
)
def seed_utr_matches(
    adata: AnnData,
    utr_fasta: str,
    *,
    target_key: Optional[str] = None,
    restrict_to_cached_targets: bool = False,
    sequence_col: str = "sequence",
    mirna_id_col: str = "mirna_id",
    result_key: str = SEED_UTR_UNS_KEY,
    feature_ids: Optional[str | Sequence[str]] = None,
    selection_key: str = TARGET_CANDIDATES_UNS_KEY,
    allow_all_features: bool = False,
) -> AnnData:
    """Store exact 7-mer seed-complement sites in local 3' UTR records.

    The default is a direct sequence-only scan of the persisted target
    candidate set. This is the intended mode for isomiR, whose sequence is
    sufficient and whose parent miRNA is optional. Pass ``feature_ids`` for a
    documented explicit selection; full-matrix scans require
    ``allow_all_features=True`` after user approval.
    ``restrict_to_cached_targets=True`` instead intersects hits with cached
    starBase TSV targets. Set ``result_key`` to retain parent-miRNA and
    observed-isomiR seed scans in one AnnData.
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")
    fasta_path = Path(utr_fasta).expanduser().resolve()
    state_key = _result_key(result_key)
    if not fasta_path.is_file():
        raise FileNotFoundError(f"3' UTR FASTA not found: {fasta_path}")

    if restrict_to_cached_targets and not target_key:
        raise ValueError("target_key is required when restrict_to_cached_targets=True")
    targets = _target_edges(adata, str(target_key)) if restrict_to_cached_targets else {}
    selected_features = resolve_target_features(
        adata,
        feature_ids,
        selection_key=selection_key,
        allow_all_features=allow_all_features,
    )
    features = _feature_rows(adata, sequence_col, mirna_id_col, selected_features)
    utr_by_gene: Dict[str, List[Tuple[str, str, str]]] = {}
    for header, utr_sequence in _iter_fasta(fasta_path):
        transcript, gene = _utr_header(header)
        if transcript and gene and utr_sequence:
            utr_by_gene.setdefault(gene.casefold(), []).append((transcript, gene, utr_sequence))

    rows: List[Dict[str, object]] = []
    summaries: List[Dict[str, object]] = []
    for feature in features:
        parent = feature["mirna_id"]
        predicted_genes = targets.get(parent.casefold(), set()) if restrict_to_cached_targets and parent else set()
        seed = feature["sequence"][1:8]
        seed_rc = str(Seq(seed).reverse_complement())
        checked_transcripts = 0
        matched_transcripts: set[str] = set()
        feature_sites = 0
        utr_records = (
            [record for gene_key in predicted_genes for record in utr_by_gene.get(gene_key, [])]
            if restrict_to_cached_targets
            else [record for records in utr_by_gene.values() for record in records]
        )
        for transcript, gene, utr_sequence in utr_records:
            checked_transcripts += 1
            positions = find_seed_matches(feature["sequence"], utr_sequence)
            if positions:
                matched_transcripts.add(transcript)
            for position in positions:
                feature_sites += 1
                rows.append({
                    "feature_id": feature["feature_id"],
                    "mirna_id": parent,
                    "sequence": feature["sequence"].replace("T", "U"),
                    "seed_sequence": seed.replace("T", "U"),
                    "seed_reverse_complement": seed_rc,
                    "gene_name": gene,
                    "transcript_id": transcript,
                    "utr_match_start": position,
                    "utr_match_end": position + len(seed_rc),
                    "utr_match_start_1based": position + 1,
                })
        summaries.append({
            "feature_id": feature["feature_id"],
            "mirna_id": parent,
            "predicted_genes": len(predicted_genes) if restrict_to_cached_targets else None,
            "scanned_utr_transcripts": checked_transcripts,
            "predicted_utr_transcripts": checked_transcripts,
            "matched_transcripts": len(matched_transcripts),
            "seed_match_sites": feature_sites,
        })

    parameters = {
        "utr_fasta": str(fasta_path),
        "target_key": target_key,
        "restrict_to_cached_targets": restrict_to_cached_targets,
        "sequence_col": sequence_col,
        "mirna_id_col": mirna_id_col,
        "result_key": state_key,
        "feature_ids": selected_features,
        "selection_key": selection_key,
        "allow_all_features": allow_all_features,
        "seed_definition": "miRNA positions 2-8 (7-mer); exact reverse-complement match; U/T normalized",
        "mode": "cached_starbase_validation" if restrict_to_cached_targets else "direct_all_utr_scan",
    }
    signature = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode("utf-8")).hexdigest()
    summary_frame = pd.DataFrame(summaries)
    if "predicted_genes" in summary_frame.columns:
        # Direct all-UTR scans have no cached target-set size. Store that as
        # floating NaN rather than an object column containing ``None`` so the
        # state remains serializable in H5AD.
        summary_frame["predicted_genes"] = pd.to_numeric(summary_frame["predicted_genes"], errors="coerce")
    adata.uns[state_key] = {
        "tool": "seed_utr_matches",
        "signature": signature,
        "parameters": parameters,
        "results": pd.DataFrame(rows),
        "summary": summary_frame,
    }
    return adata


__all__ = ["find_seed_matches", "seed_utr_matches"]
