"""Differential expression and QC analysis utilities.

Provides wrappers for:
- ``filter_low_expression`` — filter lowly expressed miRNAs (mean count ≤ 1)
- ``pca_logcpm`` — PCA on logcpm-normalised expression (scanpy backend)
- ``de_analysis`` — limma-voom differential expression via pylimma
"""

from .pylimma import de_analysis
from .qc import filter_low_expression, pca_logcpm

__all__ = [
    "filter_low_expression",
    "pca_logcpm",
    "de_analysis",
]
