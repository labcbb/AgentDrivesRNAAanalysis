r"""FASTQ processing utilities for sRNA-seq data.

This module provides wrappers for common FASTQ processing tools including:
- Download: ``fastq_dl`` — download FASTQ from ENA / SRA
- Quality control: ``fastqc`` — generate QC reports
- Adapter / quality trimming: ``cutadapt`` — trim adapters, quality, and length filter
- Report aggregation: ``multiqc`` — aggregate QC reports from multiple tools
"""

from .fastq_dl import fastq_dl
from .cutadapt import cutadapt
from .fastqc import fastqc
from .multiqc import multiqc

__all__ = [
    "fastq_dl",
    "cutadapt",
    "fastqc",
    "multiqc",
]
