# isoMiR (isomiR) Quantification Quick Reference

## End-to-end: QC → hairpin index → hairpin alignment → mirtop

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# 0. 独立 isomiR 模态（禁止与 srna 合并）
adata_iso = ad.AnnData(obs=pd.DataFrame(index=["SRR26304152", "SRR26304153"]))

# 1. 参考文件：hairpin.fa + 前体 GFF3 + 物种代码（见 reference-download skill）
codes = sa.reference.list_mirbase_codes()            # 查物种三位代码
result = sa.reference.download_mirbase(species="hsa", output_dir="ref", jobs=4)
# result["hairpin"] → ref/hairpin_hsa.fa ; result["gff3"] → ref/hsa.gff3

# 2. 质控：接头去尽（adapter 序列先按 fastq-qc skill 确认）
adata_iso = sa.fastq.cutadapt(
    adata_iso,
    output_dir="trimmed_iso",
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    min_length=15,
    max_length=35,
    jobs=4,
)

# 3. 用 hairpin.fa 建索引（不是全基因组！）
sa.alignment.bowtie_build("ref/hairpin_hsa.fa", "ref/hairpin_hsa", threads=4)

# 4. 比对到 hairpin 前体（等价 bowtie -n 1 -l 15 -m 100 --best --strata）
adata_iso = sa.alignment.bowtie(
    adata_iso,
    index_basename="ref/hairpin_hsa",
    seed_mismatches=1,   # -n 1
    seed_len=15,         # -l 15
    m=100,               # -m 100
    best=True,           # --best
    strata=True,         # --strata
    threads=8,
    jobs=4,
    output_dir="aligned_hairpin",
)

# 5. isomiR 定量
adata_iso = sa.quant.mirtop(
    adata_iso,
    gff="ref/hsa.gff3",
    hairpin="ref/hairpin_hsa.fa",
    species="hsa",
    granularity="variant",   # 默认不聚合：每个 isomiR 一个特征（要成熟体汇总才用 "miRNA"）
    output_dir="mirtop_out",
)

# 6. 查看 + 保存（独立 h5ad）
print(adata_iso.uns["mirtop_result"]["stats_log"])   # 变异类型分布
adata_iso.write("isomir_counts.h5ad")
```

## Only BAMs exist — skip QC/alignment

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata_iso = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2"]))
adata_iso.obs["bam_path"] = ["aligned/S1.hairpin.bam", "aligned/S2.hairpin.bam"]

adata_iso = sa.quant.mirtop(
    adata_iso,
    gff="ref/hsa.gff3",
    hairpin="ref/hairpin_hsa.fa",
    species="hsa",
    granularity="variant",
    output_dir="mirtop_out",
)
```

## Variant-level counts (each isomiR UID is a feature)

```python
adata_iso = sa.quant.mirtop(
    adata_iso,
    gff="ref/hsa.gff3",
    hairpin="ref/hairpin_hsa.fa",
    species="hsa",
    granularity="variant",
    output_dir="mirtop_out",
)
print(adata_iso.var_names[:5])   # e.g. hsa-let-7a-5p|0,0,0,0,0,0
```

## Key function signatures

```python
sa.quant.mirtop(
    adata,                 # 独立 isomiR 模态；读取 adata.obs["bam_path"]
    gff,                   # 前体 GFF3（--gtf），如 ref/hsa.gff3
    hairpin,               # hairpin 前体 FASTA，如 ref/hairpin_hsa.fa
    output_dir="mirtop_out",
    bam_col="bam_path",
    species="hsa",         # --sps 物种三位代码
    granularity="variant",   # 默认不聚合：每个 isomiR 一个特征（要成熟体汇总才用 "miRNA"）
    normalize=True,        # 写 layers["logcpm"]
    create_index=True,     # 缺 .bai 时自动建
    rna_type="isoMiR",
    extra_args=None,       # 追加给 mirtop gff 的参数
    overwrite=False,       # 已覆盖全部样本时跳过重跑
)

sa.alignment.bowtie(
    adata,                 # 读取 adata.obs["trimmed_path"]（或 fastq_path）
    index_basename,        # hairpin 索引（bowtie_build 产物）
    seed_mismatches=1, seed_len=15, m=100, best=True, strata=True,
    threads=8, jobs=4,
)

sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG", min_length=15, max_length=35, jobs=4,
)

sa.reference.download_mirbase(species="hsa", output_dir="ref", jobs=4)
# → ref/hairpin_hsa.fa + ref/hsa.gff3；species 代码用 sa.reference.list_mirbase_codes() 查
```

## CLI equivalents

```bash
# 建索引（hairpin 前体）
bowtie-build ref/hairpin_hsa.fa ref/hairpin_hsa

# 比对（允许 1 个 seed 错配，最多 100 个 hit，最优 + 分层报告）
bowtie -n 1 -l 15 -m 100 --best --strata -S ref/hairpin_hsa clean_reads.fq | samtools sort -o sorted.bam

# mirtop 三阶段
mirtop gff --sps hsa --gtf ref/hsa.gff3 --hairpin ref/hairpin_hsa.fa --out mirtop_out/ bam1.bam bam2.bam
mirtop counts --gff mirtop_out/mirtop.gff --out mirtop_out/     # → mirtop_out/mirtop.tsv
mirtop stats -o mirtop_out/ mirtop_out/mirtop.gff               # → mirtop_stats.txt/.log
```

## Outputs written to adata_iso

```python
adata_iso.X / adata_iso.layers["counts"]   # counts 矩阵（samples × isomiRs）
adata_iso.layers["logcpm"]                 # log2(CPM+1)（normalize=True）
adata_iso.var["mirna_id"]                  # isomiR 特征 ID（UID 或 miRNA 名）
adata_iso.var["rna_type"]                  # "isoMiR"
adata_iso.var["variant_type"]              # granularity
adata_iso.obs["mirtop_gff"]                # 每样本 GFF
adata_iso.obs["mirtop_dir"]                # 输出目录
adata_iso.uns["mirtop_result"]             # 输出路径 + stats_log（变异类型分布）
```
