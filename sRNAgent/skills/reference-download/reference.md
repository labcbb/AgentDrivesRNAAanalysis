## List available species

```python
import sRNAgent as sa

species = sa.reference.list_species()
print(f"{len(species)} species available")
print(species[:10])
```

## Download human genome (GRCh38 primary assembly) + .dict

```python
import sRNAgent as sa

result = sa.reference.download_genome(
    "homo_sapiens",
    output_dir="ref",
    jobs=8,
)
print(f"FASTA: {result['fasta']}")
print(f"DICT:  {result['dict']}")
```

## Build Bowtie index (for alignment + miRDeep2 mapper.pl)

```python
import sRNAgent as sa

# download_genome 自动解压 + 清理 header，直接返回 .fa 路径
result = sa.reference.download_genome("homo_sapiens", output_dir="ref", jobs=8)
sa.alignment.bowtie_build(result["fasta"], "ref/grch38", threads=8)
```

## Download human GTF annotation

```python
import sRNAgent as sa

result = sa.reference.download_gtf(
    "homo_sapiens",
    output_dir="ref",
    jobs=4,
)
print(f"GTF: {result['gtf']}")
```

## Download human ncRNA FASTA

```python
import sRNAgent as sa

result = sa.reference.download_ncrna(
    "homo_sapiens",
    output_dir="ref",
    jobs=4,
)
print(f"ncRNA: {result['ncrna']}")
```

## Download human miRBase data

```python
import sRNAgent as sa

# Download all-species FASTA + extract human
result = sa.reference.download_mirbase("hsa", output_dir="ref", jobs=4)
# Files kept: ref/hairpin.fa.gz, ref/mature.fa.gz
# Extracted: ref/hairpin_hsa.fa, ref/mature_hsa.fa, ref/hsa.gff3
```

## Extract mouse from existing all-species files (no re-download)

```python
import sRNAgent as sa

# Directly extract from ref/hairpin.fa.gz / ref/mature.fa.gz
result = sa.reference.download_mirbase("mmu", output_dir="ref", extract_only=True)
# Only creates: ref/hairpin_mmu.fa, ref/mature_mmu.fa
```

## Download mouse (GRCm39) genome + GTF + ncRNA

```python
import sRNAgent as sa

sa.reference.download_genome("mus_musculus", output_dir="ref", jobs=8)
sa.reference.download_gtf("mus_musculus", output_dir="ref", jobs=4)
sa.reference.download_ncrna("mus_musculus", output_dir="ref", jobs=4)
```

## Download zebrafish (GRCz11) genome

```python
import sRNAgent as sa

sa.reference.download_genome("danio_rerio", output_dir="ref", jobs=8)
```

## Download human genome + ncRNA (GTF on-demand)

```python
import sRNAgent as sa

# ── Download all in one batch ──
sa.reference.download_genome("homo_sapiens", output_dir="ref", jobs=8)
sa.reference.download_ncrna("homo_sapiens", output_dir="ref", jobs=4)
# GTF only if needed:
# sa.reference.download_gtf("homo_sapiens", output_dir="ref", jobs=4)
```
sa.reference.download_gtf("homo_sapiens", output_dir="ref", jobs=4)
sa.reference.download_ncrna("homo_sapiens", output_dir="ref", jobs=4)

# ── Check files ──
from pathlib import Path

ref_dir = Path("ref")
for f in sorted(ref_dir.iterdir()):
    size_mb = f.stat().st_size / 1_000_000
    print(f"{f.name:<55} {size_mb:>8.1f} MB")
```

## Download genome without .dict generation

```python
import sRNAgent as sa

result = sa.reference.download_genome(
    "homo_sapiens",
    output_dir="ref",
    generate_dict=False,
)
```

## Force re-download

```python
import sRNAgent as sa

sa.reference.download_genome("homo_sapiens", output_dir="ref", force=True)
```

## Verify files with CHECKSUMS (manual)

```bash
# Ensembl provides CHECKSUMS files in each directory
curl -O https://ftp.ensembl.org/pub/current/fasta/homo_sapiens/dna/CHECKSUMS
# Then verify:
while read -r algo hash file; do
    echo "$hash  ref/$file" | md5sum -c -
done < CHECKSUMS
```

## Key function signatures

```python
sa.reference.list_species()
    # → ["homo_sapiens", "mus_musculus", ...]

sa.reference.download_genome(
    species="homo_sapiens",
    output_dir=".",
    assembly=None,       # auto-detect
    jobs=4,              # download threads
    force=False,         # force re-download
    generate_dict=True,  # auto-generate .dict
)

sa.reference.download_gtf(
    species="homo_sapiens",
    output_dir=".",
    assembly=None,
    jobs=4,
    force=False,
)

sa.reference.download_ncrna(
    species="homo_sapiens",
    output_dir=".",
    jobs=4,
    force=False,
)
```

## miRBase: list species codes

```python
import sRNAgent as sa

# Requires mature.fa.gz already downloaded
codes = sa.reference.list_mirbase_codes(fasta_path="ref/mature.fa.gz")
print(codes)
```

## miRBase: download human miRNA hairpin + mature + GFF3

```python
import sRNAgent as sa

result = sa.reference.download_mirbase(
    species="hsa",
    output_dir="ref",
    jobs=4,
)
print(f"Hairpin (all): {result.get('hairpin_all')}")
print(f"Mature  (all): {result.get('mature_all')}")
print(f"Hairpin (hsa): {result['hairpin']}")
print(f"Mature  (hsa): {result['mature']}")
print(f"GFF3    (hsa): {result.get('gff3')}")
```

## miRBase: download all-species FASTA only (no extraction)

```python
import sRNAgent as sa

result = sa.reference.download_mirbase(output_dir="ref", jobs=4)
# Only downloads hairpin.fa.gz + mature.fa.gz
```

## miRBase: extract mouse miRNA from existing FASTA

```python
import sRNAgent as sa

result = sa.reference.download_mirbase(
    species="mmu",
    output_dir="ref",
    extract_only=True,
)
```

## miRBase: download mouse miRNA data

```python
import sRNAgent as sa

result = sa.reference.download_mirbase(species="mmu", output_dir="ref", jobs=4)
```

## Key function signatures

```python
sa.reference.list_mirbase_codes(
    fasta_path="mature.fa.gz",  # path to downloaded miRBase FASTA
)
# → ["hsa", "mmu", "rno", ...]

sa.reference.download_mirbase(
    species=None,         # 3-letter code, e.g. "hsa"
    output_dir=".",
    jobs=4,
    force=False,
    download_fasta=True,  # download hairpin.fa.gz + mature.fa.gz
    download_gff3=True,   # download species GFF3
    extract_only=False,   # extract from existing files only
)

## piRBase: list available species

```python
import sRNAgent as sa

species = sa.reference.list_pirna_species()
print(f"{len(species)} species available")
for code, name in sorted(species.items())[:10]:
    print(f"  {code}: {name}")
```

## piRBase: download human piRNA FASTA

```python
import sRNAgent as sa

# Full piRNA set
result = sa.reference.download_pirna("hsa", output_dir="ref", jobs=4)
print(f"piRNA FASTA: {result['fasta']}")

# Gold standard set
result = sa.reference.download_pirna("hsa", output_dir="ref", gold=True)
print(f"Gold FASTA: {result['gold_fasta']}")
```

## piRBase: download mouse piRNA FASTA

```python
import sRNAgent as sa

result = sa.reference.download_pirna("mmu", output_dir="ref", jobs=4)
```

## tRNA: download tRNAscan-SE results (hg38)

```python
import sRNAgent as sa

result = sa.reference.download_trnascan_hg38(output_dir="ref")
print(f"tRNA FASTA: {result['trna_fasta']}")
print(f"tRNA BED:   {result['trna_bed']}")
```

## tRNA: build tRAX human GTF

```python
import sRNAgent as sa

# Download GTF first, then build
sa.reference.download_gtf("homo_sapiens", output_dir="ref", jobs=4)
result = sa.reference.build_trax_human_gtf(output_dir="ref")
print(f"tRAX GTF: {result['trax_gtf']}")

# Or build from existing GTF
result = sa.reference.build_trax_human_gtf(
    output_dir="ref",
    gtf_path="ref/gencode.v50.primary_assembly.annotation.gtf.gz",
)
```

sa.reference.list_species()
    # → ["homo_sapiens", "mus_musculus", ...]

sa.reference.download_genome(
    species="homo_sapiens",
    output_dir=".",
    assembly=None,       # auto-detect
    jobs=4,              # download threads
    force=False,         # force re-download
    generate_dict=True,  # auto-generate .dict
)
