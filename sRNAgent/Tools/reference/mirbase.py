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
    output_dir: str = ".",
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
            gff3_url = f"{MIRBASE_BASE}/{code}.gff3"
            gff3_local = out_dir / f"{code}.gff3"
            resumable_download(gff3_url, gff3_local, jobs=jobs, force=force)
            result["gff3"] = str(gff3_local)

    return result
