"""miRBase reference data download and species extraction.

Wraps `miRBase <https://www.mirbase.org/download/>`_ for downloading
all-species miRNA hairpin / mature FASTA files and species-specific GFF3
annotations, with automatic extraction of per-species sequences.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Dict, List, Optional

from ..._registry import register_function
from .util import resumable_download


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIRBASE_BASE = "https://www.mirbase.org/download"

# miRBase's download host is occasionally unreachable from compute nodes.  The
# BioBricks DVC objects below are content-addressed mirrors of miRBase 22.1.
HAIRPIN_URLS = (
    "https://ins-dvc.s3.amazonaws.com/insdvc/files/md5/af/ade358ab4bd799414a0ae1948defbd",
    f"{MIRBASE_BASE}/hairpin.fa",
)
MATURE_URLS = (
    "https://ins-dvc.s3.amazonaws.com/insdvc/files/md5/c0/fbc0ae2aa8241afeae4b89fea9ed0f",
    f"{MIRBASE_BASE}/mature.fa",
)
GFF3_URLS = (
    "https://mirbase.org/ftp/22.1/genomes/{code}.gff3",
    "https://www.mirbase.org/ftp/22.1/genomes/{code}.gff3",
    f"{MIRBASE_BASE}/{{code}}.gff3",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_gzip(path: Path) -> bool:
    """Check if a file has valid gzip magic bytes."""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def _open_fasta(path: Path, mode: str = "rt", **kwargs):
    """Open a FASTA file — handles .gz extension even if not actual gzip."""
    if str(path).endswith(".gz") and not _is_gzip(path):
        return open(path, mode, **kwargs)
    return gzip.open(path, mode, **kwargs)


def _extract_fasta_by_prefix(
    input_path: Path,
    output_path: Path,
    prefix: str,
) -> int:
    """Extract sequences whose header starts with ``>{prefix}-``.

    Returns the number of sequences extracted.
    """
    opener = _open_fasta if str(input_path).endswith(".gz") else open
    count = 0
    write_mode = False

    with opener(input_path, "rt", errors="replace") as f_in, \
         open(output_path, "w") as f_out:
        for line in f_in:
            if line.startswith(">"):
                write_mode = line[1:].startswith(f"{prefix}-")
                if write_mode:
                    count += 1
                    # Keep only the first identifier (before any space)
                    # e.g. ">hsa-let-7a-5p MIMAT... Homo sapiens..." → ">hsa-let-7a-5p"
                    ident = line[1:].split(None, 1)[0]
                    f_out.write(f">{ident}\n")
            elif write_mode:
                f_out.write(line)

    return count


def _scan_species_codes(fasta_path: Path) -> List[str]:
    """Scan a miRBase FASTA file and extract all unique 3-letter species codes."""
    codes: set[str] = set()
    opener = _open_fasta if str(fasta_path).endswith(".gz") else open

    with opener(fasta_path, "rt", errors="replace") as f:
        for line in f:
            if line.startswith(">"):
                # Header format: >hsa-let-7a-1 ...
                rest = line[1:].strip()
                code = rest.split("-")[0]
                if len(code) == 3 and code.isalpha() and code.islower():
                    codes.add(code)

    return sorted(codes)


def _download_from_sources(
    urls: tuple[str, ...],
    destination: Path,
    *,
    jobs: int,
    force: bool,
) -> None:
    """Download from the first working source, preserving the last error."""
    last_error: Exception | None = None
    for url in urls:
        try:
            resumable_download(url, destination, jobs=jobs, force=force)
            return
        except Exception as exc:
            last_error = exc

    assert last_error is not None
    raise RuntimeError(
        f"Failed to download miRBase data from {len(urls)} sources"
    ) from last_error


def _read_fasta_sequences(path: Path) -> Dict[str, str]:
    """Read FASTA records as DNA strings keyed by the first header token."""
    records: Dict[str, str] = {}
    identifier = ""
    chunks: List[str] = []
    with open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if identifier:
                    records[identifier] = "".join(chunks).upper().replace("U", "T")
                identifier = line[1:].split()[0]
                chunks = []
            else:
                chunks.append(line)
    if identifier:
        records[identifier] = "".join(chunks).upper().replace("U", "T")
    return {name: sequence for name, sequence in records.items() if sequence}


def _parse_gff_attributes(text: str) -> Dict[str, str]:
    return {
        key: value
        for item in text.split(";")
        if "=" in item
        for key, value in [item.split("=", 1)]
    }


def _mirtop_gff_issues(gff_path: Path, hairpin_ids: set[str]) -> List[str]:
    """Return semantic problems that make a GFF3 unusable by mirtop."""
    primary_ids: set[str] = set()
    mature_rows: List[tuple[str, Dict[str, str]]] = []
    malformed = 0

    with open(gff_path, encoding="utf-8") as handle:
        for raw_line in handle:
            if raw_line.startswith("#") or not raw_line.strip():
                continue
            fields = raw_line.rstrip("\n").split("\t")
            if len(fields) != 9:
                malformed += 1
                continue
            attrs = _parse_gff_attributes(fields[8])
            if fields[2] == "miRNA_primary_transcript":
                identifier = attrs.get("ID")
                if identifier:
                    primary_ids.add(identifier)
            elif fields[2] == "miRNA":
                mature_rows.append((fields[0], attrs))

    issues: List[str] = []
    if malformed:
        issues.append(f"{malformed} malformed GFF3 rows")
    if primary_ids != hairpin_ids:
        issues.append(
            "precursor IDs do not exactly match the hairpin FASTA headers "
            f"({len(primary_ids)} vs {len(hairpin_ids)})"
        )
    if not mature_rows:
        issues.append("no mature miRNA feature rows")
    else:
        broken = [
            attrs.get("Derives_from", "")
            for _seqid, attrs in mature_rows
            if not attrs.get("Name")
            or attrs.get("Derives_from") not in primary_ids
            or _seqid != attrs.get("Derives_from")
        ]
        if broken:
            issues.append(
                f"{len(broken)} mature miRNA rows have a missing or invalid Derives_from precursor"
            )
    return issues


def _write_mirtop_gff3(
    output_path: Path,
    hairpins: Dict[str, str],
    mature: Dict[str, str],
) -> Dict[str, int]:
    """Build a hairpin-coordinate GFF3 with valid mature-to-precursor links."""
    primary_lines: List[str] = ["##gff-version 3", "##source sRNAgent miRBase mirtop reference"]
    mature_lines: List[str] = []
    unmapped = 0
    ambiguous = 0
    mapping_index = 0

    for hairpin_id, sequence in hairpins.items():
        primary_lines.append(f"##sequence-region {hairpin_id} 1 {len(sequence)}")
        primary_lines.append(
            f"{hairpin_id}\tsRNAgent\tmiRNA_primary_transcript\t1\t{len(sequence)}\t.\t+\t."
            f"\tID={hairpin_id};Name={hairpin_id}"
        )

    for mature_id, mature_sequence in mature.items():
        mapped_here = 0
        for hairpin_id, hairpin_sequence in hairpins.items():
            positions: List[int] = []
            start = 0
            while True:
                position = hairpin_sequence.find(mature_sequence, start)
                if position < 0:
                    break
                positions.append(position)
                start = position + 1
            if not positions:
                continue
            if len(positions) > 1:
                ambiguous += 1
            # mirtop stores one coordinate per (precursor, mature name). The
            # earliest exact occurrence is deterministic and avoids overwriting
            # that key in mirtop's parser.
            position = positions[0]
            mapping_index += 1
            mapped_here += 1
            start_1based = position + 1
            end_1based = position + len(mature_sequence)
            mature_lines.append(
                f"{hairpin_id}\tsRNAgent\tmiRNA\t{start_1based}\t{end_1based}\t.\t+\t."
                f"\tID={mature_id}.on.{hairpin_id}.{mapping_index};Name={mature_id};"
                f"Derives_from={hairpin_id}"
            )
        if not mapped_here:
            unmapped += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join([*primary_lines, *mature_lines, ""]), encoding="utf-8")
    return {
        "hairpins": len(hairpins),
        "mature_mirnas": len(mature),
        "mature_mappings": mapping_index,
        "unmapped_mature_mirnas": unmapped,
        "ambiguous_mature_hairpin_hits": ambiguous,
    }


@register_function(
    aliases=["prepare_mirtop_reference", "mirtop_reference", "prepare_isomir_reference"],
    category="reference",
    description=(
        "Validate a miRBase precursor GFF3 against its hairpin FASTA and, when "
        "needed, build a mirtop-compatible GFF3 with mature miRNA coordinates "
        "and valid Derives_from precursor links."
    ),
    examples=[
        'sa.reference.prepare_mirtop_reference("ref/hsa.gff3", "ref/hairpin_hsa.fa", "ref/mature_hsa.fa")',
    ],
    related=["reference.download_mirbase", "quant.mirtop"],
)
def prepare_mirtop_reference(
    gff3: str,
    hairpin_fasta: str,
    mature_fasta: str,
    output_path: Optional[str] = None,
    *,
    overwrite: bool = False,
) -> Dict[str, object]:
    """Return a mirtop-compatible GFF3 for a miRBase hairpin reference.

    miRBase's genome GFF3 often contains precursor records only.  mirtop also
    requires mature-miRNA coordinates in hairpin space and a ``Derives_from``
    value that is the exact precursor FASTA identifier.  This function derives
    those coordinates from release-matched hairpin and mature FASTA files.
    """
    source = Path(gff3).expanduser().resolve()
    hairpin_path = Path(hairpin_fasta).expanduser().resolve()
    mature_path = Path(mature_fasta).expanduser().resolve()
    for label, path in (("GFF3", source), ("hairpin FASTA", hairpin_path), ("mature FASTA", mature_path)):
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")

    hairpins = _read_fasta_sequences(hairpin_path)
    mature = _read_fasta_sequences(mature_path)
    if not hairpins:
        raise ValueError(f"No sequences found in hairpin FASTA: {hairpin_path}")
    if not mature:
        raise ValueError(f"No sequences found in mature FASTA: {mature_path}")

    issues_before = _mirtop_gff_issues(source, set(hairpins))
    target = (
        Path(output_path).expanduser().resolve()
        if output_path
        else source.with_name(f"{source.stem}.mirtop.gff3")
    )
    if not issues_before and not output_path:
        return {
            "gff3": str(source),
            "source_gff3": str(source),
            "rebuilt": False,
            "issues_before": [],
            "issues_after": [],
        }

    if target.is_file() and not overwrite:
        issues_after = _mirtop_gff_issues(target, set(hairpins))
        if not issues_after:
            return {
                "gff3": str(target),
                "source_gff3": str(source),
                "rebuilt": False,
                "issues_before": issues_before,
                "issues_after": [],
            }

    summary = _write_mirtop_gff3(target, hairpins, mature)
    issues_after = _mirtop_gff_issues(target, set(hairpins))
    if issues_after:
        raise RuntimeError(
            "Generated mirtop GFF3 did not pass validation: " + "; ".join(issues_after)
        )
    return {
        "gff3": str(target),
        "source_gff3": str(source),
        "rebuilt": True,
        "issues_before": issues_before,
        "issues_after": [],
        **summary,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@register_function(
    aliases=[
        "list_mirbase_codes", "mirbase_species", "mirna_species",
    ],
    category="reference",
    description=(
        "List all species 3-letter codes available in a downloaded miRBase "
        "FASTA file. Useful for finding the correct code to pass to "
        "``download_mirbase()``."
    ),
    examples=[
        'codes = sa.reference.list_mirbase_codes(fasta_path="ref/mature.fa.gz")',
    ],
    related=["reference.download_mirbase"],
)
def list_mirbase_codes(
    fasta_path: str = "mature.fa.gz",
) -> List[str]:
    """Scan a miRBase FASTA file and list all unique species 3-letter codes.

    Parameters
    ----------
    fasta_path
        Path to a miRBase FASTA file (``mature.fa.gz`` or ``hairpin.fa.gz``).

    Returns
    -------
    list of str
        Sorted list of 3-letter species codes (e.g. ``"hsa"``, ``"mmu"``).
    """
    path = Path(fasta_path)
    if not path.exists():
        raise FileNotFoundError(f"miRBase FASTA file not found: {path}")
    return _scan_species_codes(path)


@register_function(
    aliases=[
        "download_mirbase", "mirbase", "mirna_reference",
        "下载miRBase",
    ],
    category="reference",
    description=(
        "Download miRBase reference data: all-species ``hairpin.fa`` and "
        "``mature.fa``, then extract sequences for a specific species "
        "(e.g. ``hsa`` for human) into per-species FASTA files, and "
        "optionally download the species-specific GFF3 annotation."
    ),
    examples=[
        'sa.reference.download_mirbase("hsa", output_dir="ref", jobs=4)',
        'sa.reference.download_mirbase("mmu", output_dir="ref", extract_only=True)',
    ],
    related=[
        "reference.list_mirbase_codes",
        "reference.download_genome", "reference.download_gtf",
    ],
    produces={"uns": ["mirna_hairpin", "mirna_mature", "mirna_gff3"]},
)
def download_mirbase(
    species: Optional[str] = None,
    output_dir: str = "references",
    jobs: int = 4,
    force: bool = False,
    download_fasta: bool = True,
    download_gff3: bool = True,
    extract_only: bool = False,
) -> Dict[str, str]:
    """Download miRBase hairpin / mature FASTA files and extract a species.

    Parameters
    ----------
    species
        3-letter species code (e.g. ``"hsa"``, ``"mmu"``). When provided,
        species-specific ``hairpin_{code}.fa`` and ``mature_{code}.fa``
        are extracted from the all-species files, and the species GFF3
        (``{code}.gff3``) is downloaded.
    output_dir
        Output directory for all downloaded and generated files.
    jobs
        Download threads. Default 4.
    force
        Re-download all-species files even if they exist.
    download_fasta
        Download ``hairpin.fa.gz`` and ``mature.fa.gz``. Default ``True``.
        Set to ``False`` when you only need to extract / GFF3.
    download_gff3
        Download the species GFF3 file (requires *species*). Default ``True``.
    extract_only
        When ``True``, only extract species FASTA from already-downloaded
        all-species files; do not download anything.

    Returns
    -------
    dict
        Paths to all downloaded and generated files.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    result: Dict[str, str] = {}
    all_fasta: Path | None = None
    mature_fasta: Path | None = None

    # ── Download all-species FASTA files ──
    if not extract_only and download_fasta:
        hairpin_local = out_dir / "hairpin.fa.gz"
        _download_from_sources(
            HAIRPIN_URLS, hairpin_local, jobs=jobs, force=force
        )
        result["hairpin_all"] = str(hairpin_local)
        all_fasta = hairpin_local

        mature_local = out_dir / "mature.fa.gz"
        _download_from_sources(
            MATURE_URLS, mature_local, jobs=jobs, force=force
        )
        result["mature_all"] = str(mature_local)
        mature_fasta = mature_local

    # ── Extract species-specific FASTA ──
    if species:
        code = species.strip().lower()
        if len(code) != 3 or not code.isalpha():
            raise ValueError(
                f"Species code must be a 3-letter code (e.g. 'hsa', 'mmu'), got '{species}'"
            )

        # Determine input FASTA paths
        hairpin_in = all_fasta or (out_dir / "hairpin.fa.gz")
        mature_in = mature_fasta or (out_dir / "mature.fa.gz")

        if not hairpin_in.exists():
            raise FileNotFoundError(
                f"All-species hairpin.fa.gz not found at {hairpin_in}. "
                f"Set download_fasta=True or place the file in {out_dir}"
            )
        if not mature_in.exists():
            raise FileNotFoundError(
                f"All-species mature.fa.gz not found at {mature_in}."
            )

        # Extract hairpin species FASTQ
        hairpin_sp = out_dir / f"hairpin_{code}.fa"
        _extract_fasta_by_prefix(hairpin_in, hairpin_sp, code)
        result["hairpin"] = str(hairpin_sp)

        # Extract mature species FASTQ
        mature_sp = out_dir / f"mature_{code}.fa"
        _extract_fasta_by_prefix(mature_in, mature_sp, code)
        result["mature"] = str(mature_sp)

        # ── Download species GFF3 ──
        if download_gff3 and not extract_only:
            gff3_local = out_dir / f"{code}.gff3"
            _download_from_sources(
                tuple(url.format(code=code) for url in GFF3_URLS),
                gff3_local,
                jobs=jobs,
                force=force,
            )
            result["gff3"] = str(gff3_local)

    return result
