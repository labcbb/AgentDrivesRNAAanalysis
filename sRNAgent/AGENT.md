# sRNAgent Agent Guide

## 核心规则：维护同一个 adata

所有 `sa.fastq.*`、`sa.alignment.*`、`sa.quant.*` 工具都操作并返回**同一个 AnnData 对象**。

```python
import anndata as ad
import pandas as pd

# 初始化一次
adata = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2"]))

# 沿流程传递同一个 adata，不断扩展 obs/uns/X
adata = sa.fastq.fastq_dl(adata, ...)      # → obs["fastq_path"]
adata = sa.fastq.cutadapt(adata, ...)      # → obs["trimmed_path"]
adata = sa.fastq.fastqc(adata, ...)        # → obs["fastqc_html"]
adata = sa.alignment.bowtie(adata, ...)    # → obs["bam_path"]
adata = sa.quant.quantify_mirna(adata, ...) # → obs["collapsed_path"], adata.X

print(adata.obs.columns)  # 所有步骤的结果都在同一个 adata 里
```

**禁止**：
- ❌ 每个工具创建新的 AnnData 对象
- ❌ 忘记接收返回值（工具是 in-place 修改，但必须用返回值覆盖）

```python
adata = sa.fastq.cutadapt(adata, ...)   # ✅ 必须接收返回值
sa.fastq.cutadapt(adata, ...)           # ❌ 修改会丢失！
```

## 调用工具前先检查 adata

先检查 `adata.obs_keys()` 看对应字段是否存在，避免重复运行步骤：

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
