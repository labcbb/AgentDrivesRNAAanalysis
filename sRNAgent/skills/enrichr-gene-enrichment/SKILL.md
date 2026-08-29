---
name: enrichr-gene-enrichment
description: "Run local GMT gene-set and pathway enrichment through GSEApy for a user-provided gene list, including genes derived from canonical miRNA starBase/miRanda targets or exact isomiR miRanda predictions, storing results and parameters in AnnData."
---

# Gene Enrichment Analysis with Enrichr

## Overview

This skill runs Enrichr-compatible local gene-set enrichment with
`sa.target.enrichr`. It accepts an explicit gene list and a local MSigDB GMT,
then saves the complete result table in `adata.uns["enrichr"]`. The enrichment
query does not contact the Enrichr service.

For the full function signature, output schema, and input-selection guidance,
see [reference.md](reference.md).

| Item | Default | Description |
|------|---------|-------------|
| Tool | `sa.target.enrichr` | GSEApy local GMT wrapper |
| Gene set | local MSigDB GMT | Downloaded KEGG GMT, when available |
| Organism | `human` | Human; `Human`, `hs`, and `homo_sapiens` are accepted |
| Result | `adata.uns["enrichr"]["results"]` | Enrichr result DataFrame |

> 模态边界：该 skill 只向传入的单个 AnnData 写入 `uns`。不创建 MuData，也不合并不同模态的表达矩阵。

## Prerequisites

- An AnnData object containing the analysis provenance.
- A user-specified or explicitly selected gene list. Do not submit all genes from `adata.var` without a stated selection rule.
- A local `.gmt` file. Download it once with `sa.download.download_msigdb`; network access is only required for that download.
- The `gseapy` dependency in the analysis environment.

Gene lists may come from user input, a selected subset of DE genes, or genes
from a miRNA target-analysis result. When the source is a table, first state the
selection threshold and extract unique gene symbols before enrichment. For a
miRNA/isomiR-derived list, run `mirna-target-prediction` first; it owns method
selection, target evidence, and seed-site validation. This skill only consumes
the resulting documented gene-symbol list.

## Instructions

### 1. Download a human KEGG GMT once

```python
import sRNAgent as sa

msigdb = sa.download.download_msigdb("human", output_dir="references/msigdb")
```

The downloader selects a KEGG symbols GMT where the MSigDB release provides
one. A downloaded file under `references/msigdb` becomes the default local GMT.

### 2. Consume miRNA/isomiR target genes

Do not duplicate target prediction here. Use the selected output of
`mirna-target-prediction`: canonical miRNA target genes may come from starBase
or miRanda, while isomiR target genes must be exact-sequence miRanda candidates
with documented seed support. Do not submit miRNA or isomiR IDs themselves to
GMT enrichment.

```python
predictions = isomir_adata.uns["miranda_targets"]["results"]
seed_sites = isomir_adata.uns["seed_utr_matches"]["results"]
selected_genes = predictions.loc[
    predictions["score"].ge(140) & predictions["energy"].le(-20),
    "gene_name",
].dropna()
selected_genes = sorted(set(selected_genes) & set(seed_sites["gene_name"].dropna()))
```

For parent-miRNA versus isomiR comparisons, consume one explicit target-change
set at a time instead of their union:

```python
comparison = isomir_adata.uns["isomir_parent_target_comparison"]
gained_genes = comparison["gene_sets"]["isomir_gained"]

isomir_adata = sa.target.enrichr(
    isomir_adata,
    genes=gained_genes,
    gene_sets=msigdb["gmt"],
    organism="human",
    result_key="isomir_gained_enrichr",
)
```

Run `parent_lost` and `conserved` with their own result keys. Do not overwrite
one contrast with another at `adata.uns["enrichr"]`.

The unified skill records all upstream evidence in `adata.uns`. Preserve the
target method, score/energy threshold, and seed-support policy alongside the
gene list in the enrichment report.

### 3. Run local GMT enrichment

Pass the returned local GMT path explicitly when reproducibility requires it:

```python
adata = sa.target.enrichr(
    adata,
    genes=selected_genes,
    gene_sets=msigdb["gmt"],
    organism="human",
)
```

For another collection, list and download its matching MSigDB GMT first:

```python
available = sa.download.list_msigdb_collections("human")
reactome = sa.download.download_msigdb(
    "human", collection="c2.cp.reactome", output_dir="references/msigdb"
)
adata = sa.target.enrichr(adata, selected_genes, gene_sets=reactome["gmt"])
```

### 4. Read and persist results

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

### 5. Reuse an existing result

The tool reuses `adata.uns["enrichr"]` when the gene list and parameters are
unchanged. Use `force=True` only when the user explicitly asks to recompute
against the local GMT:

```python
adata = sa.target.enrichr(
    adata,
    genes,
    gene_sets=msigdb["gmt"],
    organism="human",
    force=True,
)
```
