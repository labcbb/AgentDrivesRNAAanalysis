"""Extract strand-aware three-prime UTR transcript sequences from an annotation."""

from __future__ import annotations

import gzip
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from ..._registry import register_function


_GTF_ATTR_RE = re.compile(r'\s*([^\s;=]+)\s+"([^"]*)"\s*')
_RC_TABLE = str.maketrans("ACGTRYKMSWBDHVNacgtrykmswbdhvn", "TGCAYRMKSWVHDBNtgcayrmkswvhdbn")


def _open_text(path: Path):
    return gzip.open(path, "rt", encoding="utf-8", errors="replace") if path.suffix == ".gz" else path.open(
        "rt", encoding="utf-8", errors="replace"
    )


def _parse_attributes(text: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for part in text.strip().strip(";").split(";"):
        part = part.strip()
        if not part:
            continue
        match = _GTF_ATTR_RE.fullmatch(part)
        if match:
            attrs[match.group(1)] = match.group(2)
            continue
        key, sep, value = part.partition("=")
        if sep:
            attrs[key.strip()] = value.strip().strip('"')
    return attrs


def _first_attr(attrs: Dict[str, str], keys: Iterable[str]) -> str:
    for key in keys:
        value = str(attrs.get(key) or "").strip()
        if value:
            return value
    return ""


def _transcript_id(attrs: Dict[str, str]) -> str:
    value = _first_attr(attrs, ("transcript_id", "transcript", "transcript_name"))
    if value:
        return value
    parent = _first_attr(attrs, ("Parent", "parent"))
    return parent.split(",")[0].strip()


def _gene_name(attrs: Dict[str, str]) -> str:
    """Return a human-readable gene label, never a bare gene identifier."""
    return _first_attr(attrs, ("gene_name", "gene_symbol", "gene", "Name"))


def _reverse_complement(sequence: str) -> str:
    return sequence.translate(_RC_TABLE)[::-1]


def _iter_fasta(path: Path) -> Iterator[Tuple[str, str]]:
    name: Optional[str] = None
    chunks: List[str] = []
    with _open_text(path) as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            if line.startswith(">"):
                if name is not None:
                    yield name, "".join(chunks).upper()
                name = line[1:].split(None, 1)[0]
                chunks = []
            elif name is not None:
                chunks.append(line)
    if name is not None:
        yield name, "".join(chunks).upper()


def _collect_annotation(annotation_path: Path):
    transcripts: Dict[str, Dict[str, object]] = {}
    feature_types: set[str] = set()
    gene_names: Dict[str, str] = {}
    transcript_gene_ids: Dict[str, str] = {}
    with _open_text(annotation_path) as handle:
        for raw in handle:
            if not raw or raw.startswith("#"):
                continue
            fields = raw.rstrip("\n").split("\t")
            if len(fields) < 9:
                continue
            feature = fields[2].strip().lower()
            attrs = _parse_attributes(fields[8])
            if feature == "gene":
                gene_id = _first_attr(attrs, ("gene_id", "ID"))
                gene_label = _gene_name(attrs)
                if gene_id and gene_label:
                    gene_names[gene_id] = gene_label
                continue

            transcript = _transcript_id(attrs)
            if feature in {"transcript", "mrna", "rna"} and transcript:
                parent = _first_attr(attrs, ("gene_id", "gene", "Parent", "parent"))
                if parent:
                    transcript_gene_ids[transcript] = parent.split(",")[0].strip()
                continue

            if feature not in {"utr", "three_prime_utr", "three_prime_utr_region", "3utr", "3'utr", "cds"}:
                continue
            try:
                start, end = int(fields[3]), int(fields[4])
            except ValueError:
                continue
            if start <= 0 or end < start or fields[6] not in {"+", "-"}:
                continue
            if not transcript:
                continue
            record = transcripts.setdefault(
                transcript,
                {
                    "chrom": fields[0],
                    "strand": fields[6],
                    "gene": _gene_name(attrs),
                    "cds": [],
                    "utr": [],
                    "three_prime": [],
                },
            )
            if record["chrom"] != fields[0] or record["strand"] != fields[6]:
                raise ValueError(f"Transcript {transcript!r} has inconsistent chromosome or strand annotations")
            if not record["gene"]:
                record["gene"] = _gene_name(attrs)
            interval = (start, end)
            if feature == "cds":
                record["cds"].append(interval)
            elif feature == "utr":
                feature_types.add("UTR")
                record["utr"].append(interval)
            else:
                feature_types.add("three_prime_utr")
                record["three_prime"].append(interval)

    for transcript, record in transcripts.items():
        gene_id = transcript_gene_ids.get(transcript)
        if gene_id and gene_names.get(gene_id):
            record["gene"] = gene_names[gene_id]
    if not feature_types:
        raise ValueError(
            "Annotation has no UTR or three_prime_utr features in column 3; "
            "cannot extract three-prime UTR sequences."
        )
    return transcripts, sorted(feature_types)


def _three_prime_intervals(record: Dict[str, object]) -> List[Tuple[int, int]]:
    explicit = list(record["three_prime"])
    if explicit:
        return explicit
    utrs = list(record["utr"])
    cds = list(record["cds"])
    if not utrs or not cds:
        return []
    strand = str(record["strand"])
    if strand == "+":
        coding_edge = max(end for _, end in cds)
        return [(max(start, coding_edge + 1), end) for start, end in utrs if end > coding_edge]
    coding_edge = min(start for start, _ in cds)
    return [(start, min(end, coding_edge - 1)) for start, end in utrs if start < coding_edge]


def _header_token(value: str) -> str:
    return re.sub(r"\s+", "_", value.strip())


@register_function(
    aliases=["extract_3utr", "extract_three_prime_utr", "three_prime_utr_fasta", "3utr_fasta"],
    category="reference",
    description=(
        "Extract strand-aware three-prime UTR sequences from a genome FASTA and GTF/GFF annotation. "
        "The output FASTA headers include transcript and gene names."
    ),
    examples=[
        'sa.reference.extract_three_prime_utr("references/GRCh38.primary_assembly.genome.fa", '
        '"references/gencode.annotation.gtf.gz", output_fasta="references/human_3utr.fa")',
    ],
    related=["reference.download_genome", "reference.download_gtf"],
)
def extract_three_prime_utr(
    genome_fasta: str,
    annotation: str,
    output_fasta: str = "three_prime_utr.fa",
) -> Dict[str, object]:
    """Write one spliced, transcript-oriented three-prime UTR per FASTA record.

    ``three_prime_utr`` records are used directly. For annotations exposing only
    generic ``UTR`` records, the CDS boundary and strand determine which UTR
    intervals are three-prime. Transcripts without an explicit three-prime UTR
    and without CDS coordinates are omitted because their UTR orientation is
    ambiguous.
    """
    fasta_path = Path(genome_fasta)
    annotation_path = Path(annotation)
    output_path = Path(output_fasta)
    if not fasta_path.is_file():
        raise FileNotFoundError(f"Genome FASTA not found: {fasta_path}")
    if not annotation_path.is_file():
        raise FileNotFoundError(f"Annotation file not found: {annotation_path}")

    transcripts, source_features = _collect_annotation(annotation_path)
    by_chrom: Dict[str, List[Tuple[str, Dict[str, object], List[Tuple[int, int]]]]] = defaultdict(list)
    skipped_ambiguous = 0
    for transcript, record in transcripts.items():
        intervals = [(start, end) for start, end in _three_prime_intervals(record) if start <= end]
        if not intervals:
            skipped_ambiguous += 1
            continue
        by_chrom[str(record["chrom"])].append((transcript, record, intervals))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    missing_chromosomes = set(by_chrom)
    seen_headers: Dict[str, int] = defaultdict(int)
    with output_path.open("wt", encoding="utf-8") as output:
        for chrom, sequence in _iter_fasta(fasta_path):
            entries = by_chrom.get(chrom)
            if not entries:
                continue
            missing_chromosomes.discard(chrom)
            for transcript, record, intervals in entries:
                if any(end > len(sequence) for _, end in intervals):
                    raise ValueError(
                        f"{transcript!r} has a UTR interval beyond the end of {chrom!r} "
                        f"({len(sequence):,} bases)."
                    )
                # Build in genomic order, then reverse-complement the spliced
                # sequence for negative-strand transcripts.
                ordered = sorted(intervals)
                parts = [sequence[start - 1:end] for start, end in ordered]
                utr_sequence = "".join(parts)
                if str(record["strand"]) == "-":
                    utr_sequence = _reverse_complement(utr_sequence)
                if not utr_sequence:
                    continue
                gene = _header_token(str(record["gene"]) or "unknown_gene")
                base_header = f"{_header_token(transcript)}|gene={gene}"
                seen_headers[base_header] += 1
                header = base_header if seen_headers[base_header] == 1 else f"{base_header}|copy={seen_headers[base_header]}"
                output.write(f">{header}\n")
                for offset in range(0, len(utr_sequence), 60):
                    output.write(f"{utr_sequence[offset:offset + 60]}\n")
                written += 1

    if missing_chromosomes:
        missing = ", ".join(sorted(missing_chromosomes)[:8])
        raise ValueError(f"Genome FASTA is missing annotation chromosomes: {missing}")
    if written == 0:
        raise ValueError("No unambiguous three-prime UTR sequences were extracted")
    return {
        "fasta": str(output_path),
        "transcripts": written,
        "source_features": source_features,
        "skipped_ambiguous_transcripts": skipped_ambiguous,
    }
