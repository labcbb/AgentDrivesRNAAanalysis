# Fragmentomics Reference

## Minimal run

```python
# bam_path must be coordinate-sorted whole-genome BAM compatible with genome_fasta
result = sa.fragment.fragomics(
    adata,
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
    output_dir="fragmentomics_out",
)
```

## Tune motif/window size

```python
# do not use transcriptome / miRNA / piRNA reference BAM here
result = sa.fragment.fragomics(
    adata,
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
    output_dir="fragmentomics_out",
    motif_k=8,
    region_size=1_000_000,
    jobs=8,
)
```

## Use genome FASTA from adata.uns

```python
adata.uns["genome_fasta"] = "ref/GRCh38.primary_assembly.genome.fa"
result = sa.fragment.fragomics(adata, output_dir="fragmentomics_out", jobs=8)
```

## BAM requirement

```python
# accepted:
# - coordinate-sorted BAM from whole-genome alignment
# - BAM header references must match genome_fasta chromosome names and lengths

# rejected:
# - SAM
# - unsorted BAM
# - BAM aligned against transcriptome / miRNA / piRNA / local reference databases
```

## Use the independent fragmentomics result

```python
result = sa.fragment.fragomics(
    adata,
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
)

# Compatibility extraction only; do not perform cross-modal analysis.
frag = result.mod["fragmentomics"] if hasattr(result, "mod") else result
frag.layers["counts"]
frag.layers["CPM"]
frag.var[["type", "feature"]].head()
```

## Read merged outputs

```python
frag = result.mod["fragmentomics"] if hasattr(result, "mod") else result

raw_tsv = frag.uns["fragomics_raw_tsv"]
cpm_tsv = frag.uns["fragomics_cpm_tsv"]

raw_df = pd.read_csv(raw_tsv, sep="\t")
cpm_df = pd.read_csv(cpm_tsv, sep="\t")
```

## Check feature subsets

```python
frag = result.mod["fragmentomics"] if hasattr(result, "mod") else result

fsd = frag[:, frag.var["type"] == "FSD"]
fsc = frag[:, frag.var["type"] == "FSC"]
rcd = frag[:, frag.var["type"] == "RCD"]
edm5 = frag[:, frag.var["type"] == "EDM_5P"]
bpm = frag[:, frag.var["type"].isin(["BPM_START", "BPM_END"])]
```

## Save outputs

```python
frag = result.mod["fragmentomics"] if hasattr(result, "mod") else result
frag.write("fragmentomics.h5ad")
```

## Safe handling rule

```python
# always capture the return value:
result = sa.fragment.fragomics(...)

# Extract the fragmentomics result and keep it as a separate AnnData.
if hasattr(result, "mod"):
    frag = result.mod["fragmentomics"]
else:
    frag = result
frag.write("fragmentomics.h5ad")
```
