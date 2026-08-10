---
name: starbase-mirna-targets
title: miRNA target analysis with starBase/ENCORI
description: "Retrieve cached, evidence-supported miRNA-mRNA targets from the starBase/ENCORI API for user-selected or significant DE miRNAs."
---

# miRNA Target Analysis With starBase/ENCORI

Use `sa.target.starbase_mirna_targets` for miRNA target retrieval. This is an online starBase/ENCORI API query, not a local sequence-based prediction tool.

## Target selection

Choose target miRNAs in this priority order:

1. User-specified miRNA names: pass `mirnas="hsa-miR-21-5p"` or a list.
2. User-specified `adata.var` feature filters: pass `feature_filters={...}`.
3. Otherwise select significant miRNAs from `adata.uns["de_results"]` using adjusted p value <= 0.05. Do not select piRNA, tRNA, or isomiR features for this tool.

Do not run target analysis before the user has either requested a specific miRNA or requested target analysis from differential-expression results.

## Default API parameters

For human use `assembly="hg38"`; for mouse use `assembly="mmu10"`. Defaults reproduce the project standard:

```python
adata = sa.target.starbase_mirna_targets(
    adata,
    mirnas="hsa-miR-21-5p",
    assembly="hg38",
    gene_type="mRNA",
    clip_exp_num=5,
    degra_exp_num=1,
    pancancer_num=10,
    program_num=5,
    program=None,
    target="all",
    cell_type="all",
    output_dir="starbase_targets",
)
```

## Request discipline

- Queries are serial by design. Do not wrap the tool in a thread pool or run multiple API requests concurrently.
- Each `(miRNA, parameter set)` query is cached in `adata.uns["starbase_mirna_targets"]` and written to a local TSV. Reuse it unchanged when the same query is requested again.
- Use `force=True` only when the user explicitly asks to refresh a cached query.
- The current API may add columns beyond the historical 22-column response. Preserve every returned column rather than truncating the TSV.

## Outputs

For each queried miRNA, the tool writes `starbase_targets/<miRNA>.<parameter-hash>.starbase.tsv` and stores its path, parameter values, result count, columns, and fetch timestamp in `adata.uns["starbase_mirna_targets"]`.

Use these persisted TSVs and `uns` metadata as inputs to subsequent target-network analysis. Do not re-query starBase merely to inspect targets already cached locally.
