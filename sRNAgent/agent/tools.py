"""Agent tool handlers wired to sRNAgent function + skill registries."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .execution import ExecutionBackend, execute_agent_code
from .task_supervisor import TaskProgressSupervisor
from ..skill_registry import SkillDefinition, SkillMetadata, SkillRegistry

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_EXPLICIT_QUANT_METHOD_RE = re.compile(
    r"feature[-_ ]?counts?|idxstats?|samtools|mirdeep(?:2)?|mirtop|trax",
    re.IGNORECASE,
)
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

# The front matter is deliberately brief, so a literal comparison against it
# misses common user wording (especially Chinese workflow terms).  These are
# retrieval aliases, not alternative workflows; the corresponding SKILL.md
# remains the source of truth for execution.
_SKILL_ALIASES = {
    "alignment-srna": ("比对", "映射", "bowtie", "bam", "基因组比对"),
    "differential-analysis": ("差异", "差异表达", "差异分析", "de", "limma", "voom", "分组"),
    "enrichr-gene-enrichment": ("富集", "通路", "gene set", "enrichr", "gsea", "基因集"),
    "fastq-dl-srna": ("下载", "fastq下载", "ena", "sra", "geo", "测序数据"),
    "fastq-qc": ("质控", "去接头", "剪接头", "adapter", "cutadapt", "fastqc", "multiqc"),
    "feature-count": ("featurecounts", "特征计数", "注释计数", "基因组计数"),
    "fragment-analysis": ("片段组学", "fragmentomics", "fragomics", "片段特征"),
    "isomir-quantification": ("isomir", "iso-mir", "异构体", "mirtop"),
    "mirdeep2-mirna": ("mirna定量", "mirdeep", "已知mirna", "新mirna"),
    "modeling": ("建模", "分类", "预测", "cox", "生存", "特征选择"),
    "plotting": ("绘图", "作图", "可视化", "图表", "plot"),
    "reference-download": ("参考基因组", "参考下载", "gencode", "ensembl", "mirbase"),
    "reporting": ("报告", "html报告", "分析报告", "结果汇总"),
    "samtools_idxstats": ("pirna定量", "pirna计数", "idxstats", "samtools"),
    "starbase-mirna-targets": ("靶基因", "靶标", "mirna靶标", "starbase", "encori"),
    "trna-fragment-quantification-with-trax": ("trna定量", "trf", "tdr", "trna片段", "trax"),
}


def _tokenize(text: str) -> List[str]:
    return [token.lower() for token in _TOKEN_RE.findall(str(text or "")) if token.strip()]


def _normalize(text: str) -> str:
    """Normalize spelling variants that should not affect skill retrieval."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(text or "").lower())


def _cjk_ngrams(text: str) -> set[str]:
    """Return informative CJK fragments without needing a Chinese tokenizer."""
    grams: set[str] = set()
    for run in _CJK_RUN_RE.findall(str(text or "")):
        for width in range(2, min(4, len(run)) + 1):
            grams.update(run[index:index + width] for index in range(len(run) - width + 1))
    return grams


def _field_score(query: str, query_tokens: List[str], query_grams: set[str], field: str, *, weight: int) -> int:
    """Score one searchable field with bounded lexical and CJK overlap."""
    if not field:
        return 0
    normalized_query = _normalize(query)
    normalized_field = _normalize(field)
    score = 0
    if normalized_query and normalized_query == normalized_field:
        score += weight * 12
    elif normalized_query and len(normalized_query) >= 4 and normalized_query in normalized_field:
        score += weight * 5

    field_tokens = set(_tokenize(field))
    for token in query_tokens:
        if len(token) < 2:
            continue
        if token in field_tokens:
            score += weight * 2
        elif len(token) >= 4 and token in normalized_field:
            score += weight

    common_grams = query_grams & _cjk_ngrams(field)
    # Longer overlaps are more specific; the cap prevents verbose text from
    # overwhelming an exact slug or explicit method match.
    score += min(weight * 4, sum((len(gram) - 1) * weight for gram in common_grams))
    return score


def _default_quantification_skills(query: str) -> set[str]:
    """Return independent defaults when a request contains multiple RNA types."""
    lowered = str(query or "").lower()
    if not any(term in lowered for term in ("quantif", "quantify", "定量", "计数", "count")):
        return set()
    if _EXPLICIT_QUANT_METHOD_RE.search(lowered):
        return set()
    defaults: set[str] = set()
    if "pirna" in lowered:
        defaults.add("samtools_idxstats")
    if "mirna" in lowered or "micro-rna" in lowered or "microrna" in lowered:
        defaults.add("mirdeep2-mirna")
    return defaults


def _explicit_method_skills(query: str, skill_registry: SkillRegistry) -> set[str]:
    """Resolve a named method before applying broad biological defaults."""
    lowered = str(query or "").lower()
    method_slugs = {
        "feature-count": r"feature[-_ ]?counts?",
        "samtools_idxstats": r"idxstats?|samtools",
        "mirdeep2-mirna": r"mirdeep(?:2)?",
        "isomir-quantification": r"mirtop",
        "trna-fragment-quantification-with-trax": r"trax",
    }
    available = {slug.lower() for slug in skill_registry.skill_metadata}
    return {
        slug for slug, pattern in method_slugs.items()
        if slug in available and re.search(pattern, lowered, re.IGNORECASE)
    }


def rank_skill_matches(
    skill_registry: Optional[SkillRegistry],
    query: str,
) -> List[Tuple[SkillMetadata, int]]:
    if not skill_registry or not skill_registry.skill_metadata:
        return []
    raw_query = str(query or "").strip()
    if not raw_query:
        return []

    query_tokens = _tokenize(raw_query)
    query_grams = _cjk_ngrams(raw_query)
    default_skills = _default_quantification_skills(raw_query)
    explicit_method_slugs = _explicit_method_skills(raw_query, skill_registry)
    scored: List[Tuple[SkillMetadata, int]] = []

    for meta in skill_registry.skill_metadata.values():
        score = 0
        slug = meta.slug.lower()
        name = meta.name.lower()
        description = meta.description.lower()
        aliases = " ".join(_SKILL_ALIASES.get(slug, ()))

        # Identity signals are intentionally much stronger than words in a
        # prose description.  An explicitly requested skill/method must win.
        if _normalize(raw_query) == _normalize(slug):
            score += 1_200
        if _normalize(raw_query) == _normalize(name):
            score += 1_000
        if _normalize(slug).startswith(_normalize(raw_query)):
            score += 250

        score += _field_score(raw_query, query_tokens, query_grams, slug, weight=40)
        score += _field_score(raw_query, query_tokens, query_grams, name, weight=32)
        score += _field_score(raw_query, query_tokens, query_grams, description, weight=12)
        score += _field_score(raw_query, query_tokens, query_grams, aliases, weight=28)

        # Biological defaults are stronger than generic keyword overlap.  This
        # keeps piRNA requests on FASTA-level idxstats and miRNA requests on
        # miRDeep2, while an explicitly named method remains authoritative.
        if slug in default_skills:
            score += 500
        if slug in explicit_method_slugs:
            score += 2_000

        if score > 0:
            scored.append((meta, score))

    scored.sort(key=lambda item: (-item[1], len(item[0].slug), item[0].slug))
    return scored


def resolve_skill_query(
    skill_registry: Optional[SkillRegistry],
    query: str,
) -> Optional[SkillDefinition]:
    matches = rank_skill_matches(skill_registry, query)
    if not matches:
        return None
    return skill_registry.load_full_skill(matches[0][0].slug) if skill_registry else None


def search_functions(function_registry: Any, query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "Please provide a non-empty function search query."

    matches = function_registry.find(query)
    if not matches:
        return f"No functions found matching '{query}'. Try broader keywords."

    lines: List[str] = [f"Found {len(matches)} match(es) for '{query}':\n"]
    seen: set[str] = set()
    for entry in matches[:15]:
        full_name = entry.get("full_name", "")
        if full_name in seen:
            continue
        seen.add(full_name)
        sig = entry.get("signature", "")
        desc = (entry.get("description") or "")[:400]
        lines.append(f"  {full_name}{sig}")
        lines.append(f"    {desc}")
        examples = entry.get("examples") or []
        if examples:
            lines.append(f"    Example: {examples[0]}")
        lines.append("")
    return "\n".join(lines).strip()


def search_skills(skill_registry: Optional[SkillRegistry], query: str) -> str:
    if not skill_registry or not skill_registry.skill_metadata:
        return "No domain skills available."

    scored = rank_skill_matches(skill_registry, query)

    if not scored:
        slugs = ", ".join(m.slug for m in skill_registry.skill_metadata.values())
        return f"No skills matched '{query}'. Available skills: {slugs}"

    results: List[str] = []
    for meta, _ in scored[:2]:
        full_skill = skill_registry.load_full_skill(meta.slug)
        if full_skill:
            body = full_skill.prompt_instructions(max_chars=4000)
            results.append(f"=== {full_skill.name} ===\n{body}")

    if not results:
        return "Skills matched but content could not be loaded."
    return "\n\n".join(results)


def list_available_skills(skill_registry: Optional[SkillRegistry]) -> str:
    if not skill_registry or not skill_registry.skill_metadata:
        return "No skills registered."
    lines = ["Available skills:"]
    for meta in sorted(skill_registry.skill_metadata.values(), key=lambda m: m.slug):
        lines.append(f"  - {meta.slug}: {meta.description}")
    return "\n".join(lines)


def execute_code(
    code: str,
    project_root: Path,
    execution_backend: Optional[ExecutionBackend] = None,
    on_stream: Optional[Callable[[str, str], None]] = None,
    supervisor: Optional[TaskProgressSupervisor] = None,
) -> str:
    if execution_backend is None:
        from .execution import initialize_execution_backend

        execution_backend = initialize_execution_backend(project_root=project_root)
    return execute_agent_code(
        execution_backend, code, project_root, on_stream=on_stream, supervisor=supervisor,
    )


AGENT_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_functions",
            "description": (
                "Search the sRNAgent function registry. Returns signatures, "
                "descriptions, and examples. Call before writing code."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_skills",
            "description": (
                "Search installed sRNA-seq workflow skills (SKILL.md guides). "
                "Use for multi-step pipelines like FASTQ download."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_code",
            "description": (
                "Execute Python in the active sRNAgent execution session. "
                "Namespace includes `import sRNAgent as sa`. "
                "Prefer sa.fastq.* functions discovered via search_functions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["code", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Send your final reply directly to the user in chat. "
                "Write as if talking to the user — not an internal status report."
            ),
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    },
]
