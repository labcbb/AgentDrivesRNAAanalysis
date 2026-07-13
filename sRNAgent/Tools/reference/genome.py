"""Genome reference download API.

Provides functions to download reference genome sequences and annotation
files. For human (``homo_sapiens``) and mouse (``mus_musculus``), downloads
from `GENCODE <https://www.gencodegenes.org/>`_ (primary assembly genome +
primary assembly GTF). For all other species, downloads from
`Ensembl FTP <https://ftp.ensembl.org/pub/current/>`_.
Supports multi-threaded resumable download via ``_utils.py``.
"""

from __future__ import annotations

import gzip
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from ..._registry import register_function
from ._utils import resumable_download


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENSEMBL_CURRENT = "https://ftp.ensembl.org/pub/current"
FASTA_BASE = f"{ENSEMBL_CURRENT}/fasta"
GTF_BASE = f"{ENSEMBL_CURRENT}/gtf"

GENCODE_BASE = "https://ftp.ebi.ac.uk/pub/databases/gencode"
GENCODE_HUMAN = f"{GENCODE_BASE}/Gencode_human/latest_release"
GENCODE_MOUSE = f"{GENCODE_BASE}/Gencode_mouse/latest_release"

# Species names that use GENCODE instead of Ensembl
_GENCODE_SPECIES = {"homo_sapiens", "mus_musculus"}
_GENCODE_CONFIG = {
    "homo_sapiens": {
        "base_url": GENCODE_HUMAN,
        "genome_suffix": "primary_assembly.genome.fa.gz",
        "gtf_suffix": "primary_assembly.annotation.gtf.gz",
    },
    "mus_musculus": {
        "base_url": GENCODE_MOUSE,
        "genome_suffix": "primary_assembly.genome.fa.gz",
        "gtf_suffix": "primary_assembly.annotation.gtf.gz",
    },
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _species_dirname(name: str) -> str:
    """Normalise a species name to Ensembl directory format.

    >>> _species_dirname("Homo sapiens")
    "homo_sapiens"
    >>> _species_dirname("homo_sapiens")
    "homo_sapiens"
    """
    return name.strip().lower().replace(" ", "_")


def _species_filename(name: str) -> str:
    """Capitalise species name for Ensembl file naming.

    >>> _species_filename("homo_sapiens")
    "Homo_sapiens"
    """
    return "_".join(part.capitalize() for part in name.strip().lower().split("_"))


def _is_gencode_species(species: str) -> bool:
    """Check if a species should use GENCODE instead of Ensembl."""
    return _species_dirname(species) in _GENCODE_SPECIES


def _parse_html_listing(url: str) -> List[str]:
    """Fetch and parse an HTML/FTP directory listing, returning entry names."""
    entries: List[str] = []
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise RuntimeError(f"Failed to list {url}: {exc}") from exc

    # Apache/Nginx HTML directory listing
    for m in re.finditer(r'<a\s+href="([^"]+)"', html):
        name = m.group(1).rstrip("/")
        if name in ("", "..", "../", "."):
            continue
        if name.startswith("/") or name.startswith("?"):
            continue
        entries.append(name)

    # Fallback: FTP plain listing (one entry per line)
    if not entries:
        for line in html.splitlines():
            line = line.strip()
            if line and not line.startswith(("total", "d", "-", "l")):
                parts = line.split()
                if parts:
                    name = parts[-1]
                    if name not in ("", ".", ".."):
                        entries.append(name)

    return sorted(set(entries))


def _find_gencode_genome(species: str) -> str:
    """Find the primary assembly genome FASTA filename from GENCODE."""
    sp = _species_dirname(species)
    cfg = _GENCODE_CONFIG[sp]
    url = f"{cfg['base_url']}/"
    files = _parse_html_listing(url)
    candidates = [f for f in files if f.endswith(cfg["genome_suffix"])]
    if not candidates:
        raise FileNotFoundError(
            f"No GENCODE primary assembly genome found for '{species}' at {url}"
        )
    return candidates[0]


def _find_gencode_gtf(species: str) -> str:
    """Find the primary assembly annotation GTF filename from GENCODE."""
    sp = _species_dirname(species)
    cfg = _GENCODE_CONFIG[sp]
    url = f"{cfg['base_url']}/"
    files = _parse_html_listing(url)
    candidates = [f for f in files if f.endswith(cfg["gtf_suffix"])]
    if not candidates:
        raise FileNotFoundError(
            f"No GENCODE primary assembly GTF found for '{species}' at {url}"
        )
    return candidates[0]


def _find_ensembl_dna_file(species: str, assembly: Optional[str] = None) -> str:
    """Find the primary assembly FASTA filename for a species from Ensembl."""
    dna_url = f"{FASTA_BASE}/{_species_dirname(species)}/dna/"
    files = _parse_html_listing(dna_url)

    candidates = [f for f in files if f.endswith(".fa.gz") and "primary_assembly" in f]
    if not candidates:
        candidates = [f for f in files if f.endswith(".fa.gz") and "toplevel" in f]
    if not candidates:
        candidates = [f for f in files if f.endswith(".fa.gz") and ".dna." in f]
    if not candidates:
        raise FileNotFoundError(
            f"No primary assembly FASTA found for '{species}' at {dna_url}"
        )
    return candidates[0]


def _find_ensembl_gtf_file(species: str, assembly: Optional[str] = None) -> str:
    """Find the GTF annotation filename for a species from Ensembl."""
    gtf_url = f"{GTF_BASE}/{_species_dirname(species)}/"
    files = _parse_html_listing(gtf_url)

    candidates = [
        f for f in files
        if f.endswith(".gtf.gz") and "abinitio" not in f and "chr_patch" not in f
    ]
    if not candidates:
        candidates = [f for f in files if f.endswith(".gtf.gz")]
    if not candidates:
        raise FileNotFoundError(
            f"No GTF file found for '{species}' at {gtf_url}"
        )
    candidates.sort(key=len, reverse=True)
    return candidates[0]


def _find_ensembl_ncrna_file(species: str) -> str:
    """Find the ncRNA FASTA filename for a species from Ensembl."""
    species_dir = _species_dirname(species)
    ncrna_url = f"{FASTA_BASE}/{species_dir}/ncrna/"
    files = _parse_html_listing(ncrna_url)

    candidates = [f for f in files if f.endswith(".fa.gz") and "ncrna" in f]
    if not candidates:
        raise FileNotFoundError(
            f"No ncRNA FASTA file found for '{species}' at {ncrna_url}"
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# Internal: sequence dictionary generation + FASTA cleanup
# ---------------------------------------------------------------------------


def _clean_fasta_headers(input_fa: Path, output_fa: Path) -> Path:
    """Read a FASTA file, truncate each header at the first space, write result.

    Ensembl/GENCODE headers like::

        >chr1 AC:1234 ... Homo sapiens GRCh38 ...

    become::

        >chr1

    This avoids errors in tools that cannot handle whitespace in identifiers
    (e.g. miRDeep2, bowtie-build).
    """
    with open(input_fa, "r") as f_in, open(output_fa, "w") as f_out:
        for line in f_in:
            if line.startswith(">"):
                ident = line[1:].split(None, 1)[0]
                f_out.write(f">{ident}\n")
            else:
                f_out.write(line)
    return output_fa


def _generate_dict(fasta_path: str | Path) -> str:
    """Generate a ``.dict`` file from a FASTA using ``samtools dict``."""
    fasta_path = Path(fasta_path)
    if not fasta_path.exists():
        raise FileNotFoundError(f"FASTA not found: {fasta_path}")

    stem = str(fasta_path.name)
    for sfx in (".fa", ".fna", ".fasta"):
        stem = stem.replace(sfx, "")
    dict_path = fasta_path.parent / f"{stem}.dict"

    cmd = ["samtools", "dict", str(fasta_path), "-o", str(dict_path)]
    print(f"[samtools] Generating dict: {dict_path.name}", flush=True)
    subprocess.run(cmd, check=True)

    return str(dict_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@register_function(
    aliases=["list_species", "ensembl_species", "list_ensembl_species"],
    category="reference",
    description=(
        "List all available species in the current Ensembl release. "
        "Returns species directory names (e.g. ``homo_sapiens``, ``mus_musculus``) "
        "that can be passed to ``download_genome()``, ``download_gtf()``, etc. "
        "Note: ``homo_sapiens`` and ``mus_musculus`` download from GENCODE; "
        "all other species download from Ensembl."
    ),
    examples=[
        'species = sa.reference.list_species()',
        'human_species = [s for s in sa.reference.list_species() if "homo" in s]',
    ],
    related=[
        "reference.download_genome", "reference.download_gtf",
        "reference.download_ncrna",
    ],
)
def list_species() -> List[str]:
    """List available species in the current Ensembl release.

    Returns
    -------
    list of str
        Species directory names (e.g. ``"homo_sapiens"``, ``"mus_musculus"``).
    """
    return _parse_html_listing(f"{FASTA_BASE}/")


@register_function(
    aliases=[
        "download_genome", "fetch_genome", "get_genome",
        "下载基因组",
    ],
    category="reference",
    description=(
        "Download a reference genome FASTA (primary assembly).\n\n"
        "For **human** (homo_sapiens) and **mouse** (mus_musculus), downloads "
        "from **GENCODE** "
        "(e.g. ``GRCh38.primary_assembly.genome.fa.gz``).\n"
        "For all other species, downloads from **Ensembl**.\n"
        "Automatically decompresses the FASTA, cleans sequence headers "
        "(removes everything after the first space in each ``>`` line), "
        "and generates a ``.dict`` sequence dictionary file."
    ),
    examples=[
        'sa.reference.download_genome("homo_sapiens", output_dir="ref", jobs=8)',
        'sa.reference.download_genome("mus_musculus", output_dir="ref")',
    ],
    related=[
        "reference.list_species", "reference.download_gtf",
        "reference.download_ncrna",
    ],
    produces={"uns": ["genome_fasta", "genome_dict"]},
)
def download_genome(
    species: str = "homo_sapiens",
    output_dir: str = ".",
    assembly: Optional[str] = None,
    jobs: int = 4,
    force: bool = False,
    generate_dict: bool = True,
) -> Dict[str, str]:
    """Download a reference genome FASTA (primary assembly).

    Downloads the gzipped FASTA, decompresses it, cleans sequence headers
    (truncates each ``>`` line at the first space), and optionally generates
    a ``.dict`` sequence dictionary.

    Parameters
    ----------
    species
        Species name, e.g. ``"homo_sapiens"``, ``"mus_musculus"``.
    output_dir
        Directory to save the downloaded files.
    assembly
        Assembly name (e.g. ``"GRCh38"``). Ignored for GENCODE species.
    jobs
        Number of download threads. Default 4.
    force
        Re-download even if the file already exists.
    generate_dict
        Generate a ``.dict`` file using ``samtools dict`` after download.

    Returns
    -------
    dict
        ``{"fasta": "<path>", "dict": "<path>"}`` where *fasta* points to the
        decompressed and header-cleaned FASTA file.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if _is_gencode_species(species):
        filename = _find_gencode_genome(species)
        sp = _species_dirname(species)
        url = f"{_GENCODE_CONFIG[sp]['base_url']}/{filename}"
    else:
        filename = _find_ensembl_dna_file(species, assembly)
        url = f"{FASTA_BASE}/{_species_dirname(species)}/dna/{filename}"

    # Download gzipped FASTA
    gz_path = out_dir / filename
    gz_path = Path(resumable_download(url, gz_path, jobs=jobs, force=force))

    # Determine cleaned FASTA path (strip .gz)
    fa_path = gz_path.parent / gz_path.name.replace(".gz", "")

    # Decompress if needed
    if force or not fa_path.exists():
        print(f"[genome] Decompressing {gz_path.name} -> {fa_path.name}", flush=True)
        with gzip.open(gz_path, "rb") as f_in, open(fa_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    # Clean headers: truncate at first space in each > line
    tmp_fa = fa_path.with_suffix(".tmp.fa")
    _clean_fasta_headers(fa_path, tmp_fa)
    shutil.move(str(tmp_fa), str(fa_path))
    print(f"[genome] Cleaned headers: {fa_path.name}", flush=True)

    result: Dict[str, str] = {"fasta": str(fa_path)}
    if generate_dict:
        dict_path = _generate_dict(fa_path)
        result["dict"] = dict_path

    return result


@register_function(
    aliases=[
        "download_gtf", "fetch_gtf", "get_gtf", "download_annotation",
        "下载注释",
    ],
    category="reference",
    description=(
        "Download a GTF annotation file.\n\n"
        "For **human** (homo_sapiens) and **mouse** (mus_musculus), downloads "
        "GENCODE ``primary_assembly.annotation.gtf.gz``.\n"
        "For all other species, downloads from Ensembl."
    ),
    examples=[
        'sa.reference.download_gtf("homo_sapiens", output_dir="ref")',
        'sa.reference.download_gtf("danio_rerio", output_dir="ref", jobs=4)',
    ],
    related=[
        "reference.list_species", "reference.download_genome",
        "reference.download_ncrna",
    ],
    produces={"uns": ["gtf_file"]},
)
def download_gtf(
    species: str = "homo_sapiens",
    output_dir: str = ".",
    assembly: Optional[str] = None,
    jobs: int = 4,
    force: bool = False,
) -> Dict[str, str]:
    """Download a GTF annotation file.

    Parameters
    ----------
    species
        Species name, e.g. ``"homo_sapiens"``, ``"mus_musculus"``.
    output_dir
        Directory to save the downloaded file.
    assembly
        Assembly name. Ignored for GENCODE species.
    jobs
        Number of download threads. Default 4.
    force
        Re-download even if the file already exists.

    Returns
    -------
    dict
        ``{"gtf": "<path>"}``
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if _is_gencode_species(species):
        filename = _find_gencode_gtf(species)
        sp = _species_dirname(species)
        url = f"{_GENCODE_CONFIG[sp]['base_url']}/{filename}"
    else:
        filename = _find_ensembl_gtf_file(species, assembly)
        url = f"{GTF_BASE}/{_species_dirname(species)}/{filename}"

    output_path = out_dir / filename
    gtf_path = resumable_download(url, output_path, jobs=jobs, force=force)
    return {"gtf": gtf_path}


@register_function(
    aliases=[
        "download_ncrna", "fetch_ncrna", "get_ncrna",
        "下载非编码RNA",
    ],
    category="reference",
    description=(
        "Download a non-coding RNA FASTA file from Ensembl. Contains "
        "miRNA, piRNA, snoRNA, lncRNA, and other non-coding sequences. "
        "Only available from Ensembl (not GENCODE)."
    ),
    examples=[
        'sa.reference.download_ncrna("homo_sapiens", output_dir="ref")',
        'sa.reference.download_ncrna("mus_musculus", output_dir="ref", jobs=4)',
    ],
    related=[
        "reference.list_species", "reference.download_genome",
        "reference.download_gtf",
    ],
    produces={"uns": ["ncrna_fasta"]},
)
def download_ncrna(
    species: str = "homo_sapiens",
    output_dir: str = ".",
    jobs: int = 4,
    force: bool = False,
) -> Dict[str, str]:
    """Download an Ensembl ncRNA FASTA file.

    Parameters
    ----------
    species
        Ensembl species name, e.g. ``"homo_sapiens"``, ``"mus_musculus"``.
    output_dir
        Directory to save the downloaded file.
    jobs
        Number of download threads. Default 4.
    force
        Re-download even if the file already exists.

    Returns
    -------
    dict
        ``{"ncrna": "<path>"}``
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = _find_ensembl_ncrna_file(species)
    species_dir = _species_dirname(species)
    url = f"{FASTA_BASE}/{species_dir}/ncrna/{filename}"
    output_path = out_dir / filename

    ncrna_path = resumable_download(url, output_path, jobs=jobs, force=force)
    return {"ncrna": ncrna_path}
