---
name: fastq-dl-srna
title: Download sRNA-seq FASTQ from ENA/SRA with fastq-dl
description: "sRNA-seq FASTQ download via fastq-dl: AnnData-based API, single-end default, R1-only for paired-end, ENA/SRA, BioProject/Experiment/Run accessions."
---

# Download sRNA-seq FASTQ from ENA/SRA with fastq-dl

## Overview

Small RNA sequencing (sRNA-seq, miRNA-seq, piRNA-seq) is typically **single-end** (36-50 bp). Even when a dataset was sequenced as **paired-end, only R1 is biologically meaningful** -- R2 contains adapter/index and is discarded. This skill covers downloading sRNA-seq FASTQ files using `sa.fastq.fastq_dl`, a wrapper around [fastq-dl](https://github.com/rpetit3/fastq-dl) that:

> ⚡ **批量样本时务必使用 `jobs=N` 参数并行下载**
>
> `sa.fastq.fastq_dl` 支持 `jobs` 参数控制并行下载的样本数（通过线程池实现）。
> 样本多时（比如 >5 个），设置 `jobs=4` 或 `jobs=5` 可大幅缩短总耗时。
> 如果用户没主动提并行数，**agent 应该根据样本量推荐一个合理的 `jobs` 值**。

- Accepts **any accession level**: BioProject (`PRJNA...`), SRA Study (`SRP...`), BioSample (`SRS/SAMN...`), Experiment (`SRX...`), Run (`SRR/ERR/DRR...`)
- Queries ENA's Data Warehouse API to resolve all associated Runs automatically
- Downloads from **ENA** (direct FASTQ via FTP/HTTPS, faster) or **SRA** (via `sra-tools`)
- Produces **R1-only** results for sRNA-seq analysis
- Takes and returns an **AnnData** object -- download paths are stored in `adata.obs["fastq_path"]`

Throughout this skill, we use **SRP464891** -- a real sRNA-seq project with 10 samples -- as the running example.

| Run | BioSample | Sample label (for downstream) |
|-----|-----------|-------------------------------|
| SRR26304152 | SAMN37706862 | S1 |
| SRR26304153 | SAMN37706863 | S2 |
| SRR26304154 | SAMN37706864 | S3 |
| SRR26304155 | SAMN37706865 | S4 |
| SRR26304156 | SAMN37706866 | S5 |
| SRR26304157 | SAMN37706867 | S6 |
| SRR26304158 | SAMN37706868 | S7 |
| SRR26304159 | SAMN37706869 | S8 |
| SRR26304160 | SAMN37706870 | S9 |
| SRR26304161 | SAMN37706871 | S10 |

## Instructions

1. **Set up the environment**

   ```python
   import sRNAgent as sa
   import anndata as ad
   import pandas as pd
   ```

   Ensure `fastq-dl` is available:

   ```python
   # Install once before running the wrapper:
   # conda install -c conda-forge -c bioconda fastq-dl
   # or: pip install fastq-dl
   adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
   adata = sa.fastq.fastq_dl(adata, accessions="SRR26304152", output_dir="srna_fastq")
   ```

2. **Create an AnnData object with your sample labels**

   The API requires an existing AnnData whose `obs` index lists the sample names. The `accessions` parameter must match the `obs` length.

   ```python
   # 10 samples labelled S1 through S10
   adata = ad.AnnData(obs=pd.DataFrame(index=[f"S{i}" for i in range(1, 11)]))
   ```

3. **Choose the right accession level for sRNA-seq data**

   sRNA-seq datasets on SRA/ENA are organised hierarchically:

   ```
   SRA Study (SRP464891)                -- whole project, e.g. "miRNA profiling of ..."
       +-- BioSample (SAMN37706862)     -- biological sample
       |   +-- Experiment (SRX...)      -- sequencing experiment
       |       +-- Run (SRR26304152)    -- actual FASTQ file
       +-- BioSample (SAMN37706863)
       |   +-- Experiment (SRX...)
       |       +-- Run (SRR26304153)
       +-- ...
   ```

   **CORRECT -- SRA Study / BioProject level (most common, download all at once):**

   ```python
   # SRP464891 contains 10 Runs; fastq-dl resolves them all automatically
   # When using a project-level accession, pass a single string for accessions
   adata_small = ad.AnnData(obs=pd.DataFrame(index=["sample"]))
   adata_small = sa.fastq.fastq_dl(adata_small, accessions="SRP464891", output_dir="srna_fastq")
   ```

   **CORRECT -- Run-level accessions (one per sample):**

   ```python
   adata = ad.AnnData(obs=pd.DataFrame(index=[f"S{i}" for i in range(1, 11)]))
   adata = sa.fastq.fastq_dl(
       adata,
       accessions=[
           "SRR26304152", "SRR26304153", "SRR26304154", "SRR26304155",
           "SRR26304156", "SRR26304157", "SRR26304158", "SRR26304159",
           "SRR26304160", "SRR26304161",
       ],
       output_dir="srna_fastq",
   )
   ```

   **CORRECT -- BioSample or Experiment level:**

   ```python
   adata_small = ad.AnnData(obs=pd.DataFrame(index=["sample"]))
   adata_small = sa.fastq.fastq_dl(adata_small, accessions="SAMN37706862", output_dir="srna_fastq")
   ```

   **WRONG -- manually stringing together SRR prefixes:**

   ```python
   # WRONG! fastq-dl auto-resolves, no need to manually build lists
   # srr_list = [f"SRR2630415{n}" for n in range(2, 12)]
   ```

4. **Provider: ENA vs SRA**

   **CORRECT -- ENA (default, recommended, direct FASTQ download):**

   ```python
   adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
   # ENA directly downloads FASTQ files -- no intermediate .sra conversion
   adata = sa.fastq.fastq_dl(
       adata,
       accessions="SRR26304152",
       provider="ena",        # default
       protocol="ftp",        # default, also supports "https"
       output_dir="srna_fastq",
   )
   ```

   **CORRECT -- SRA (fallback, for data not on ENA):**

   ```python
   adata = ad.AnnData(obs=pd.DataFrame(index=["S1"]))
   # SRA path: download .sra then convert to FASTQ -- slower
   adata = sa.fastq.fastq_dl(
       adata,
       accessions="SRR26304152",
       provider="sra",
       cpus=4,                 # CPUs for .sra-to-FASTQ conversion
       output_dir="srna_fastq",
   )
   ```

   **CORRECT -- strict provider, no auto-fallback:**

   ```python
   adata = sa.fastq.fastq_dl(
       adata,
       accessions="SRR26304152",
       provider="ena",
       only_provider=True,     # fail if ENA doesn't have it, no SRA fallback
       output_dir="srna_fastq",
   )
   ```

   Note: for sRNA-seq data, ENA is typically 2-5x faster than SRA because it skips the intermediate `.sra` download-and-convert step.

5. **Batch download all 10 sRNA-seq samples**

   Provide one accession per row in `adata.obs`:

   ```python
   import sRNAgent as sa
   import anndata as ad
   import pandas as pd

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
       jobs=5,                 # download 5 in parallel
   )

   # Results are in adata.obs
   print(adata.obs[["fastq_path"]])
   ```

6. **Download an entire SRA Study (single call, auto-resolves all Runs)**

   When using a project-level accession (string), the corresponding obs row gets the full run listing embedded in its metadata:

   ```python
   adata = ad.AnnData(obs=pd.DataFrame(index=["SRP464891"]))
   adata = sa.fastq.fastq_dl(
       adata,
       accessions="SRP464891",
       output_dir="srna_fastq",
       provider="ena",
       connections=8,
   )

   # For project-level downloads, the resolved run details are stored
   # in adata.uns["fastq_dl_runs"] as a dict keyed by run accession
   runs = adata.uns.get("fastq_dl_runs", {})
   print(f"SRP464891 resolved to {len(runs)} Runs:")
   for run_acc, info in runs.items():
       print(f"  {run_acc} -> {info['fq1']}")
   ```

7. **Output file discovery: extract R1 (single-end or paired-end R1)**

   sRNA-seq only uses R1 even for paired-end data.

   **CORRECT -- paths stored in `adata.obs["fastq_path"]`:**

   ```python
   adata = ad.AnnData(obs=pd.DataFrame(index=[f"S{i}" for i in range(1, 11)]))
   adata = sa.fastq.fastq_dl(
       adata,
       accessions=[
           "SRR26304152", "SRR26304153", "SRR26304154", "SRR26304155",
           "SRR26304156", "SRR26304157", "SRR26304158", "SRR26304159",
           "SRR26304160", "SRR26304161",
       ],
       output_dir="srna_fastq",
   )

   # fastq_path is the R1 path -- exactly what sRNA-seq needs
   for sample_name, row in adata.obs.iterrows():
       print(f"{sample_name}: {row['fastq_path']}")
   ```

   **WRONG -- manually globbing the output directory:**

   ```python
   # WRONG! No need to manually glob
   # import glob; fq1 = glob.glob("srna_fastq/SRR26304152/*_1.fastq.gz")[0]
   ```

8. **Feed downloads into sRNA-seq downstream analysis**

   After download, pass the FASTQ paths to cutadapt for 3' adapter trimming. The paths are right in `adata.obs`:

   ```python
   import sRNAgent as sa
   import anndata as ad
   import pandas as pd

   # --- Download ---
   adata = ad.AnnData(obs=pd.DataFrame(index=[f"S{i}" for i in range(1, 11)]))
   adata = sa.fastq.fastq_dl(
       adata,
       accessions=[
           "SRR26304152", "SRR26304153", "SRR26304154", "SRR26304155",
           "SRR26304156", "SRR26304157", "SRR26304158", "SRR26304159",
           "SRR26304160", "SRR26304161",
       ],
       output_dir="srna_fastq",
   )

   # --- Trim 3' adapter ---
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

9. **Skip already-downloaded samples**

   Default `overwrite=False` skips existing FASTQ files automatically:

   ```python
   adata = ad.AnnData(obs=pd.DataFrame(index=[f"S{i}" for i in range(1, 11)]))
   # Incremental download: already-fetched Runs are skipped
   adata = sa.fastq.fastq_dl(
       adata,
       accessions=[
           "SRR26304152", "SRR26304153", "SRR26304154", "SRR26304155",
           "SRR26304156", "SRR26304157", "SRR26304158", "SRR26304159",
           "SRR26304160", "SRR26304161",
       ],
       output_dir="srna_fastq",
       overwrite=False,       # default
   )
   ```

   Force re-download:

   ```python
   adata = sa.fastq.fastq_dl(
       adata,
       accessions=...,
       output_dir="srna_fastq",
       overwrite=True,
   )
   ```

10. **Metadata preview (query ENA without downloading)**

    ```python
    adata = ad.AnnData(obs=pd.DataFrame(index=["SRP464891"]))
    adata = sa.fastq.fastq_dl(
        adata,
        accessions="SRP464891",
        only_metadata=True,
        output_dir="srna_fastq",
    )

    # The resolved Run listing is in .uns
    runs = adata.uns.get("fastq_dl_runs", {})
    print(f"Discovered {len(runs)} Runs:")
    for acc in runs:
        print(f"  {acc}")
    ```

11. **From GEO paper to the corresponding SRA Study**

    Many sRNA-seq papers only publish a GEO accession (GSE number) without a direct SRP link. Lookup approach:

    ```
    # Method 1: NCBI -> GEO (https://www.ncbi.nlm.nih.gov/geo/)
    #       -> search GSE number -> click "SRA Run Selector"
    #       -> obtain SRP / PRJNA / SRR list

    # Method 2: search for SRP/PRJNA directly
    #       -> Google "GSExxxxx SRA" usually turns up the SRP
    ```

    Once you have the SRP:

    ```python
    adata = ad.AnnData(obs=pd.DataFrame(index=["my_project"]))
    adata = sa.fastq.fastq_dl(adata, accessions="SRP464891", output_dir="srna_fastq")
    ```

## Critical API Reference

### sRNA-seq download mode quick reference

| Scenario | Recommended call | Notes |
|----------|-----------------|-------|
| Download whole project (recommended) | `sa.fastq.fastq_dl(adata, accessions="SRP464891")` | Single string, auto-resolves all Runs |
| Download specific Samples | `sa.fastq.fastq_dl(adata, accessions=[list of SRRs])` | One accession per obs row |
| From GEO paper reproduction | Lookup SRP/PRJNA first -> `sa.fastq.fastq_dl(adata, accessions="SRPxxxxx")` | |
| Limited network | `provider="sra", protocol="https"` | SRA HTTPS more stable on poor connections |
| Preview only | `only_metadata=True` | Query the API, no file download |

### Single-end vs paired-end file handling

```python
# sRNA-seq rule: only R1 matters
# adata.obs["fastq_path"] always points to R1

# Layout information is stored per-run in adata.uns["fastq_dl_runs"] (project-level)
# or can be reconstructed from adata.uns["fastq_dl_run_info"] (per-sample)
```

### Output AnnData structure

```python
# Per-sample accessions (one SRR per obs row):
#   adata.obs["fastq_path"]  -- path to the R1 FASTQ file
#   adata.uns["fastq_dl_run_info"] -- per-sample dict with fq1, fq2, layout

# Project-level accession (single string, e.g. SRP464891):
#   adata.obs["fastq_path"]  -- first discovered R1 for the single obs row
#   adata.uns["fastq_dl_runs"] -- full dict {run_acc: {fq1, fq2, layout, ...}}
#   adata.uns["fastq_dl_metadata_files"] -- metadata TSV path when available
```

### Complete sample-to-accession mapping (SRP464891 -> 10 samples)

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

adata = ad.AnnData(obs=pd.DataFrame(index=[s[0] for s in srna_samples_srp464891]))
srrs = [s[1] for s in srna_samples_srp464891]

adata = sa.fastq.fastq_dl(
    adata,
    accessions=srrs,
    output_dir="srna_fastq",
    provider="ena",
    jobs=5,
)

# adata.obs now has fastq_path for every sample
print(adata.obs[["fastq_path"]])
```

## Troubleshooting

- **`fastq-dl: command not found`**: install it before running the wrapper: `conda install -c conda-forge -c bioconda fastq-dl` or `pip install fastq-dl`.
- **`accessions length does not match obs`**: the number of accessions passed must equal `adata.n_obs`. Each row gets one accession. For a single project-level accession (string), use a 1-row AnnData or a string; for per-sample accessions use a list of the same length as obs.
- **`RequestError: No runs found for accession`**: confirm the accession prefix. SRR/ERR/DRR = Run, SAMN/SRS = BioSample, SRX/ERX = Experiment, SRP/PRJNA/PRJEB = project-level.
- **Slow download / timeout**: switch to `provider="sra"` (sometimes faster for certain datasets), increase `sleep` and `max_attempts`, or use `protocol="https"` instead of `ftp`.
- **`only_provider=True` fails even though data exists on ENA/SRA**: remove `only_provider=True` to let fastq-dl auto-fallback to the other provider.
- **Downloaded FASTQ files are much smaller than expected**: sRNA-seq fragments are short (18-30 nt) -- this is normal; a single-end 36 bp FASTQ has only one line of sequence per record. Run FastQC/MultiQC to check the length distribution.
- **Server without fastq-dl on PATH**: activate the intended conda environment, install `fastq-dl`, and retry.
- **SRP/PRJNA downloads many irrelevant Runs**: some BioProjects contain multiple experiment types (e.g. RNA-seq + ChIP-seq + sRNA-seq). In this case use BioSample or Experiment-level accessions for more precision.

## Examples

- "Download all 10 sRNA-seq samples from SRP464891 using the AnnData API, fetching directly from ENA."
- "Given GEO paper GSE123456, find the matching SRP via SRA Run Selector, then download with fastq_dl."
- "Download only samples S1 (SRR26304152) and S2 (SRR26304153) from SRP464891."
- "Batch download multiple independent SRR accessions with 5 parallel jobs."
- "Preview SRP464891 metadata (Run list) first before deciding to download."
- "Download from SRP464891, then feed the R1 paths into cutadapt for 3' adapter trimming."
- "Provide 10 SRRs and BioSample IDs, download them, and have results stored in adata.obs['fastq_path']."

## References

- Quick copy/paste code templates: [`reference.md`](reference.md)
- SRP464891 on NCBI: <https://www.ncbi.nlm.nih.gov/Traces/study/?acc=SRP464891>
- fastq-dl GitHub: <https://github.com/rpetit3/fastq-dl>
- ENA Data Warehouse API: <https://www.ebi.ac.uk/ena/browser/about>
- NCBI SRA Run Selector: <https://www.ncbi.nlm.nih.gov/Traces/study/>
