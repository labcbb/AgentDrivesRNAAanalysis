## Environment setup

```bash
# Verify tools
bowtie --version
bowtie-build --version

## Download genome + Build Bowtie index (one time per genome)

```python
import sRNAgent as sa

# download_genome 自动解压 + 清理 header，直接返回 .fa 路径
result = sa.reference.download_genome("homo_sapiens", output_dir="ref", jobs=8)
sa.alignment.bowtie_build(result["fasta"], "grch38", threads=8)
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
print(f"BAM: {adata.obs['bam_path'].iloc[0]}")
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

print(adata.obs["bam_path"])
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
    bam_path = row["bam_path"]
    import os
    size_mb = os.path.getsize(bam_path) / 1_000_000 if os.path.exists(bam_path) else 0
    print(f"{row.name}: {bam_path} ({size_mb:.1f} MB)")
```

## BAM stats (bowtie now outputs sorted BAM + index)

```python
import subprocess

bam_path = "aligned/S1.bam"

# Count aligned reads
result = subprocess.run(
    ["samtools", "view", "-F", "4", "-c", bam_path],
    capture_output=True, text=True,
)
print(f"Aligned reads: {result.stdout.strip()}")

# Count unaligned reads
result = subprocess.run(
    ["samtools", "view", "-f", "4", "-c", bam_path],
    capture_output=True, text=True,
)
print(f"Unaligned reads: {result.stdout.strip()}")
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

# bowtie outputs (written to adata.obs):
#   sam_path                     — temporary SAM file path (deleted after BAM conversion)
#   bam_path                     — BAM file path (final output, sorted + indexed)
#   bowtie_log                   — bowtie log path
#   bowtie_total_reads           — total reads processed
#   bowtie_aligned_reads         — reads with ≥1 alignment
#   bowtie_alignment_rate (%)    — alignment rate
#   bowtie_unaligned_reads       — reads that failed to align
#   bowtie_suppressed_reads      — reads suppressed by -m
#   bowtie_reported_alignments   — total reported alignments
```
