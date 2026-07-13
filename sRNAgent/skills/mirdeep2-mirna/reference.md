## Data preparation

```python
import sRNAgent as sa

# miRBase
sa.reference.download_mirbase("hsa", output_dir="ref", jobs=4)

# Genome reference + Bowtie index
sa.reference.download_genome("homo_sapiens", output_dir="ref", jobs=8)
sa.alignment.bowtie_build("ref/GRCh38.primary_assembly.genome.fa",
                          "ref/grch38", threads=8)
```

## Init AnnData

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# Single sample
adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["fastq_path"] = ["trimmed/S1_trimmed.fastq.gz"]

# Multiple samples
adata = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2", "S3"]))
adata.obs["fastq_path"] = [
    "trimmed/S1_trimmed.fastq.gz",
    "trimmed/S2_trimmed.fastq.gz",
    "trimmed/S3_trimmed.fastq.gz",
]
```

## Quantify known miRNAs (single sample)

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["fastq_path"] = ["trimmed/S1_trimmed.fastq.gz"]

adata = sa.quant.quantify_mirna(
    adata,
    genome_index="ref/grch38",
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    output_dir="mirdeep2",
)
print(f"Expression:     {adata.obs['counts_csv'].iloc[0]}")
print(f"Count matrix:   {adata.X.shape}")
print(f"miRNA IDs:      {adata.var['mirna_id'].tolist()[:5]}")
```

## Quantify with adapter clipping

```python
adata = sa.quant.quantify_mirna(
    adata,
    genome_index="ref/grch38",
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    adapter="TGGAATTCTCGGGTGCCAAGG",
    min_length=18,
    output_dir="mirdeep2",
)
```

## Batch quantify 3 samples with 3 jobs

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2", "S3"]))
adata.obs["fastq_path"] = [
    "trimmed/S1_trimmed.fastq.gz",
    "trimmed/S2_trimmed.fastq.gz",
    "trimmed/S3_trimmed.fastq.gz",
]

adata = sa.quant.quantify_mirna(
    adata,
    genome_index="ref/grch38",
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    output_dir="mirdeep2",
    jobs=3,
)

# Cross-sample expression matrix
print(f"Count matrix shape: {adata.X.shape}")
print(f"Samples: {adata.obs_names.tolist()}")
print(f"miRNAs:  {len(adata.var)}")

# Per-sample file paths
print(adata.obs[["collapsed_path", "arf_path", "counts_csv"]])
```

## Access count matrix and miRNA IDs

```python
# Count matrix (n_samples x n_mirnas)
print(adata.X.shape)

# Export to DataFrame
import pandas as pd
exp_df = pd.DataFrame(
    adata.X.toarray() if hasattr(adata.X, 'toarray') else adata.X,
    index=adata.obs_names,
    columns=adata.var["mirna_id"],
)
print(exp_df.iloc[:3, :5])

# miRNA IDs
print(adata.var["mirna_id"].head())
```

## Predict known + novel miRNAs (single sample)

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["fastq_path"] = ["trimmed/S1_trimmed.fastq.gz"]

adata = sa.quant.predict_mirna(
    adata,
    genome_index="ref/grch38",
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    output_dir="mirdeep2",
)
print(f"Result HTML: {adata.obs['prediction_html'].iloc[0]}")
print(f"Result CSV:  {adata.obs['prediction_csv'].iloc[0]}")
```

## Predict with related species and strict filtering

```python
adata = sa.quant.predict_mirna(
    adata,
    genome_index="ref/grch38",
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    related_mature_fa="ref/mature_mmu.fa",
    species="hsa",
    score_cutoff=4,
    min_stack=10,
)
```

## Mouse (mmu) known miRNA quantification

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

sa.reference.download_mirbase("mmu", output_dir="ref", jobs=4)

adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["fastq_path"] = ["S1.fastq.gz"]

adata = sa.quant.quantify_mirna(
    adata,
    genome_index="ref/grcm39",
    mature_fa="ref/mature_mmu.fa",
    hairpin_fa="ref/hairpin_mmu.fa",
    species="mmu",
)
```

## Full pipeline: init -> cutadapt -> quantify -> predict

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# ── Init AnnData ──
adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["fastq_path"] = ["SRR26304152.fastq.gz"]

# ── References ──
sa.reference.download_mirbase("hsa", output_dir="ref", jobs=4)
sa.reference.download_genome("homo_sapiens", output_dir="ref", jobs=8)
sa.alignment.bowtie_build("ref/GRCh38.primary_assembly.genome.fa",
                          "ref/grch38", threads=8)

# ── Trim ──
adata = sa.fastq.cutadapt(adata,
                          adapter_3="TGGAATTCTCGGGTGCCAAGG",
                          min_length=18, max_length=36,
                          output_dir="trimmed")

# ── Quantify known miRNAs ──
adata = sa.quant.quantify_mirna(adata,
                                genome_index="ref/grch38",
                                mature_fa="ref/mature_hsa.fa",
                                hairpin_fa="ref/hairpin_hsa.fa",
                                output_dir="mirdeep2")
print(f"Counts:  {adata.X.shape}")
print(f"miRNAs:  {adata.var['mirna_id'].tolist()[:5]}")

# ── Predict novel miRNAs ──
adata = sa.quant.predict_mirna(adata,
                               genome_index="ref/grch38",
                               genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
                               mature_fa="ref/mature_hsa.fa",
                               hairpin_fa="ref/hairpin_hsa.fa",
                               output_dir="mirdeep2")
print(f"Novel report: {adata.obs['prediction_html'].iloc[0]}")
```

## Key function signatures

```python
sa.quant.quantify_mirna(
    adata,                    # AnnData; reads from adata.obs["fastq_path"] / ["trimmed_path"]
    genome_index="grch38",    # Bowtie index basename
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    species="hsa",            # 3-letter species code
    output_dir="mirdeep2",
    adapter=None,             # 3' adapter sequence
    min_length=18,            # min read length
    max_multi=5,              # max mapping positions
    one_mismatch_seed=False,  # allow 1 seed mismatch
    mismatches=1,             # mismatches vs precursors
    upstream=2,               # upstream bases
    downstream=5,             # downstream bases
    discard_multimappers=False,
    prefix="seq",
    jobs=None,                # parallel samples
    force=False,
)

sa.quant.predict_mirna(
    adata,                    # AnnData; reads from adata.obs["fastq_path"] / ["trimmed_path"]
    genome_index="grch38",
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    related_mature_fa=None,   # related species mature miRNAs
    species="hsa",
    output_dir="mirdeep2",
    adapter=None,
    min_length=18,
    max_multi=5,
    one_mismatch_seed=False,
    score_cutoff=0,           # min score for novel miRNAs
    min_stack=None,           # min read stack height
    prefix="seq",
    jobs=None,
    force=False,
)
```
