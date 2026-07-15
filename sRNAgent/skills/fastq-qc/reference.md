## Environment setup

```bash
# All tools should already be available in the conda environment
# Verify:
cutadapt --version
fastqc --version
multiqc --version
```

## Minimal: trim a single sRNA-seq sample + QC

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# Initialise AnnData with one sample
adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata.obs["fastq_path"] = ["srna_fastq/SRR26304152.fastq.gz"]

# ── Trim 3' adapter with TruSeq Small RNA adapter ──
# ⚠️  必须确认 adapter 序列与建库试剂盒匹配！不同试剂盒 adapter 不同
#     常见: TruSeq=TGGAATTCTCGGGTGCCAAGG, NEBNext=AGATCGGAAGAGCACACGTCTGAAC
adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    min_length=18,
    max_length=36,
    quality_cutoff="20",
    output_dir="trimmed",
)
print(f"Trimmed FASTQ: {adata.obs['trimmed_path'].iloc[0]}")

# 查看自动提取的 cutadapt 质控指标
print(adata.obs[[
    "cutadapt_in_reads", "cutadapt_out_reads",
    "cutadapt_trim_rate", "cutadapt_w_adapters",
]].to_string())

# ── FastQC ──
adata = sa.fastq.fastqc(adata, output_dir="fastqc_reports")

# ── MultiQC ──
adata = sa.fastq.multiqc(adata, output_dir="multiqc_out", force=True)

# multiqc 自动提取质控指标到 adata.obs
print(adata.obs.filter(like="multiqc_").to_string())
print(f"MultiQC report: {adata.uns['multiqc_html']}")
print(f"MultiQC data dir: {adata.uns['multiqc_data_dir']}")
print(f"MultiQC output dir: {adata.uns['multiqc_dir']}")
```

## Batch trim 10 sRNA-seq samples (SRP464891)

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# Initialise AnnData with 10 samples
adata = ad.AnnData(obs=pd.DataFrame(index=[
    "S1", "S2", "S3", "S4", "S5",
    "S6", "S7", "S8", "S9", "S10",
]))
adata.obs["fastq_path"] = [
    "srna_fastq/SRR26304152.fastq.gz",
    "srna_fastq/SRR26304153.fastq.gz",
    "srna_fastq/SRR26304154.fastq.gz",
    "srna_fastq/SRR26304155.fastq.gz",
    "srna_fastq/SRR26304156.fastq.gz",
    "srna_fastq/SRR26304157.fastq.gz",
    "srna_fastq/SRR26304158.fastq.gz",
    "srna_fastq/SRR26304159.fastq.gz",
    "srna_fastq/SRR26304160.fastq.gz",
    "srna_fastq/SRR26304161.fastq.gz",
]

adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    min_length=18,
    max_length=36,
    quality_cutoff="20",
    output_dir="trimmed",
    jobs=4,
)
```

## Complete pipeline: download -> trim -> FastQC -> MultiQC

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# Initialise AnnData for all samples
samples = ["S1", "S2", "S3", "S4", "S5",
           "S6", "S7", "S8", "S9", "S10"]
adata = ad.AnnData(obs=pd.DataFrame(index=samples))

# ── Step 0: Download data (skip if already have FASTQ) ──
adata = sa.fastq.fastq_dl(
    adata,
    accessions=[
        "SRR26304152", "SRR26304153", "SRR26304154", "SRR26304155",
        "SRR26304156", "SRR26304157", "SRR26304158", "SRR26304159",
        "SRR26304160", "SRR26304161",
    ],
    output_dir="srna_fastq",
    provider="ena",
)

# ── Step 1: Trim 3' adapter ──
adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    min_length=18,
    max_length=36,
    quality_cutoff="20",
    output_dir="trimmed",
    jobs=4,                     # 4个样本并行
)

# ── Step 2: FastQC ──
adata = sa.fastq.fastqc(adata, output_dir="fastqc_reports",
                         threads=2, jobs=4)
print(f"FastQC reports: {adata.obs['fastqc_html'].notna().sum()} samples")

# ── Step 3: MultiQC ──
adata = sa.fastq.multiqc(
    adata,
    output_dir="multiqc_out",
    force=True,
)
print(f"Open report: {adata.uns['multiqc_html']}")
print(f"Data dir: {adata.uns['multiqc_data_dir']}")
print(f"Output dir: {adata.uns['multiqc_dir']}")
```

## Trim with different length ranges

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# miRNA only (18-25 nt)
adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata = sa.fastq.cutadapt(adata,
                          adapter_3="TGGAATTCTCGGGTGCCAAGG",
                          min_length=18, max_length=25,
                          output_dir="trimmed_mirna")

# miRNA + siRNA (18-30 nt)
adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata = sa.fastq.cutadapt(adata,
                          adapter_3="TGGAATTCTCGGGTGCCAAGG",
                          min_length=18, max_length=30,
                          output_dir="trimmed_srna")

# piRNA (24-32 nt)
adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata = sa.fastq.cutadapt(adata,
                          adapter_3="TGGAATTCTCGGGTGCCAAGG",
                          min_length=24, max_length=32,
                          output_dir="trimmed_pirna")
```

## Trim with poly-A and N trimming

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata = sa.fastq.cutadapt(
    adata,
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    min_length=18, max_length=36,
    quality_cutoff="20",
    trim_n=True,       # trim flanking N bases
    poly_a=True,       # trim poly-A tails
    output_dir="trimmed_extra",
)
```

## FastQC on trimmed output with contaminants file

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata = sa.fastq.cutadapt(adata,
                          adapter_3="TGGAATTCTCGGGTGCCAAGG",
                          output_dir="trimmed")

adata = sa.fastq.fastqc(
    adata,
    output_dir="fastqc_reports",
    contaminants="adapters.txt",   # tab-separated: name\tsequence
    threads=2,
)
```

## MultiQC with specific modules only

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
# Assumes cutadapt + fastqc have been run on adata first
adata = sa.fastq.multiqc(
    adata,
    output_dir="multiqc_out",
    modules=["fastqc", "cutadapt"],
    data_format="json",
    export_plots=True,
    force=True,
)
```

## Key function signatures

```python
sa.fastq.cutadapt(
    adata,                         # AnnData with sample names in obs.index
    adapter_3=None,                # 3' adapter sequence(s) — crucial for sRNA-seq
    adapter_5=None,                # 5' adapter sequence(s)
    adapter_any=None,              # adapter at either end
    adapter_file_3=None,           # FASTA file with 3' adapters
    adapter_file_5=None,           # FASTA file with 5' adapters
    adapter_file_any=None,         # FASTA file with anywhere adapters
    adapter_3_r2=None,             # R2 3' adapter (paired-end only)
    adapter_5_r2=None,             # R2 5' adapter (paired-end only)
    adapter_any_r2=None,           # R2 anywhere adapter (paired-end only)
    error_rate=None,               # max error rate (default 0.1)
    min_overlap=3,                 # min adapter overlap
    no_indels=False,               # disallow indels in matching
    times=1,                       # max trimming rounds per read
    quality_cutoff=None,           # quality trimming (-q), e.g. "20" or "15,10"
    nextseq_trim=None,             # NextSeq/NovaSeq quality trim
    cut=None,                      # fixed base removal (-u)
    cut_r2=None,                   # fixed base removal for R2 (-U)
    min_length=None,               # discard shorter than this (-m)
    max_length=None,               # discard longer than this (-M)
    max_n=None,                    # discard reads with >N N bases
    trim_n=False,                  # trim flanking Ns
    poly_a=False,                  # trim poly-A tails
    action=None,                   # trim/retain/mask/crop/lowercase/none
    revcomp=False,                 # also search reverse complement
    json_report=True,              # write JSON report
    report=None,                   # full or minimal
    info_file=None,                # per-read info TSV
    quiet=False,                   # suppress output
    gc_content=None,               # expected GC% for better estimates
    extra_args=None,               # extra cutadapt arguments
    output_dir="trimmed",
    jobs=None,                     # samples to process concurrently
    overwrite=False,               # force re-run
)

sa.fastq.fastqc(
    adata,                         # AnnData with trimmed FASTQ paths in obs
    output_dir="fastqc_out",
    format=None,                   # force format: fastq, bam, sam
    threads=2,                     # FastQC internal threads per process
    contaminants=None,             # contaminants file
    adapters=None,                 # adapters file
    limits=None,                   # custom warn/error limits
    kmers=7,                       # kmer length (2-10)
    casava=False,                  # Casava input mode
    nano=False,                    # Nanopore input mode
    nofilter=False,                # keep filtered reads (Casava)
    extract=True,                  # unzip output zip
    nogroup=False,                 # disable base grouping
    quiet=False,                   # suppress progress
    java_path=None,                # custom Java path
    temp_dir=None,                 # temp directory
    jobs=None,                     # files to process concurrently
    overwrite=False,               # force re-run
)

sa.fastq.multiqc(
    adata,                         # AnnData (reads obs for report dirs)
    output_dir=".",
    filename=None,                 # custom report filename
    force=False,                   # overwrite existing
    modules=None,                  # only run these modules
    exclude=None,                  # exclude these modules
    data_format=None,              # tsv, json, or yaml
    data_dir=None,                 # force/suppress data dir
    export_plots=False,            # export PNG plots
    template=None,                 # custom template
    dirs=False,                    # prepend dir to sample names
    dirs_depth=None,               # dir levels to prepend
    ignore=None,                   # ignore glob pattern(s)
    file_list=None,                # file with list of paths
    pdf=False,                     # generate PDF
    verbose=False,                 # verbose output
    quiet=False,                   # suppress output
    cl_config=None,                # YAML config string
    extra_args=None,               # extra multiqc arguments
)

sa.fastq.fastq_dl(
    adata,                         # AnnData with sample names in obs.index
    accessions,                    # str or list of accessions
    output_dir="fastq",
    provider="ena",                # "ena" or "sra"
    # ... (other params)
    jobs=None,                     # accessions to process concurrently
)
```
