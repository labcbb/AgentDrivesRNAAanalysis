"""miRTarBase miRNA-target interaction reference data download.

Wraps `miRTarBase <https://awi.cuhk.edu.cn/miRTarBase/downloads/>`_
for downloading species-specific miRNA-target interaction (MTI) CSV
files from the Catalog by Species section.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from ..._registry import register_function
from ._utils import resumable_download


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIRTAR_BASE = "https://awi.cuhk.edu.cn/miRTarBase/downloads"

# Species code → common name mapping for miRTarBase MTI CSV files
MIRTAR_SPECIES: Dict[str, str] = {
    "ath": "Arabidopsis thaliana",
    "bmo": "Bombyx mori",
    "bta": "Bos taurus",
    "cel": "Caenorhabditis elegans",
    "cfa": "Canis familiaris",
    "cgr": "Cricetulus griseus",
    "chi": "Capra hircus",
    "dme": "Drosophila melanogaster",
    "dre": "Danio rerio",
    "ebv": "Epstein-Barr virus",
    "eca": "Equus caballus",
    "gga": "Gallus gallus",
    "ggo": "Gorilla gorilla",
    "gma": "Glycine max",
    "hsa": "Homo sapiens",
    "kshv": "Kaposi sarcoma-associated virus",
    "mml": "Macaca mulatta",
    "mmu": "Mus musculus",
    "oar": "Ovis aries",
    "ocu": "Oryctolagus cuniculus",
    "ppa": "Pan paniscus",
    "ptr": "Pan troglodytes",
    "rno": "Rattus norvegicus",
    "sly": "Solanum lycopersicum",
    "ssc": "Sus scrofa",
    "tgu": "Taeniopygia guttata",
    "vsv": "Vesicular stomatitis virus",
    "xtr": "Xenopus tropicalis",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@register_function(
    aliases=[
        "list_mirtarbase_species", "mirtar_species",
        "mir_target_species",
    ],
    category="reference",
    description=(
        "List all species codes available in miRTarBase for miRNA-target "
        "interaction (MTI) CSV downloads. The returned dict maps 3-letter "
        "codes (e.g. ``'hsa'``) to common species names "
        "(e.g. ``'Homo sapiens'``). Use these codes with "
        "``download_mirtarbase()``."
    ),
    examples=[
        'species = sa.reference.list_mirtarbase_species()',
    ],
    related=["reference.download_mirtarbase"],
)
def list_mirtarbase_species() -> Dict[str, str]:
    """List all species codes available in miRTarBase.

    Returns
    -------
    dict
        ``{code: common_name}`` mapping for all 28 species.
    """
    return dict(MIRTAR_SPECIES)


@register_function(
    aliases=[
        "download_mirtarbase", "mirtarbase", "mir_target",
        "mirna_target", "下载miRTarBase",
    ],
    category="reference",
    description=(
        "Download miRNA-target interaction (MTI) CSV for a given species "
        "from miRTarBase. The file contains experimentally validated "
        "miRNA-target interactions curated from the literature.\n\n"
        "File format: CSV with columns including miRNA, target gene, "
        "supporting experiments, and PubMed IDs.\n\n"
        "Default species is human (``hsa``). The downloaded file is saved "
        "as ``{code}_MTI.csv`` in the output directory."
    ),
    examples=[
        'result = sa.reference.download_mirtarbase("hsa", output_dir="ref")',
        'result = sa.reference.download_mirtarbase("mmu", output_dir="ref")',
    ],
    related=[
        "reference.list_mirtarbase_species",
        "reference.download_mirbase",
    ],
    produces={"uns": ["mirtarbase_csv"]},
)
def download_mirtarbase(
    code: str = "hsa",
    output_dir: str = ".",
    jobs: int = 4,
    force: bool = False,
) -> Dict[str, str]:
    """Download miRTarBase MTI CSV for a species.

    Parameters
    ----------
    code
        3-letter species code (e.g. ``'hsa'`` for human).
        Default ``'hsa'``. Use ``list_mirtarbase_species()`` to see
        all available codes.
    output_dir
        Output directory for the downloaded CSV file.
    jobs
        Download threads for ``resumable_download``. Default 4.
    force
        Re-download even if the file exists locally.

    Returns
    -------
    dict
        Paths to downloaded files. Key ``"csv"`` — the MTI CSV path.
    """
    code = code.strip().lower()

    if code not in MIRTAR_SPECIES:
        known = sorted(MIRTAR_SPECIES.keys())
        raise ValueError(
            f"Unknown species code '{code}'. "
            f"Use list_mirtarbase_species() to see all codes. "
            f"Known: {known}"
        )

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    url = f"{MIRTAR_BASE}/{code}_MTI.csv"
    filename = f"{code}_MTI.csv"
    local = out_dir / filename

    resumable_download(url, local, jobs=jobs, force=force)

    name = MIRTAR_SPECIES[code]
    print(
        f"[download_mirtarbase] Downloaded MTI CSV for "
        f"{code} ({name}) → {local}",
        flush=True,
    )

    return {"csv": str(local)}
