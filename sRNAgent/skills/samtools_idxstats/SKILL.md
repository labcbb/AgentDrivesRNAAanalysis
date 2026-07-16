---
name: samtools_idxstats
title: Small-RNA BAM quantification with samtools idxstats
description: "Quantify reads per small-RNA reference sequence from BAM files using samtools idxstats, writing counts into AnnData."
---

# Small-RNA BAM Quantification with samtools idxstats

## Overview

Use `sa.quant.idxstats` to quantify reads mapped to small-RNA reference sequences in BAM files.

This skill is for BAM files produced by aligning reads to a **small-RNA FASTA reference index** such as piRBase piRNA sequences, Ensembl ncRNA sequences, mature tRNAs, miRNAs, or other transcript-level sequences.

It is **not** the right method for BAM files aligned to a whole reference genome. For whole-genome BAMs, use annotation-based counting such as `sa.quant.feature_count`.

| Step | Tool | Function | Purpose |
|------|------|----------|---------|
| 1 | Bowtie | `sa.alignment.bowtie` | Align sRNA reads to a small-RNA FASTA index |
| 2 | samtools idxstats | `sa.quant.idxstats` | Count reads mapped to each reference sequence |
| 3 | AnnData writer | `sa.quant.idxstats` | Write counts to `adata.X` and `adata.layers["idxstats"]` |

Typical workflow:

```text
small-RNA FASTA
    |
    v
bowtie-build
    |
    v
FASTQ -> bowtie -> BAM
    |
    v
samtools idxstats -> adata.X
```

## Requirements

- `adata.obs["bam_path"]` from `sa.alignment.bowtie`
- BAM files aligned to a small-RNA FASTA reference, not a whole genome
- `samtools` available in `PATH`

The Bowtie index should be built from a FASTA where each sequence is a feature to quantify. Good examples include:

```text
>piR-hsa-1
...
>piR-hsa-2
...
```

from piRBase, or:

```text
>ENST00000383977.1 gene_biotype:miRNA
...
>ENST00000607772.1 gene_biotype:snoRNA
...
```

The first column of `samtools idxstats` then corresponds directly to quantifiable small-RNA feature IDs.

## Instructions

### 1. Build a small-RNA Bowtie index

**Example A - piRBase FASTA**

```python
sa.alignment.bowtie_build(
    "ref/piRBase_human.fa",
    "ref/piRBase_human",
    threads=4,
)
```

**Example B - Ensembl ncRNA FASTA**

```python
sa.alignment.bowtie_build(
    "ref/Homo_sapiens.GRCh38.ncrna.fa",
    "ref/human_ncrna",
    threads=4,
)
```

Do not use a whole-genome FASTA if the goal is direct `idxstats` quantification of tRNA or other small-RNA features.

### 2. Align reads to the small-RNA index

```python
adata = sa.alignment.bowtie(
    adata,
    index_basename="ref/piRBase_human",
    output_dir="aligned_piRBase",
    total_mismatches=0,
    m=1,
    best=True,
    threads=4,
)
```

`sa.alignment.bowtie` writes:

```python
adata.obs["bam_path"]
```

### 3. Quantify by idxstats

```python
adata = sa.quant.idxstats(
    adata,
    output_dir="idxstats_out",
)
```

## Outputs

`samtools idxstats` returns four columns:

| idxstats column | Meaning | AnnData destination |
|-----------------|---------|---------------------|
| 1 | Reference sequence name, e.g. a specific tRNA ID | `adata.var["reference_name"]` and `adata.var_names` |
| 2 | Reference sequence nucleotide length | `adata.var["reference_length"]` |
| 3 | Reads mapped to that reference | `adata.X`, `adata.layers["idxstats"]` |
| 4 | Unmapped reads for that reference | ignored |

Returned `AnnData` fields:

```python
adata.X
adata.layers["idxstats"]
adata.var["reference_name"]
adata.var["reference_length"]
adata.obs["idxstats_bam"]
adata.obs["idxstats_file"]
adata.uns["idxstats_result"]
```

## Correct Usage

**CORRECT - tRNA FASTA reference:**

```python
sa.alignment.bowtie_build("ref/mature_tRNAs.fa", "ref/mature_tRNAs")
adata = sa.alignment.bowtie(adata, index_basename="ref/mature_tRNAs")
adata = sa.quant.idxstats(adata)
```

**CORRECT - piRBase FASTA reference:**

```python
sa.alignment.bowtie_build("ref/piRBase_human.fa", "ref/piRBase_human")
adata = sa.alignment.bowtie(adata, index_basename="ref/piRBase_human")
adata = sa.quant.idxstats(adata)
```

**CORRECT - Ensembl ncRNA FASTA reference:**

```python
sa.alignment.bowtie_build("ref/Homo_sapiens.GRCh38.ncrna.fa", "ref/human_ncrna")
adata = sa.alignment.bowtie(adata, index_basename="ref/human_ncrna")
adata = sa.quant.idxstats(adata)
```

**CORRECT - miRNA mature FASTA reference:**

```python
sa.alignment.bowtie_build("ref/mature_hsa.fa", "ref/mature_hsa")
adata = sa.alignment.bowtie(adata, index_basename="ref/mature_hsa")
adata = sa.quant.idxstats(adata)
```

**WRONG - whole-genome BAM:**

```python
# WRONG for direct small-RNA feature abundance:
# adata = sa.alignment.bowtie(adata, index_basename="ref/grch38")
# adata = sa.quant.idxstats(adata)
```

For whole-genome BAMs, `idxstats` counts reads per chromosome/contig, not per tRNA or miRNA feature. Use annotation-based counting instead.

## Common Problems

**No `bam_path` column**

Run `sa.alignment.bowtie` first. If only `sam_path` exists, the wrapper will look for a `.bam` file with the same basename.

**BAM index missing**

`sa.quant.idxstats` creates a BAM index with `samtools index` by default when needed.

**Unexpected feature names**

Feature names come from the FASTA headers used to build the Bowtie index. Clean FASTA headers before building the index if you need stable IDs.
