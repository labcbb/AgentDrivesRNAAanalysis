"""tRAX reference helper utilities.

This module builds the small-RNA GTF input expected by the tRAX
quantification workflow. It intentionally mirrors the historical shell filter:

    grep -v '^#' | awk '{print "chr" $0;}' |
    grep -e Mt_rRNA -e miRNA -e misc_RNA -e rRNA -e snRNA \
        -e snoRNA -e ribozyme -e sRNA -e scaRNA
"""

from __future__ import annotations

import gzip
import tarfile
from pathlib import Path
from typing import Dict, Iterable, Optional

from ..._registry import register_function
from . import genome


TRAX_GTF_FEATURE_TERMS = (
    "Mt_rRNA",
    "miRNA",
    "misc_RNA",
    "rRNA",
    "snRNA",
    "snoRNA",
    "ribozyme",
    "sRNA",
    "scaRNA",
)

TRNASCAN_HG38_URL = (
    "http://gtrnadb.ucsc.edu/GtRNAdb2/genomes/eukaryota/"
    "Hsapi38/hg38-tRNAs.tar.gz"
)

TRNASCAN_HG38_FILES = (
    "hg38-filtered-tRNAs.fa",
    "hg38-mature-tRNAs.fa",
    "hg38-tRNAs.bed",
    "hg38-tRNAs-confidence-set.out",
    "hg38-tRNAs-confidence-set.ss",
    "hg38-tRNAs-detailed.out",
    "hg38-tRNAs-detailed.ss",
    "hg38-tRNAs.fa",
    "hg38-tRNAs_name_map.txt",
)


def _ensure_human_hg38(assembly: Optional[str]) -> None:
    """Reject unsupported assemblies; this tRAX helper is hg38-only."""
    if assembly is None:
        return
    if assembly.lower() not in {"hg38", "grch38"}:
        raise ValueError("Only human hg38/GRCh38 is supported.")


def _iter_filtered_gtf_lines(gtf_gz: str | Path, terms: Iterable[str]) -> Iterable[str]:
    """Yield Ensembl GTF lines after applying the legacy tRAX filter."""
    terms = tuple(terms)
    with gzip.open(gtf_gz, "rt") as handle:
        for raw_line in handle:
            if raw_line.startswith("#"):
                continue
            line = "chr" + raw_line.rstrip("\n")
            if any(term in line for term in terms):
                yield line + "\n"


def _safe_extract_tar(archive: str | Path, output_dir: str | Path) -> None:
    """Extract a tar archive without allowing path traversal."""
    out_dir = Path(output_dir).resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (out_dir / member.name).resolve()
            if not str(target).startswith(str(out_dir) + "/") and target != out_dir:
                raise RuntimeError(f"Refusing unsafe tar member: {member.name}")
        tar.extractall(out_dir)


def _find_expected_file(output_dir: Path, filename: str) -> Optional[Path]:
    """Find an extracted expected file, allowing archives with one subdirectory."""
    direct = output_dir / filename
    if direct.exists():
        return direct
    matches = list(output_dir.rglob(filename))
    if matches:
        return matches[0]
    return None


@register_function(
    aliases=[
        "download_trax_human_gtf",
        "download_human_trax_gtf",
        "build_trax_human_gtf",
        "下载tRAX人类GTF",
    ],
    category="reference",
    description=(
        "Download the current human Ensembl GTF through reference.genome and "
        "write the filtered small-RNA GTF used by tRAX quantification. The "
        "filter is equivalent to: grep -v '^#' | awk '{print \"chr\" $0;}' | "
        "grep -e Mt_rRNA -e miRNA -e misc_RNA -e rRNA -e snRNA -e snoRNA "
        "-e ribozyme -e sRNA -e scaRNA."
    ),
    examples=[
        'sa.reference.download_trax_human_gtf(output_dir="ref")',
        (
            'sa.reference.build_trax_human_gtf('
            'output_dir="ref", output_name="hg38-genes.gtf")'
        ),
    ],
    related=["reference.download_genome", "reference.download_gtf"],
    produces={"uns": ["trax_gtf"]},
)
def download_trax_human_gtf(
    output_dir: str = ".",
    output_name: str = "hg38-genes.gtf",
    assembly: Optional[str] = "GRCh38",
    jobs: int = 4,
    force: bool = False,
) -> Dict[str, str]:
    """Download human Ensembl GTF and save the tRAX-filtered GTF.

    Parameters
    ----------
    output_dir
        Directory to save the downloaded ``.gtf.gz`` and filtered ``.gtf``.
    output_name
        Filename for the filtered GTF.
    assembly
        Assembly name passed to the Ensembl finder. Defaults to ``"GRCh38"``.
    jobs
        Number of download threads.
    force
        Re-download and regenerate even if output files already exist.

    Returns
    -------
    dict
        ``{"source_gtf_gz": "<downloaded .gtf.gz>", "trax_gtf": "<filtered .gtf>"}``
    """
    _ensure_human_hg38(assembly)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    species = "homo_sapiens"
    filename = genome._find_ensembl_gtf_file(species, assembly)
    url = f"{genome.GTF_BASE}/{genome._species_dirname(species)}/{filename}"
    source_gtf = Path(
        genome.resumable_download(url, out_dir / filename, jobs=jobs, force=force)
    )

    filtered_gtf = out_dir / output_name
    if force or not filtered_gtf.exists():
        with open(filtered_gtf, "w") as out_handle:
            for line in _iter_filtered_gtf_lines(source_gtf, TRAX_GTF_FEATURE_TERMS):
                out_handle.write(line)

    return {
        "source_gtf_gz": str(source_gtf),
        "trax_gtf": str(filtered_gtf),
    }


build_trax_human_gtf = download_trax_human_gtf


@register_function(
    aliases=[
        "download_trnascan_hg38",
        "download_human_trnascan",
        "download_hg38_trnascan",
        "下载人类tRNAscan",
    ],
    category="reference",
    description=(
        "Download and extract the human hg38 tRNAscan-SE files from GtRNAdb. "
        "Only human hg38/GRCh38 is supported."
    ),
    examples=[
        'sa.reference.download_trnascan_hg38(output_dir="ref")',
    ],
    related=["reference.download_trax_human_gtf"],
    produces={"uns": ["trnascan_hg38_files"]},
)
def download_trnascan_hg38(
    output_dir: str = ".",
    assembly: Optional[str] = "hg38",
    jobs: int = 4,
    force: bool = False,
) -> Dict[str, object]:
    """Download and extract the human hg38 tRNAscan-SE archive.

    Parameters
    ----------
    output_dir
        Directory to save ``hg38-tRNAs.tar.gz`` and the extracted files.
    assembly
        Assembly selector. Only ``"hg38"`` or ``"GRCh38"`` is accepted.
    jobs
        Number of download threads.
    force
        Re-download and re-extract even if files already exist.

    Returns
    -------
    dict
        ``{"archive": "<tar.gz>", "output_dir": "<dir>", "files": {...}}``
    """
    _ensure_human_hg38(assembly)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    archive = Path(
        genome.resumable_download(
            TRNASCAN_HG38_URL,
            out_dir / "hg38-tRNAs.tar.gz",
            jobs=jobs,
            force=force,
        )
    )

    extracted = {
        filename: _find_expected_file(out_dir, filename)
        for filename in TRNASCAN_HG38_FILES
    }
    if force or any(path is None for path in extracted.values()):
        _safe_extract_tar(archive, out_dir)
        extracted = {
            filename: _find_expected_file(out_dir, filename)
            for filename in TRNASCAN_HG38_FILES
        }

    missing = [filename for filename, path in extracted.items() if path is None]
    if missing:
        raise FileNotFoundError(
            "tRNAscan-SE archive did not contain expected files: "
            + ", ".join(missing)
        )

    return {
        "archive": str(archive),
        "output_dir": str(out_dir),
        "files": {filename: str(path) for filename, path in extracted.items()},
    }
