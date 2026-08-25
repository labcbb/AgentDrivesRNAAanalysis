# Enrichr Gene Enrichment Reference

## Function

```python
adata = sa.target.enrichr(
    adata,
    genes,
    gene_sets="references/msigdb/c2.cp.kegg_legacy.v2026.1.Hs.symbols.gmt",
    organism="human",
    background=None,
    cutoff=0.05,
    force=False,
    result_key="enrichr",
)
```

## Arguments

| Argument | Meaning |
|----------|---------|
| `adata` | AnnData object to annotate; the registered wrapper also accepts MuData and routes to its `srna` modality by default |
| `genes` | A gene symbol string or a list/tuple/array of gene symbols; empty values and duplicates are removed |
| `gene_sets` | Local `.gmt` path, a sequence of local GMT paths, or a custom gene-set mapping; with `None`, selects a downloaded GMT under `references/msigdb` |
| `organism` | Metadata for the source species; common aliases such as `Human`, `human`, `hs`, `Mouse`, and `mm` are normalized |
| `background` | Optional local GSEApy background: gene list, gene count, or background file |
| `cutoff` | Local GSEApy cutoff; default `0.05`, valid range `0` to `1` |
| `force` | Recompute an unchanged local GMT query instead of reusing `adata.uns["enrichr"]` |
| `result_key` | AnnData `uns` key for this result; use distinct keys for parallel contrasts such as isomiR-gained and parent-lost targets |

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

For target analysis, extract gene symbols from `geneName` in a cached starBase
TSV or `gene_name` in a miRanda result table, remove missing values, and
deduplicate before calling local GMT enrichment. Do not submit miRNA names,
isomiR IDs, piRNA IDs, or tRNA feature IDs as gene symbols.

## Target-Prediction Handoff

Run `mirna-target-prediction` before target-derived enrichment. It owns the
target-method decision and writes the auditable intermediate states. This skill
only converts its selected target gene symbols into a local GMT query.

| Feature | Permitted target method | Query identity |
|---------|-------------------------|----------------|
| Canonical mature miRNA | Cached starBase evidence or local miRanda | Mature miRNA ID for starBase; mature sequence for miRanda |
| Observed isomiR | Local miRanda only | Exact sequence from `adata.var["sequence"]` |

For isomiR, never substitute a parent mature miRNA or query starBase. The
unified target skill produces exact-sequence miRanda and seed results. Consume
their documented intersection:

```python
predictions = isomir_adata.uns["miranda_targets"]["results"]
seed_sites = isomir_adata.uns["seed_utr_matches"]["results"]
candidate_genes = predictions.loc[
    predictions["score"].ge(140) & predictions["energy"].le(-20),
    "gene_name",
].dropna()
candidate_genes = sorted(set(candidate_genes) & set(seed_sites["gene_name"].dropna()))
```

For canonical mature miRNAs, the target skill may use cached starBase evidence
or local miRanda; record the chosen method and selection rule before calling
`sa.target.enrichr`.

For isomiR-parent comparisons, retain separate enrichment states:

```python
comparison = isomir_adata.uns["isomir_parent_target_comparison"]
for change in ("isomir_gained", "parent_lost", "conserved"):
    genes = comparison["gene_sets"][change]
    if genes:
        isomir_adata = sa.target.enrichr(
            isomir_adata,
            genes,
            gene_sets="references/msigdb/c2.cp.kegg_legacy.symbols.gmt",
            result_key=f"{change}_enrichr",
        )
```

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
| `Gene_set` | Local GMT collection used |
| `Term` | Enriched pathway or gene-set name |
| `Overlap` | Input overlap count and set size |
| `P-value` | Local hypergeometric nominal p-value |
| `Adjusted P-value` | Multiple-testing adjusted p-value |
| `Odds Ratio` | Enrichment odds ratio |
| `Combined Score` | GSEApy local enrichment score, when provided by the installed version |
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

The enrichment itself is offline once the GMT is present. Download MSigDB GMT
files with `sa.download.download_msigdb(...)`; only that acquisition step needs
network access. Library names such as `KEGG_2016` are rejected to prevent an
accidental online Enrichr query.
