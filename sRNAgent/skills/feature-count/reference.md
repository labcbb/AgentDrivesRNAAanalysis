# featureCounts Quick Reference

## Quantify miRNA with default settings (TruSeq stranded)

```python
import sRNAgent as sa
import anndata as ad
import pandas as pd

adata = ad.AnnData(obs=pd.DataFrame(index=["S1", "S2"]))
adata.obs["bam_path"] = [
    "aligned/S1.bam",
    "aligned/S2.bam",
]

adata = sa.quant.feature_count(
    adata,
    annotation="ref/hairpin_hsa.gff3",
    feature_type="miRNA",
    attr_type="Name",
    strand=1,            # TruSeq Small RNA stranded
    threads=6,
    output_dir="fc_out",
)

print(adata.X.shape)
print(adata.var["feature_id"].tolist()[:10])
```

## Quantify piRNA (unstranded, appends to existing adata.X)

```python
# If adata.X already has miRNA counts, piRNA results merge alongside them
adata = sa.quant.feature_count(
    adata,
    annotation="ref/piRNA.gff3",
    feature_type="piRNA",
    attr_type="ID",
    rna_type="piRNA",
    strand=0,
    threads=4,
    output_dir="fc_out",
)
```

## Quantify all genes (unspecific)

```python
adata = sa.quant.feature_count(
    adata,
    annotation="ref/gencode.v50.annotation.gtf.gz",
    feature_type="gene",
    attr_type="gene_id",
    strand=1,
    threads=8,
    output_dir="fc_out",
)
```

## Allow overlapping features

```python
adata = sa.quant.feature_count(
    adata,
    annotation="ref/hairpin_hsa.gff3",
    feature_type="miRNA",
    attr_type="Name",
    strand=1,
    allow_overlap=True,      # -O: count reads overlapping multiple features
    threads=6,
    output_dir="fc_out",
)
```

## Key function signature

```python
sa.quant.feature_count(
    adata,                         # AnnData; reads from adata.obs["bam_path"]
    annotation,                    # GTF or GFF3 file path
    output_dir="fc_out",           # output directory
    rna_type="miRNA",              # RNA type label in adata.var["rna_type"]
    feature_type="miRNA",          # -t: feature type in GTF/GFF3
    attr_type="Name",              # -g: attribute for feature ID
    strand=1,                      # -s: 0=unstranded, 1=stranded, 2=reverse
    allow_overlap=False,           # -O: allow multi-feature overlap
    threads=4,                     # -T: CPU threads
    extra_args=None,               # extra featureCounts arguments
)
```

## CLI equivalent

```bash
featureCounts -T 6 -t miRNA -g Name -s 1 -a annotation.gtf -o counts.txt \
  sample1.bam sample2.bam sample3.bam
```
