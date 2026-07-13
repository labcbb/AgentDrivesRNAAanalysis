---
name: fastq-qc
title: sRNA-seq FASTQ quality control pipeline
description: "sRNA-seq FASTQ QC pipeline: 3' adapter trimming with cutadapt, per-sample FastQC reports, and aggregated MultiQC report."
---

# sRNA-seq FASTQ Quality Control Pipeline

## Overview

Small RNA sequencing (sRNA-seq) produces short reads (18–50 bp) with a **3' adapter** ligated during library preparation. This adapter must be removed before mapping, and read quality should be verified. This skill covers the complete FASTQ QC pipeline using `sa.fastq.*` tools:

| Step | Tool | Function | Purpose |
|------|------|----------|---------|
| 1 | cutadapt | `sa.fastq.cutadapt` | Remove 3' adapter, trim low-quality bases, filter by read length |
| 2 | FastQC | `sa.fastq.fastqc` | Generate per-sample quality reports |
| 3 | MultiQC | `sa.fastq.multiqc` | Aggregate all FastQC reports into a single HTML summary |

The typical sRNA-seq workflow:

```
Raw FASTQ (.fastq.gz)
    │
    ▼
cutadapt ─── 3' adapter removal  (TGGAATTCTCGGGTGCCAAGG)
           ─── quality trimming   (-q 20)
           ─── length filter      (-m 18 -M 36)
    │
    ▼
Trimmed FASTQ
    │
    ▼
FastQC ─── per-sample HTML reports (*_fastqc.html, *_fastqc.zip)
    │
    ▼
MultiQC ─── aggregated multiqc_report.html
```

> ⚡ **批量样本时务必使用 `jobs=N` 并行处理**
>
> `sa.fastq.cutadapt` 和 `sa.fastq.fastqc` 都支持 `jobs` 参数控制并行处理的样本数。
> 样本多时（比如 >3 个），设置 `jobs=4` 可大幅缩短总耗时。
> 注意 `multiqc` 是单进程聚合报告，不需要 `jobs` 参数。
> 如果用户没主动提并行数，**agent 应该根据样本量推荐一个合理的 `jobs` 值**。

## Instructions

### 0. Initialisation

The `sa.fastq.*` functions operate on an AnnData object. Create one with sample names in `adata.obs.index`:

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2", "S3"]))
```

Each sample in `adata.obs.index` becomes a row that the pipeline populates with file paths and QC metrics.

> ⚠️ **必须提醒用户确认 adapter 序列 —— 这是 sRNA-seq 质控最关键的一步**
>
> sRNA-seq 文库构建时，3' adapter 被连接在 insert 两端，测序后 adapter 直接跟在 insert 后面。
> **如果 adapter 序列给错，cutadapt 无法正确切除接头，大部分 reads 无法比对到基因组，整个分析失败。**
>
> **Agent 行动要求：不要默认使用 TruSeq 的 adapter！必须先问用户：**
> 1. 询问用户使用的建库试剂盒名称
> 2. 让用户确认是否使用下面的默认序列，还是自己指定
> 3. 如果用户不确定，让对方查一下实验方法的 "Library preparation" 部分
> 4. 如果完全无法确定，可以建议先跑 FastQC 查看 Overrepresented Sequences
>
> 常见 3' adapter 序列参考（供用户选择）：
>
> | 建库试剂盒 | 3' adapter 序列 |
> |-----------|----------------|
> | TruSeq Small RNA (Illumina) | `TGGAATTCTCGGGTGCCAAGG` |
> | NEXTflex Small RNA | `TGGAATTCTCGGGTGCCAAGG` (通常相同) |
> | NEBNext Small RNA | `AGATCGGAAGAGCACACGTCTGAAC` |
> | QIAseq miRNA | `AACTGTAGGCACCATCAAT` |
> | SMARTer smRNA-Seq | `GTTCAGAGTTCTACAGTCCGACGATC` |

### 1. Adapter trimming with cutadapt

sRNA-seq libraries use a specific 3' adapter that must be removed:

```python
adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",  # TruSeq Small RNA 3' adapter
    min_length=18,     # miRNA minimal length
    max_length=36,     # small RNA maximal length
    quality_cutoff="20",
    output_dir="trimmed",
)
```

**关于 adapter_3 参数：**

- `adapter_3` 对应 cutadapt 的 `-a` 参数，表示 3' adapter
- 对于 sRNA-seq，这是**最关键的参数**——文库构建时 3' adapter 被连接在 insert 两端，测序后 adapter 直接跟在 insert 后面
- TruSeq Small RNA 的 3' adapter 序列是 `TGGAATTCTCGGGTGCCAAGG`
- 如果使用其他建库试剂盒（如 NEXTflex、NEBNext），请确认对应的 adapter 序列

**CORRECT — 单端 sRNA-seq:**

```python
adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    min_length=18,
    max_length=36,
)
```

**CORRECT — 多个样本批量处理:**

```python
adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    min_length=18,
    max_length=36,
    quality_cutoff="20",
    output_dir="trimmed",
    jobs=4,           # 4个样本并行处理
)
```

**关于 `jobs` 参数：** `cutadapt`、`fastq_dl`、`fastqc` 都支持 `jobs` 参数控制并行度。默认 `None`（串行），设为 >1 时用线程池并行处理多个样本。例如 `jobs=4` 表示同时跑 4 个样本。

**CORRECT — 额外修剪 poly-A 和 N 碱基:**

```python
adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    min_length=18,
    max_length=36,
    quality_cutoff="20",
    trim_n=True,     # 修剪 flanking N 碱基
    poly_a=True,     # 修剪 poly-A 尾巴
)
```

**WRONG — sRNA-seq 用 paired-end adapter 参数:**

```python
# WRONG! sRNA-seq 通常是单端测序，不需要 R2 adapter
# sa.fastq.cutadapt(..., adapter_3_r2="...")
```

**关于长度过滤：**

| RNA 类型 | 典型长度范围 | 建议参数 |
|----------|-------------|----------|
| miRNA | 18–25 nt | `min_length=18, max_length=25` |
| miRNA + siRNA | 18–30 nt | `min_length=18, max_length=30` |
| Full sRNA | 18–36 nt | `min_length=18, max_length=36` |
| piRNA | 24–32 nt | `min_length=24, max_length=32` |

### 2. 查看 cutadapt 结果

`cutadapt` 将修剪后的文件路径和 JSON 报告写入 `adata.obs` 列：

```python
adata = sa.fastq.cutadapt(adata,
                           adapter_3="TGGAATTCTCGGGTGCCAAGG")

print(adata.obs[["trimmed_path", "cutadapt_json"]].to_string())
print(adata.obs["cutadapt_report"].apply(
    lambda r: f"in={r.get('in_reads','?')}, "
              f"w_adapters={r.get('w_adapters','?')}, "
              f"out={r.get('out_reads','?')}, "
              f"too_short={r.get('too_short','?')}"
).to_string())
```

### 3. FastQC 质量报告

对修剪后的 FASTQ 文件运行 FastQC：

```python
# 单个样本（通过 adata 自动使用 cutadapt 输出）
adata = sa.fastq.fastqc(
    adata,
    output_dir="fastqc_reports",
    threads=2,
)

print(adata.obs[["fastqc_html", "fastqc_zip"]])
```

**CORRECT — 多个样本一起跑（自动对所有样本执行）:**

```python
adata = sa.fastq.fastqc(
    adata,
    output_dir="fastqc_reports",
    threads=4,        # FastQC 内部线程数
)
```

**CORRECT — 提取 HTML 路径:**

```python
adata = sa.fastq.fastqc(adata, output_dir="fastqc_reports")

for sample_name, html_path in adata.obs["fastqc_html"].items():
    print(f"{sample_name}: {html_path}")
```

**CORRECT — 并行处理多个 FastQC 文件:**

```python
adata = sa.fastq.fastqc(
    adata,
    output_dir="fastqc_reports",
    threads=2,        # 每个 FastQC 进程的线程数
    jobs=4,           # 同时跑 4 个文件
)
```

### 4. MultiQC 汇总报告

将 FastQC 报告目录传给 MultiQC，生成单个聚合 HTML，**并自动提取各样本质控指标到 `adata.obs`**：

```python
adata = sa.fastq.multiqc(
    adata,
    output_dir="multiqc_out",
    force=True,
)

print(f"Aggregated report: {adata.uns['multiqc_html']}")
# 浏览器打开 multiqc_out/multiqc_report.html 查看

# 查看自动提取的质控指标
print(adata.obs[[
    "multiqc_total_seqs", "multiqc_avg_length",
    "multiqc_pct_gc", "multiqc_pct_dups",
]].to_string())
```

**CORRECT — 查看每个样本的质控详情:**

```python
adata = sa.fastq.multiqc(adata, output_dir="multiqc_out")

# 通用质控指标
print(adata.obs.filter(like="multiqc_").to_string())

# FastQC 模块 pass/warn/fail 状态
qc_cols = [c for c in adata.obs.columns if c.startswith("multiqc_fastqc_")]
print(adata.obs[qc_cols].to_string())
```

**CORRECT — 同时包含 cutadapt 日志:**

```python
# MultiQC 会自动识别 cutadapt 的 JSON 报告和 FastQC zip 文件
adata = sa.fastq.multiqc(
    adata,
    output_dir="multiqc_out",
    modules=["fastqc", "cutadapt"],  # 明确指定模块
)
```

**CORRECT — 自定义文件名:**

```python
adata = sa.fastq.multiqc(
    adata,
    output_dir="multiqc_out",
    filename="srna_qc_report.html",
)
```

**CORRECT — 导出为 JSON 数据:**

```python
adata = sa.fastq.multiqc(
    adata,
    output_dir="multiqc_out",
    data_format="json",
)
```

## Critical API Reference

### 完整 sRNA-seq FASTQ QC 流程

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# ── 初始化 AnnData ──
adata = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2"]))

# ── 配置 ──
trimmed_dir = "trimmed"          # 修剪后输出目录
qc_dir = "fastqc_reports"        # FastQC 报告目录
multi_dir = "multiqc_out"        # MultiQC 输出目录

# ── Step 1: 3' adapter 修剪 + 长度过滤 ──
adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    min_length=18,
    max_length=36,
    quality_cutoff="20",
    output_dir=trimmed_dir,
    jobs=4,
)

# ── Step 2: FastQC 质量报告 ──
adata = sa.fastq.fastqc(adata, output_dir=qc_dir, threads=2, jobs=4)

# ── Step 3: MultiQC 汇总 ──
adata = sa.fastq.multiqc(adata, output_dir=multi_dir, force=True)

print(f"Final report: {adata.uns['multiqc_html']}")
```

### 输出格式

各函数的结果存储在 `adata.obs` 和 `adata.uns` 中：

```python
# cutadapt 写入 adata.obs 的列
# adata.obs["trimmed_path"]      — 修剪后 FASTQ 路径
# adata.obs["cutadapt_json"]     — JSON 报告路径
# adata.obs["cutadapt_report"]   — 解析后的 dict，包含:
#     {
#         "in_reads": 1000000,
#         "out_reads": 850000,
#         "too_short": 120000,
#         "w_adapters": 980000,
#     }
# adata.obs["cutadapt_log"]      — cutadapt 完整 stdout 日志路径
#                                  (含质控统计，可用于提取报告信息)

# FastQC 写入 adata.obs 的列
# adata.obs["fastqc_html"]       — HTML 报告路径
# adata.obs["fastqc_zip"]        — ZIP 文件路径

# MultiQC 写入 adata.uns
# adata.uns["multiqc_html"]      — multiqc_report.html 路径
# adata.uns["multiqc_data_dir"]  — multiqc_data 目录路径

# MultiQC 自动提取到 adata.obs 的列（multiQC 运行后自动填充）:
# adata.obs["multiqc_total_seqs"]        — 总序列数
# adata.obs["multiqc_avg_length"]        — 平均读长 (bp)
# adata.obs["multiqc_med_length"]        — 中位数读长 (bp)
# adata.obs["multiqc_pct_gc"]            — GC 含量 (%)
# adata.obs["multiqc_pct_dups"]          — 重复 reads 比例 (%)
# adata.obs["multiqc_pct_fails"]         — FastQC 模块失败率 (%)
# adata.obs["multiqc_fastqc_basic_statistics"]  — FastQC 模块状态: pass/warn/fail
# adata.obs["multiqc_fastqc_per_base_sequence_quality"]
# adata.obs["multiqc_fastqc_per_sequence_quality_scores"]
# adata.obs["multiqc_fastqc_per_base_sequence_content"]
# adata.obs["multiqc_fastqc_per_sequence_gc_content"]
# adata.obs["multiqc_fastqc_per_base_n_content"]
# adata.obs["multiqc_fastqc_sequence_length_distribution"]
# adata.obs["multiqc_fastqc_sequence_duplication_levels"]
# adata.obs["multiqc_fastqc_overrepresented_sequences"]
# adata.obs["multiqc_fastqc_adapter_content"]
```

## Troubleshooting

- **cutadapt 没有找到 adapter**: 确认 adapter 序列是否正确。TruSeq Small RNA 的 3' adapter 是 `TGGAATTCTCGGGTGCCAAGG`。不同建库试剂盒（NEBNext、NEXTflex）序列不同。
- **修剪后 reads 太短**: sRNA 片段本身短（18–30 nt），这是正常的。检查 `min_length` 是否设得太高。
- **修剪后 reads 太长**: 检查 adapter 是否被正确识别。用 `quality_cutoff` 提高严格度，或用 `error_rate=0.15` 降低匹配容错率。
- **FastQC 报 "No sequences in file"**: 确认 cutadapt 输出文件路径正确，且文件非空。
- **MultiQC 看不到某个模块**: MultiQC 自动识别文件后缀。FastQC 需要 `*_fastqc.zip`（不是 HTML），cutadapt 需要 `*.cutadapt.json`。确保文件后缀正确。
- **MultiQC 报告为空**: 确认输入目录路径正确，且包含可识别的 log 文件。可以用 `-v` 查看详细扫描日志。
- **HTML 报告无法打开**: 检查 `filename` 参数是否以 `.html` 结尾。
- **批量处理顺序**: 确保样本标签在 cutadapt 和下游分析中保持一致，方便追溯。

## References

- Copy-paste-ready code templates: [`reference.md`](reference.md)
- cutadapt: <https://cutadapt.readthedocs.io/>
- FastQC: <https://www.bioinformatics.babraham.ac.uk/projects/fastqc/>
- MultiQC: <https://multiqc.info/>
- TruSeq Small RNA adapter: <https://support.illumina.com/sequencing/sequencing_kits/truseq-small-rna.html>
