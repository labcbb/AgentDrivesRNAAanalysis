"""miRNA quantification and prediction utilities.

Provides wrappers for:
- ``quantify_mirna`` — miRDeep2 known miRNA quantification
- ``predict_mirna`` — miRDeep2 novel miRNA prediction
- ``feature_count`` — featureCounts read summarisation over genomic features
- ``mirtop`` — mirtop isoMiR (isomiR) quantification and variant analysis
- ``normalize_cpm`` — log2(CPM+1) normalisation for any count matrix
"""

from .feature_count import feature_count
from .idxstats import idxstats, samtools_idxstats
from .mirtop import quantify_isomir, mirtop, mirtop_quant
from .mirdeep2 import normalize_cpm, predict_mirna, quantify_mirna
from .tRAX import quantify_trna_fragments, tRAX, trax_quant

__all__ = [
    "quantify_mirna",
    "predict_mirna",
    "feature_count",
    "idxstats",
    "samtools_idxstats",
    "mirtop",
    "mirtop_quant",
    "quantify_isomir",
    "normalize_cpm",
    "trax_quant",
    "tRAX",
    "quantify_trna_fragments",
]
