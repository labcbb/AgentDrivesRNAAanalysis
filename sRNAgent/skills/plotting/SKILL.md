---
name: plotting
description: "Generate grouped, publication-ready CNS-style plots from existing AnnData results using sRNAgent plot tools. Use for QC, alignment, expression, differential expression, enrichment, small-RNA/isomiR, fragmentomics, miRNA-target networks, classification, or Cox visualisation, including batch plot discovery and export."
---

# Scientific Plotting

Use this skill after analysis results already exist in an AnnData object. Plot tools consume stored `obs`, `var`, `layers`, `obsm`, and `uns` results only; they must not rerun QC, quantification, differential expression, enrichment, target retrieval, or modelling.

## Workflow

1. Identify the requested figure and the relevant AnnData modality. Use the dedicated `fragmentomics_adata` for fragmentomics, never the small-RNA AnnData.
2. Inspect plot availability before creating a batch:

```python
available = sa.plot.available(adata)
print({name: item for name, item in available.items() if item["available"]})
```

3. When a group comparison is requested, confirm the explicit `group_col` from `adata.obs`. Do not infer groups from sample IDs or ordering.
4. Generate only the requested plots, or batch-generate existing results. Save the AnnData after plotting so `adata.uns["plots"]` persists.

```python
result = sa.plot.generate(
    adata,
    scope="standard",
    group_col="condition",
    output_dir="plots",
)
print(result)  # generated / skipped / failed
adata.write("results_with_plots.h5ad")
```

`scope="minimal"` produces the central figures that are available; `scope="standard"` adds QC, composition, enrichment, fragmentomics, target, and validation figures; `scope="all"` attempts every registered plot. Missing prerequisites are reported in `skipped` and must not trigger analysis recomputation.

## Visual Contract

All `sa.plot.*` tools use the shared CNS-style static theme and export `PNG` (350 dpi), editable `PDF`, and editable-text `SVG`. Figures use a white background, restrained typography, thin axes, direct labels where practical, and this fixed palette:

```text
#A1A0A5  #db7094  #e79db6  #becfe9  #a3c0c8
#c5d2b8  #8babd2  #ff9fa0  #ffc080
```

Do not override the palette with unrelated colours. `group_col` maps sorted group levels deterministically to this palette. Every plot is registered in `adata.uns["plots"]` with source, parameters, timestamp, and output paths.

## Plot Catalogue

| Result | Function | Required stored result |
|---|---|---|
| QC metrics | `sa.plot.qc_metrics` | cutadapt / MultiQC / Bowtie numeric `obs` columns |
| Alignment rate | `sa.plot.alignment_summary` | `obs["bowtie_alignment_rate"]` |
| PCA | `sa.plot.pca` | `obsm["X_pca"]` |
| Sample correlation | `sa.plot.sample_correlation` | expression matrix/layer |
| RNA composition | `sa.plot.rna_composition` | `var["rna_type"]`, counts layer |
| Volcano / DE heatmap | `sa.plot.volcano`, `sa.plot.de_heatmap` | `uns["de_results"]` |
| Enrichment dotplot | `sa.plot.enrichment_dotplot` | `uns["enrichr"]["results"]` |
| Fragmentomics | `sa.plot.fragment_profile`, `sa.plot.fragment_heatmap` | fragmentomics `var["type"]`, `layers["CPM"]` |
| Target network | `sa.plot.target_network` | cached starBase target TSV records |
| Classification | `sa.plot.classification_performance` | `uns["classification"]["performance"]` |
| Cox | `sa.plot.cox_forest`, `sa.plot.cox_cross_validation` | `uns["cox"]` result tables |

## Grouped Figures

Use `group_col` for plots that compare samples or groups:

```python
sa.plot.qc_metrics(adata, group_col="condition")
sa.plot.pca(adata, group_col="condition")
sa.plot.sample_correlation(adata, group_col="condition")
sa.plot.de_heatmap(adata, group_col="condition")
```

For fragmentomics, pass the grouping column from the independent fragmentomics AnnData:

```python
sa.plot.fragment_profile(
    fragmentomics_adata,
    feature_type="FSD",
    group_col="condition",
)
sa.plot.fragment_heatmap(
    fragmentomics_adata,
    feature_type="BPM_START",
    group_col="condition",
)
```

Available feature types are recorded in `fragmentomics_adata.var["type"]`; use only an existing type such as `FSD`, `FSC`, `RCD`, `EDM_5P`, `EDM_3P`, `BPM_START`, or `BPM_END`.

## Differential, Targets, and Models

```python
# Differential expression and enrichment
sa.plot.volcano(adata, fdr=0.05, abs_logfc=1.0)
sa.plot.de_heatmap(adata, top_n=30, group_col="condition")
sa.plot.enrichment_dotplot(adata, top_n=15)

# Cached starBase targets only: no API call during plotting.
sa.plot.target_network(adata, top_mirnas=10, top_targets=15)

# Classification and Cox results.
sa.plot.classification_performance(adata, metric="roc_auc")
sa.plot.cox_forest(adata, result="multivariate", top_n=25)
sa.plot.cox_cross_validation(adata)
```

The target-network tool writes the displayed network plus a complete edge table (`.edges.tsv`) and Cytoscape-compatible (`.graphml`) graph beside the figure. Keep `top_mirnas` and `top_targets` bounded for an interpretable manuscript figure; use the exported network files for exhaustive exploration.

## Output Discipline

Use a named `output_dir` for each analysis. Do not use generated pictures as proof that upstream analysis succeeded; always inspect the corresponding stored result table. For a manuscript composite or a custom multi-panel figure beyond the registered plots, use the `nature-figure` skill after these source figures have been generated.
