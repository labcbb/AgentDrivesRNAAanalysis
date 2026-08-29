# samtools idxstats Reference

## Function

```python
adata = sa.quant.idxstats(
    adata,
    output_dir="idxstats_out",
    bam_col="bam_path",
    create_index=True,
    drop_unmapped_reference=True,
    jobs=None,
    rna_type="smallRNA",
)
```

## Arguments

| Argument | Meaning |
|----------|---------|
| `adata` | AnnData with BAM paths in `adata.obs` |
| `output_dir` | Directory for per-sample `*.idxstats.tsv` files |
| `bam_col` | Column containing BAM paths, default `bam_path` |
| `create_index` | Run `samtools index` when `.bai` is missing |
| `drop_unmapped_reference` | Drop the special `*` row from idxstats output |
| `jobs` | Number of BAM files to process concurrently |
| `rna_type` | RNA type label stored in `adata.var["rna_type"]` |

## Input

Preferred input:

```python
adata.obs["bam_path"]
```

Fallback:

```python
adata.obs["sam_path"]
```

If `bam_path` is missing and `sam_path` exists, the wrapper looks for the corresponding `.bam` file.

## Output Matrix

For each BAM:

```bash
samtools idxstats sample.bam
```

Rows are reference sequences from the BAM header. In the intended workflow, each reference sequence is one small-RNA feature from the FASTA used to build the Bowtie index.

AnnData output:

```python
adata.X                    # mapped read counts
adata.layers["counts"]     # shared raw expression counts
adata.var["reference_name"]
adata.var["reference_length"]
adata.var["rna_type"]
adata.obs["idxstats_bam"]
adata.obs["idxstats_file"]
adata.uns["idxstats_result"]
```

## Examples

```python
import sRNAgent as sa
```

### piRBase FASTA

```python
sa.alignment.bowtie_build(
    "ref/piRBase_human.fa",
    "ref/piRBase_human",
    threads=4,
)

adata = sa.alignment.bowtie(
    adata,
    index_basename="ref/piRBase_human",
    output_dir="aligned_piRBase",
    total_mismatches=0,
    m=1,
    best=True,
)

adata = sa.quant.idxstats(
    adata,
    output_dir="idxstats_out",
)
```

### Ensembl ncRNA FASTA

```python
sa.alignment.bowtie_build(
    "ref/Homo_sapiens.GRCh38.ncrna.fa",
    "ref/human_ncrna",
    threads=4,
)

adata = sa.alignment.bowtie(
    adata,
    index_basename="ref/human_ncrna",
    output_dir="aligned_ncrna",
    total_mismatches=0,
    m=1,
    best=True,
)

adata = sa.quant.idxstats(
    adata,
    output_dir="idxstats_ncrna",
)
```

### mature tRNA FASTA

```python
sa.alignment.bowtie_build(
    "ref/mature_tRNAs.fa",
    "ref/mature_tRNAs",
    threads=4,
)

adata = sa.alignment.bowtie(
    adata,
    index_basename="ref/mature_tRNAs",
    output_dir="aligned_tRNA",
    total_mismatches=0,
    m=1,
    best=True,
)

adata = sa.quant.idxstats(
    adata,
    output_dir="idxstats_tRNA",
)
```
