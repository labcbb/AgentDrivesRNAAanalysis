---
name: trax_quantification
title: tRNA fragment quantification with tRAX
description: "Quantify tRNA-derived fragments from sRNA-seq FASTQ files using tRAX, with results written back to AnnData."
---

# tRNA Fragment Quantification with tRAX

## Overview

This skill quantifies tRNA-derived fragments (tRFs/tDRs) from small RNA-seq FASTQ files using the bundled tRAX workflow.

Detailed API notes and output file examples are in `reference.md`.

| Step | Tool | Function | Purpose |
|------|------|----------|---------|
| 1 | tRAX `processsamples.py` | `sa.quant.tRAX` | Map reads to a tRNAdb reference and count tRNA fragments |
| 2 | tRAX `*-trnacounts.txt` parser | `sa.quant.tRAX` | Convert fragment counts into `adata.X`, `adata.var`, and `adata.layers["tRAXcount"]` |

Typical workflow:

```text
AnnData with FASTQ paths
    |
    v
sa.quant.tRAX
    |-- builds tRAX sample file
    |-- runs processesamples.py
    |-- parses <experiment>-trnacounts.txt
    v
AnnData with tRNA fragment count matrix
```

> Batch runs should use `cores=N`.
>
> tRAX uses `cores` internally for mapping/counting. For multiple samples, use a practical value such as `cores=4` or `cores=8`, depending on available CPU and memory.

## Requirements

Before running tRAX quantification, you need:

- An `AnnData` object with sample names in `adata.obs_names`
- FASTQ paths in `adata.obs["clean_fastq_path"]` or `adata.obs["fastq_path"]`, or a `fastq_dir` that contains files named by sample ID
- A tRNAdb database built with `sa.reference.build_trnadb`
- `bowtie2`, `samtools`, and Infernal tools available in `PATH`

The `databasename` argument can be either a database prefix:

```python
databasename="ref/tRNAdb/hg38"
```

for files like:

```text
hg38-trnatable.txt
hg38-maturetRNAs.bed
hg38-trnaloci.bed
hg38-tRNAgenome.1.bt2l
```

or a directory containing exactly one `*-trnatable.txt` prefix:

```python
databasename="ref/tRNAdb"
```

## Input FASTQ Selection

`sa.quant.tRAX` never overwrites the original FASTQ columns. It resolves the actual tRAX input path into:

```python
adata.obs["trax_fq"]
```

FASTQ priority:

1. `adata.obs["clean_fastq_path"]`
2. `adata.obs["fastq_path"]`
3. `fastq_dir`, matched by FASTQ basename to `adata.obs_names`

If a path in `clean_fastq_path` or `fastq_path` is stale or missing, and `fastq_dir` has a matching file, tRAX uses the matched file for `trax_fq`.

> Prefer `clean_fastq_path` from `sa.fastq.cutadapt`.
>
> tRNA fragment quantification is sensitive to adapter contamination and read length. If raw FASTQ files still contain adapter sequence, run cutadapt first.

## Instructions

### 1. Recommended preprocessing with cutadapt

```python
adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    min_length=18,
    max_length=36,
    quality_cutoff="20",
    output_dir="trimmed",
    jobs=4,
)
```

`cutadapt` writes cleaned reads to:

```python
adata.obs["clean_fastq_path"]
```

### 2. Quantify tRNA fragments

```python
adata = sa.quant.tRAX(
    adata,
    databasename="ref/tRNAdb/hg38",
    output_dir="trax_out",
    experiment_name="trax_quant",
    cores=4,
)
```

### 3. Use `fastq_dir` only as a fallback or matcher

Use this when `adata.obs["fastq_path"]` contains stale relative paths or only a subset of FASTQ files is available locally:

```python
adata = sa.quant.tRAX(
    adata,
    fastq_dir="data/srna_fastq",
    databasename="ref/tRNAdb/hg38",
    output_dir="trax_out",
    experiment_name="trax_quant",
    cores=4,
)
```

This keeps only samples with usable FASTQ paths in the returned `AnnData`.

### 4. Reuse existing BAM files

When tRAX mapping already ran and you only need to rerun counting/parsing:

```python
adata = sa.quant.tRAX(
    adata,
    databasename="ref/tRNAdb/hg38",
    output_dir="trax_out",
    experiment_name="trax_quant",
    lazyremap=True,
    cores=4,
)
```

## Outputs

`sa.quant.tRAX` returns an `AnnData` object. If the input has no existing variables, tRNA fragment counts become the main matrix:

```python
adata.X                    # raw counts, samples x tRNA fragment features
adata.layers["tRAXcount"]  # same raw count matrix
```

If the input already contains an expression matrix, for example miRNA counts in `adata.X`, the existing matrix is preserved and tRAX counts are stored separately:

```python
adata.obsm["tRAXcount"]    # raw tRNA fragment counts
adata.uns["tRAX_var"]      # feature metadata for columns in obsm["tRAXcount"]
```

Use `replace_x=True` only when you explicitly want tRAX counts to replace the current `adata.X` and `adata.var`.

Feature annotations:

```python
adata.var["trax_feature_id"]  # original row name from *-trnacounts.txt
adata.var["trna_id"]          # parent tRNA ID
adata.var["fragment_type"]    # wholecounts, fiveprime, threeprime, other
```

Per-sample paths:

```python
adata.obs["trax_fq"]          # FASTQ used by tRAX
adata.obs["trax_bam"]         # tRAX BAM output
adata.obs["trax_sample"]      # sanitized tRAX sample name
adata.obs["trax_replicate"]   # replicate/group name in tRAX sample file
```

Run metadata:

```python
adata.uns["trax_result"]
adata.uns["trax_count_matrix"]
```

The parsed count file is:

```text
<output_dir>/<experiment_name>/<experiment_name>-trnacounts.txt
```

## Correct Usage

**CORRECT - use cleaned FASTQ paths when available:**

```python
adata = sa.fastq.cutadapt(adata, adapter_3="TGGAATTCTCGGGTGCCAAGG")
adata = sa.quant.tRAX(adata, databasename="ref/tRNAdb/hg38")
```

**CORRECT - preserve existing miRNA matrix and add tRAX counts separately:**

```python
# Existing adata.X contains miRNA counts
adata = sa.quant.tRAX(
    adata,
    databasename="ref/tRNAdb/hg38",
)
print(adata.X.shape)                 # unchanged miRNA matrix
print(adata.obsm["tRAXcount"].shape) # tRNA fragment matrix
```

**CORRECT - explicitly replace X with tRAX counts:**

```python
adata = sa.quant.tRAX(
    adata,
    databasename="ref/tRNAdb/hg38",
    replace_x=True,
)
```

**CORRECT - use `fastq_dir` to match only locally available samples:**

```python
adata = sa.quant.tRAX(
    adata,
    fastq_dir="data/srna_fastq",
    databasename="ref/tRNAdb/hg38",
)
```

**CORRECT - map only when explicitly requested:**

```python
adata = sa.quant.tRAX(
    adata,
    databasename="ref/tRNAdb/hg38",
    maponly=True,
)
```

In `maponly=True` mode, `adata.X` is not replaced by tRNA fragment counts because no `*-trnacounts.txt` is parsed.

## Common Problems

**Missing tRNAdb index**

Make sure `databasename` points to the prefix used by `sa.reference.build_trnadb`, not just an arbitrary output directory.

**Permission denied for tRAX helper scripts**

The wrapper calls `choosemappings.py` with the current Python interpreter, so executable permission on that helper script is not required.

**Stale FASTQ paths in h5ad**

Pass `fastq_dir`. The wrapper writes resolved paths to `trax_fq` and leaves the old `fastq_path` column unchanged.

**Unexpected sample dropping**

Samples are dropped only when no usable FASTQ path can be resolved from `clean_fastq_path`, `fastq_path`, or `fastq_dir`.
