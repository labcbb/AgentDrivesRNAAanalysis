---
name: reference-download
title: Download reference data (GENCODE + Ensembl + miRBase)
description: "Download reference genomes, GTF annotations, and miRNA data from GENCODE (human/mouse), Ensembl (other species), and miRBase with multi-threaded resumable download."
---

# Download Reference Data

## Overview

This skill covers downloading genome reference data — **GENCODE** for human/mouse, **Ensembl** for other species — and miRNA reference data from **miRBase**. All downloads use multi-threaded resumable download.

**Reference genome tools** (`sa.reference.*`):

| Step | Function | Purpose | Source |
|------|----------|---------|--------|
| 1 | `sa.reference.list_species` | List available species in current Ensembl release | Ensembl |
| 2 | `sa.reference.download_genome` | Download primary assembly FASTA + auto-generate ``.dict`` | **GENCODE** (human/mouse), Ensembl (others) |
| 3 | `sa.reference.download_ncrna` | (按需) Download non-coding RNA FASTA | Ensembl (all species) |
| — | `sa.reference.download_gtf` | (按需) Download GTF gene annotation | **GENCODE** (human/mouse), Ensembl (others) |

**miRBase** tools (`sa.reference.*`):

| Step | Function | Purpose |
|------|----------|---------|
| 1 | `sa.reference.list_mirbase_codes` | List all species 3-letter codes in miRBase |
| 2 | `sa.reference.download_mirbase` | Download all-species hairpin/mature FASTA + GFF3; extract per-species sequences |

File naming conventions:

| Source | File | Example |
|--------|------|---------|
| GENCODE (human) | Primary assembly genome FASTA | ``GRCh38.primary_assembly.genome.fa.gz`` |
| GENCODE (human) | Primary assembly annotation GTF | ``gencode.v50.primary_assembly.annotation.gtf.gz`` |
| GENCODE (mouse) | Primary assembly genome FASTA | ``GRCm39.primary_assembly.genome.fa.gz`` |
| GENCODE (mouse) | Primary assembly annotation GTF | ``gencode.vM39.primary_assembly.annotation.gtf.gz`` |
| Ensembl | Primary assembly FASTA | ``Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz`` |
| Ensembl | GTF annotation | ``Homo_sapiens.GRCh38.116.gtf.gz`` |
| Ensembl | ncRNA FASTA | ``Homo_sapiens.GRCh38.ncrna.fa.gz`` |

> ℹ️ **hsa/mmu ↔ species name 映射：** 在 sRNA-seq 分析中，hsa（人）和 mmu（鼠）是 miRBase 的物种代码。调用 `download_genome` / `download_gtf` 时，使用 Ensembl 物种名 `homo_sapiens` 或 `mus_musculus`，**API 自动走 GENCODE 下载**。无需手动切换来源。

## Instructions

### 1. 查看当前 Ensembl 版本可用的物种

```python
import sRNAgent as sa

# 列出所有物种（返回 species 目录名列表）
species_list = sa.reference.list_species()

# 查看前 5 个
print(species_list[:5])
# ['ailuropoda_melanoleuca', 'bos_taurus', 'caenorhabditis_elegans', ...]

# 搜索特定物种
human = [s for s in species_list if "homo" in s]
mouse = [s for s in species_list if "mus_musculus" in s]
zebrafish = [s for s in species_list if "danio_rerio" in s]

print(f"Human: {human}")
print(f"Mouse: {mouse}")
```

### 2. 下载人类参考基因组 (GENCODE GRCh38)

人类基因组从 **GENCODE** 下载，文件名格为 `GRCh38.primary_assembly.genome.fa.gz`（约 841 MB），完成后自动生成 `.dict` 序列字典文件：

```python
# 8 线程并行下载
result = sa.reference.download_genome(
    "homo_sapiens",
    output_dir="ref",
    jobs=8,
)

print(f"Genome FASTA: {result['fasta']}")
print(f"Dict file:    {result['dict']}")
# Genome FASTA: /path/to/ref/GRCh38.primary_assembly.genome.fa.gz
# Dict file:    /path/to/ref/GRCh38.primary_assembly.genome.dict
```

**CORRECT — 不生成 .dict 文件（如果不需要）：**

```python
result = sa.reference.download_genome(
    "homo_sapiens", output_dir="ref",
    generate_dict=False,
)
```

**CORRECT — 强制重新下载：**

```python
result = sa.reference.download_genome(
    "homo_sapiens", output_dir="ref",
    force=True,
)
```

**CORRECT — 下载后直接构建 Bowtie 索引（`download_genome` 已自动解压并清理序列名）：**

```python
import sRNAgent as sa

# 下载基因组（自动解压 + 清理 header + 生成 .dict）
result = sa.reference.download_genome("homo_sapiens", output_dir="ref", jobs=8)

# 直接使用返回的 FASTA 路径（已解压，可读）
sa.alignment.bowtie_build(
    result["fasta"],  # 已解压，header 已清理
    "ref/grch38",
    threads=8,
)
```

> Bowtie 索引所需的参考序列来自 **GENCODE**（`GRCh38.primary_assembly.genome.fa.gz`），`download_genome` 已自动解压并清理 header（取第一个空格前的内容），返回的 `result["fasta"]` 可直接用于 `bowtie_build`。

**WRONG — 物种名拼写错误:**

```python
# WRONG! Ensembl 使用小写+下划线格式
# sa.reference.download_genome("Homo sapiens", ...)  # 错误
# sa.reference.download_genome("Homo_sapiens", ...)  # 错误

# CORRECT
sa.reference.download_genome("homo_sapiens", ...)
```

### 3. 按需下载 GTF 注释文件

仅当需要基因注释信息时才下载。人类基因组注释从 **GENCODE** 下载，命名为 `gencode.v{version}.primary_assembly.annotation.gtf.gz`（版本号自动发现）：

```python
result = sa.reference.download_gtf(
    "homo_sapiens",
    output_dir="ref",
    jobs=4,
)

print(f"GTF: {result['gtf']}")
# GTF: /path/to/ref/gencode.v50.primary_assembly.annotation.gtf.gz
```

**CORRECT — 小鼠 GTF 也来自 GENCODE：**

```python
result = sa.reference.download_gtf("mus_musculus", output_dir="ref", jobs=4)
print(f"GTF: {result['gtf']}")
# GTF: /path/to/ref/gencode.vM39.primary_assembly.annotation.gtf.gz
```

**CORRECT — 其他物种 GTF 从 Ensembl 下载：**

```python
result = sa.reference.download_gtf("danio_rerio", output_dir="ref", jobs=4)
print(f"GTF: {result['gtf']}")
# GTF: /path/to/ref/Danio_rerio.GRCz11.116.gtf.gz
```

### 4. 下载非编码 RNA 序列

用于 miRNA/piRNA/snoRNA 等分析：

```python
result = sa.reference.download_ncrna(
    "homo_sapiens",
    output_dir="ref",
    jobs=4,
)

print(f"ncRNA FASTA: {result['ncrna']}")
# ncRNA FASTA: /path/to/ref/Homo_sapiens.GRCh38.ncrna.fa.gz
```

### 5. 一次性下载参考数据（人类基因组常用）

```python
import sRNAgent as sa

result = sa.reference.download_genome("homo_sapiens", output_dir="ref", jobs=8)
print(f"Genome: {result['fasta']}")

result = sa.reference.download_gtf("homo_sapiens", output_dir="ref", jobs=4)
print(f"GTF:    {result['gtf']}")

result = sa.reference.download_ncrna("homo_sapiens", output_dir="ref", jobs=4)
print(f"ncRNA:  {result['ncrna']}")
```

---

## miRBase miRNA 参考数据

miRBase 提供所有物种的 miRNA 序列和注释，用于 sRNA-seq 中 miRNA 鉴定和定量。

### 6. 查看 miRBase 中有哪些物种

前提是已经下载了 miRBase 的 FASTA 文件（或先下载再扫描）：

```python
import sRNAgent as sa

# 如果已经下载了 mature.fa.gz
codes = sa.reference.list_mirbase_codes(fasta_path="ref/mature.fa.gz")
print(f"miRBase species: {len(codes)}")
print(codes[:20])
# ['aga', 'aga', 'aae', 'aal', 'aan', 'aau', 'aca', ...]
```

### 7. 下载人类 miRNA 数据

下载所有物种的 hairpin.fa 和 mature.fa，自动提取 **hsa**（人）的序列，并下载 hsa 的 GFF3 注释：

```python
result = sa.reference.download_mirbase(
    species="hsa",
    output_dir="ref",
    jobs=4,
)

print(f"All hairpin:   {result.get('hairpin_all')}")
print(f"All mature:    {result.get('mature_all')}")
print(f"Hairpin (hsa): {result['hairpin']}")    # ref/hairpin_hsa.fa
print(f"Mature (hsa):  {result['mature']}")     # ref/mature_hsa.fa
print(f"GFF3 (hsa):    {result.get('gff3')}")   # ref/hsa.gff3
```

**CORRECT — 仅提取已有 miRBase 数据（不重新下载）：**

```python
result = sa.reference.download_mirbase(
    species="mmu",              # 小鼠
    output_dir="ref",
    extract_only=True,          # 只提取，不下
)
```

**CORRECT — 优先策略：一次性下载全物种文件，后续只提取（推荐）：**

```python
# 第一次：下载 hairpin.fa.gz + mature.fa.gz（全物种），同时提取 human
result = sa.reference.download_mirbase(
    species="hsa", output_dir="ref", jobs=4,
)
# 下载后的文件:
#   ref/hairpin.fa.gz   ← 全物种，保留不删
#   ref/mature.fa.gz    ← 全物种，保留不删
#   ref/hairpin_hsa.fa  ← 提取的人 hairpin
#   ref/mature_hsa.fa   ← 提取的人 mature
#   ref/hsa.gff3        ← 人 GFF3

# 后续提取小鼠：直接利用已有全物种文件，无需重新下载
result = sa.reference.download_mirbase(
    species="mmu", output_dir="ref",
    extract_only=True,          # 直接从已有 hairpin.fa.gz / mature.fa.gz 提取
)
```

**CORRECT — 只下载 FASTA 不下载 GFF3：**

```python
result = sa.reference.download_mirbase(
    species="hsa",
    output_dir="ref",
    download_gff3=False,
)
```

**CORRECT — 下载小鼠 miRNA 数据：**

```python
result = sa.reference.download_mirbase(
    species="mmu",
    output_dir="ref",
    jobs=4,
)
```

**WRONG — 物种代码格式错误:**

```python
# WRONG! 物种代码必须是3位小写字母
# sa.reference.download_mirbase(species="human", ...)   # 错误
# sa.reference.download_mirbase(species="HSA", ...)     # 错误

# CORRECT
sa.reference.download_mirbase(species="hsa", ...)       # 正确
```

miRBase 常用物种代码：

| 物种 | 代码 | 说明 |
|------|------|------|
| Human | ``hsa`` | 人 |
| Mouse | ``mmu`` | 小鼠 |
| Rat | ``rno`` | 大鼠 |
| Zebrafish | ``dre`` | 斑马鱼 |
| Fruit fly | ``dme`` | 果蝇 |
| C. elegans | ``cel`` | 线虫 |
| Arabidopsis | ``ath`` | 拟南芥 |
| Rice | ``osa`` | 水稻 |

### 8. 利用 miRBase 提取结果进行 miRNA 分析

下载并提取后的文件可以直接用于下游分析：

```bash
# 查看提取的 human miRNA 数量
grep -c "^>" ref/hairpin_hsa.fa    # hairpin 数
grep -c "^>" ref/mature_hsa.fa     # mature miRNA 数

# 查看文件头部
zcat ref/hairpin.fa.gz | head -2
# >hsa-let-7a-1 MI0000060 Homo sapiens let-7a-1 stem-loop

zcat ref/mature.fa.gz | head -2
# >hsa-let-7a-5p MIMAT0000062 Homo sapiens let-7a-5p
```

提取后的 per-species FASTA 可以和 Bowtie 索引一起用于 miRNA 定量：

```python
import sRNAgent as sa

# 比对 trimmed sRNA-seq 到 miRNA 参考序列
result = sa.alignment.bowtie(
    "S1",
    fq1="trimmed/S1_trimmed.fastq.gz",
    index_basename="ref/mature_hsa",  # 需要先 bowtie_build
    total_mismatches=0,
    m=1,
    sam_out=True,
)
```

---

## piRBase piRNA 参考数据

piRBase 提供 43 个物种的 piRNA FASTA 序列，适用于 piRNA 定量分析。

### 9. 查看 piRBase 可用的物种

```python
import sRNAgent as sa

species = sa.reference.list_pirna_species()
print(f"piRBase species: {len(species)}")
for code, name in list(species.items())[:10]:
    print(f"  {code}: {name}")
```

常用物种：

| 物种 | 代码 | 说明 |
|------|------|------|
| Human | ``hsa`` | 人 |
| Mouse | ``mmu`` | 小鼠 |
| Rat | ``rno`` | 大鼠 |
| Zebrafish | ``dre`` | 斑马鱼 |
| Fruit fly | ``dme`` | 果蝇 |
| C. elegans | ``cel`` | 线虫 |
| Cow | ``bta`` | 牛 |
| Pig | ``ssc`` | 猪 |
| Chicken | ``gga`` | 鸡 |

### 10. 下载 piRNA FASTA

默认下载完整 piRNA fasta 文件。**除非用户明确要求 gold standard，否则不要使用 `gold=True`。**

**CORRECT — 默认下载完整 piRNA 集（所有 43 个物种都支持）：**

```python
# 下载人类 piRNA 完整集（默认行为）
result = sa.reference.download_pirna("hsa", output_dir="ref", jobs=4)
print(f"piRNA FASTA: {result['fasta']}")
# piRNA FASTA: ref/hsa.piRNA.fa.gz
```

**仅当用户指定时 — 下载 gold standard piRNA 集（仅部分物种）：**

```python
# Gold standard 仅适用于: hsa, mmu, dme, bta, rno, mfa
result = sa.reference.download_pirna("hsa", output_dir="ref", gold=True)
print(f"Gold piRNA: {result['gold_fasta']}")
# Gold piRNA: ref/hsa.gold.fa.gz
```

**WRONG — 无效的物种代码：**

```python
# WRONG! 3 位小写字母代码
# sa.reference.download_pirna("human", ...)  # 错误

# CORRECT
sa.reference.download_pirna("hsa", ...)  # 正确
```

---

## tRNA 参考数据

tRNA 相关工具提供 tRNAscan-SE 预计算结果和 tRAX 所需的 GTF 注释。

### 11. 下载 tRNAscan-SE 结果（hg38）

从 GtRNAdb 下载人类 (hg38) 的 tRNAscan-SE 预计算结果，包括 tRNA 序列、BED 注释和详细预测报告：

```python
result = sa.reference.download_trnascan_hg38(output_dir="ref")
print(f"tRNA FASTA:    {result['trna_fasta']}")
print(f"tRNA BED:      {result['trna_bed']}")
print(f"Detailed out:  {result['trna_detailed']}")
```

返回的文件列表：
- ``hg38-tRNAs.fa`` — 完整 tRNA 序列
- ``hg38-filtered-tRNAs.fa`` — 过滤后的 tRNA 序列
- ``hg38-mature-tRNAs.fa`` — 成熟 tRNA 序列
- ``hg38-tRNAs.bed`` — tRNA BED 注释
- ``hg38-tRNAs-confidence-set.out`` — 高置信度 tRNA 预测
- ``hg38-tRNAs-detailed.out`` — 详细 tRNA 预测报告
- ``hg38-tRNAs_name_map.txt`` — tRNA 名称映射表

### 12. 构建 tRAX 小 RNA GTF 注释

从 GENCODE/Ensembl GTF 中提取 tRAX 定量所需的小 RNA 特征（miRNA、rRNA、snRNA、snoRNA 等）。需先下载 GTF：

```python
# 先下载 GTF
sa.reference.download_gtf("homo_sapiens", output_dir="ref", jobs=4)

# 构建 tRAX 专用 GTF
result = sa.reference.build_trax_human_gtf(output_dir="ref")
print(f"tRAX GTF: {result['trax_gtf']}")
# tRAX GTF: ref/trax_human.gtf
```

**CORRECT — 从已有的 GTF 文件直接构建（不依赖 download_gtf）：**

```python
result = sa.reference.build_trax_human_gtf(
    output_dir="ref",
    gtf_path="ref/gencode.v50.primary_assembly.annotation.gtf.gz",
)
```

构建的 GTF 包含以下特征类型：``Mt_rRNA``、``miRNA``、``misc_RNA``、``rRNA``、``snRNA``、``snoRNA``、``ribozyme``、``sRNA``、``scaRNA``。

## 返回值格式补充

```python
# download_pirna
{"fasta":       "ref/hsa.piRNA.fa.gz",       # 完整 piRNA 集
 "gold_fasta":  "ref/hsa.gold.fa.gz"}        # gold standard（仅 gold=True 时）

# download_trnascan_hg38
{"trna_fasta":         "ref/hg38-tRNAs.fa",
 "trna_filtered":      "ref/hg38-filtered-tRNAs.fa",
 "trna_mature":        "ref/hg38-mature-tRNAs.fa",
 "trna_bed":           "ref/hg38-tRNAs.bed",
 "trna_confidence":    "ref/hg38-tRNAs-confidence-set.out",
 "trna_confidence_ss": "ref/hg38-tRNAs-confidence-set.ss",
 "trna_detailed":      "ref/hg38-tRNAs-detailed.out",
 "trna_detailed_ss":   "ref/hg38-tRNAs-detailed.ss",
 "trna_name_map":      "ref/hg38-tRNAs_name_map.txt"}

# build_trax_human_gtf
{"trax_gtf": "ref/trax_human.gtf"}
```

## Critical API Reference

### 完整下载流程（Ensembl）

### 完整下载流程

```python
import sRNAgent as sa

# ── 列出物种 ──
species = sa.reference.list_species()
print(f"Available: {len(species)} species")

# ── 下载参考基因组 ──
genome = sa.reference.download_genome(
    "homo_sapiens",
    output_dir="ref",
    jobs=8,          # 8 线程分片下载
    force=False,     # 文件存在时跳过
    generate_dict=True,
)

# ── 按需下载 GTF 注释（仅当需要基因注释时）──
# gtf = sa.reference.download_gtf("homo_sapiens", output_dir="ref", jobs=4)

# ── 下载 ncRNA 序列 ──
ncrna = sa.reference.download_ncrna("homo_sapiens", output_dir="ref", jobs=4)
```

### 返回值格式（以人类 hsa 为例）

```python
# download_genome (GENCODE)
{"fasta": "ref/GRCh38.primary_assembly.genome.fa.gz",
 "dict":  "ref/GRCh38.primary_assembly.genome.dict"}

# download_genome (其他物种，从 Ensembl)
{"fasta": "ref/Danio_rerio.GRCz11.dna.primary_assembly.fa.gz",
 "dict":  "ref/Danio_rerio.GRCz11.dna.primary_assembly.dict"}

# download_gtf (人类，GENCODE)
{"gtf": "ref/gencode.v50.primary_assembly.annotation.gtf.gz"}

# download_gtf (其他物种，Ensembl)
{"gtf": "ref/Danio_rerio.GRCz11.116.gtf.gz"}

# download_ncrna (Ensembl, 所有物种)
{"ncrna": "ref/Homo_sapiens.GRCh38.ncrna.fa.gz"}

# download_mirbase (with species)
{"hairpin_all": "ref/hairpin.fa.gz",
 "mature_all":  "ref/mature.fa.gz",
 "hairpin":     "ref/hairpin_hsa.fa",
 "mature":      "ref/mature_hsa.fa",
 "gff3":        "ref/hsa.gff3"}

# download_mirbase (without species)
{"hairpin_all": "ref/hairpin.fa.gz",
 "mature_all":  "ref/mature.fa.gz"}
```

## Troubleshooting

- **下载速度慢**: 增大 `jobs` 参数（如 `jobs=8`），分片越多下载越快。
- **下载中断后重新开始**: 不会重头下载。`resumable_download` 自动检测已下载的分片，只续传未完成的部分。
- **"No primary assembly FASTA found"**: 某些物种可能没有 primary_assembly 版本。工具会自动降级到 toplevel 或普通 dna FASTA。
- **文件校验不一致**: 如果下载完成后 `resumable_download` 报大小不匹配，用 `force=True` 强制重新下载。
- **"No ncRNA FASTA found"**: 并非所有 Ensembl 物种都有 ncRNA 文件。如果不需要，忽略即可。
- **samtools dict 失败**: 确认 `samtools` 已安装且在 PATH 中（``samtools --version``）。
- **物种名大小写**: 始终使用小写+下划线格式：``homo_sapiens``、``mus_musculus``、``danio_rerio``。
- **miRBase 下载慢**: 增大 ``jobs`` 加速。hairpin.fa (~30 MB) 和 mature.fa (~10 MB) 较小，通常很快。
- **miRBase 提取为 0 条序列**: 确认物种代码正确。如人和 hsa 对应关系：``hsa`` 是 Homo sapiens。用 ``list_mirbase_codes()`` 查看可用代码。
- **miRBase GFF3 不存在**: 并非所有物种都有 GFF3 文件。如果下载失败，用 ``download_gff3=False`` 跳过。
- **hairpin.fa.gz / mature.fa.gz 不是 gzip 格式**: miRBase 可能返回未压缩的文件。``resumable_download`` 会自动处理。如果遇到问题，用 ``gzip -t file`` 检查。

## References

- Copy-paste-ready code templates: [`reference.md`](reference.md)
- Ensembl FTP (current): <https://ftp.ensembl.org/pub/current/>
- Ensembl FTP (all releases): <https://ftp.ensembl.org/pub/>
- miRBase download: <https://www.mirbase.org/download/>
- Download utilities source: ``sRNAgent/Tools/reference/_utils.py``
