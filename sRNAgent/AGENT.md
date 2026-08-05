# sRNAgent Agent Guide

## 核心规则：只维护一个 adata，全程留痕

全流程只维护**一个 AnnData 对象**。所有 `sa.fastq.*`、`sa.alignment.*`、`sa.quant.*`、`sa.diff.*` 工具都操作并返回**同一个 adata**，每一步的产物与元数据都写回这个 adata（`obs` / `uns` / `X` / `var`），在 adata 中留下痕迹。

**因此，检查分析过程 = 直接加载 adata 即可**：`adata.obs` 看样本级别结果，`adata.uns` 看步骤元数据与中间结果，`adata.X` 看表达矩阵。不需要去翻日志或重新推断中间文件，一切状态都在 adata 里。

```python
import anndata as ad
import pandas as pd

# 初始化一次
adata = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2"]))

# 沿流程传递同一个 adata，不断扩展 obs/uns/X，每步都留痕
adata = sa.fastq.fastq_dl(adata, ...)      # → obs["fastq_path"], uns["output_dir"]
adata = sa.fastq.cutadapt(adata, ...)      # → obs["trimmed_path"]
adata = sa.fastq.fastqc(adata, ...)        # → obs["fastqc_html"]
adata = sa.alignment.bowtie(adata, ...)    # → obs["bam_path"], uns["genome_index"]
adata = sa.quant.quantify_mirna(adata, ...) # → obs["collapsed_path"], adata.X

# 检查分析过程：直接打印这个 adata 的所有痕迹
print(adata.obs.columns)  # 每个样本经历了哪些步骤、产物路径在哪
print(list(adata.uns.keys()))  # 每一步的参数、版本、中间结果
```

**禁止**：
- ❌ 每个工具创建新的 AnnData 对象
- ❌ 忘记接收返回值（工具是 in-place 修改，但必须用返回值覆盖）
- ❌ 把步骤结果只写到日志/临时文件，而不写回 adata（不留痕）

```python
adata = sa.fastq.cutadapt(adata, ...)   # ✅ 必须接收返回值
sa.fastq.cutadapt(adata, ...)           # ❌ 修改会丢失！
```

## 留痕规范：每步必须写回 adata

每个工具运行后必须把结果写回 adata，让分析过程可追溯：

| 数据类型 | 写入位置 | 示例 |
|---------|---------|------|
| 样本级结果（文件路径、QC 值） | `adata.obs[列]` | `obs["trimmed_path"]`, `obs["fastqc_html"]` |
| 步骤元数据（参数、版本、全局信息） | `adata.uns[key]` | `uns["genome_index"]`, `uns["de_params"]`, `uns["multiqc_html"]` |
| 表达矩阵 | `adata.X` / `adata.layers` | 定量结果、logcpm 层 |
| 特征注释 | `adata.var` | miRNA 长度、类型 |

## 检查已跑过的步骤（避免重复运行）

先检查 `adata.obs_keys()` / `adata.uns_keys()` 看对应痕迹是否存在，避免重复运行步骤：

```python
if "trimmed_path" not in adata.obs_keys():
    adata = sa.fastq.cutadapt(adata, ...)
```

## 各工具需要的输入列

| 工具 | 读取 `adata.obs` 列 |
|------|-------------------|
| `cutadapt` | `fastq_path` |
| `fastqc` | `trimmed_path` (fallback `fastq_path`) |
| `multiqc` | 自动扫描 obs 路径的父目录 |
| `bowtie` | `trimmed_path` (fallback `fastq_path`) |
| `quantify_mirna` | `fastq_path` (prefer `trimmed_path`) |
| `predict_mirna` | 同上 |

## Reference Tools

`sa.reference.*` 是 stateless 的，不接受 adata，返回 dict。
