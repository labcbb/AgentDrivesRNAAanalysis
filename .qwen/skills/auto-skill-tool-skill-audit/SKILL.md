---
name: tool-skill-audit
description: Systematic audit of tool API consistency (anndata compliance) and tool-skill cross-reference for the sRNAgent project
source: auto-skill
extracted_at: '2026-07-13T09:36:53.047Z'
---

# Tool API & Skill Correspondence Audit

A structured procedure for auditing whether sRNAgent project tools are using AnnData consistently and whether skills correspond to tools.

## When to use

Use this approach when:
- Asked to "check if tools are using anndata" or "verify tool-skill correspondence"
- Reviewing new tool implementations for API consistency
- After a round of tool refactoring, to verify nothing was missed

## Procedure

### Step 1: Discover all tool modules

List the Tools directory tree:

```
ls -R sRNAgent/Tools/
```

Identify the structure:
- **fastq/** — FASTQ processing (fastq_dl, cutadapt, fastqc, multiqc)
- **alignment/** — Alignment (bowtie, bowtie_build)
- **quant/** — Quantification (quantify_mirna, predict_mirna)
- **reference/** — Reference data download (ensembl_genome, mirbase)

Check each module's `__init__.py` to find what functions are exported as public API.

### Step 2: AnnData audit

For each public tool function, check:

**Data-processing tools** (fastq, alignment, quant) must:
- Accept `adata: AnnData` as first parameter
- Return `AnnData`
- Read sample paths from `adata.obs` columns
- Write results back to `adata.obs`, `adata.var`, or `adata.uns`

**Reference download tools** are the exception:
- Do NOT take/return AnnData (they download files, not process samples)
- Return `Dict[str, str]` (file paths) or `List[str]`
- Examples: `download_genome`, `download_gtf`, `download_ncrna`, `download_mirbase`, `list_species`, `list_mirbase_codes`

**Index-building tools** (e.g., `bowtie_build`) are also exempt:
- They build genome indexes, not sample-level operations
- Return `Dict[str, str]`

Utility/helper modules (e.g., `download_progress.py`) not exposed via `__init__.py` are internal and don't need AnnData.

### Step 3: Discover all skills

List the skills directory:

```
ls sRNAgent/skills/
```

Each skill subdirectory contains a `SKILL.md` (guide) and optionally a `reference.md` (code templates).

### Step 4: Cross-reference tools to skills

Create a correspondence table:

| Tool | Functions | Skill | Status |
|------|-----------|-------|--------|
| `fastq/fastq_dl.py` | `fastq_dl` | `skills/fastq-dl-srna/` | ✅/❌ |
| `fastq/cutadapt.py` | `cutadapt` | `skills/fastq-qc/` (covered) | ✅/❌ |
| `fastq/fastqc.py` | `fastqc` | `skills/fastq-qc/` (covered) | ✅/❌ |
| `fastq/multiqc.py` | `multiqc` | `skills/fastq-qc/` (covered) | ✅/❌ |
| `alignment/bowtie.py` | `bowtie`, `bowtie_build` | `skills/alignment-srna/` | ✅/❌ |
| `quant/mirdeep2.py` | `quantify_mirna`, `predict_mirna` | `skills/mirdeep2-mirna/` | ✅/❌ |
| `reference/ensembl_genome.py` | `list_species`, `download_genome`, `download_gtf`, `download_ncrna` | `skills/reference-download/` | ✅/❌ |
| `reference/mirbase.py` | `list_mirbase_codes`, `download_mirbase` | `skills/reference-download/` (covered) | ✅/❌ |

Check for:
- **Missing skills**: A tool module exposed in `__init__.py` but no matching skill
- **Orphan skills**: A skill directory with no corresponding tool module
- **Skill coverage**: A skill that omits important tool functions from its documentation
- **API consistency**: The skill's code examples match actual function signatures (parameter names, return types)

### Step 5: Report findings

Structure the report as:
1. AnnData compliance: which tools are correct, which need fixing, and why exceptions are valid
2. Tool-skill correspondence: match/mismatch for each pair
3. Any gaps or issues found
