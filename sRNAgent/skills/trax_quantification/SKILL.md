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
| 2 | tRAX `*-trnacounts.txt` parser | `sa.quant.tRAX` | Convert fragment counts into the shared `adata.layers["counts"]` matrix |

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

`sa.quant.tRAX` returns an `AnnData` object. tRNA fragment counts are stored in the shared expression matrix:

```python
adata.X                    # raw counts, samples x all shared small-RNA features
adata.layers["counts"]     # same raw count matrix
```

If `adata.layers["counts"]` already contains another RNA type, such as miRNA, tRNA features are appended. If it already contains tRNA features, the old tRNA block is replaced.

Feature annotations:

```python
adata.var["trax_feature_id"]  # original row name from *-trnacounts.txt
adata.var["trna_id"]          # parent tRNA ID
adata.var["fragment_type"]    # wholecounts, fiveprime, threeprime, other
adata.var["rna_type"]         # tRNA
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

**CORRECT - preserve existing miRNA matrix and append tRAX counts in the shared layer:**

```python
adata = sa.quant.tRAX(
    adata,
    databasename="ref/tRNAdb/hg38",
)
print(adata.layers["counts"].shape)  # merged expression matrix
print(adata.var["rna_type"].value_counts())
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

In `maponly=True` mode, no tRNA fragment counts are written because no `*-trnacounts.txt` is parsed.

## Common Problems

**Missing tRNAdb index**

Make sure `databasename` points to the prefix used by `sa.reference.build_trnadb`, not just an arbitrary output directory.

**Permission denied for tRAX helper scripts**

The wrapper calls `choosemappings.py` with the current Python interpreter, so executable permission on that helper script is not required.

**Stale FASTQ paths in h5ad**

Pass `fastq_dir`. The wrapper writes resolved paths to `trax_fq` and leaves the old `fastq_path` column unchanged.

**Unexpected sample dropping**

Samples are dropped only when no usable FASTQ path can be resolved from `clean_fastq_path`, `fastq_path`, or `fastq_dir`.
