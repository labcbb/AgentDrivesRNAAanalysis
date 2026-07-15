---
name: differential-analysis
title: miRNA differential expression analysis (limma-voom)
description: "Filter lowly expressed miRNAs and run limma-voom differential expression on AnnData. User must provide group labels in adata.obs."
---

# miRNA Differential Expression Analysis

## Overview

This skill covers differential expression analysis for miRNA-seq data using limma-voom (pylimma):

| Step | Tool | Function | Purpose |
|------|------|----------|---------|
| 1 | filter_low_expression | `sa.diff.filter_low_expression` | Remove miRNAs with mean count ≤ 1 |
| 2 | de_analysis | `sa.diff.de_analysis` | limma-voom DE (voom → lmFit → contrasts → eBayes) |
| 3 | — | `adata.uns["de_results"]` | Inspect results for specific miRNAs |

```
Raw counts (adata.layers["counts"] 或 adata.X)
    │
    ▼
filter_low_expression(min_mean=1.0)
    │  移除平均 count ≤ 1 的低表达 miRNA
    │  原始完整矩阵备份到 adata.uns["raw_counts"]
    ▼
de_analysis(control_group="Normal")
    │  voom → lmFit → contrasts_fit → eBayes
    ▼
adata.uns["de_results"]  ← 全部基因的差异分析结果
adata.uns["de_params"]   ← 对比元信息
```

## Prerequisites

- **AnnData object** with raw miRNA counts in `adata.layers["counts"]` (or `adata.X`)
- **Group labels** in `adata.obs` — a column indicating which samples belong to which group (e.g., `"Tumor"` vs `"Normal"`, `"Treat"` vs `"Ctrl"`)

> ⚠️ **Agent 行动要求：必须先让用户确认分组信息！**
>
> 在开始差异分析之前，必须执行以下步骤：
> 1. 检查 `adata.obs` 中是否有分组列。常见列名：`group`、`Condition`、`treatment`、`Group` 等。
> 2. **主动向用户展示当前的分组情况**，让用户确认是否正确。
> 3. 如果用户尚未设置分组，询问用户希望如何分组。
> 4. 如果用户不确定分组来源，可以询问用户是否有样本信息表（CSV/Excel），或从 SRA/GEO 元数据获取（参见附录）。

## Instructions

### 1. 检查分组信息

首先检查 `adata.obs` 中是否有分组列。`de_analysis` 会自动检测常见列名（`group`、`Condition`、`treatment` 等），也可以通过 `group_col` 参数指定任意列名：

```python
import sRNAgent as sa
import anndata as ad

adata = ad.read_h5ad("quantified_mirna.h5ad")

# 查看所有可用的 obs 列
print("所有 obs 列:", adata.obs.columns.tolist())

# 如果已知道分组列名，直接确认
group_col = "group"  # 也可以是 "Condition"、"treatment" 等，由用户指定
if group_col in adata.obs.columns:
    print(f"\n分组列 '{group_col}' 的分布:")
    print(adata.obs[group_col].value_counts())
```

**向用户展示分组情况并确认：**

```python
for sample, grp in zip(adata.obs_names, adata.obs[group_col]):
    print(f"  {sample}: {grp}")
```

> ⚠️ **必须让用户确认分组无误后再继续。** 如果用户要使用不同的列，通过后续 `de_analysis` 的 `group_col` 参数指定即可。

如果用户还没有设置分组，询问后写入：

```python
# 示例：用户提供了分组列表
adata.obs["group"] = ["Tumor", "Normal", ...]  # 用户提供
```

**确认无误后再继续后续步骤。**

### 2. 过滤低表达 miRNA

```python
# 备份原始 counts
adata.layers["counts"] = adata.X.copy()

# 过滤：保留平均 count > 1 的 miRNA
sa.diff.filter_low_expression(adata, min_mean=1.0)
```

过滤后：
- 原始完整矩阵保存在 `adata.uns["raw_counts"]`
- `adata.X`、`adata.layers["counts"]`、`adata.layers["logcpm"]` 等所有 layer 同步过滤
- 打印保留/去除的 miRNA 数量

### 3. 差异分析

```python
# 自动检测 group 列，指定对照组
sa.diff.de_analysis(adata, control_group="Normal")

# 查看结果
print(adata.uns["de_results"].head())
print(adata.uns["de_params"])
```

**`de_analysis` 自动完成：**
1. 从 `adata.layers["counts"]` 读取原始 counts，写入 `adata.X`
2. 自动检测 `adata.obs` 中的分组列（`group` / `Condition` / `treatment` 等）
3. 创建设计矩阵 + 对比矩阵
4. `voom` → `lm_fit` → `contrasts_fit` → `e_bayes`
5. 全部基因的结果存入 `adata.uns["de_results"]`
6. 对比元信息存入 `adata.uns["de_params"]`

**参数说明：**

| 参数 | 默认 | 说明 |
|------|------|------|
| `group_col` | 自动检测 | 指定分组列名 |
| `control_group` | 字母序第一个 | 指定对照组，该组作为比较基线 |

### 4. 查看特定 miRNA 的差异结果

```python
de = adata.uns["de_results"]

# 查看 miR-21-5p
if "hsa-miR-21-5p" in de.index:
    row = de.loc["hsa-miR-21-5p"]
    print(f"logFC:     {row['log_fc']:.2f}")
    print(f"P.Value:   {row['p_value']:.2e}")
    print(f"adj.P.Val: {row['adj_p_value']:.2e}")

# 按 p 值排序查看 top DE miRNAs
print(de[["log_fc", "p_value", "adj_p_value"]].head(20))

# 筛选显著差异的 miRNA
sig = de[de["adj_p_value"] < 0.05]
up = sig[sig["log_fc"] > 0]
down = sig[sig["log_fc"] < 0]
print(f"显著差异: {len(sig)} (上调 {len(up)}, 下调 {len(down)})")
```

### 5. 保存结果

```python
# 所有结果都在 adata 中，直接保存 h5ad 即可
adata.write("de_results.h5ad")

# 重新加载后结果仍在
reload = ad.read_h5ad("de_results.h5ad")
print(reload.uns["de_results"].head())
print(reload.uns["de_params"])
```

## 附录：如何获取分组信息

如果用户没有现成的分组信息，可以尝试以下方式：

### 方式一：用户提供样本信息表

用户可能有 CSV/Excel 文件包含样本名与分组的对应关系：

```python
import pandas as pd
info = pd.read_csv("sample_info.csv")  # 用户提供
adata.obs["group"] = info.set_index("sample_name").loc[adata.obs_names, "group"]
```

### 方式二：从 ENA / GEO 元数据获取

如果样本是 SRA Run ID（如 SRR 开头），可以通过公共数据库查询分组：

```python
# 通过 ENA API 获取 SRR → sample_title 映射
import urllib.request, urllib.parse
params = {"accession": "SRP335685", "result": "read_run",
          "fields": "run_accession,sample_title", "format": "tsv", "limit": "0"}
url = "https://www.ebi.ac.uk/ena/portal/api/filereport?" + urllib.parse.urlencode(params)
with urllib.request.urlopen(url, timeout=30) as resp:
    data = resp.read().decode()
srr_to_sample = {}
for line in data.strip().split("\n")[1:]:
    parts = line.split("\t")
    if len(parts) >= 2:
        srr_to_sample[parts[0]] = parts[1]

# 再通过 GEO 查询每个 sample 的分组
# ...（详见 reference.md）
```

## Critical API Reference

### 完整差异分析流程

```python
import sRNAgent as sa
import anndata as ad

# ── 1. 加载数据 ──
adata = ad.read_h5ad("quantified_mirna.h5ad")

# ── 2. 确认分组 ──
# 确保 adata.obs 中有分组列，并经用户确认
# group_col 可以是 "group"、"Condition"、"treatment" 等任意列名
group_col = "group"
print(adata.obs[[group_col]].to_string())  # 向用户展示确认

# ── 3. 过滤低表达 ──
adata.layers["counts"] = adata.X.copy()
sa.diff.filter_low_expression(adata, min_mean=1.0)

# ── 4. 差异分析 ──
sa.diff.de_analysis(adata, control_group="Normal")

# ── 5. 查看结果 ──
de = adata.uns["de_results"]
print(f"差异基因: {(de['adj_p_value'] < 0.05).sum()} 个")

# ── 6. 保存 ──
adata.write("de_results.h5ad")
```

### 输出格式

```python
# adata.uns["de_results"] — DataFrame，index = miRNA 名称
# 列:
#   log_fc       — log2 差异倍数 (treatment vs control)
#   ave_expr     — 平均表达量
#   t            — t 统计量
#   p_value      — P 值
#   adj_p_value  — FDR (BH) 校正 P 值
#   b            — 对数 odds 值

# adata.uns["de_params"] — dict
#   group_col           — 使用的分组列名
#   groups              — 所有分组列表
#   treatment           — 处理组名称
#   control             — 对照组名称
#   contrast_formula    — 对比公式
#   n_samples           — 样本数
#   n_features          — 检测的特征数

# adata.uns["raw_counts"] — 过滤前的完整 count 矩阵 (numpy array)
```

## References

- Copy-paste-ready code templates: [`reference.md`](reference.md)
- pylimma: <https://pypi.org/project/pylimma/>
