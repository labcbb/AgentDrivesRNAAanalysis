"""Reference genome, annotation, and miRBase download utilities.

Provides functions to download reference genomes and annotation from
**GENCODE** (human/mouse) or `Ensembl FTP <https://ftp.ensembl.org/pub/current/>`_
(other species), and miRNA data from `miRBase <https://www.mirbase.org/download/>`_.
"""

from importlib import import_module

from .genome import download_genome, download_gtf, download_ncrna, list_species
from .mir_target import download_mirtarbase, list_mirtarbase_species
from .mirbase import download_mirbase, list_mirbase_codes
from .piRBase import download_pirna, list_pirna_species
from .tRNAdb import (
    build_trnadb,
    build_trax_human_gtf,
    download_trax_human_gtf,
    download_trnascan_hg38,
)

extract_three_prime_utr = import_module(".3UTR", __name__).extract_three_prime_utr

__all__ = [
    "list_species",
    "download_genome",
    "download_gtf",
    "download_ncrna",
    "extract_three_prime_utr",
    "list_mirbase_codes",
    "download_mirbase",
    "list_pirna_species",
    "download_pirna",
    "list_mirtarbase_species",
    "download_mirtarbase",
    "build_trnadb",
    "download_trax_human_gtf",
    "build_trax_human_gtf",
    "download_trnascan_hg38",
]
