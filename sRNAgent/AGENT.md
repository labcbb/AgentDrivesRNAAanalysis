# sRNAgent Agent Guide

## 核心规则

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
