"""Reference genome, annotation, and miRBase download utilities.

Provides functions to download reference genomes and annotation from
**GENCODE** (human/mouse) or `Ensembl FTP <https://ftp.ensembl.org/pub/current/>`_
(other species), and miRNA data from `miRBase <https://www.mirbase.org/download/>`_.
"""

from .genome import download_genome, download_gtf, download_ncrna, list_species
from .mirbase import download_mirbase, list_mirbase_codes
from .tRNAdb import (
    build_trax_human_gtf,
    download_trax_human_gtf,
    download_trnascan_hg38,
)

__all__ = [
    "list_species",
    "download_genome",
    "download_gtf",
    "download_ncrna",
    "list_mirbase_codes",
    "download_mirbase",
    "download_trax_human_gtf",
    "build_trax_human_gtf",
    "download_trnascan_hg38",
]
