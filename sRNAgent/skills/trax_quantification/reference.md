# tRAX Quantification Reference

## Function

```python
adata = sa.quant.tRAX(
    adata,
    databasename,
    output_dir="trax_out",
    experiment_name="trax_quant",
    fastq_dir=None,
    ensemblgtf=None,
    bedfiles=None,
    cores=4,
    lazyremap=False,
    maponly=False,
    nofrag=False,
    maxmismatches=None,
    minnontrnasize=20,
    local=False,
    skipfqcheck=False,
    path_col=None,
    replicate_col=None,
    replace_x=None,
)
```

## Arguments

| Argument | Meaning |
|----------|---------|
| `adata` | AnnData object with sample IDs in `adata.obs_names` |
| `databasename` | tRNAdb prefix or directory containing one `*-trnatable.txt` prefix |
| `output_dir` | tRAX output root |
| `experiment_name` | tRAX experiment name and output filename prefix |
| `fastq_dir` | Optional FASTQ fallback/matcher by sample basename |
| `ensemblgtf` | Optional GTF annotation passed to tRAX |
| `bedfiles` | Optional extra BED feature files |
| `cores` | tRAX internal parallelism |
| `lazyremap` | Reuse existing BAMs when present |
| `maponly` | Run mapping only; does not parse counts into `adata.X` |
| `nofrag` | Omit fragment determination |
| `maxmismatches` | Maximum allowed mismatches for counting |
| `minnontrnasize` | Minimum read length for non-tRNA reads |
| `local` | Use Bowtie2 local alignment mode |
| `skipfqcheck` | Skip FASTQ-vs-BAM read group consistency checks |
| `path_col` | Force a specific `adata.obs` column as FASTQ input |
| `replicate_col` | Optional `adata.obs` column for tRAX replicate/group labels |
| `replace_x` | `None` replaces X only for empty-var AnnData; `False` stores counts in `obsm`; `True` replaces X/var |

## FASTQ Resolution

The wrapper creates `adata.obs["trax_fq"]`.

Default priority:

1. `clean_fastq_path`
2. `fastq_path`
3. `fastq_dir` basename match

If `path_col` is supplied, only that column is used before `fastq_dir` fallback.

The original source columns are not modified.

## tRNAdb Input

Prefix style:

```python
databasename="/path/to/tRNAdb/hg38"
```

Expected files:

```text
hg38-trnatable.txt
hg38-maturetRNAs.bed
hg38-trnaloci.bed
hg38-tRNAgenome.fa
hg38-tRNAgenome.1.bt2 or hg38-tRNAgenome.1.bt2l
```

Directory style:

```python
databasename="/path/to/tRNAdb"
```

This is accepted only when the directory contains exactly one `*-trnatable.txt` prefix.

## tRAX Output Files

For:

```python
output_dir="trax_out"
experiment_name="trax_quant"
```

Key files:

```text
trax_out/trax_quant-samples.txt
trax_out/bam/<sample>.bam
trax_out/trax_quant/trax_quant-trnacounts.txt
trax_out/trax_quant/trax_quant-typecounts.txt
trax_out/trax_quant/trax_quant-aminocounts.txt
trax_out/trax_quant/trax_quant-anticodoncounts.txt
trax_out/trax_quant/trax_quant-readlengths.txt
trax_out/trax_quant/trax_quant-mapstats.txt
```

## AnnData Output

After a full run on an AnnData object with no existing variables:

```python
adata.X
adata.layers["tRAXcount"]
adata.var["trax_feature_id"]
adata.var["trna_id"]
adata.var["fragment_type"]
adata.obs["trax_fq"]
adata.obs["trax_bam"]
adata.obs["trax_sample"]
adata.obs["trax_replicate"]
adata.uns["trax_result"]
adata.uns["trax_count_matrix"]
```

`adata.X` and `adata.layers["tRAXcount"]` contain the same raw count matrix.

If the input already has an expression matrix, tRAX results are stored without replacing it:

```python
adata.obsm["tRAXcount"]
adata.uns["tRAX_var"]
adata.uns["trax_result"]
adata.uns["trax_count_matrix"]
```

Use `replace_x=True` to force tRAX counts into `adata.X`.

## Parsed Feature IDs

tRAX feature rows such as:

```text
tRNA-Glu-CTC-1_fiveprime
tRNA-Glu-CTC-1_threeprime
tRNA-Glu-CTC-1_wholecounts
tRNA-Glu-CTC-1_other
```

are parsed into:

| Column | Example |
|--------|---------|
| `trax_feature_id` | `tRNA-Glu-CTC-1_fiveprime` |
| `trna_id` | `tRNA-Glu-CTC-1` |
| `fragment_type` | `fiveprime` |

## Examples

Basic run:

```python
adata = sa.quant.tRAX(
    adata,
    databasename="ref/tRNAdb/hg38",
    output_dir="trax_out",
    experiment_name="trax_quant",
    cores=4,
)
```

Use local FASTQ directory as fallback:

```python
adata = sa.quant.tRAX(
    adata,
    fastq_dir="data/srna_fastq",
    databasename="ref/tRNAdb/hg38",
    output_dir="trax_out",
    experiment_name="trax_quant",
    cores=4,
)
```

Reuse existing BAMs:

```python
adata = sa.quant.tRAX(
    adata,
    databasename="ref/tRNAdb/hg38",
    output_dir="trax_out",
    experiment_name="trax_quant",
    lazyremap=True,
)
```

Use a specific FASTQ column:

```python
adata = sa.quant.tRAX(
    adata,
    path_col="my_clean_fastq",
    databasename="ref/tRNAdb/hg38",
)
```
