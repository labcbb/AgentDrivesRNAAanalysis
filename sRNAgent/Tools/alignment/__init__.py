"""Alignment analysis utilities for sRNA-seq data.

This module provides wrappers for short-read alignment tools including:
- sRNA-seq alignment: ``bowtie`` — align sRNA-seq reads to reference genome
- Index building: ``bowtie_build`` — build Bowtie genome indices
"""

from .bowtie import bowtie, bowtie_build

__all__ = [
    "bowtie",
    "bowtie_build",
]
