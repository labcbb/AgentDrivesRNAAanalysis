---
name: isomir-quantification
title: isoMiR/isomiR quantification with mirtop
description: "Quantify isoMiR (isomiR) variants (5'/3' trimming, additions, SNPs) from hairpin-aligned BAM files using mirtop, with dedicated adata modality."
---

# isoMiR (isomiR) Quantification with mirtop

## ⚠️ 两个不可违背的原则

**1. isomiR 是独立模态，必须使用专门的 adata，禁止与 srna 合并。**

- 本 skill 全程操作**独立的 isomiR AnnData**（下文记为 `adata_iso`），与主流程的 srna AnnData 完全分离。
- isomiR 定量结果（variant 级 UID 或 miRNA 级计数）**不得** `store_count_matrix` 到 srna 的 `adata.X` / `adata.layers["counts"]`。
- 保存时用独立文件，如 `isomir_counts.h5ad`；不要写回 srna 的 h5ad。

**2. isomiR 定量的 BAM 是比对到 miRBase hairpin 前体序列的，不是全基因组。**

- 参考序列是 `hairpin_hsa.fa`（前体），索引由 `sa.alignment.bowtie_build(hairpin_fa, ...)` 构建。
- **禁止**把全基因组比对的 BAM 传给 `sa.quant.mirtop`（mirtop 会把 reads 重比对到 hairpin 坐标，基因组坐标的 BAM 结果无意义）。
- 比对参数面向"成熟体在 hairpin 上的精确位置"：`bowtie -n 1 -l 15 -m 100 --best --strata`。

## Overview

isomiR（isomiR/isomiR）是 miRNA 成熟体的序列变异形式——5'/3' 端修剪（tailing/trimming）、3' 端加尾（non-templated addition）、单核苷酸替换（SNP）。mirtop 把这些变异从 hairpin 比对 BAM 中精确注释出来，并导出 variant 级 / miRNA 级 counts 与变异类型分布。

| Step | Tool | Function | Purpose |
|------|------|----------|---------|
| 0 | — | `ad.AnnData(obs=...)` | 初始化**独立 isomiR 模态** adata |
| 1 | miRBase | `sa.reference.download_mirbase` | 下载 hairpin FASTA + 前体 GFF3（含物种三位代码） |
| 2 | cutadapt/FastQC | `sa.fastq.cutadapt` / `sa.fastq.fastqc` | 接头去尽 + 质控（clean reads） |
| 3 | bowtie-build | `sa.alignment.bowtie_build` | 用 **hairpin.fa** 构建比对索引 |
| 4 | bowtie | `sa.alignment.bowtie` | clean reads 比对到 **hairpin 前体** → sorted BAM |
| 5 | mirtop | `sa.quant.mirtop` | isomiR 注释 + counts（variant/miRNA 粒度）+ stats |

典型流程：

```
raw FASTQ ──cutadapt──> clean FASTQ ──bowtie(→hairpin)──> sorted BAM
                                                              │
                                              sa.quant.mirtop │
                                                              ▼
                          adata_iso.X / layers["counts"] (isomiR 独立模态)
                          + adata_iso.uns["mirtop_result"]（含 stats 分布）
```

> ⚡ **批量样本：** `sa.fastq.cutadapt` / `sa.fastq.fastqc` / `sa.alignment.bowtie` 均支持 `jobs=N` 样本级并行；`sa.quant.mirtop` 一次调用处理所有 BAM，运行中输出 `[mirtop] progress: N/M` 进度（监控线程，UI 代码卡可见）。

## Prerequisites

- **独立 isomiR AnnData**：样本名在 `adata_iso.obs.index`，与 srna 模态分开。
- **miRBase hairpin 数据**：`ref/hairpin_hsa.fa`（前体序列）+ `ref/hsa.gff3`（前体 GFF3 注释），来自 `sa.reference.download_mirbase`（见 `reference-download` skill，物种三位代码可用 `sa.reference.list_mirbase_codes` 查）。
- **clean FASTQ**：`adata_iso.obs["trimmed_path"]`（cutadapt 后自动写入，见 `fastq-qc` skill）。
- 或 **现成 hairpin 比对 BAM**：`adata_iso.obs["bam_path"]`。

## Instructions

### 0. 初始化独立的 isomiR 模态

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# 独立模态：不要复用 srna 的 adata
adata_iso = ad.AnnData(obs=pd.DataFrame(index=["SRR26304152", "SRR26304153"]))
```

> ⚠️ **样本命名规则：** 默认使用 SRR Run ID 作为 `obs_names`；用户自备数据或明确要求时才用自定义名（如 `S1`）。

### 1. 参考文件准备：hairpin 前体序列 + GFF3 + 物种代码

```python
# 物种三位代码（hsa/mmu/...）可在 reference-download skill 查到：
codes = sa.reference.list_mirbase_codes()
print(codes)  # 确认目标物种的 3-letter code

result = sa.reference.download_mirbase(species="hsa", output_dir="ref", jobs=4)
# result["hairpin"]  → ref/hairpin_hsa.fa   （hairpin 前体序列，比对参考）
# result["gff3"]     → ref/hsa.gff3          （前体 GFF3 注释，mirtop 的 --gtf 输入）
```

- `hairpin_hsa.fa` 用作 **bowtie 索引的参考序列**（后续比对到它）。
- `hsa.gff3` 用作 **`sa.quant.mirtop` 的 `gff` 参数**（前体 + 成熟体坐标）。

> ❌ 不要用 `mature_hsa.fa`（成熟体）建索引 —— isomiR 的 5'/3' 修剪需要 reads 落在 **hairpin 前体**上才有上下文。

### 2. 质控：接头去尽（Adapter Trimming）

isomiR 定量必须用去接头后的 clean reads（接头残留会变成 3' 加尾假象，污染 isomiR 判定）。

```python
adata_iso = sa.fastq.cutadapt(
    adata_iso,
    output_dir="trimmed_iso",
    adapter_3="TGGAATTCTCGGGTGCCAAGG",   # 先用 fastq-qc skill 的流程确认 adapter
    min_length=15,
    max_length=35,
    jobs=4,
)
# 产物：adata_iso.obs["trimmed_path"]
```

- 参考 `fastq-qc` skill：先确认建库试剂盒的 3' adapter 序列，必要时跑 FastQC 看 Overrepresented Sequences。
- 长度过滤建议 15–35 nt（miRNA 成熟体 + isomiR 变异范围）。

### 3. 构建 hairpin 比对索引

```python
sa.alignment.bowtie_build("ref/hairpin_hsa.fa", "ref/hairpin_hsa", threads=4)
```

### 4. 比对到 hairpin 前体（不是全基因组）

对应 CLI：`bowtie -n 1 -l 15 -m 100 --best --strata -S hairpin_ref clean_reads.fq | samtools sort -o sorted.bam`

```python
adata_iso = sa.alignment.bowtie(
    adata_iso,
    index_basename="ref/hairpin_hsa",
    seed_mismatches=1,      # -n 1：seed 允许 1 个错配
    seed_len=15,            # -l 15：seed 长度
    m=100,                  # -m 100：最多报告 100 个比对位置
    best=True,              # --best：只保留最优比对
    strata=True,            # --strata：按层报告
    threads=8,
    jobs=4,
    output_dir="aligned_hairpin",
)
# 产物：adata_iso.obs["bam_path"]（sorted BAM + .bai，pysam 完成排序索引）
```

> ⚠️ **再次强调：** `index_basename` 必须是第 3 步用 `hairpin_hsa.fa` 建的索引。如果误用了基因组索引，`sa.quant.mirtop` 的结果毫无意义。

### 5. isomiR 定量（mirtop）

```python
adata_iso = sa.quant.mirtop(
    adata_iso,
    gff="ref/hsa.gff3",            # 前体 GFF3（--gtf）
    hairpin="ref/hairpin_hsa.fa",  # hairpin 前体 FASTA（--hairpin）
    species="hsa",                 # 物种三位代码（--sps）
    granularity="variant",         # 默认 variant：不聚合，每个 isomiR 一个特征
                                   # 需要成熟体水平汇总时才显式传 "miRNA"
    output_dir="mirtop_out",
    normalize=True,                # 额外写 layers["logcpm"]
)
```

工具内部（真实 mirtop CLI）：

```bash
mirtop gff --sps hsa --gtf ref/hsa.gff3 --hairpin ref/hairpin_hsa.fa \
  --out mirtop_out/ aligned_hairpin/SRR1.bam aligned_hairpin/SRR2.bam ...
mirtop counts --gff mirtop_out/mirtop.gff --out mirtop_out/     # → mirtop.tsv
mirtop stats -o mirtop_out/ mirtop_out/mirtop.gff               # → mirtop_stats.txt
```

> 幂等：`mirtop.gff` / counts TSV 已覆盖全部样本时自动跳过重跑（`overwrite=False` 默认）。

### 6. 已有 hairpin 比对 BAM：直接从 BAM 往下做

如果用户已有比对好的 BAM（跳过质控/比对），直接构造独立模态并调用 mirtop：

```python
adata_iso = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2"]))
adata_iso.obs["bam_path"] = ["aligned/S1.hairpin.bam", "aligned/S2.hairpin.bam"]

adata_iso = sa.quant.mirtop(
    adata_iso,
    gff="ref/hsa.gff3",
    hairpin="ref/hairpin_hsa.fa",
    species="hsa",
    granularity="variant",   # 默认值：不聚合，每个 isomiR 变异一行
    output_dir="mirtop_out",
)
```

> 前提：BAM 必须是对 **hairpin 前体序列** 的比对；缺 `.bai` 时工具会自动用 samtools 建索引。

### 7. 查看结果与保存

```python
print(adata_iso.shape)                              # (n_samples, n_isomirs)
print(adata_iso.var[["mirna_id", "rna_type", "variant_type"]].head())
print(adata_iso.uns["mirtop_result"]["stats_log"])  # 各样本变异类型分布

# 独立模态 → 独立 h5ad
adata_iso.write("isomir_counts.h5ad")
```

- **默认 `granularity="variant"`（不聚合）**：`var_names` 是 isomiR UID（如 `hsa-let-7a-5p|0,0,0,0,0,0`），每个变异一行，保留完整变异信息。
- **`adata.var` 携带 isomiR 类型信息**（variant 粒度）：`variant_type`（如 `iso_5p` / `iso_3p` / `iso_add3p` / `iso_snp` 或组合）、`reads`（该 isomiR 总读数）、`iso_5p` / `iso_3p` / `iso_add3p` / `iso_snp`（各变异类型计数）、`mirna_id`（成熟体名）。
- **需要成熟体水平汇总时**才显式传 `granularity="miRNA"`：`var_names` 是成熟体名（如 `hsa-let-7a-5p`），同一 miRNA 的变异求和；此时 `variant_type` 为空、`iso_*` 按成熟体求和。
- `adata.uns["mirtop_result"]["stats_log"]` 含 mirtop stats 的变异类型分布（iso_5p / iso_3p / iso_add3p / iso_snp …），用于判断测序质量与加尾修饰偏好。

## Troubleshooting

- **mirtop 启动报 `ModuleNotFoundError: No module named 'pkg_resources'`**：环境里的 setuptools ≥ 82 移除了 pkg_resources。修复：`pip install "setuptools<81"`。
- **mirtop 报 `Database not found in ... header`**：前体 GFF3 头部缺 `##miRBase` 标识。用 `sa.reference.download_mirbase` 下载的正规 miRBase GFF3 自带该标识；自制的 GFF 需在头部补 `##miRBase <version> <species>`。
- **counts 全为 0 / stats 报 IndexError**：该样本没有 reads 落在成熟体位置——检查是否用了成熟体索引而非 hairpin 索引、adapter 是否去干净、`--sps` 物种代码是否与 GFF 一致。
- **`sa.quant.mirtop` 找不到 BAM**：`adata.obs` 需有 `bam_path` 列（`sa.alignment.bowtie` 自动写）；只有 `sam_path` 时会自动找同名 `.bam`。

## 结果持久化与查询纪律

### 保存结果（必须，保证跨会话可查）

isomiR 定量结果写入**独立** `adata_iso` 的 `X`/`layers["counts"]`、`var`（isomiR 特征）、`uns["mirtop_result"]`，**必须保存独立 h5ad**：

```python
adata_iso.write("isomir_counts.h5ad")
reload = ad.read_h5ad("isomir_counts.h5ad")
print(reload.shape, list(reload.var.columns))
```

### 查询已有结果（只读查询，禁止重跑）

用户问"XX isomiR 变异 / 加尾偏好 / 变异比例"时，**先查已有数据，绝不重跑 mirtop**：

```python
# 1) 加载已保存的 isomiR h5ad，直接查
adata_iso = ad.read_h5ad("isomir_counts.h5ad")
print(adata_iso.var_names[:5])
print(adata_iso.uns["mirtop_result"]["stats_log"])   # 变异类型分布

# 2) mirtop_out 下 mirtop.gff / mirtop.tsv 已覆盖全部样本时，重跑会被跳过（overwrite=False）
# 3) 都没有 → 告知用户"现有结果不存在"，询问是否重新分析；不要偷偷重跑
```

## References

- Copy-paste-ready code templates: [`reference.md`](reference.md)
- mirtop docs: <https://github.com/miRTop/mirtop>
- Upstream skills:
  - [`fastq-qc`](../fastq-qc/SKILL.md) — 接头去尽与质控
  - [`alignment-srna`](../alignment-srna/SKILL.md) — bowtie 比对（本 skill 用 hairpin 索引）
  - [`reference-download`](../reference-download/SKILL.md) — hairpin FASTA / GFF3 / 物种三位代码
  - [`feature-count`](../feature-count/SKILL.md) — 需要成熟体 level 定量（非 isomiR 变异）时的替代方案
