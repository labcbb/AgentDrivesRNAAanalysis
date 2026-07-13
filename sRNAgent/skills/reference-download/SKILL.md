---
name: reference-download
title: Download reference data (Ensembl + miRBase)
description: "Download reference genomes, GTF annotations, and miRNA data from Ensembl and miRBase with multi-threaded resumable download."
---

# Download Reference Data

## Overview

This skill covers downloading genome reference data from **Ensembl** and miRNA reference data from **miRBase**. All downloads use multi-threaded resumable download — interrupted transfers can be resumed, and large files download faster with parallel threads.

**Ensembl** tools (`sa.reference.*`):

| Step | Function | Purpose |
|------|----------|---------|
| 1 | `sa.reference.list_species` | List available species in the current Ensembl release |
| 2 | `sa.reference.download_genome` | Download primary assembly FASTA + auto-generate ``.dict`` |
| 3 | `sa.reference.download_gtf` | Download GTF gene annotation file |
| 4 | `sa.reference.download_ncrna` | Download non-coding RNA FASTA (miRNA, piRNA, etc.) |

**miRBase** tools (`sa.reference.*`):

| Step | Function | Purpose |
|------|----------|---------|
| 1 | `sa.reference.list_mirbase_codes` | List all species 3-letter codes in miRBase |
| 2 | `sa.reference.download_mirbase` | Download all-species hairpin/mature FASTA + GFF3; extract per-species sequences |

Ensembl file naming conventions:

| File | Pattern | Example |
|------|---------|---------|
| Primary assembly FASTA | ``{Genus}_{species}.{Assembly}.dna.primary_assembly.fa.gz`` | ``Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz`` |
| GTF annotation | ``{Genus}_{species}.{Assembly}.{Version}.gtf.gz`` | ``Homo_sapiens.GRCh38.116.gtf.gz`` |
| ncRNA FASTA | ``{Genus}_{species}.{Assembly}.ncrna.fa.gz`` | ``Homo_sapiens.GRCh38.ncrna.fa.gz`` |

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

### 2. 下载人类参考基因组 (GRCh38)

下载 primary assembly FASTA（约 841 MB），完成后自动生成 `.dict` 序列字典文件：

```python
# 4 线程并行下载
result = sa.reference.download_genome(
    "homo_sapiens",
    output_dir="ref",
    jobs=4,
)

print(f"Genome FASTA: {result['fasta']}")
print(f"Dict file:    {result['dict']}")
# Genome FASTA: /path/to/ref/Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz
# Dict file:    /path/to/ref/Homo_sapiens.GRCh38.dna.primary_assembly.dict
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

**CORRECT — 下载后构建 Bowtie 索引（用于 read 比对和 miRDeep2）：**

```python
import sRNAgent as sa

# 先下载基因组 FASTA
result = sa.reference.download_genome("homo_sapiens", output_dir="ref", jobs=8)

# 再构建 Bowtie 索引（需要基因组 FASTA 的解压版本）
import gzip
from pathlib import Path

fa_gz = Path(result["fasta"])
fa_unzipped = fa_gz.with_name(fa_gz.name.replace(".gz", ""))
if not fa_unzipped.exists():
    print("Decompressing genome FASTA for bowtie-build...")
    with gzip.open(fa_gz, "rb") as f_in, open(fa_unzipped, "wb") as f_out:
        import shutil
        shutil.copyfileobj(f_in, f_out)

sa.alignment.bowtie_build(
    str(fa_unzipped),
    "ref/grch38",
    threads=8,
)
```

> Bowtie 索引是 **`sa.alignment.bowtie`** 比对和 **miRDeep2 mapper.pl** 的必需输入。`bowtie_build` 需要未压缩的 FASTA 文件。如果已解压过则跳过解压步骤，直接指向 `.fa` 文件即可。具体用法见 `alignment-srna` skill。

**WRONG — 物种名拼写错误:**

```python
# WRONG! Ensembl 使用小写+下划线格式
# sa.reference.download_genome("Homo sapiens", ...)  # 错误
# sa.reference.download_genome("Homo_sapiens", ...)  # 错误

# CORRECT
sa.reference.download_genome("homo_sapiens", ...)
```

### 3. 下载 GTF 注释文件

自动发现当前 Ensembl 版本号（如 116）：

```python
result = sa.reference.download_gtf(
    "homo_sapiens",
    output_dir="ref",
    jobs=4,
)

print(f"GTF: {result['gtf']}")
# GTF: /path/to/ref/Homo_sapiens.GRCh38.116.gtf.gz
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

### 返回值格式

```python
# download_genome
{"fasta": "ref/Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz",
 "dict":  "ref/Homo_sapiens.GRCh38.dna.primary_assembly.dict"}

# download_gtf
{"gtf": "ref/Homo_sapiens.GRCh38.116.gtf.gz"}

# download_ncrna
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
