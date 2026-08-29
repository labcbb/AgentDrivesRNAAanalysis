---
name: reporting
description: "Generate traceable, result-only HTML scientific reports from one or more independent AnnData modalities and existing sRNAgent plots. Use for analysis report, HTML report, QC-to-modeling summary, multi-modal result delivery, artifact manifest, or reproducible reporting requests."
---

# Analysis Reporting

Generate the scientific analysis report only after results and figures already exist. `sa.report.html` reads stored AnnData state and registered figures; it must not rerun analysis, generate missing plots, contact remote APIs, or combine independent modalities into one matrix.

## Inputs and Modality Boundary

Use one explicit AnnData per modality. Reports can display them side by side, but must not merge their expression matrices, rewrite their results, or run cross-modal modelling.

```python
report = sa.report.html(
    srna_adata=srna_adata,
    fragmentomics_adata=fragmentomics_adata,
    isomir_adata=isomir_adata,
    rna_adata=rna_adata,
    output_dir="analysis_report",
    title="Small RNA analysis report",
    group_col="condition",
    level="standard",
)
```

For one modality, pass it positionally and save the returned object:

```python
adata = sa.report.html(
    adata,
    output_dir="analysis_report",
    group_col="condition",
)
adata.write("results_with_report.h5ad")
```

When a grouped result is reported, use a known `group_col` from each modality's `adata.obs`. The report records unavailable group columns instead of guessing labels or sample ordering.

## Before Reporting

1. Use existing figures in `adata.uns["plots"]`. Generate additional figures explicitly with the `plotting` skill before reporting when required.
2. Verify essential upstream results with small slices only. Examples: `de_results`, `enrichr["results"]`, `candidate_prioritization["audit"]`, `classification["performance"]`, or `cox["multivariate_results"]`.
3. Save all supplied AnnData objects after report generation so their `uns["report"]` provenance persists.

Do not confuse the following outputs:

- `multiqc_report.html`: sequencing QC report.
- UI `run_report.json`: Agent execution log.
- `analysis_report/report.html`: scientific analysis report built by this skill.

## Report Levels

| Level | Default included sections |
|---|---|
| `minimal` | Expression, DE, candidate prioritization, classification/Cox, artifacts |
| `standard` | QC, alignment, expression, DE, enrichment, fragmentomics, target network, candidate prioritization, modeling, artifacts |
| `publication` | DE, enrichment, target network, candidate prioritization, classification/Cox, artifacts; use selected figures only |

The report only includes a figure if a registered plot is available. It marks an unavailable section as not executed rather than generating a blank figure or recomputing a result.

## Contents and Outputs

`analysis_report/` contains:

```text
report.html
report_manifest.json
assets/plots/<modality>/
assets/artifacts/<modality>/
tables/
```

The report copies registered figures and recorded file artifacts into relative paths, so the output directory can be moved as a self-contained deliverable. It displays at most `top_n=15` rows per table and exports complete CSV tables. It also collects methods/provenance recorded in `de_params`, `fragomics_params`, feature selection, candidate prioritization, classification, Cox, and upstream tool metadata.

When `uns["candidate_prioritization"]` exists, the report includes both the eligible recommendation table and the full audit table. The audit's `exclusion_reasons` and `evidence_gaps` are part of the scientific record and must not be removed in favor of a recommendation-only summary. The report only copies the existing audit CSV/manifest and registered priority figure; it never recomputes DE, models, targets, enrichment, or candidate ranks.

Each supplied AnnData receives:

```python
adata.uns["report"]
```

This records the HTML path, manifest path, report level, group column, included sections, and figure/table count.

## Result-Only Rule

Use `include_existing_plots=True` by default. Set it to `False` only for a table-and-provenance report. Do not request missing figures from inside `sa.report.html`; call the appropriate `sa.plot.*` function first, then run the report function again.

For publication composites, use the exported source figures and tables from this report as the basis for external figure assembly. The HTML report is an analysis deliverable, not a substitute for a curated manuscript figure.
