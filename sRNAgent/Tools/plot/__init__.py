"""Result-driven, publication-style visualisation tools."""

from .qc import alignment_summary, qc_metrics
from .expression import pca, rna_composition, sample_correlation
from .differential import de_heatmap, enrichment_dotplot, volcano
from .fragmentomics import fragment_heatmap, fragment_profile
from .model import candidate_priorities, classification_performance, cox_cross_validation, cox_forest
from .registry import available_plots, generate_plots
from .target import target_network

available = available_plots
generate = generate_plots

__all__ = ["available", "generate", "qc_metrics", "alignment_summary", "pca", "sample_correlation", "rna_composition", "volcano", "de_heatmap", "enrichment_dotplot", "fragment_profile", "fragment_heatmap", "target_network", "candidate_priorities", "classification_performance", "cox_forest", "cox_cross_validation"]
