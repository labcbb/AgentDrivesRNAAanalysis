---
name: fragment-analysis
title: Fragmentomics Analysis
description: Generate small-RNA fragmentomics features (FSD/FSC/RCD/EDM/BPM) from QC-completed FASTQ, coordinate-sorted whole-genome BAM, and reference FASTA; return fragmentomics AnnData or srna+fragmentomics MuData.
---

# Fragmentomics Analysis

## Overview

This skill extracts **small-RNA fragmentomics** features from QC-completed FASTQ files, coordinate-sorted **whole-genome** BAM files, and a reference genome FASTA using `sa.fragment.fragomics`.

> MuData 兼容说明：当前 skill 仍只操作 `srna` 模态。若输入是 `MuData`，默认取 `mdata.mod["srna"]` 作为 `adata` 执行对象；未显式指定 `mod` 时不要切换到其他模态。

The generated features include:

| Feature type | Meaning | Primary input |
| --- | --- | --- |
| `FSD` | Fragmentation Size Distribution | QC FASTQ |
| `FSC` | Fragmentation Size Coverage by chromosome | whole-genome sorted BAM |
| `RCD` | Reads Coverage Distribution per genomic window | whole-genome sorted BAM |
| `EDM_5P` / `EDM_3P` | End motif frequencies at read ends | QC FASTQ |
| `BPM_START` / `BPM_END` | Breakpoint motifs around aligned start/end positions | whole-genome sorted BAM + reference FASTA |

## Instructions

1. **先检查输入是否齐全，不齐就补前置步骤**
   - FASTQ 质控后路径必须存在于 `adata.obs["trimmed_path"]` 或 `adata.obs["clean_fastq_path"]`
   - 比对后路径必须存在于 `adata.obs["bam_path"]`
   - 参考基因组 FASTA 必须由 `genome_fasta=` 提供，或存在于 `adata.uns["genome_fasta"]`
   - `bam_path` 必须对应**全基因组坐标系**的 BAM，且 header 中的参考序列名/长度必须和 `genome_fasta` 一致
2. **如果还没做 FASTQ 质控**
   - 先运行 `fastq-qc` skill
   - 不要直接用原始 `fastq_path` 去做 fragmentomics
3. **如果还没做比对**
   - 先运行 `alignment-srna` skill
   - `fragomics` 只接受 **coordinate-sorted whole-genome BAM**
   - 不接受 `SAM`，也不接受未排序的 `BAM`
   - 不接受转录组、小 RNA 参考库、局部参考序列上的 BAM
4. **如果已经有小 RNA 定量结果**
   - 直接运行 `result = sa.fragment.fragomics(...)`
   - **必须把返回值当作 `MuData` 处理**
   - 其中 `srna` 模态保留原有定量结果，`fragmentomics` 模态存放新特征
   - 必须通过 `result.mod["fragmentomics"]` 访问片段组学结果，并优先写出 `srna_fragmentomics.h5mu`
   - 不要把返回值直接当普通 `AnnData` 使用
5. **如果当前 adata 还没有小 RNA 表达矩阵**
   - 仍可运行 `sa.fragment.fragomics(...)`
   - 此时直接在当前 `adata` 上承载 fragmentomics 特征并返回 `AnnData`

## Recommended Workflow

### Case 1: QC + alignment already finished

```python
adata = sa.fragment.fragomics(
    adata,
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
    output_dir="fragmentomics_out",
    jobs=8,
    motif_k=6,
)
```

### Case 2: FASTQ QC missing

```python
adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    output_dir="trimmed",
    jobs=8,
)

adata = sa.fragment.fragomics(
    adata,
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
    output_dir="fragmentomics_out",
    jobs=8,
)
```

### Case 3: Alignment missing

```python
adata = sa.alignment.bowtie(
    adata,
    index_basename="ref/grch38",
    output_dir="aligned",
    jobs=8,
)

adata = sa.fragment.fragomics(
    adata,
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
    output_dir="fragmentomics_out",
    jobs=8,
)
```

## Critical Checks

- `bam_path` 必须全部是 `.bam`
- `BAM` header 必须标记 `SO:coordinate`
- `BAM` header 里的 `SQ/SN/LN` 必须与 `genome_fasta` 的染色体名和长度一致
- 如果 `BAM` 来自 transcriptome / miRNA / piRNA / 其他局部参考比对，必须先换成全基因组比对结果
- `genome_fasta` 必须真实存在
- `motif_k` 必须是正整数
- `region_size` 必须是正整数

## Outputs

### Per-sample tables

Each sample writes one TSV:

```text
fragmentomics_out/<sample>/<sample>.fragmentomics.tsv
```

Columns:

1. `feature_type`
2. `feature`
3. `raw_value`
4. `cpm`

### Merged outputs

The merged matrices are exported to:

```text
fragmentomics_out/fragmentomics_raw.tsv
fragmentomics_out/fragmentomics_cpm.tsv
```

### Returned object

- If the input `adata` already contains small-RNA expression:
  - returns `MuData({"srna": <old adata>, "fragmentomics": <new adata>})`
- Otherwise:
  - returns fragmentomics `AnnData`
- 如果已有小 RNA 定量，**默认交付物应是 `srna_fragmentomics.h5mu`，而不是只留 fragmentomics 单模态 h5ad**

### Fragmentomics AnnData fields

- `adata.layers["counts"]` — raw fragmentomics matrix
- `adata.layers["CPM"]` — CPM-normalised matrix
- `adata.var["type"]` — feature type (`FSD`, `FSC`, `RCD`, `EDM_5P`, `EDM_3P`, `BPM_START`, `BPM_END`)
- `adata.var["feature"]` — concrete feature label
- `adata.var["modality"]` — always `fragmentomics`
- `adata.obs["fragomics_table"]` — per-sample feature table path
- `adata.uns["fragomics_raw_tsv"]` / `adata.uns["fragomics_cpm_tsv"]` — merged matrix paths
- `adata.uns["fragomics_params"]` — key parameters

## Persistence Discipline

### Save results after analysis

If the return value is still `AnnData`:

```python
adata.write("fragmentomics_only.h5ad")
```

If the return value is `MuData`:

```python
mdata.write("srna_fragmentomics.h5mu")
```

### Query existing results before rerunning

- If `fragmentomics` modality already exists in `MuData`, inspect it first
- If `adata.uns["fragomics_raw_tsv"]` already exists and the user only asks to view or summarise results, do **not** rerun
- Only rerun when the user explicitly asks to recompute with new parameters

## Wrong vs Correct

**WRONG — use raw FASTQ without QC**

```python
# adata.obs only has fastq_path
# do not call sa.fragment.fragomics(...) directly
```

**WRONG — pass SAM or unsorted BAM**

```python
# adata.obs["bam_path"] = "sample.sam"
# adata.obs["bam_path"] = "sample.unsorted.bam"
```

**WRONG — pass transcriptome/local-reference BAM**

```python
# adata.obs["bam_path"] points to BAM aligned against miRNA / piRNA / transcriptome references
# this is not a whole-genome coordinate BAM and must not be used for sa.fragment.fragomics(...)
```

**CORRECT — run QC/alignment first, then fragmentomics**

```python
adata = sa.fastq.cutadapt(adata, adapter_3="TGGAATTCTCGGGTGCCAAGG")
adata = sa.alignment.bowtie(adata, index_basename="ref/grch38")
# bam_path must now be whole-genome coordinate BAM compatible with genome_fasta
result = sa.fragment.fragomics(
    adata,
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
)
if hasattr(result, "mod"):
    result.write("srna_fragmentomics.h5mu")
else:
    result.write("fragmentomics_only.h5ad")
```

## References

- See [reference.md](file:///mnt/data/home/zhongxu/work/HIM/sRNAAgent/AgentDrivesRNAAanalysis/sRNAgent/skills/fragment-analysis/reference.md) for copy-paste examples.
