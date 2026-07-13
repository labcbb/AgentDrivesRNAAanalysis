"""miRNA quantification and prediction utilities.

Provides wrappers for miRDeep2 tools:
- ``quantify_mirna`` — quantify known miRNAs (mapper.pl + quantifier.pl)
- ``predict_mirna`` — predict known and novel miRNAs (mapper.pl + miRDeep2.pl)
"""

from .mirdeep2 import predict_mirna, quantify_mirna

__all__ = [
    "quantify_mirna",
    "predict_mirna",
]
