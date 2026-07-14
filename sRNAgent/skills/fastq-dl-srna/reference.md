## Environment setup

```bash
# Manual install (recommended for repeated use)
conda install -c conda-forge -c bioconda -y fastq-dl
```

## Minimal: download a single sRNA-seq SRR

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# One sample, one accession
adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata = sa.fastq.fastq_dl(adata, accessions="SRR26304152", output_dir="srna_fastq")

# FASTQ path is in adata.obs
print(f"R1 FASTQ: {adata.obs.loc['S1', 'fastq_path']}")
```

## Download an entire SRA Study (recommended)

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# Single-row AnnData for a project-level accession
adata = ad.AnnData(obs=pd.DataFrame(index=["SRP464891"]))
adata = sa.fastq.fastq_dl(
    adata,
    accessions="SRP464891",
    output_dir="srna_fastq",
    provider="ena",
    connections=8,
)

# Resolved runs are stored in .uns
runs = adata.uns.get("fastq_dl_runs", {})
print(f"Total runs: {len(runs)}")
for acc, info in runs.items():
    print(f"  {acc}  layout={info['layout']}  R1={info['fq1']}")
```

## Batch download specific SRRs

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# One obs row per sample
adata = ad.AnnData(obs=pd.DataFrame(index=[f"S{i}" for i in range(1, 11)]))

adata = sa.fastq.fastq_dl(
    adata,
    accessions=[
        "SRR26304152", "SRR26304153", "SRR26304154", "SRR26304155",
        "SRR26304156", "SRR26304157", "SRR26304158", "SRR26304159",
        "SRR26304160", "SRR26304161",
    ],
    output_dir="srna_fastq",
    provider="ena",
    jobs=5,
)

# adata.obs has fastq_path for every sample
print(adata.obs[["fastq_path"]])
```

## Only fetch metadata (no download)

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["SRP464891"]))
adata = sa.fastq.fastq_dl(
    adata, accessions="SRP464891", only_metadata=True, output_dir="srna_fastq"
)

runs = adata.uns.get("fastq_dl_runs", {})
print(f"Discovered runs: {list(runs.keys())}")
```

## From SRA instead of ENA

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
adata = sa.fastq.fastq_dl(
    adata,
    accessions="SRR26304152",
    output_dir="srna_fastq",
    provider="sra",
    protocol="https",
    cpus=4,
)
```

## Complete sRNA-seq pipeline: download -> cutadapt trimming -> downstream

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

# --- Step 1: Create AnnData for 10 samples ---
adata = ad.AnnData(obs=pd.DataFrame(index=[f"S{i}" for i in range(1, 11)]))

# --- Step 2: Download SRP464891 samples ---
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

# --- Step 3: Trim 3' adapter ---
adata = sa.fastq.cutadapt(
    adata,
    output_dir="trimmed_srna",
    adapter_3="TGGAATTCTCGGGTGCCAAGG",
    min_length=18,
    max_length=36,
    jobs=4,
)

print("Trimmed FASTQs:")
print(adata.obs["trimmed_path"])
```

## Sample-to-BioSample mapping table (SRP464891)

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

srna_samples_srp464891 = [
    ("S1",  "SRR26304152", "SAMN37706862"),
    ("S2",  "SRR26304153", "SAMN37706863"),
    ("S3",  "SRR26304154", "SAMN37706864"),
    ("S4",  "SRR26304155", "SAMN37706865"),
    ("S5",  "SRR26304156", "SAMN37706866"),
    ("S6",  "SRR26304157", "SAMN37706867"),
    ("S7",  "SRR26304158", "SAMN37706868"),
    ("S8",  "SRR26304159", "SAMN37706869"),
    ("S9",  "SRR26304160", "SAMN37706870"),
    ("S10", "SRR26304161", "SAMN37706871"),
]

# Build AnnData with sample labels as obs index
adata = ad.AnnData(obs=pd.DataFrame(index=[s[0] for s in srna_samples_srp464891]))
srrs = [s[1] for s in srna_samples_srp464891]

# Download all
adata = sa.fastq.fastq_dl(
    adata, accessions=srrs, output_dir="srna_fastq", provider="ena", jobs=5
)

# fastq_path is set for every sample
for label, row in adata.obs.iterrows():
    print(f"{label}: {row['fastq_path']}")
```

## Key function signature

```python
sa.fastq.fastq_dl(
    adata,                # AnnData: obs.index used as sample names
    accessions,           # str or list[str]: SRP/PRJNA (project-level)
                          #   or per-sample SRR/ERR/DRR, SRX/ERX, SAMN/SRS
                          #   Must match adata.n_obs when given as a list.
    output_dir="fastq",   # output directory
    provider="ena",       # "ena" or "sra"
    protocol="ftp",       # "ftp" or "https"
    group_by=None,        # None, "experiment", or "sample"
    cpus=4,               # CPUs for SRA conversion
    connections=8,        # HTTP connections for download
    max_attempts=3,       # retry count
    overwrite=False,      # skip existing files
    only_provider=False,  # don't fallback to other provider
    only_metadata=False,  # only fetch metadata, skip download
    skip_compression=False,  # skip gzip compression of .fastq files
    gzip_level=6,         # gzip compression level (1-9)
    ignore_md5=False,     # skip MD5 checksum verification
    prefix=None,          # prefix for output filenames
    silent=False,         # suppress download progress
    sleep=None,           # seconds to sleep between accessions
    sra_lite=False,       # download SRA lite format
    jobs=None,            # parallel accessions count
)
```

Returns `AnnData` with `adata.obs["fastq_path"]` set for per-sample downloads, `adata.uns["fastq_dl_run_info"]` keyed by obs name, and `adata.uns["fastq_dl_runs"]` containing the full run listing for project-level downloads.
