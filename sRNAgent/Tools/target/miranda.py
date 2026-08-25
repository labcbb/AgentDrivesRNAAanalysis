"""Local miRanda miRNA/isomiR target prediction with AnnData persistence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd
from anndata import AnnData

from ..._registry import register_function
from ..._utils import run_cli_cmd
from .candidates import TARGET_CANDIDATES_UNS_KEY, resolve_target_features


MIRANDA_UNS_KEY = "miranda_targets"
_SCORE_RE = re.compile(r"(?:Scores?\s+for\s+this\s+duplex|Score)\s*:\s*([-+]?\d+(?:\.\d+)?)", re.I)
_ENERGY_RE = re.compile(r"Energy\s*:\s*([-+]?\d+(?:\.\d+)?)", re.I)
_READ_RE = re.compile(r"^\s*Read\s+Sequence\s*:\s*(\S+)", re.I)
_QUERY_RE = re.compile(r"^\s*Query\s*:\s*(\S+)", re.I)
_TARGET_RE = re.compile(r"^\s*>>\s*(\S+)\s*$", re.I)
# miRanda 3.3a writes its authoritative per-hit result as a single tabular
# row after ``Scores for this hit:``.  It is prefixed with one ``>``; the
# later ``>>`` line is only a scan summary and must not be parsed as a target.
_HIT_ROW_RE = re.compile(
    r"^>(?!>)\s*(\S+)\s+(\S+)\s+([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)"
)
_SCAN_RE = re.compile(r"^\s*Performing\s+Scan\s*:\s*(\S+)\s+vs\s+(\S+)", re.I)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _result_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError("result_key must be a non-empty AnnData uns key")
    return key


def _validate_fasta(path: str | Path, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} FASTA not found: {resolved}")
    with resolved.open("rt", encoding="utf-8") as handle:
        has_header = any(line.lstrip().startswith(">") for line in handle)
    if not has_header:
        raise ValueError(f"{label} is not a FASTA file: {resolved}")
    return resolved


def _fasta_metadata(path: Path) -> Dict[str, Dict[str, str]]:
    """Index first-token FASTA IDs and transcript|gene headers for annotation."""
    metadata: Dict[str, Dict[str, str]] = {}
    with path.open("rt", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line.startswith(">"):
                continue
            header = line[1:].split(None, 1)[0]
            fields = header.split("|")
            transcript = fields[0]
            gene = ""
            for field in fields[1:]:
                key, separator, value = field.partition("=")
                if separator and key.casefold() in {"gene", "gene_name", "symbol"}:
                    gene = value.strip()
                    break
            metadata[header] = {"transcript_id": transcript, "gene_name": gene}
    return metadata


def _fasta_query_ids(path: Path) -> list[str]:
    """Return stable first-token IDs from a miRNA/isomiR query FASTA."""
    identifiers: list[str] = []
    with path.open("rt", encoding="utf-8") as handle:
        for raw_line in handle:
            if raw_line.lstrip().startswith(">"):
                identifier = raw_line.lstrip()[1:].split(None, 1)[0].strip()
                if identifier:
                    identifiers.append(identifier)
    return list(dict.fromkeys(identifiers))


def _normalise_target_id(value: str) -> str:
    return str(value or "").strip().strip(">")


def _parse_miranda_output(path: Path, utr_metadata: Mapping[str, Mapping[str, str]]) -> pd.DataFrame:
    """Parse miRanda duplex blocks while preserving the raw block for audit."""
    columns = ["mirna_id", "target_id", "transcript_id", "gene_name", "score", "energy", "raw_block"]

    # miRanda 3.3a does not use the older ``>> TARGET`` block format. It
    # alternates ``Read Sequence`` lines for the query and reference, then
    # emits one tabular hit row. Parse that row first because it has the exact
    # query, target, score, and energy values. Without this branch the
    # reference transcript is mistaken for the miRNA, and the complete result
    # table cannot be consumed through ``adata.uns['miranda_targets']``.
    tabular_rows: List[Dict[str, Any]] = []
    block: List[str] = []
    with path.open("rt", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            if _SCAN_RE.match(line):
                block = [line]
            elif block:
                block.append(line)
            hit_match = _HIT_ROW_RE.match(line)
            if not hit_match:
                continue
            mirna_id = _normalise_target_id(hit_match.group(1))
            target_id = _normalise_target_id(hit_match.group(2))
            metadata = utr_metadata.get(target_id, {})
            tabular_rows.append({
                "mirna_id": mirna_id,
                "target_id": target_id,
                "transcript_id": str(metadata.get("transcript_id") or target_id),
                "gene_name": str(metadata.get("gene_name") or ""),
                "score": float(hit_match.group(3)),
                "energy": float(hit_match.group(4)),
                "raw_block": "\n".join(block).strip(),
            })
            block = []

    if tabular_rows:
        return pd.DataFrame(tabular_rows, columns=columns)

    # Compatibility with compact/older miRanda output used by prior versions
    # of this tool and by externally supplied result files.
    rows: List[Dict[str, Any]] = []
    current_mirna = ""
    current_target = ""
    block: List[str] = []
    score: Optional[float] = None
    energy: Optional[float] = None

    def flush() -> None:
        nonlocal block, score, energy, current_target
        if not current_mirna and not current_target and score is None and energy is None:
            block = []
            return
        target = _normalise_target_id(current_target)
        metadata = utr_metadata.get(target, {})
        rows.append({
            "mirna_id": current_mirna,
            "target_id": target,
            "transcript_id": str(metadata.get("transcript_id") or target),
            "gene_name": str(metadata.get("gene_name") or ""),
            "score": score,
            "energy": energy,
            "raw_block": "\n".join(block).strip(),
        })
        block = []
        score = None
        energy = None

    with path.open("rt", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            read_match = _READ_RE.match(line)
            target_match = _TARGET_RE.match(line)
            query_match = _QUERY_RE.match(line)
            if read_match:
                if block and (score is not None or energy is not None):
                    flush()
                current_mirna = _normalise_target_id(read_match.group(1))
            elif target_match:
                if block and (score is not None or energy is not None):
                    flush()
                current_target = _normalise_target_id(target_match.group(1))
            elif query_match and not current_mirna:
                current_mirna = _normalise_target_id(query_match.group(1))

            score_match = _SCORE_RE.search(line)
            energy_match = _ENERGY_RE.search(line)
            if score_match:
                score = float(score_match.group(1))
            if energy_match:
                energy = float(energy_match.group(1))
            if line.strip():
                block.append(line)
            if energy_match and score is not None:
                # A standard miRanda record ends after its energy line. Keep
                # the current miRNA for subsequent target records, but reset
                # the target-local values and alignment block.
                flush()

    if block and (score is not None or energy is not None):
        flush()

    return pd.DataFrame(rows, columns=columns)


def parse_miranda_output(path: str | Path, utr_fasta: str | Path) -> pd.DataFrame:
    """Public parser for a miRanda output file and its UTR FASTA metadata."""
    output_path = Path(path).expanduser().resolve()
    if not output_path.is_file():
        raise FileNotFoundError(f"miRanda output not found: {output_path}")
    utr_path = _validate_fasta(utr_fasta, "UTR")
    return _parse_miranda_output(output_path, _fasta_metadata(utr_path))


@register_function(
    aliases=[
        "miranda", "miranda_target_prediction", "miranda_mirna_targets",
        "miRanda靶标预测", "isomiR靶标预测", "isomir_miranda",
    ],
    category="target",
    description=(
        "Run the local miRanda sequence-based target predictor against a miRNA or observed isomiR FASTA "
        "and a transcript-oriented 3' UTR FASTA. Parses score/energy duplex records, adds transcript/gene "
        "metadata from UTR headers, writes a prediction file, and stores the complete table in "
        "adata.uns['miranda_targets']. This is the preferred target method for isomiR sequences; it does not "
        "require a parent miRNA or starBase."
    ),
    examples=[
        "adata = sa.target.miranda(adata, 'results/mirna.fa', 'references/human_3utr.fa', feature_ids=['hsa-miR-21-5p'])",
        "isomir_adata = sa.target.miranda(isomir_adata, 'results/isomir.fa', 'references/human_3utr.fa', score_threshold=140, energy_threshold=-20)",
    ],
    related=["target.starbase_mirna_targets", "target.seed_utr_matches", "reference.extract_three_prime_utr"],
    produces={"uns": [MIRANDA_UNS_KEY]},
)
def miranda(
    adata: AnnData,
    mirna_fasta: str,
    utr_fasta: str,
    output_dir: str = "results/targets/miranda",
    *,
    score_threshold: float = 140.0,
    energy_threshold: float = -20.0,
    executable: str = "miranda",
    extra_args: Optional[Sequence[str]] = None,
    force: bool = False,
    result_key: str = MIRANDA_UNS_KEY,
    feature_ids: Optional[str | Sequence[str]] = None,
    selection_key: str = TARGET_CANDIDATES_UNS_KEY,
    allow_all_features: bool = False,
) -> AnnData:
    """Run miRanda and persist parsed target predictions in ``adata``.

    ``mirna_fasta`` can contain mature miRNAs or observed isomiR sequences.
    For isomiR analysis, pass a FASTA generated from the actual isomiR
    sequences in ``adata.var``; the parent miRNA is not required. Set
    ``result_key`` to retain parent and isomiR predictions in one AnnData.
    """
    if not isinstance(adata, AnnData):
        raise TypeError("adata must be an AnnData object")
    mirna_path = _validate_fasta(mirna_fasta, "miRNA/isomiR")
    utr_path = _validate_fasta(utr_fasta, "UTR")
    state_key = _result_key(result_key)
    selected_features = resolve_target_features(
        adata,
        feature_ids,
        selection_key=selection_key,
        allow_all_features=allow_all_features,
    )
    query_ids = _fasta_query_ids(mirna_path)
    if set(query_ids) != set(selected_features):
        raise ValueError(
            "miRanda query FASTA IDs must exactly equal the selected target candidates; "
            "write a FASTA for the selected feature_ids rather than scanning extra features."
        )
    if float(score_threshold) < 0:
        raise ValueError("score_threshold must be non-negative")
    if extra_args and any(str(arg) in {"-sc", "-en", "-out"} for arg in extra_args):
        raise ValueError("extra_args must not override -sc, -en, or -out")

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / "miranda_predictions.txt"
    parameters = {
        "mirna_fasta": str(mirna_path),
        "mirna_fasta_sha256": _file_digest(mirna_path),
        "utr_fasta": str(utr_path),
        "utr_fasta_sha256": _file_digest(utr_path),
        "score_threshold": float(score_threshold),
        "energy_threshold": float(energy_threshold),
        "executable": str(executable),
        "extra_args": [str(value) for value in (extra_args or [])],
        "feature_ids": selected_features,
        "selection_key": selection_key,
        "allow_all_features": allow_all_features,
    }
    signature = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode("utf-8")).hexdigest()
    existing = adata.uns.get(state_key)
    if (
        not force
        and isinstance(existing, Mapping)
        and existing.get("signature") == signature
        and output_path.is_file()
    ):
        result_table = parse_miranda_output(output_path, utr_path)
        state = dict(existing)
        state["results"] = result_table
        state["last_run"] = {"reused": True, "completed_at": _utc_now(), "n_predictions": len(result_table)}
        adata.uns[state_key] = state
        return adata

    command = [
        str(executable), mirna_path.as_posix(), utr_path.as_posix(),
        "-sc", str(score_threshold), "-en", str(energy_threshold),
        "-out", output_path.as_posix(),
    ]
    command.extend(str(value) for value in (extra_args or []))
    try:
        run_cli_cmd(command)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "miRanda executable was not found. Install miRanda and ensure `miranda` is on PATH, "
            "or pass executable='/path/to/miranda'."
        ) from exc
    if not output_path.is_file():
        raise RuntimeError(f"miRanda completed without creating output: {output_path}")

    result_table = parse_miranda_output(output_path, utr_path)
    adata.uns[state_key] = {
        "tool": "miranda",
        "signature": signature,
        "parameters": parameters,
        "prediction_file": str(output_path),
        "results": result_table,
        "last_run": {
            "reused": False,
            "completed_at": _utc_now(),
            "n_predictions": len(result_table),
            "n_mirnas": int(result_table["mirna_id"].nunique()) if not result_table.empty else 0,
            "n_targets": int(result_table["target_id"].nunique()) if not result_table.empty else 0,
        },
    }
    return adata


__all__ = ["MIRANDA_UNS_KEY", "miranda", "parse_miranda_output"]
