# sRNAgent Agent Guide

## 核心规则：只维护一个 adata，全程留痕

全流程只维护**一个 AnnData 对象**。所有 `sa.fastq.*`、`sa.alignment.*`、`sa.quant.*`、`sa.diff.*` 工具都操作并返回**同一个 adata**，每一步的产物与元数据都写回这个 adata（`obs` / `uns` / `X` / `var`），在 adata 中留下痕迹。

**因此，检查分析过程 = 直接加载 adata 即可**：`adata.obs` 看样本级别结果，`adata.uns` 看步骤元数据与中间结果，`adata.X` 看表达矩阵。不需要去翻日志或重新推断中间文件，一切状态都在 adata 里。

## MuData 兼容规则：外层容器可用，但执行内核仍是 srna AnnData

当前 tool / skill 仍然围绕 **sRNA 模态的 AnnData** 设计；如果上层传入的是 `MuData`，默认一律取 `mdata.mod["srna"]` 作为执行对象，工具运行完成后再写回这个模态。

- 允许：`AnnData` 直接作为输入
- 允许：`MuData` 作为外层容器输入，默认操作 `mod="srna"`
- 暂不支持：同一个 tool / skill 在一次调用里跨多个模态联合执行
- 若用户显式指定其他模态，可通过 `mod=...` 选择；未指定时默认 `srna`

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

## 样本级并行：批量样本必须并行，禁止串行

处理**多个样本**的批量任务（下载 / 修剪 / QC / 比对 / 定量 / 计数）时，**必须用并行参数**，禁止逐样本串行跑：

| 工具 | 并行参数 | 说明 |
|------|---------|------|
| `sa.fastq.fastq_dl` / `cutadapt` / `fastqc` | `jobs=N` | N 个样本同时处理 |
| `sa.alignment.bowtie` | `jobs=N`（+ `threads` 为 bowtie 内部线程） | 每个样本一个 bowtie 进程 |
| `sa.quant.quantify_mirna` / `predict_mirna` | `jobs=N` | 每个样本独立 mapper+quantifier/miRDeep2 |
| `sa.quant.idxstats` | `jobs=N` | 多 BAM 并行 |
| `sa.quant.feature_count` | `threads=N`（一次传全部 BAM） | featureCounts 单进程多文件 + 内部线程 |
| `sa.quant.trax_quant` | `cores=N` | tRAX 内部并行 |

- 样本数 > 3 时推荐 `jobs=4`（或按机器 CPU 核数 `os.cpu_count()` 取合理值）；内存吃紧时降低到 `jobs=2`。
- 各 skill 已给出对应的 `jobs` / `cores` / `threads` 示例，调用前先查 skill。
- 如果用户没指定并行数，**agent 应根据样本量主动选择一个合理的 `jobs` 值**，不要默认串行。

## 批处理调用与产物复用

避免两种反复重做的反模式：

- **批量工具一次 `execute_code` 处理全部样本**，不要按样本拆 N 次 cell：
  按样本拆 cell 会让 Jupyter 内核反复重启（import + 读 h5ad 开销叠加），且与前一次手动 stop 留下的内核残留锁叠加，容易在下一步 `read_h5ad` 死锁。正确做法：`sa.quant.trax_quant(adata, ...)`、`sa.alignment.bowtie(adata, ...)`、`sa.quant.feature_count(adata, ...)`、`sa.quant.quantify_mirna(adata, ...)` 都接受整个 adata，在**一次调用**里处理 `adata.obs` 里的全部样本（工具内部 Pool/run_threads 已处理并发）。
- **优先读取已落盘产物**，不重算：
  - 合并 counts → 读 `trax_out/<exp>-trnacounts.txt` / `mirna_expression_counts.csv` / `de_results_all.csv` / `*_manifest.json`
  - 查询差异结果 → 读 `adata.uns['de_results']` / `adata.uns['de_top']`（**不要为了查 miR-21 重跑 limma-voom**）
  - 跑前先 `Path(...).exists()` 确认产物齐全；齐全就直接用，不存在再调用工具
- 工具自带的幂等（`pylimma.de_analysis` 缓存命中、`tRAX.trax_quant` trnacounts 已存在则跳过）会自动复用结果，**优先信任缓存，不要主动 force=True**。

## 严格遵循 skill：禁止发明 skill 外的流程与命名

用户的定量/比对/分析任务**必须严格按 skill 执行**，这是"智能"的第一条标准：

- **目录名、参数、函数**一律采用 skill 里写明的（如比对一律 `output_dir="aligned"`；定量用 `mirdeep2` / `trax_quant` / `idxstats` / `feature_count`，各自 output_dir 见对应 skill）。**禁止发明** skill 外的目录/变体（历史上出现过 agent 自创 `aligned_perm`、`aligned_strict`，skill 里根本没有）。
- skill 里的**概念说明**（如"stringent/permissive mapping"）只是备选信息，**不是要求跑两条独立流程**；除非用户明确要对比两种模式，否则按 skill 主流程**一次完成**。
- 执行前先加载对应 skill 的 SKILL.md，确认里面**有**这个操作；skill 没有覆盖的需求 → **先向用户报告缺口并询问**，不要自由发挥。
- 不要因为"觉得更严谨"就额外加步骤、加目录、加参数。多做的、skill 没有的，就是偏离。

## 任务记忆与去重（避免"失忆"和"反复确认"）

每一次回答前**先查会话级持久记忆**（`build_session_memory_context` 会在每次启动时注入 system prompt）：

- 已确认的方案 / 关键参数 / 对比公式 → 在 session_memory 或 `adata.uns` 里
- 已落盘的产物路径 → `*_manifest.json` / `session_memory.artifacts`
- 已完成的步骤 / 已写出的报告 → `run_report.json` 的 tasks 列表
- 已知的"哪些做了哪些没做" → 看 `run_ledger.json` 和 `plan.json` 的 step status

**禁止**：

- 反复问用户"请确认是否 X"—— 如果已确认过，直接执行；只有当持久状态**真的自相矛盾**或用户之前表达确实歧义时才提问。
- 把已完成的工作再检查一遍—— 摘要已在 memory context，看一眼就知道做过什么。
- 为了"保险"重复 `read_h5ad` / `list uns` / `print shape` —— 产物已在内存 adata 或落盘 h5ad 里，直接用。

**关键事实在做完后立即登记**（写 `adata.uns` 或 session_memory），不要只留在对话气泡里 —— 对话会被 compaction 摘要，**memory context 不丢**。

## 执行超时处理（内核无响应）

`execute_code` 提交后如果**长时间（默认 120 秒）没有任何输出**，系统会中断内核并返回以 `⚠️ execute_code 内核无响应` 开头的诊断。遇到时：

- **不要盲目重试同一段代码**（很可能再次触发同样的阻塞）。
- 先确认所需产物是否已落盘：能读文件就拿文件（`*_counts.csv` / `-trnacounts.txt` / `de_results.csv` / `*_manifest.json`），不要为了取数据重跑计算。
- 向用户说明"内核疑似被前一个任务占用或卡死"，并询问：等待重试、还是基于已有产物继续后续步骤。
- 若是合并/汇总类步骤，直接读取已生成的中间文件完成合并，跳过阻塞的计算调用。
