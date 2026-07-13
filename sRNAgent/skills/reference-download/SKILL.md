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
| 3 | `sa.reference.download_gtf` | Download GTF gene annotation file | **GENCODE** (human/mouse), Ensembl (others) |
| 4 | `sa.reference.download_ncrna` | Download non-coding RNA FASTA | Ensembl (all species) |

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

### 3. 下载 GTF 注释文件（人类来自 GENCODE）

人类基因组注释从 **GENCODE** 下载，命名为 `gencode.v{version}.primary_assembly.annotation.gtf.gz`（版本号自动发现）：

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

# ── 下载 GTF 注释 ──
gtf = sa.reference.download_gtf("homo_sapiens", output_dir="ref", jobs=4)

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
