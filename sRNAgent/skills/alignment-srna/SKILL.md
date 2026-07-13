---
name: alignment-srna
title: sRNA-seq alignment with Bowtie
description: "Align trimmed sRNA-seq FASTQ reads to the human reference genome using Bowtie, covering index building, stringent/permissive mapping, and batch processing."
---

# sRNA-seq Alignment with Bowtie

## Overview

After adapter trimming and quality control (`fastq-qc` skill), the next step is **alignment** — mapping the trimmed sRNA-seq reads to a reference genome. This skill uses `sa.alignment.bowtie` to align small RNA reads (18–36 nt) with **single-end** mode.

| Step | Tool | Function | Purpose |
|------|------|----------|---------|
| 1 | bowtie-build | `sa.alignment.bowtie_build` | Build Bowtie index from reference genome FASTA |
| 2 | bowtie | `sa.alignment.bowtie` | Align trimmed sRNA-seq reads to the genome |
| — | (upstream) | `sa.fastq.cutadapt` | 3' adapter trimming (from `fastq-qc` skill) |

Typical sRNA-seq workflow:

```
Trimmed FASTQ (from fastq-qc)
    │
    ▼
Bowtie alignment ─── single-end mode
                  ─── -v 0 or -v 1 (0–1 mismatch)
                  ─── -m 1 (unique) or -k 10 (multi-mapping)
                  ─── --best --strata
    │
    ▼
Aligned SAM file → downstream analysis (miRNA quantification, etc.)
```

> ⚡ **批量样本时务必使用 `jobs=N` 并行比对**
>
> `sa.alignment.bowtie` 支持 `jobs` 参数控制并行比对的样本数（通过线程池，每个样本一个 bowtie 进程）。
> 样本多时（比如 >3 个），设置 `jobs=4` 可显著缩短总耗时。
> 如果用户没主动提并行数，**agent 应该根据样本量推荐一个合理的 `jobs` 值**。

## Instructions

### 1. 下载并构建参考基因组索引

以人类参考基因组 GRCh38 (GENCODE release 50) 为例。用 `download_genome` 自动下载、解压、清理 header，然后用 `bowtie_build` 构建索引。

```python
import sRNAgent as sa

# 下载基因组（自动解压 + 清理 header + 生成 .dict）
result = sa.reference.download_genome("homo_sapiens", output_dir="ref", jobs=8)
# result["fasta"] 指向解压后的 .fa 文件，header 已清理（取第一个空格前的内容）

# 构建 Bowtie 索引
sa.alignment.bowtie_build(
    result["fasta"],
    "ref/grch38",
    threads=8,
)
```

> **注意：** bowtie-build 只需要一次。索引构建完成后，后续比对直接引用 `"grch38"` 这个 basename 即可。

### 2. sRNA-seq 单端比对

sRNA-seq 是**单端测序**。函数从 `adata.obs["trimmed_path"]`（或回退到 `adata.obs["fastq_path"]`）读取 FASTQ 路径。以下是几种常用的比对策略：

#### 2a. 严谨比对 — perfect match, unique only

适用于 miRNA 精确鉴定。只保留完全匹配、唯一比对的 reads。

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["trimmed_path"] = "trimmed/S1_trimmed.fastq.gz"

adata = sa.alignment.bowtie(
    adata,
    index_basename="grch38",
    total_mismatches=0,    # -v 0: 不允许错配
    m=1,                   # 只保留唯一比对
    best=True,             # 输出最佳比对
    strata=True,           # 仅最佳 stratum
    output_dir="aligned",
)
print(f"SAM: {adata.obs['sam_path'].iloc[0]}")
```

> **CORRECT — 严谨比对**：使用 `-v 0`（零错配）+ `-m 1`（唯一比对）+ `--best --strata`，适用于需要精确位置信息的分析。

#### 2b. 容错比对 — 1 mismatch, unique only

允许 1 个错配，仍然只保留唯一比对。适用于测序质量有轻微波动但仍希望获得唯一位置的场景。

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["trimmed_path"] = "trimmed/S1_trimmed.fastq.gz"

adata = sa.alignment.bowtie(
    adata,
    index_basename="grch38",
    total_mismatches=1,    # -v 1: 最多 1 个错配
    m=1,                   # 唯一比对
    best=True,
    strata=True,
    output_dir="aligned",
)
```

> **CORRECT — 容错比对**：比 `-v 0` 更敏感，适合更多 sRNA 获得唯一比对位置。

#### 2c. 多比对报告 — 1 mismatch, up to 10 hits

允许 1 个错配，一个 read 最多报告 10 个比对位置。适用于在重复区域也能获得比对结果的场景（如某些 piRNA 簇）。

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
    k=10,                  # -k 10: 最多报告 10 个位置
    best=True,
    output_dir="aligned",
)
```

> **CORRECT — 多比对**：使用 `-k` 而不是 `-m`，不丢弃读段，保留多个候选位置。适合 piRNA 或重复区域分析。

### 3. 批量比对多个样本

接上游 `fastq-qc` 产出：

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2", "S3"]))
adata.obs["trimmed_path"] = [
    "trimmed/S1_trimmed.fastq.gz",
    "trimmed/S2_trimmed.fastq.gz",
    "trimmed/S3_trimmed.fastq.gz",
]

adata = sa.alignment.bowtie(
    adata,
    index_basename="grch38",
    total_mismatches=0,
    m=1,
    best=True,
    strata=True,
    output_dir="aligned",
    jobs=4,               # 4 个样本并行比对
)

print(adata.obs["sam_path"])
```

### 4. 对比对参数的选择建议

| 分析目标 | total_mismatches | m / k | best | strata | 适用场景 |
|----------|-----------------|-------|------|--------|----------|
| 严格鉴定 miRNA | 0 | `m=1` | True | True | 已知 miRNA 精确定位 |
| 宽松鉴定 miRNA | 1 | `m=1` | True | True | 允许测序误差 |
| piRNA 分析 | 0–1 | `k=10` 或 `k=100` | True | False | piRNA 簇多比对 |
| 所有 sRNA 表达谱 | 1 | `m=1` 或 `m=10` | True | True | 整体 sRNA 定量 |

### 5. 查看比对结果

Bowtie 输出 SAM 文件，每行是一个 read 的比对信息：

```bash
# 快速查看比对统计
samtools view -F 4 aligned/S1.sam | wc -l     # 比对上的 reads 数
samtools view -f 4 aligned/S1.sam | wc -l     # 未比对的 reads 数

# SAM 转 BAM + 排序
samtools sort -o aligned/S1.bam aligned/S1.sam
samtools index aligned/S1.bam
```

## Critical API Reference

### 完整 sRNA-seq 分析流程：质控 → 比对

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# 创建 AnnData 对象，设置原始 FASTQ 路径
adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["fastq_path"] = "srna_fastq/SRR26304152.fastq.gz"

# ── Step 1: 上游质控 (fastq-qc skill) ──
adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",  # TruSeq Small RNA
    min_length=18,
    max_length=36,
    quality_cutoff="20",
    output_dir="trimmed",
)

# ── Step 2: 比对到人类参考基因组 ──
adata = sa.alignment.bowtie(
    adata,
    index_basename="grch38",
    total_mismatches=0,
    m=1,
    best=True,
    strata=True,
    output_dir="aligned",
)

print(f"Aligned SAM: {adata.obs['sam_path'].iloc[0]}")
print(f"Genome index: {adata.uns['genome_index']}")

# 查看比对统计指标
print(adata.obs.filter(like="bowtie_").to_string())
```

### 输出存储

```python
# bowtie_build 返回值
{"index_basename": "/path/to/grch38", "directory": "/path/to/"}

# bowtie 修改 adata in-place 并返回
adata.obs["sam_path"]                    # 每个样本的 SAM 文件路径
adata.obs["bowtie_log"]                  # bowtie 日志路径（含完整比对信息）
adata.obs["bowtie_total_reads"]          # 总 reads 数
adata.obs["bowtie_aligned_reads"]        # 比对上的 reads 数
adata.obs["bowtie_alignment_rate"]       # 比对率 (%)
adata.obs["bowtie_unaligned_reads"]      # 未比对上的 reads 数
adata.obs["bowtie_suppressed_reads"]     # 因 -m 参数被抑制的 reads 数
adata.obs["bowtie_reported_alignments"]  # 报告的比对总数
adata.uns["genome_index"]                # 使用的基因组索引 basename

# 批量比对时，adata.obs 每一行对应一个样本
print(adata.obs[["sam_path", "bowtie_alignment_rate"]])
```

## Troubleshooting

- **"Could not locate a Bowtie index"**: 确保 `index_basename` 是 bowtie-build 输出的前缀路径（如 `"./grch38"`），且 `.ebwt`/`.ebwtl` 文件存在。
- **比对率很低**: 检查上游切除是否正确。sRNA-seq 的 reads 短（18–36 nt），如果 adapter 没切干净，大部分 reads 无法比对。
- **"SAM output truncated"**: 确保磁盘空间充足。SAM 文件可能很大。
- **多线程不生效**: Bowtie 的 `-p` 对短 reads 并行效率有限。可以用 `jobs` 参数并行处理多个样本文件，效果更好。
- **内存不足**: 对较大基因组（如 GRCh38），Bowtie 索引需要约 3 GB 内存。使用 `--mm`（memory-mapped I/O）可以共享内存。
- **miRNA 比对率低**: 检查 `min_length` 是否设得太低（< 18），或者 3' adapter 序列与建库试剂盒不匹配。
- **"trimmed_path" 未设置**: 如果直接使用 bowtie（跳过 cutadapt），确保 `adata.obs["trimmed_path"]` 或 `adata.obs["fastq_path"]` 已设置。
- **reads 比对上但无法区分 miRNA 簇**: 对 miRNA 家族的重复区域，用 `-k` 报告多个比对位置，然后根据 miRNA 注释进行分配。

## References

- Copy-paste-ready code templates: [`reference.md`](reference.md)
- Bowtie manual: <https://bowtie-bio.sourceforge.net/manual.shtml>
- GENCODE human release 50: <https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_50/>
- Upstream QC skill: [`fastq-qc`](../fastq-qc/SKILL.md)
