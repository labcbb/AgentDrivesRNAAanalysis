---
name: feature-count
title: Read quantification with featureCounts
description: "Count aligned sRNA-seq reads over genomic features (miRNA, piRNA, etc.) using featureCounts, with BAM vs annotation chromosome validation."
---

# Read Quantification with featureCounts

## Overview

After aligning sRNA-seq reads to the reference genome (`sa.alignment.bowtie`), the next step is **quantification** — counting how many reads overlap each genomic feature (miRNA, piRNA, etc.). This skill uses `sa.quant.feature_count`, a wrapper around [featureCounts](https://subread.sourceforge.net/).

| Step | Tool | Function | Purpose |
|------|------|----------|---------|
| 1 | featureCounts | `sa.quant.feature_count` | Count aligned BAM reads against GTF/GFF3 features, output expression matrix merged into `adata.X` |

Typical sRNA-seq quantification workflow:

```
Aligned BAM (sorted, from bowtie)
    │
    ▼
featureCounts ─── -t miRNA -g Name -s 1
               ─── BAM vs annotation chromosome validation
    │
    ▼
Count matrix (samples × features) → adata.X / adata.layers["counts"]
(merged by rna_type — miRNA, piRNA, etc. coexist in one matrix)
```

> ⚡ **批量样本时务必使用 `threads=N` 并行处理**
>
> `featureCounts` 的 `-T` 参数控制内部线程数，不是样本级并行。一般设 4–8。
> 如果用户没主动提线程数，**agent 应该根据计算资源推荐一个合理的值**。

## Instructions

> 💡 **推荐流程：用 featureCounts 对已知 miRNA/piRNA 做定量。** 速度快、结果兼容主流下游分析（DESeq2、edgeR 等）。
>
> miRDeep2 适合已有 miRBase 的物种；featureCounts 适用于任意已注释的基因组特征。

> ⚠️ **必须先确认链特异性参数 `-s` 是否正确 —— 这直接影响定量准确性**
>
> sRNA-seq 建库方案决定了链特异性：
>
> | 建库方案 | `-s` 值 | 说明 |
> |---------|---------|------|
> | **TruSeq Small RNA**（Illumina） | **1** | Read1 与 RNA 同向，正链方案 |
> | NEBNext Small RNA | 2 | Read1 与 RNA 反向 |
> | QIAseq miRNA | 1 | 与 TruSeq 类似 |
> | 其他 | 不确定时问用户 | — |
>
> **Agent 行动要求：不要默认用 `-s 1` 就跳过确认！必须先问用户**使用的是哪种建库试剂盒。

> ⚠️ **样本命名规则：** 默认使用 SRR 开头的 Run ID（如 `SRR26304152`）作为 `adata.obs_names`。从 ENA/SRA 下载的数据自动就是 SRR ID。仅当用户上传自己的数据或明确要求不用 SRR 格式时，才使用自定义名称（如 `S1`）。

> ⚠️ **feature_type 和 attr_type 按需调整：**
>
> | 目标 RNA | `-t` 值 | `-g` 值 | GTF 示例 |
> |----------|---------|---------|----------|
> | miRNA | `miRNA` | `Name` | GENCODE/miRBase GTF |
> | piRNA | `piRNA` | `ID` | piRBank GFF3 |
> | snoRNA | `snoRNA` | `gene_id` | Ensembl GTF |
> | lncRNA | `lncRNA` | `gene_id` | Ensembl GTF |
> | 全部基因 | `gene` | `gene_id` | Ensembl GTF |
>
> 默认 `-t miRNA -g Name` 适用于 miRBase GFF3/GENCODE miRNA 注释。用户定量其他 RNA 时需调整。

### 1. 定量已知 miRNA（默认，人类 hsa，TruSeq 链特异性）

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# 已有 bowtie 比对结果
adata = ad.AnnData(obs=pd.DataFrame(index=["SRR26304152"]))
adata.obs["bam_path"] = ["aligned/SRR26304152.bam"]

adata = sa.quant.feature_count(
    adata,
    annotation="ref/gencode.v50.primary_assembly.annotation.gtf.gz",
    feature_type="miRNA",
    attr_type="Name",
    strand=1,            # TruSeq Small RNA 正链方案
    threads=6,
    output_dir="fc_out",
)

print(f"Count matrix: {adata.X.shape}")
print(f"Count CSV:    {adata.obs['fc_counts_csv'].iloc[0]}")
print(f"Features:     {adata.var['feature_id'].tolist()[:5]}")
```

**自动染色体名校验：**

```python
# 工具内部会用 pysam 检查 BAM 的 @SQ 头与 GTF 第一列是否一致
# 不一致时会打印警告，避免用了不对应的参考基因组
```

**CORRECT — 批量定量多个样本：**

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

samples = [f"SRR2630415{i}" for i in range(2, 12)]
adata = ad.AnnData(obs=pd.DataFrame(index=samples))
adata.obs["bam_path"] = [f"aligned/{s}.bam" for s in samples]

adata = sa.quant.feature_count(
    adata,
    annotation="ref/hairpin_hsa.gff3",
    feature_type="miRNA",
    attr_type="Name",
    strand=1,
    threads=6,
    output_dir="fc_out",
)

print(adata.X.shape)
```

> BAM 文件列表自动从 `adata.obs["bam_path"]` 读取，featureCounts 一次调用即可处理所有样本。

**CORRECT — 定量 piRNA（调整 feature_type 和 attr_type，指定 rna_type 以便合并）：**

```python
# 如果 adata.X 已有 miRNA 数据，piRNA 结果会追加到同一矩阵中
adata = sa.quant.feature_count(
    adata,
    annotation="ref/piRBank_hsa.gff3",
    feature_type="piRNA",
    attr_type="ID",
    rna_type="piRNA",
    strand=0,            # piRNA 通常是非链特异性
)
```

> 不同 `rna_type` 的特征会共存于 `adata.X` / `adata.layers["counts"]` 中。
> 同一 `rna_type` 重复运行会替换旧特征，不影响其他 RNA 类型。

### 2. 查看定量结果

```python
# 查看表达矩阵
print(f"Samples: {adata.X.shape[0]}, Features: {adata.X.shape[1]}")
print(adata.var.head())
```

### 输出格式

```python
# featureCounts 写入 adata.obs 的列
# adata.obs["fc_counts_csv"]       — featureCounts 原始输出路径 (.txt)
# adata.obs["fc_summary_csv"]      — 统计摘要路径 (.txt.summary)

# featureCounts 写入 adata.X / adata.layers["counts"]
# adata.X                          — 合并的表达矩阵 (samples × features)
# adata.layers["counts"]           — 与 adata.X 相同的原始计数矩阵
# adata.var["feature_id"]          — 特征 ID 列表
# adata.var["rna_type"]            — RNA 类型（miRNA、piRNA 等）

# featureCounts 写入 adata.uns
# adata.uns["fc_annotation"]       — 使用的注释文件路径
```

不同 `rna_type` 的定量结果会合并到同一个 `adata.X` 中。例如先定量 miRNA 再定量 piRNA：

```python
# 第一步：定量 miRNA（rna_type="miRNA"）
adata = sa.quant.feature_count(adata, annotation="mirna.gff3", ...)
# adata.var["rna_type"] → ["miRNA", "miRNA", ...]

# 第二步：定量 piRNA（结果追加到同一矩阵）
adata = sa.quant.feature_count(adata, annotation="pirna.gff3",
                               feature_type="piRNA", attr_type="ID",
                               rna_type="piRNA", ...)
# adata.X → (samples × miRNA+piRNA features)
# adata.var["rna_type"] → ["miRNA", ..., "piRNA", ...]
```

## Troubleshooting

- **featureCounts 报 "Cannot open annotation file"**: 确保 `annotation` 路径正确，文件存在且可读。支持 `.gtf`、`.gff3`、`.gtf.gz`、`.gff3.gz`。
- **featureCounts 报 "cannot find any mapped fragment"**: 检查 BAM 文件是否为空或所有 reads 都未比对上。用 `samtools view -c -F 4 aligned.bam` 查看比对上的 reads 数。
- **染色体名不匹配**: 如果 BAM 的 @SQ 头（如 `chr1`）与 GTF 第 1 列（如 `1`）不一致，featureCounts 会忽略所有比对的 reads。工具已自动检测并打印警告。
- **`-s` 参数错误导致 0 count**: sRNA-seq 通常是链特异性的。TruSeq Small RNA 用 `-s 1`。如果结果为 0，尝试 `-s 2` 或 `-s 0`。
- **太多 reads 被 multi-mapping 丢弃**: 用 `-O` 参数允许 reads 在多 feature 上计数。默认不开启。
- **featureCounts 未安装**: 确保 `featureCounts` 可执行文件在 PATH 中。它是 Subread 包的一部分，可用 `conda install subread` 安装。

## References

- Copy-paste-ready code templates: [`reference.md`](reference.md)
- featureCounts manual: <https://subread.sourceforge.net/SubreadUsersGuide.pdf>
- GENCODE annotation: <https://www.gencodegenes.org/>
- Upstream alignment skill: [`alignment-srna`](../alignment-srna/SKILL.md)
