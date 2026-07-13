---
name: mirdeep2-mirna
title: miRNA quantification with miRDeep2
description: "Quantify known miRNAs and predict novel miRNAs using miRDeep2, with human (hsa) as the default species."
---

# miRNA Quantification with miRDeep2

## Overview

miRDeep2 is a widely used tool for identifying known and novel miRNAs from small RNA-seq data. This skill wraps three miRDeep2 modules:

| Step | Tool | Function | Purpose |
|------|------|----------|---------|
| 1 | mapper.pl | `sa.quant.quantify_mirna` (internal) | Preprocess FASTQ (adapter clip, length filter, collapse) + map to genome |
| 2 | quantifier.pl | `sa.quant.quantify_mirna` | Quantify known miRNAs against miRBase |
| 3 | miRDeep2.pl | `sa.quant.predict_mirna` | Predict known + novel miRNAs with structure and randfold analysis |

**默认流程（只定量已知 miRNA）：**

```
Trimmed FASTQ  ──→  mapper.pl  ──→  quantifier.pl  ──→  adata.X (count matrix)
```

**预测新 miRNA 流程（额外步骤）：**

```
Trimmed FASTQ  ──→  mapper.pl  ──→  miRDeep2.pl  ──→  result.html + result.csv + pdfs/
```

> ⚡ **批量样本时务必使用 `jobs=N` 并行定量/预测**
>
> `sa.quant.quantify_mirna` 和 `sa.quant.predict_mirna` 都支持 `jobs` 参数控制并行处理的样本数（每个样本独立跑 mapper.pl + quantifier.pl 或 miRDeep2.pl）。
> 样本多时（比如 >3 个），设置 `jobs=3` 可显著缩短总耗时。
> 如果用户没主动提并行数，**agent 应该根据样本量推荐一个合理的 `jobs` 值**。

## Prerequisites

Before running miRDeep2, you need:

- **Anndata object** with sample names in `adata.obs.index` and FASTQ paths in `adata.obs["fastq_path"]` (or `adata.obs["trimmed_path"]` if cutadapt has run)
- **Bowtie genome index** — from `sa.alignment.bowtie_build` (see `alignment-srna` skill)
- **Reference genome FASTA** — from `sa.reference.download_genome`
- **miRBase data** — from `sa.reference.download_mirbase`

### 数据准备示例

```python
import sRNAgent as sa

# 1. 下载人类 miRBase 数据（提取 hsa 序列）
sa.reference.download_mirbase("hsa", output_dir="ref", jobs=4)

# 2. 下载参考基因组（用于 novel miRNA 预测）
sa.reference.download_genome("homo_sapiens", output_dir="ref", jobs=8)

# 3. 构建 Bowtie 索引
sa.alignment.bowtie_build("ref/GRCh38.primary_assembly.genome.fa", "ref/grch38", threads=8)
```

## Instructions

> 💡 **推荐流程：优先使用 `quantify_mirna` 定量已知 miRNA。** 它对已知 miRNA 做表达定量，输出表达矩阵到 `adata.X`，速度快、结果可靠。
>
> `predict_mirna`（预测 novel miRNA）计算量大且结果需要人工验证，仅在有发现新 miRNA 需求时使用。**除非用户明确要求发现新 miRNA，否则默认走已知 miRNA 定量流程。**

> ⚠️ **必须先确认 sRNA-seq 的 3' adapter 序列是否正确 —— 这直接影响 miRNA 定量结果**
>
> miRDeep2 的 `mapper.pl` 内置了 adapter 剪切功能（`adapter=` 参数）。如果 adapter 序列给错，reads 无法正确比对到基因组，miRNA 定量和 novel miRNA 预测都会失败。
>
> **Agent 行动要求：不要默认使用 TruSeq 的 adapter！必须先问用户：**
> 1. 询问用户使用的建库试剂盒名称
> 2. 让用户确认是否使用下面的默认序列，还是自己指定
> 3. 如果用户不确定，让对方查一下实验方法的 "Library preparation" 部分
>
> **建议在 cutadapt 中完成 adapter 剪切**（见 `fastq-qc` skill），`mapper.pl` 中不再重复做，分工更清晰。若需要 mapper.pl 做 adapter 剪切，务必先让用户确认正确的 adapter 序列：
>
> | 建库试剂盒 | 3' adapter 序列 |
> |-----------|----------------|
> | TruSeq Small RNA (Illumina) | `TGGAATTCTCGGGTGCCAAGG` |
> | NEBNext Small RNA | `AGATCGGAAGAGCACACGTCTGAAC` |
> | QIAseq miRNA | `AACTGTAGGCACCATCAAT` |
> | SMARTer smRNA-Seq | `GTTCAGAGTTCTACAGTCCGACGATC` |

### 1. 定量已知 miRNA（默认推荐，人类 hsa）

> ⚠️ **样本命名规则：** 默认使用 SRR 开头的 Run ID（如 `SRR26304152`）作为 `adata.obs_names`。从 ENA/SRA 下载的数据自动就是 SRR ID。仅当用户上传自己的数据或明确要求不用 SRR 格式时，才使用自定义名称（如 `S1`、`sample1`）。

单个样本：

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# 初始化 AnnData
adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["fastq_path"] = ["trimmed/S1_trimmed.fastq.gz"]

adata = sa.quant.quantify_mirna(
    adata,
    genome_index="ref/grch38",
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    output_dir="mirdeep2",
)

print(f"Collapsed FASTA: {adata.obs['collapsed_path'].iloc[0]}")
print(f"Expression CSV:  {adata.obs['counts_csv'].iloc[0]}")
```

**CORRECT — 自动处理 adapter 剪切和长度过滤：**

```python
adata = sa.quant.quantify_mirna(
    adata,
    genome_index="ref/grch38",
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    adapter="TGGAATTCTCGGGTGCCAAGG",   # TruSeq Small RNA 3' adapter
    min_length=18,
)
```

> mapper.pl 会自动完成 adapter 剪切（`-k`）、长度过滤（`-l 18`）、序列折叠去重（`-m`）以及到基因组的比对（`-p`）。

**CORRECT — 批量定量多个样本，并行处理：**

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
    jobs=3,  # 3 个样本并行
)

# 每个样本的输出路径
for sample in adata.obs_names:
    print(f"{sample}: {adata.obs.at[sample, 'counts_csv']}")
```

### 2. 查看定量结果

`quantifier.pl` 的输出结果存储在 AnnData 对象中：

```python
adata = sa.quant.quantify_mirna(adata, genome_index="ref/grch38",
                                mature_fa="ref/mature_hsa.fa",
                                hairpin_fa="ref/hairpin_hsa.fa")

# 表达量矩阵（行 = miRNA, 列 = 样本）
# adata.X 是 count 矩阵，adata.var_names 是 miRNA 名称
print(f"Count matrix shape: {adata.X.shape}")
print(f"miRNA IDs: {adata.var['mirna_id'].tolist()[:5]}")

# 前 10 个高表达 miRNA
import pandas as pd
counts_df = pd.DataFrame(adata.X.toarray() if hasattr(adata.X, 'toarray') else adata.X,
                         index=adata.obs_names,
                         columns=adata.var["mirna_id"])
print(counts_df.iloc[:, :10])

# 每个样本的详细输出文件路径
print(adata.obs[["collapsed_path", "arf_path", "counts_csv"]])
```

### 3. 查看跨样本表达矩阵

定量完成后，表达矩阵存储在 `adata.X` 中：

```python
# adata.X : count 矩阵 (n_samples × n_mirnas)
# adata.var["mirna_id"] : miRNA 名称
# adata.obs_names : 样本名称

import pandas as pd
exp_matrix = pd.DataFrame(
    adata.X.toarray() if hasattr(adata.X, 'toarray') else adata.X,
    index=adata.obs_names,
    columns=adata.var["mirna_id"],
)
# 格式:         hsa-let-7a-5p  hsa-let-7a-3p  ...
#       S1         22718           124
#       S2         18304            98
#       S3         20115           117
```

### 4. 预测已知 + 新 miRNA

```python
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

**CORRECT — 提高 novel miRNA 检出灵敏度（添加近缘物种序列）：**

```python
adata = sa.quant.predict_mirna(
    adata,
    genome_index="ref/grch38",
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    related_mature_fa="ref/mature_mmu.fa",  # 小鼠 mature miRNA（近缘物种）
    species="hsa",
)
```

> `related_mature_fa` 提供近缘物种的 mature miRNA 序列，有助于识别保守的新 miRNA。

**CORRECT — 设置严格过滤条件：**

```python
adata = sa.quant.predict_mirna(
    adata,
    genome_index="ref/grch38",
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    score_cutoff=4,   # 只保留 score ≥ 4 的候选
    min_stack=10,     # 至少 10 条 reads 堆叠
)
```

**WRONG — predict_mirna 未提供 genome_fasta:**

```python
# WRONG! miRDeep2.pl 需要基因组 FASTA 文件
# sa.quant.predict_mirna(adata, genome_index="ref/grch38", ...)
# 缺少 genome_fasta 参数 → miRDeep2.pl 会报错
```

### 5. 其他物种

将 `species` 和 `mature_fa` / `hairpin_fa` 改为目标物种的：

```python
# 小鼠 (mmu)
adata = sa.quant.quantify_mirna(
    adata,
    genome_index="ref/grcm39",
    mature_fa="ref/mature_mmu.fa",
    hairpin_fa="ref/hairpin_mmu.fa",
    species="mmu",
)

# 斑马鱼 (dre)
adata = sa.quant.quantify_mirna(
    adata,
    genome_index="ref/grc11",
    mature_fa="ref/mature_dre.fa",
    hairpin_fa="ref/hairpin_dre.fa",
    species="dre",
)
```

### 6. mapper.pl 的 adapter 和长度过滤说明

mapper.pl 内置了 adapter 剪切和长度过滤功能。如果你已经在 `cutadapt` 中做过这些，mapper.pl 可以不做：
- `adapter=None`（默认）— mapper.pl 不剪切 adapter
- `adapter="TGGAATTCTCGGGTGCCAAGG"` — mapper.pl 会做 adapter 剪切

建议在 `cutadapt` 中做 adapter 剪切，mapper.pl 中不再重复做，分工更清晰：

```python
# cutadapt：只做 adapter 修剪
adata = sa.fastq.cutadapt(adata,
                          adapter_3="TGGAATTCTCGGGTGCCAAGG",
                          min_length=18, max_length=36,
                          output_dir="trimmed")

# mapper.pl：只做 collapse + 比对，不做 adapter 剪切
# 此时 adata.obs 中已有 trimmed_path 列
adata = sa.quant.quantify_mirna(adata, genome_index="ref/grch38",
                                mature_fa="ref/mature_hsa.fa",
                                hairpin_fa="ref/hairpin_hsa.fa")
```

> `quantify_mirna` 会优先使用 `adata.obs["trimmed_path"]`（如果存在），否则使用 `adata.obs["fastq_path"]`。

## Critical API Reference

### 完整端到端流程

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# ── Init AnnData ──
adata = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2", "S3"]))
adata.obs["fastq_path"] = [
    "srna_fastq/SRR26304152.fastq.gz",
    "srna_fastq/SRR26304153.fastq.gz",
    "srna_fastq/SRR26304154.fastq.gz",
]

# ── Reference preparation ──
sa.reference.download_mirbase("hsa", output_dir="ref", jobs=4)
sa.reference.download_genome("homo_sapiens", output_dir="ref", jobs=8)
sa.alignment.bowtie_build("ref/GRCh38.primary_assembly.genome.fa",
                          "ref/grch38", threads=8)

# ── Trim (cutadapt) ──
adata = sa.fastq.cutadapt(adata, adapter_3="TGGAATTCTCGGGTGCCAAGG",
                          min_length=18, max_length=36, output_dir="trimmed")

# ── Quantify known miRNAs ──
adata = sa.quant.quantify_mirna(
    adata,
    genome_index="ref/grch38",
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    output_dir="mirdeep2",
    jobs=3,
)

print(f"Count matrix: {adata.X.shape}")
print(f"miRNA IDs:    {adata.var['mirna_id'].tolist()[:5]}")

# ── Predict novel miRNAs ──
adata = sa.quant.predict_mirna(
    adata,
    genome_index="ref/grch38",
    genome_fasta="ref/GRCh38.primary_assembly.genome.fa",
    mature_fa="ref/mature_hsa.fa",
    hairpin_fa="ref/hairpin_hsa.fa",
    output_dir="mirdeep2",
)
print(f"Novel miRNA report: {adata.obs['prediction_html'].iloc[0]}")
```

### 输出格式

```python
# quantify_mirna 返回更新后的 AnnData 对象
# adata.obs 新增列:
#   "collapsed_path"   : mirdeep2/S1_collapsed.fa
#   "arf_path"         : mirdeep2/S1_vs_genome.arf
#   "counts_csv"       : mirdeep2/S1/miRNA_counts.csv

# adata.X : count 矩阵 (n_samples × n_mirnas)
# adata.var["mirna_id"] : miRNA 名称, 如 hsa-let-7a-5p

# adata.uns["genome_index"] : "ref/grch38"
# adata.uns["mature_fa"]    : "ref/mature_hsa.fa"
# adata.uns["hairpin_fa"]   : "ref/hairpin_hsa.fa"

# predict_mirna 进一步新增:
#   "prediction_html"  : mirdeep2/S1/result.html
#   "prediction_csv"   : mirdeep2/S1/result.csv
```

## Troubleshooting

- **mapper.pl 报 "Can't locate ..."**: miRDeep2 使用 Perl。确认 miRDeep2 已正确安装，且 Perl 模块路径已设置（`perl -e 'use Bio::Perl'`）。
- **mapper.pl 找不到 Bowtie 索引**: `genome_index` 是 bowtie-build 输出的前缀路径（含路径），如 `ref/grch38`。确认 `.ebwt` 文件存在。
- **quantifier.pl 报序列 ID 不匹配**: 从 miRBase 下载的 mature.fa 和 hairpin.fa 必须来自同一个 miRBase 版本。用 `download_mirbase` 自动获取即可保证一致。
- **"No reads mapped"**: 检查 adapter 序列是否正确，以及 min_length 是否设得太高。sRNA-seq reads 通常 18-30 nt。
- **miRDeep2.pl 输出为空**: 降低 `score_cutoff`，或提供 `related_mature_fa` 提高灵敏度。
- **FASTQ.gz 处理**: `quantify_mirna` 和 `predict_mirna` 自动解压 .gz 文件到临时目录，运行后自动清理。
- **并行处理卡顿**: 减少 `jobs` 数量。mapper.pl 本身已使用多线程（Bowtie 比对），过多的并行可能超出内存。
- **miRDeep2 运行缓慢**: novel miRNA 预测的计算量远大于定量。对大批量样本，先用 `quantify_mirna` 定量，再选少量样本做 `predict_mirna`。

## References

- Copy-paste-ready code templates: [`reference.md`](reference.md)
- miRDeep2 documentation: <https://github.com/rajewsky-lab/mirdeep2>
- miRBase: <https://www.mirbase.org/>
- Bowtie alignment skill: `../alignment-srna/SKILL.md`
- FASTQ QC skill: `../fastq-qc/SKILL.md`
- Reference download skill: `../reference-download/SKILL.md`
