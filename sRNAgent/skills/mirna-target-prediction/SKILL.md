---
name: mirna-target-prediction
description: "Derive auditable miRNA or isomiR target genes using miRBase reference sequences, starBase/ENCORI evidence, local miRanda prediction, and 3' UTR seed-site validation. Use for miRNA/isomiR target prediction, target-gene selection, or preparing target genes for enrichment."
---

# miRNA and isomiR Target Prediction

Use this skill as the sole target-prediction workflow before target-derived
enrichment. It keeps target generation, sequence support, and target-gene
selection in one AnnData-backed evidence trail.

## Inputs and Method Choice

| Item | Role | Source |
|------|------|--------|
| Canonical mature miRNA | Query ID/sequence | miRBase mature FASTA and annotation |
| Observed isomiR | Query sequence | Exact `isomir_adata.var["sequence"]` value from mirtop; a parent miRNA is optional metadata only |
| 3' UTR target space | Transcript and gene sequences | `sa.reference.extract_three_prime_utr` output with `transcript|gene=<symbol>` headers |

miRBase provides reference sequences and annotations; it is not a target
predictor. Select a target method by feature identity:

| Feature | Primary method | Permitted supporting evidence | Prohibited method |
|---------|----------------|-------------------------------|-------------------|
| Canonical mature miRNA | starBase/ENCORI evidence | Local miRanda and seed-site scan | None |
| Observed isomiR | Local miRanda on the exact observed sequence | Seed-site scan on the exact sequence | starBase and parent-miRNA substitution |

Do not send isomiR IDs to starBase: it accepts canonical mature-miRNA IDs and
cannot represent 5'/3' trimming, non-templated additions, or substitutions.

## Candidate Gate Before Target Prediction

For an experimental dataset, do not run starBase, miRanda, or seed scanning on
the full quantified miRNA/isomiR matrix. First complete differential analysis
with a user-confirmed group design, then run
`sa.target.select_target_candidates`. This skill's `plan_contract.json` makes
that selection and its user-confirmation card executable plan stages; do not
replace them with planner prose or an ad-hoc filter.

If differential analysis is absent, either run `differential-analysis` first
or ask the user for an explicit candidate list. A user-specified candidate list
is the only exception to this DE-first gate. If the selected subset remains
large, report its size and ask whether to tighten the threshold or select a
smaller ranked set before starting miRanda or a full UTR seed scan; never
silently launch a prediction over all isomiRs.

## Default Candidate Selection

When the user requests target prediction from differential-expression results
but does not name candidate small RNAs, select the default **upregulated**
candidate set using all of the following thresholds:

| Evidence | Default rule |
|----------|--------------|
| Differential significance | `adj_p_value < 0.05` |
| Effect size | `log_fc > 1` |
| Abundance | mean CPM across all samples `> 5` |
| Prediction scope | Rank passing candidates by FDR, then effect size, and retain the top `5` |

Use the raw-count layer to calculate CPM; do not threshold the mean of
`logcpm`, because it is already log transformed. The deterministic selector
persists feature IDs, thresholds, ranking limit, direction, and arithmetic
mean CPM in `adata.uns["target_candidate_selection"]`. A user-supplied
candidate list is the documented override and is not truncated. Do not
silently add downregulated features; when requested, select `log_fc < -1` as a
separately labelled candidate set. Use a different `top_n` only when the user
explicitly requests it; do not run a bulk isomiR target scan by default.

```python
adata = sa.target.select_target_candidates(adata)
# Only continue after the candidate card has been confirmed.
target_candidates = adata.uns["target_candidate_selection"]["features"]
```

The selector calculates mean CPM from per-sample raw counts. Do not estimate
mean CPM with `2 ** mean(logcpm)`: averaging after log transformation does not
recover arithmetic mean CPM. Apply the FDR rule as well; a logFC/CPM filter
without `adj_p_value` is not the default candidate policy.

## Workflow

### 1. Prepare sequence inputs

Download or reuse miRBase references for canonical mature-miRNA identity. For
isomiR target prediction, create a FASTA whose record IDs are stable isomiR
feature IDs and whose sequences are copied exactly from `adata.var["sequence"]`.
Generate the UTR FASTA once from the genome FASTA and GTF/GFF:

```python
import sRNAgent as sa

refs = sa.reference.download_mirbase("hsa", output_dir="references/mirbase")
utr = sa.reference.extract_three_prime_utr(
    genome_fasta="references/GRCh38.fa",
    annotation="references/gencode.gtf",
    output_fasta="references/human_3utr.fa",
)
```

### 2. Retrieve canonical-miRNA evidence from starBase

Use starBase only for canonical mature miRNAs. It returns cached external
CLIP/degradome-backed evidence and is not a local sequence predictor:

```python
mirna_adata = sa.target.starbase_mirna_targets(
    mirna_adata,
    mirnas="hsa-miR-21-5p",
    assembly="hg38",
    output_dir="results/targets/starbase",
)

starbase_records = mirna_adata.uns["starbase_mirna_targets"]["last_run"]["records"]
```

### 3. Predict exact-sequence targets with miRanda

Use miRanda for an isomiR, and optionally as local sequence evidence for a
canonical mature miRNA. The output retains each predicted duplex score, free
energy, transcript, gene name, and raw alignment block:

```python
isomir_adata = sa.target.miranda(
    isomir_adata,
    mirna_fasta="results/targets/isomir.fa",
    utr_fasta="references/human_3utr.fa",
    feature_ids=target_candidates,
    score_threshold=140,
    energy_threshold=-20,
    output_dir="results/targets/miranda_isomir",
)

miranda_targets = isomir_adata.uns["miranda_targets"]["results"]
miRanda_gene_set = set(
    miranda_targets.loc[
        miranda_targets["score"].ge(140) & miranda_targets["energy"].le(-20),
        "gene_name",
    ].dropna()
)
```

The tool runs `miranda miRNA.fa UTRs.fa -sc 140 -en -20 -out predictions.txt`.
Keep the recorded `adata.uns["miranda_targets"]["prediction_file"]`; do not
replace it with a hand-filtered file.

### 4. Validate seed-complement sites in 3' UTRs

Use the observed sequence in `adata.var["sequence"]` to scan the same UTR FASTA
for the reverse complement of positions 2-8. This is deterministic supporting
evidence, not a replacement for miRanda alignment/energy or starBase evidence:

`seed_utr_matches` requires only the selected AnnData features' `var["sequence"]`
values and the transcript-oriented 3' UTR FASTA. It does **not** require
sample-level mirtop GFF files, hairpin BAMs, or per-sample quantification
artifacts. Those files are upstream inputs used to create the isomiR AnnData;
once its sequences have been persisted, they are irrelevant to this scan.

```python
isomir_adata = sa.target.seed_utr_matches(
    isomir_adata,
    utr_fasta="references/human_3utr.fa",
    feature_ids=target_candidates,
)

seed_sites = isomir_adata.uns["seed_utr_matches"]["results"]
seed_gene_set = set(seed_sites["gene_name"].dropna())
selected_genes = sorted(miRanda_gene_set & seed_gene_set)
```

For a canonical miRNA, an explicit `restrict_to_cached_targets=True` scan may
validate a starBase result. Never enable that restriction for an isomiR.

### 5. Compare an isomiR with its parent miRNA

To attribute target changes to an observed isomiR, run the same local miRanda
and seed workflow for both sequences. The tool reads the parent ID from
`isomir_adata.var["mirna_id"]` and obtains the canonical parent sequence from
the miRBase mature FASTA. The parent sequence is a comparison baseline only;
it never replaces the observed isomiR sequence in isomiR prediction.

```python
isomir_adata = sa.target.compare_isomir_parent_targets(
    isomir_adata,
    mature_fasta="references/mirbase/mature_hsa.fa",
    utr_fasta="references/human_3utr.fa",
    score_threshold=140,
    energy_threshold=-20,
    output_dir="results/targets/isomir_parent_comparison",
)

comparison = isomir_adata.uns["isomir_parent_target_comparison"]
comparison["summary"]
gained_genes = comparison["gene_sets"]["isomir_gained"]
lost_genes = comparison["gene_sets"]["parent_lost"]
conserved_genes = comparison["gene_sets"]["conserved"]
```

`comparison["results"]` preserves one row per isomiR-parent-gene relation,
with target-change class, both sequences and seeds, miRanda score/energy,
transcript IDs, and exact seed-site counts. A changed 5' seed is labelled
`seed_status="changed"`; a 3' change can retain the same seed but still change
miRanda alignment or energy.

### 6. Hand off selected target genes to enrichment

Pass only the documented, unique gene-symbol list to
`enrichr-gene-enrichment`. Record the source method, thresholds, and whether
seed support was required. Do not pass miRNA/isomiR identifiers to GMT
enrichment.

```python
isomir_adata = sa.target.enrichr(
    isomir_adata,
    genes=gained_genes,
    gene_sets="references/msigdb/c2.cp.kegg_legacy.symbols.gmt",
    organism="human",
    result_key="isomir_gained_enrichr",
)
```

Run enrichment separately for `isomir_gained`, `parent_lost`, and `conserved`
genes. Give each call a different `result_key` so all contrasts remain in the
same AnnData.

## AnnData Evidence

| State | Content |
|-------|---------|
| `adata.uns["starbase_mirna_targets"]` | Canonical-miRNA external target evidence, request parameters, and cached TSV paths |
| `adata.uns["miranda_targets"]` | Local sequence-prediction parameters, raw output path, and parsed target table |
| `adata.uns["seed_utr_matches"]` | Per-feature exact seed-complement UTR matches and coordinates |
| `adata.uns["isomir_parent_target_comparison"]` | Same-method parent/isomiR comparison, per-pair target changes, summaries, and gene sets |
| `adata.uns["enrichr"]` | Local GMT enrichment of the selected target gene symbols |
