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

## 结果持久化：分析结果必须落盘，能跨会话查询

分析完成（尤其是差异分析 DE、定量、比对）后，**结果必须持久化**，否则新会话/新提问无法直接查询，被迫重算（历史上因此出现过单个查询跑 103 轮的案例）：

1. **保存带结果的 adata**：把包含 `uns['de_results']` / `uns['de_params']` 等结果的 adata 用 `write_h5ad` 覆盖保存回原 h5ad（或显式命名的新文件），保存后 `read_h5ad` 验证结果仍在。
2. **登记结果位置**：在工作区记录结果文件路径（例如 `de_analysis(output_dir=...)` 生成的 `de_results.csv` / `de_results_manifest.json`），让后续会话能通过文件快速查到结果，无需加载整个 adata。
3. **DE 推荐带 `output_dir`**：`sa.diff.de_analysis(adata, output_dir="de_results")` 会自动把全表写到 `de_results/de_results.csv` 并登记 `de_results_manifest.json`（含 group_col / treatment / control / n_features）。

## 查询纪律：只读查询先查已有结果，禁止重跑

回答"某某结果是什么样的 / 有没有跑过 X"这类**只读查询**时，按此顺序查找，找到即止：

1. `adata.uns` / `adata.obs` / `adata.layers`（加载 h5ad 后直接看，不重算）
2. 工作区已登记的结果文件（`de_results_manifest.json`、`de_results.csv`、`de_locations_manifest.json` 等）
3. 旧 session 目录（`sessions/*/run_report.json`、`chat.json`）里的已完成记录

**只有全部找不到、且用户明确要求重新分析时，才允许重跑**；重跑前先向用户说明"已有结果不存在，需要重算"。禁止为了回答一个查询问题而重跑昂贵的 limma-voom / 定量 / 比对流程。

### 输出纪律：切片读取，禁止 print 大对象

从 `adata.uns` / `adata.obs` 读结果时，**只输出需要的切片**，禁止 `print(adata)`、`print(整张 DataFrame)` 或打印会生成巨量文本的对象 —— 大输出会触发 LLM 服务端内容过滤（`input new_sensitive`），导致任务被硬中断（历史上因此中断过，agent 被迫改走 CSV 路径）。

```python
de = adata.uns["de_results"]
# ✅ 正确：只打印目标行 / 头部
print(de.loc[["hsa-miR-21-5p", "hsa-miR-21-3p"]])
print(de.head(10))
# ❌ 错误：print(de) / print(adata) —— 输出过大触发过滤
```

优先读取 h5ad 的 `uns`（结果已随 h5ad 持久化），只有 h5ad 里没有时才读工作区 CSV；两条路都要用切片/限制行数的方式输出。

## 多任务与旁路查询（Branch Chat）

UI 左侧的 **Branch Chat（监管者）** 是**旁路只读 Agent**，与当前主任务**可以并行使用**：

- 它只读主会话的对话 / Thinking / 计划 / 运行账本 / 工作区快照来回答"任务进展 / 产物在哪 / 结果如何"类问题，**不执行代码、不修改环境、不抢 Jupyter 内核**。
- 主任务运行中打开 Branch Chat 提问是安全的，不会打断主任务（服务端断连已不会自动取消任务）。
- 同一个 chat 同时只允许一个主任务 run；新发主消息会先停掉旧的，属正常设计。
- 回答 Branch Chat 问题时，同样遵守"查询纪律 + 输出纪律"：优先引用已落盘的 `uns` / 结果文件，不重跑分析，不 print 大对象。
