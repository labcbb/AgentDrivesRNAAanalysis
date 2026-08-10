# Enrichr Gene Enrichment Reference

## Function

```python
adata = sa.target.enrichr(
    adata,
    genes,
    gene_sets="KEGG_2016",
    organism="human",
    background=None,
    cutoff=0.05,
    force=False,
)
```

## Arguments

| Argument | Meaning |
|----------|---------|
| `adata` | AnnData object to annotate; the registered wrapper also accepts MuData and routes to its `srna` modality by default |
| `genes` | A gene symbol string or a list/tuple/array of gene symbols; empty values and duplicates are removed |
| `gene_sets` | Enrichr library name, list of library names, or custom gene-set mapping; default `KEGG_2016` |
| `organism` | Enrichr organism; common aliases such as `Human`, `human`, `hs`, `Mouse`, and `mm` are normalized |
| `background` | Optional GSEApy Enrichr background: gene list, gene count, or background file |
| `cutoff` | Enrichr cutoff passed to GSEApy; default `0.05`, valid range `0` to `1` |
| `force` | Re-run an unchanged query instead of reusing `adata.uns["enrichr"]` |

## Input Gene Selection

The tool accepts genes from any upstream analysis, but selection must be
explicit. Typical sources include:

```python
# User-selected genes
genes = ["TP53", "BRCA1", "EGFR"]

# Significant DE genes, with the project's actual DE column names
de = adata.uns["de_results"]
genes = de.loc[de["adj_p_value"] < 0.05, "feature"].dropna().unique().tolist()
```

For target analysis, extract `geneName` from the cached starBase TSV or target
table, remove missing values, and deduplicate before calling Enrichr. Do not
submit miRNA names, piRNA IDs, or tRNA feature IDs as gene symbols.

## AnnData Output

The result is stored at `adata.uns["enrichr"]`:

```python
state = adata.uns["enrichr"]
state["results"]       # pandas.DataFrame returned by GSEApy
state["input_genes"]   # normalized unique input list
state["parameters"]    # gene_sets, organism, background, cutoff
state["signature"]     # cache signature for the query
state["last_run"]      # n_input_genes, n_terms, reused, completed_at
```

The result DataFrame normally contains:

| Column | Meaning |
|--------|---------|
| `Gene_set` | Enrichr library used |
| `Term` | Enriched pathway or gene-set name |
| `Overlap` | Input overlap count and set size |
| `P-value` | Enrichr nominal p-value |
| `Adjusted P-value` | Multiple-testing adjusted p-value |
| `Odds Ratio` | Enrichment odds ratio |
| `Combined Score` | Enrichr combined score |
| `Genes` | Overlapping input genes |

Preserve all columns returned by the installed GSEApy version. After running,
write the AnnData to H5AD so the result remains available across sessions:

```python
adata.write_h5ad("enriched_adata.h5ad")
```

## Environment

The project declares GSEApy in `conda_env.yml`:

```bash
python -m pip install gseapy
```

Enrichr is an online service. A network failure is reported as an error and
does not write a partial result into `adata.uns`.
