## Environment setup

```bash
# Verify tools
bowtie --version
bowtie-build --version

# Download GRCh38 human reference genome (one time only)
wget https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_50/GRCh38.primary_assembly.genome.fa.gz
gunzip GRCh38.primary_assembly.genome.fa.gz
```

## Build Bowtie index (one time per genome)

```python
import sRNAgent as sa

sa.alignment.bowtie_build(
    "GRCh38.primary_assembly.genome.fa",
    "grch38",
    threads=8,
)
```

## Stringent sRNA-seq alignment (0 mismatch, unique only)

Single sample:

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["trimmed_path"] = "trimmed/S1_trimmed.fastq.gz"

adata = sa.alignment.bowtie(
    adata,
    index_basename="grch38",
    total_mismatches=0,
    m=1,
    best=True,
    strata=True,
    output_dir="aligned",
)
print(f"SAM: {adata.obs['sam_path'].iloc[0]}")
```

## Permissive sRNA-seq alignment (1 mismatch, unique only)

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["trimmed_path"] = "trimmed/S1_trimmed.fastq.gz"

adata = sa.alignment.bowtie(
    adata,
    index_basename="grch38",
    total_mismatches=1,
    m=1,
    best=True,
    strata=True,
    output_dir="aligned",
)
```

## Multi-mapping alignment (1 mismatch, up to 10 hits)

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["trimmed_path"] = "trimmed/S1_trimmed.fastq.gz"

adata = sa.alignment.bowtie(
    adata,
    index_basename="grch38",
    total_mismatches=1,
    k=10,
    best=True,
    output_dir="aligned",
)
```

## Batch align 10 trimmed sRNA-seq samples

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2", "S3", "S4", "S5",
                                           "S6", "S7", "S8", "S9", "S10"]))
adata.obs["trimmed_path"] = [
    "trimmed/S1_trimmed.fastq.gz",
    "trimmed/S2_trimmed.fastq.gz",
    "trimmed/S3_trimmed.fastq.gz",
    "trimmed/S4_trimmed.fastq.gz",
    "trimmed/S5_trimmed.fastq.gz",
    "trimmed/S6_trimmed.fastq.gz",
    "trimmed/S7_trimmed.fastq.gz",
    "trimmed/S8_trimmed.fastq.gz",
    "trimmed/S9_trimmed.fastq.gz",
    "trimmed/S10_trimmed.fastq.gz",
]

adata = sa.alignment.bowtie(
    adata,
    index_basename="grch38",
    total_mismatches=0,
    m=1,
    best=True,
    strata=True,
    output_dir="aligned",
    jobs=4,
)

print(adata.obs["sam_path"])
```

## Complete pipeline: trim → align (human genome)

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# 创建 AnnData 对象，设置原始 FASTQ 路径
adata = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2", "S3"]))
adata.obs["fastq_path"] = [
    "srna_fastq/SRR26304152.fastq.gz",
    "srna_fastq/SRR26304153.fastq.gz",
    "srna_fastq/SRR26304154.fastq.gz",
]

# ── Step 1: 3' adapter trimming ──
adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    min_length=18,
    max_length=36,
    quality_cutoff="20",
    output_dir="trimmed",
    jobs=3,
)

# ── Step 2: Align to human genome ──
adata = sa.alignment.bowtie(
    adata,
    index_basename="grch38",
    total_mismatches=0,
    m=1,
    best=True,
    strata=True,
    output_dir="aligned",
    jobs=3,
)

# ── Step 3: Summary ──
for _, row in adata.obs.iterrows():
    sam_path = row["sam_path"]
    import os
    size_mb = os.path.getsize(sam_path) / 1_000_000 if os.path.exists(sam_path) else 0
    print(f"{row.name}: {sam_path} ({size_mb:.1f} MB)")
```

## SAM to BAM (view stats)

```python
import subprocess

sam_path = "aligned/S1.sam"

# Count aligned reads
result = subprocess.run(
    ["samtools", "view", "-F", "4", "-c", sam_path],
    capture_output=True, text=True,
)
print(f"Aligned reads: {result.stdout.strip()}")

# Count unaligned reads
result = subprocess.run(
    ["samtools", "view", "-f", "4", "-c", sam_path],
    capture_output=True, text=True,
)
print(f"Unaligned reads: {result.stdout.strip()}")

# SAM → sorted BAM
subprocess.run(["samtools", "sort", "-o", sam_path.replace(".sam", ".bam"), sam_path], check=True)
subprocess.run(["samtools", "index", sam_path.replace(".sam", ".bam")], check=True)
```

## Key function signatures

```python
sa.alignment.bowtie_build(
    reference,                     # str or list of FASTA paths
    index_basename,                # output index prefix, e.g. "grch38"
    offrate=None,                  # override offrate (smaller = faster alignment)
    threads=1,                     # threads for index building
    verbose=False,                 # verbose output
    extra_args=None,
)

sa.alignment.bowtie(
    adata,                         # AnnData object; reads from adata.obs["trimmed_path"] or adata.obs["fastq_path"]
    index_basename="index",        # Bowtie index basename
    output_dir="aligned",          # SAM output directory
    input_format="fastq",          # fastq, fasta, or raw
    trim5=None,                    # trim N bases from 5' end
    trim3=None,                    # trim N bases from 3' end
    skip=None,                     # skip first N reads
    upto=None,                     # only align first N reads
    seed_mismatches=None,          # max mismatches in seed (-n)
    total_mismatches=None,         # max total mismatches (-v), preferred for sRNA
    seed_len=None,                 # seed length (-l)
    maqerr=None,                   # max total quality at mismatches (-e)
    nomaqround=False,              # disable Maq quality rounding
    minins=None,                   # minimum insert size for paired-end alignment (-I)
    maxins=None,                   # maximum insert size for paired-end alignment (-X)
    fr=True,                       # forward-reverse orientation (--fr)
    rf=False,                      # reverse-forward orientation (--rf)
    ff=False,                      # forward-forward orientation (--ff)
    nofw=False,                    # skip forward strand
    norc=False,                    # skip reverse strand
    tryhard=False,                 # try harder to find alignments
    k=None,                        # report up to K alignments
    report_all=False,              # report all alignments (-a)
    m=None,                        # suppress reads with >M alignments
    M=None,                        # like m but random report
    best=False,                    # guarantee best alignments first
    strata=False,                  # best-stratum only
    sam_out=True,                  # SAM format output
    no_unal=False,                 # suppress unaligned SAM records
    mapq=None,                     # MAPQ score
    quiet=False,                   # suppress output
    threads=1,                     # Bowtie internal threads (-p)
    offrate=None,                  # index offrate override
    reorder=False,                 # preserve input order
    mm=False,                      # memory-mapped I/O
    shmem=False,                   # shared memory
    jobs=None,                     # samples to process concurrently
    extra_args=None,
)
```
