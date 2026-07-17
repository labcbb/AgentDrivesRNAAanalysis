"""piRBase piRNA FASTA reference data download.

Wraps `piRBase <http://bigdata.ibp.ac.cn/piRBase/download.php>`_ for
downloading species-specific piRNA FASTA files (full set and gold
standard set).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from ..._registry import register_function
from .util import resumable_download


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PIRBASE_BASE = "http://bigdata.ibp.ac.cn/piRBase/download"

# Species code → common name mapping for piRNA FASTA files
PIRNA_SPECIES: Dict[str, str] = {
    "aca": "Sea hare",
    "ame": "Giant panda",
    "bgl": "B. glabrata",
    "bmo": "Silkworm",
    "bta": "Cow",
    "c26": "Caenorhabditis sp 26",
    "c31": "Caenorhabditis sp 31",
    "c32": "Caenorhabditis sp 32",
    "cbn": "C. brenneri",
    "cbr": "C. briggsae",
    "cca": "C. castelli",
    "cdo": "C. doughertyi",
    "cel": "C. elegans",
    "cja": "Marmoset",
    "cma": "C. macrosperma",
    "crm": "C. remanei",
    "cvi": "C. virilis",
    "der": "D. erecta",
    "dme": "D. melanogaster",
    "dpa": "D. pachys",
    "dre": "Zebrafish",
    "dvi": "D. virilis",
    "dya": "D. yakuba",
    "eca": "Horse",
    "gga": "Chicken",
    "hco": "Barber pole worm",
    "hpo": "H. polygyrus",
    "hsa": "Human",
    "mfa": "Crab-eating macaque",
    "mml": "Rhesus",
    "mmu": "Mouse",
    "nbr": "N. brasiliensis",
    "nve": "Starlet sea anemone",
    "ocu": "Rabbit",
    "oti": "O. tipulae",
    "pox": "P. oxycercus",
    "ppc": "P. pacificus",
    "psa": "P. sambesii",
    "rno": "Rat",
    "spa": "Mud crab",
    "ssc": "Pig",
    "tbe": "Tree shrew",
    "xtr": "X. tropicalis",
}

# Species for which the FASTA uses the old naming scheme (without `.v3.0`)
# All others use `{code}.v3.0.fa.gz`
_OLD_FASTA_CODES = {
    "aca", "cja", "der", "dre", "dvi", "dya", "mml", "nve", "ocu", "rno", "tbe", "xtr",
}

# Species that have a gold standard piRNA set
GOLD_SPECIES = {"hsa", "mmu", "dme", "bta", "rno", "mfa"}

# Species that have a v3.0 FASTA file
_V3_FASTA_CODES = set(PIRNA_SPECIES.keys()) - _OLD_FASTA_CODES


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_fasta_url(code: str, gold: bool = False) -> str:
    """Build the piRBase download URL for a given species code.

    Tries the v3.0 naming scheme first; falls back to the old scheme.
    """
    if gold:
        return f"{PIRBASE_BASE}/{code}.gold.fa.gz"

    if code in _V3_FASTA_CODES:
        return f"{PIRBASE_BASE}/{code}.v3.0.fa.gz"

    return f"{PIRBASE_BASE}/{code}.fa.gz"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@register_function(
    aliases=[
        "list_pirna_species", "pirbase_species", "pirna_codes",
    ],
    category="reference",
    description=(
        "List all species codes available in piRBase for piRNA FASTA "
        "downloads. The returned dict maps 3-letter codes (e.g. ``'hsa'``) "
        "to common species names (e.g. ``'Human'``). Use these codes with "
        "``download_pirna()``."
    ),
    examples=[
        'species = sa.reference.list_pirna_species()',
    ],
    related=["reference.download_pirna"],
)
def list_pirna_species() -> Dict[str, str]:
    """List all species codes available for piRNA FASTA download.

    Returns
    -------
    dict
        ``{code: common_name}`` mapping for all 42 species.
    """
    return dict(PIRNA_SPECIES)


@register_function(
    aliases=[
        "download_pirna", "pirna", "pirna_reference",
        "pirbase", "下载piRNA",
    ],
    category="reference",
    description=(
        "Download piRNA FASTA file for a given species from piRBase. "
        "Downloads the full piRNA set (``{code}.fa.gz`` or "
        "``{code}.v3.0.fa.gz``) and optionally the gold standard piRNA "
        "set (``{code}.gold.fa.gz``) if available for that species.\n\n"
        "Gold standard sets are available for: human, mouse, "
        "D. melanogaster, cow, rat, and crab-eating macaque.\n\n"
        "The file is saved to the specified output directory and the "
        "path is returned."
    ),
    examples=[
        'result = sa.reference.download_pirna("hsa", output_dir="ref")',
        'result = sa.reference.download_pirna("mmu", output_dir="ref", gold=True)',
    ],
    related=[
        "reference.list_pirna_species",
        "reference.download_mirbase",
    ],
    produces={"uns": ["pirna_fasta", "pirna_gold_fasta"]},
)
def download_pirna(
    code: str,
    output_dir: str = ".",
    gold: bool = False,
    jobs: int = 4,
    force: bool = False,
) -> Dict[str, str]:
    """Download piRNA FASTA for a species from piRBase.

    Parameters
    ----------
    code
        3-letter species code (e.g. ``'hsa'`` for human).
        Use ``list_pirna_species()`` to see all available codes.
    output_dir
        Output directory for the downloaded FASTA file(s).
    gold
        When ``True``, download the gold standard piRNA set instead of
        the full set. Only available for human, mouse, D. melanogaster,
        cow, rat, and crab-eating macaque.
    jobs
        Download threads for ``resumable_download``. Default 4.
    force
        Re-download even if the file exists locally.

    Returns
    -------
    dict
        Paths to downloaded files. Keys:
        - ``"fasta"`` — the piRNA FASTA file path.
        - ``"gold_fasta"`` — only when *gold* is ``True``.

    Raises
    ------
    ValueError
        If the species code is unknown.
    """
    code = code.strip().lower()

    # Validate species code
    if len(code) != 3 or not code.isalpha():
        raise ValueError(
            f"Species code must be a 3-letter code (e.g. 'hsa', 'mmu'), "
            f"got '{code}'."
        )
    if code not in PIRNA_SPECIES:
        known = list(PIRNA_SPECIES.keys())
        raise ValueError(
            f"Unknown species code '{code}'. "
            f"Use list_pirna_species() to see all codes. "
            f"Known: {sorted(known)}"
        )

    # Validate gold standard availability
    if gold and code not in GOLD_SPECIES:
        raise ValueError(
            f"Gold standard piRNA set not available for '{code}'. "
            f"Available for: {', '.join(sorted(GOLD_SPECIES))}"
        )

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    result: Dict[str, str] = {}
    name = PIRNA_SPECIES[code]

    if gold:
        url = _resolve_fasta_url(code, gold=True)
        filename = f"{code}.gold.fa.gz"
        local = out_dir / filename
        resumable_download(url, local, jobs=jobs, force=force)
        result["gold_fasta"] = str(local)
        print(
            f"[download_pirna] Downloaded gold standard piRNA set for "
            f"{code} ({name}) → {local}",
            flush=True,
        )
    else:
        url = _resolve_fasta_url(code, gold=False)
        filename = f"{code}.piRNA.fa.gz"
        local = out_dir / filename
        resumable_download(url, local, jobs=jobs, force=force)
        result["fasta"] = str(local)
        print(
            f"[download_pirna] Downloaded piRNA FASTA for "
            f"{code} ({name}) → {local}",
            flush=True,
        )

    return result
