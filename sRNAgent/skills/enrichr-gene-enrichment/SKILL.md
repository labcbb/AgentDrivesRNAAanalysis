---
name: enrichr-gene-enrichment
description: "Run gene-set and pathway enrichment through GSEApy Enrichr for a user-provided gene list, storing the results and parameters in AnnData."
---

# Gene Enrichment Analysis with Enrichr

## Overview

This skill runs Enrichr gene-set enrichment with `sa.target.enrichr`. It accepts
an explicit gene list, sends it to the Enrichr service through GSEApy, and saves
the complete result table in `adata.uns["enrichr"]`.

For the full function signature, output schema, and input-selection guidance,
see [reference.md](reference.md).

| Item | Default | Description |
|------|---------|-------------|
| Tool | `sa.target.enrichr` | GSEApy Enrichr wrapper |
| Gene set | `KEGG_2016` | Human KEGG pathway library |
| Organism | `human` | Human; `Human`, `hs`, and `homo_sapiens` are accepted |
| Result | `adata.uns["enrichr"]["results"]` | Enrichr result DataFrame |

> 模态边界：该 skill 只向传入的单个 AnnData 写入 `uns`。不创建 MuData，也不合并不同模态的表达矩阵。

## Prerequisites

- An AnnData object containing the analysis provenance.
- A user-specified or explicitly selected gene list. Do not submit all genes from `adata.var` without a stated selection rule.
- Network access to Enrichr and the `gseapy` dependency in the analysis environment.

Gene lists may come from user input, a selected subset of DE genes, or genes
from a miRNA target-analysis result. When the source is a table, first state the
selection threshold and extract unique gene symbols before enrichment.

## Instructions

### 1. Human KEGG enrichment (default)

```python
import sRNAgent as sa

genes = ["TP53", "BRCA1", "EGFR"]
adata = sa.target.enrichr(adata, genes)
```

This uses `gene_sets="KEGG_2016"` and `organism="human"`. The documented
GSEApy spelling `organism="Human"` is also accepted and normalized internally.

### 2. Select another Enrichr library

Pass the exact Enrichr library name requested by the user:

```python
adata = sa.target.enrichr(
    adata,
    genes=selected_genes,
    gene_sets="KEGG_2021_Human",
    organism="human",
)
```

Use `organism="mouse"` for mouse genes and choose a matching Enrichr library.
Do not silently replace a user-selected library with KEGG.

### 3. Read and persist results

```python
result = adata.uns["enrichr"]["results"]
print(result[["Term", "Overlap", "Adjusted P-value", "Genes"]].head(10))

# Persist the AnnData so enrichment remains available in later sessions.
adata.write_h5ad("enriched_adata.h5ad")
```

The result table preserves Enrichr columns such as `Term`, `P-value`,
`Adjusted P-value`, `Odds Ratio`, `Combined Score`, and `Genes`. Query metadata
is stored in `adata.uns["enrichr"]["parameters"]` and the input genes in
`adata.uns["enrichr"]["input_genes"]`.

### 4. Reuse an existing result

The tool reuses `adata.uns["enrichr"]` when the gene list and parameters are
unchanged. Use `force=True` only when the user explicitly asks to refresh the
remote query:

```python
adata = sa.target.enrichr(
    adata,
    genes,
    gene_sets="KEGG_2016",
    organism="human",
    force=True,
)
```
