## Full differential expression pipeline

```python
import sRNAgent as sa
import anndata as ad

# ── 1. Load quantified miRNA data ──
adata = ad.read_h5ad("adata_mirdeep2_quantified.h5ad")

# ── 2. Confirm group labels with user ──
# adata.obs should have a column with group labels (any column name)
# Show the user and confirm before proceeding
group_col = "group"  # 也可以换成 "Condition"、"treatment" 等
print(adata.obs[[group_col]].to_string())

# ── 3. Filter low expression ──
adata.layers["counts"] = adata.X.copy()
sa.diff.filter_low_expression(adata, min_mean=1.0)

# ── 4. Differential expression ──
sa.diff.de_analysis(adata, control_group="Normal")

# ── 5. Check specific miRNAs ──
de = adata.uns["de_results"]
for mirna in ["hsa-miR-21-5p", "hsa-miR-21-3p", "hsa-miR-218-5p"]:
    if mirna in de.index:
        r = de.loc[mirna]
        print(f"{mirna}: logFC={r['log_fc']:.1f}, P={r['p_value']:.2e}, FDR={r['adj_p_value']:.2e}")

# ── 6. Summary ──
sig = de[de["adj_p_value"] < 0.05]
print(f"Significant (FDR<0.05): {len(sig)} ({(sig['log_fc']>0).sum()} up, {(sig['log_fc']<0).sum()} down)")

# ── 7. Save ──
adata.write("de_results.h5ad")
```

## Minimal: quick DE with existing group column

```python
import sRNAgent as sa
import anndata as ad

adata = ad.read_h5ad("quantified.h5ad")
# Assumes adata.obs["group"] already set

adata.layers["counts"] = adata.X.copy()
sa.diff.filter_low_expression(adata, min_mean=1.0)
sa.diff.de_analysis(adata, control_group="Ctrl")

print(adata.uns["de_results"].head())
adata.write("de_results.h5ad")
```

## Filter + DE with explicit group column

```python
import sRNAgent as sa
import anndata as ad

adata = ad.read_h5ad("quantified.h5ad")

adata.layers["counts"] = adata.X.copy()
sa.diff.filter_low_expression(adata, min_mean=1.0)
sa.diff.de_analysis(adata, group_col="Condition", control_group="WT")

de = adata.uns["de_results"]
print(de.head())
```

## Appendix: Fetch group info from ENA + GEO (if not available)

Use when samples are SRA Run IDs and no group info is available:

```python
import urllib.request, urllib.parse

# Step 1: Fetch SRR → sample_title from ENA
params = {
    "accession": "SRP335685",         # ← 替换为实际 Study 编号
    "result": "read_run",
    "fields": "run_accession,sample_title,experiment_alias",
    "format": "tsv", "limit": "0",
}
url = "https://www.ebi.ac.uk/ena/portal/api/filereport?" + urllib.parse.urlencode(params)
with urllib.request.urlopen(url, timeout=30) as resp:
    data = resp.read().decode()

srr_to_sample = {}
gsm_set = set()
for line in data.strip().split("\n")[1:]:
    parts = line.split("\t")
    if len(parts) >= 3:
        srr_to_sample[parts[0]] = parts[1]
        gsm_set.add(parts[2])

# Step 2: Query GEO for tissue type
tumor, normal = set(), set()
for gsm in sorted(gsm_set):
    url = f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={gsm}&targ=gsm&form=text"
    with urllib.request.urlopen(url, timeout=15) as resp:
        geo = resp.read().decode()
    title, source = "", ""
    for line in geo.split("\n"):
        if line.startswith("!Sample_title"):
            title = line.split("=", 1)[-1].strip()
        if line.startswith("!Sample_source_name_ch1"):
            source = line.split("=", 1)[-1].strip()
    if "tumor" in source.lower() or "cancer" in source.lower():
        tumor.add(title)
    elif "normal" in source.lower() or "noncancerous" in source.lower():
        normal.add(title)

# Step 3: Assign to adata
groups = []
for srr in adata.obs_names:
    s = srr_to_sample.get(srr, "")
    if s in tumor:
        groups.append("Tumor")
    elif s in normal:
        groups.append("Normal")
    else:
        groups.append("Unknown")
adata.obs["group"] = groups
```

## Key function signatures

```python
sa.diff.filter_low_expression(
    adata,                    # AnnData; reads from layers["counts"] or X
    min_mean=1.0,             # minimum mean count threshold
)

sa.diff.de_analysis(
    adata,                    # AnnData with raw counts in X
    group_col=None,           # auto-detected if None
    control_group=None,       # first group alphabetically if None
)
```
