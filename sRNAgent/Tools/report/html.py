"""Traceable, result-only HTML reports for one or more AnnData modalities."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import pandas as pd
from anndata import AnnData
from jinja2 import Template

from ..._registry import register_function


REPORT_UNS_KEY = "report"
_TABLE_KEYS = {
    "de_results": "Differential expression",
    "enrichr.results": "Enrichment",
    "classification.performance": "Classification performance",
    "cox.univariate_results": "Univariate Cox",
    "cox.multivariate_results": "Multivariate Cox",
    "cox.selection_coefficients": "Feature selection",
    "cox.cross_validation": "Cox cross-validation",
    "feature_selection.metadata.selection_table": "Feature selection",
}
_PATH_KEY_RE = re.compile(r"(?:path|file|html|tsv|csv|bam|sam|fasta|fa|gff|gtf|joblib|graphml|output|dir)$", re.I)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (Path,)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value[:100]]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in list(value.items())[:100]}
    if isinstance(value, pd.DataFrame):
        return {"type": "DataFrame", "shape": list(value.shape), "columns": [str(col) for col in value.columns]}
    if isinstance(value, pd.Series):
        return {"type": "Series", "length": int(len(value)), "name": str(value.name)}
    try:
        return value.item()
    except Exception:
        return str(value)


def _get_nested(root: Any, path: str) -> Any:
    current = root
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current


def _copy_file(source: str | Path, report_dir: Path, relative_dir: str) -> Optional[str]:
    path = Path(str(source)).expanduser()
    if not path.exists() or not path.is_file():
        return None
    target_dir = report_dir / "assets" / relative_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if path.resolve() != target.resolve():
        shutil.copy2(path, target)
    return target.relative_to(report_dir).as_posix()


def _table_entry(table: pd.DataFrame, title: str, key: str, report_dir: Path, modality: str, top_n: int) -> dict[str, Any]:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{modality}_{key}").strip("_") or "table"
    table_dir = report_dir / "tables"
    table_dir.mkdir(parents=True, exist_ok=True)
    full_path = table_dir / f"{safe}.csv"
    table.to_csv(full_path)
    display = table.head(int(top_n)).reset_index()
    display_columns = [str(column) for column in display.columns]
    return {
        "title": title,
        "key": key,
        "modality": modality,
        "n_rows": int(len(table)),
        "n_columns": int(len(table.columns)),
        "columns": display_columns,
        "rows": [{str(k): _json_safe(v) for k, v in row.items()} for row in display.to_dict(orient="records")],
        "csv": full_path.relative_to(report_dir).as_posix(),
    }


def _collect_tables(adata: AnnData, modality: str, report_dir: Path, top_n: int) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for key, title in _TABLE_KEYS.items():
        value = _get_nested(adata.uns, key)
        if isinstance(value, pd.DataFrame) and not value.empty:
            tables.append(_table_entry(value, title, key, report_dir, modality, top_n))
    return tables


def _collect_plots(adata: AnnData, modality: str, report_dir: Path) -> list[dict[str, Any]]:
    records = adata.uns.get("plots") or {}
    plots: list[dict[str, Any]] = []
    if not isinstance(records, Mapping):
        return plots
    for name, raw in records.items():
        if not isinstance(raw, Mapping):
            continue
        copied: dict[str, Any] = {"name": str(name), "category": str(raw.get("category") or ""), "source": str(raw.get("source") or ""), "parameters": _json_safe(raw.get("parameters") or {})}
        for fmt in ("png", "pdf", "svg"):
            path = raw.get(f"path_{fmt}")
            if path:
                copied[f"path_{fmt}"] = _copy_file(str(path), report_dir, f"plots/{modality}")
        if copied.get("path_png") or copied.get("path_svg") or copied.get("path_pdf"):
            plots.append(copied)
    return plots


def _collect_artifacts(adata: AnnData, modality: str, report_dir: Path) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    seen: set[str] = set()
    def visit(value: Any, key_hint: str = "") -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                visit(item, str(key))
        elif isinstance(value, (list, tuple)):
            for item in value[:100]:
                visit(item, key_hint)
        elif isinstance(value, (str, Path)) and _PATH_KEY_RE.search(key_hint):
            copied = _copy_file(value, report_dir, f"artifacts/{modality}")
            if copied and copied not in seen:
                seen.add(copied)
                artifacts.append({"key": key_hint, "path": copied, "modality": modality})
    visit(adata.uns)
    for column in adata.obs.columns:
        if _PATH_KEY_RE.search(str(column)):
            for value in adata.obs[column].dropna().astype(str).head(100):
                visit(value, str(column))
    return artifacts


def _collect_modality(adata: AnnData, modality: str, report_dir: Path, group_col: Optional[str], top_n: int) -> dict[str, Any]:
    if not isinstance(adata, AnnData):
        raise TypeError(f"{modality} must be an AnnData object")
    groups: dict[str, int] = {}
    group_status = "not requested"
    if group_col:
        if group_col in adata.obs.columns:
            groups = {str(key): int(value) for key, value in adata.obs[group_col].astype(str).value_counts().items()}
            group_status = "available"
        else:
            group_status = f"missing obs column {group_col!r}"
    return {
        "name": modality,
        "n_samples": int(adata.n_obs),
        "n_features": int(adata.n_vars),
        "samples": [str(name) for name in adata.obs_names],
        "obs_columns": [str(col) for col in adata.obs.columns],
        "var_columns": [str(col) for col in adata.var.columns],
        "layers": [str(key) for key in adata.layers.keys()],
        "obsm": [str(key) for key in adata.obsm.keys()],
        "uns_keys": [str(key) for key in adata.uns.keys()],
        "group_col": group_col or "",
        "group_status": group_status,
        "groups": groups,
        "plots": _collect_plots(adata, modality, report_dir),
        "tables": _collect_tables(adata, modality, report_dir, top_n),
        "artifacts": _collect_artifacts(adata, modality, report_dir),
    }


_TEMPLATE = Template(r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title><style>
:root{--ink:#24303a;--muted:#68737d;--line:#dfe5e8;--pink:#db7094;--blue:#8babd2;--pale:#f7f9fa}
*{box-sizing:border-box}body{margin:0;color:var(--ink);font:14px/1.5 Arial,Helvetica,sans-serif;background:#fff}
.shell{max-width:1320px;margin:0 auto;padding:34px 42px 70px}.hero{border-bottom:3px solid var(--pink);padding:12px 0 24px;margin-bottom:26px}.hero h1{font-size:30px;font-weight:600;letter-spacing:.01em;margin:0 0 8px}.meta{color:var(--muted);font-size:12px}.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:22px 0}.metric{background:var(--pale);border-left:4px solid var(--blue);padding:13px 15px}.metric strong{display:block;font-size:22px;font-weight:600}.metric span{color:var(--muted);font-size:12px}.toc{background:#fbfbfc;border:1px solid var(--line);padding:16px 20px;margin:22px 0}.toc a{color:#486b8c;text-decoration:none;margin-right:16px;font-size:13px}.section{margin:34px 0 0;break-inside:avoid}.section h2{font-size:20px;font-weight:600;border-bottom:1px solid var(--line);padding-bottom:7px}.section h3{font-size:15px;margin:24px 0 8px}.note{color:var(--muted);font-size:13px}.status{display:inline-block;padding:2px 8px;background:#edf4f7;color:#45606e;font-size:11px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:18px;align-items:start}.figure{border:1px solid var(--line);padding:10px;background:#fff}.figure img{width:100%;height:auto;display:block}.caption{font-size:12px;margin-top:8px;color:var(--muted)}table{border-collapse:collapse;width:100%;font-size:11px;display:block;overflow-x:auto}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;white-space:nowrap}th{background:#f2f5f6;font-weight:600}.download{font-size:11px;color:#486b8c;text-decoration:none;margin-right:10px}.missing{border-left:3px solid #ffc080;background:#fffaf3;padding:10px 13px;color:#6d6258}.footer{border-top:1px solid var(--line);margin-top:50px;padding-top:14px;color:var(--muted);font-size:11px}@media print{.shell{padding:18px}.figure{break-inside:avoid}.toc a{color:#000}.section{break-before:auto}}@media(max-width:700px){.shell{padding:22px 16px}.grid{grid-template-columns:1fr}.hero h1{font-size:24px}}
</style></head><body><main class="shell">
<header class="hero"><h1>{{ title }}</h1><div class="meta">Generated {{ generated_at }} · result-only report · {{ modalities|length }} modality{{ '' if modalities|length == 1 else 'ies' }}</div></header>
<div class="summary"><div class="metric"><strong>{{ totals.samples }}</strong><span>Total samples across supplied modalities</span></div><div class="metric"><strong>{{ totals.features }}</strong><span>Total stored features</span></div><div class="metric"><strong>{{ totals.plots }}</strong><span>Registered figures included</span></div><div class="metric"><strong>{{ totals.tables }}</strong><span>Result tables exported</span></div></div>
<nav class="toc"><strong>Contents</strong><br>{% for section in sections %}<a href="#{{ section.id }}">{{ section.title }}</a>{% endfor %}</nav>
<section class="section" id="overview"><h2>1. Analysis overview</h2><p class="note">This report summarizes data and artifacts already present in the supplied AnnData objects. It does not rerun upstream analyses or contact external services.</p><div class="grid">{% for modality in modalities %}<div class="figure"><h3>{{ modality.name }}</h3><p>{{ modality.n_samples }} samples · {{ modality.n_features }} features</p><p class="note">Layers: {{ modality.layers|join(', ') or 'none' }}<br>Stored results: {{ modality.uns_keys|join(', ') or 'none' }}</p>{% if modality.group_col %}<p><span class="status">{{ modality.group_status }}</span> {{ modality.group_col }}: {{ modality.groups }}</p>{% endif %}</div>{% endfor %}</div></section>
{% for section in sections if section.id != 'overview' %}<section class="section" id="{{ section.id }}"><h2>{{ loop.index + 1 }}. {{ section.title }}</h2>{% if section.note %}<p class="note">{{ section.note }}</p>{% endif %}{% for modality in modalities if modality.plots|selectattr('category','equalto',section.category)|list or modality.tables|selectattr('title','equalto',section.title)|list %}<h3>{{ modality.name }}</h3>{% set plots = modality.plots|selectattr('category','equalto',section.category)|list %}{% if plots %}<div class="grid">{% for plot in plots %}<figure class="figure"><a href="{{ plot.path_png or plot.path_svg or plot.path_pdf }}"><img src="{{ plot.path_png or plot.path_svg }}" alt="{{ plot.name }}"></a><figcaption class="caption"><strong>{{ plot.name }}</strong> · source: {{ plot.source }}<br><a class="download" href="{{ plot.path_pdf }}">PDF</a><a class="download" href="{{ plot.path_svg }}">SVG</a></figcaption></figure>{% endfor %}</div>{% endif %}{% set tables = modality.tables|selectattr('title','equalto',section.title)|list %}{% for table in tables %}<h3>{{ table.title }} <a class="download" href="{{ table.csv }}">full CSV ({{ table.n_rows }} rows)</a></h3><table><thead><tr>{% for column in table.columns %}<th>{{ column }}</th>{% endfor %}</tr></thead><tbody>{% for row in table.rows %}<tr>{% for column in table.columns %}<td>{{ row.get(column, '') }}</td>{% endfor %}</tr>{% endfor %}</tbody></table>{% endfor %}{% endfor %}{% if section.empty %}<div class="missing">No stored plots or result tables were available for this section. The report did not rerun analysis.</div>{% endif %}</section>{% endfor %}
<section class="section" id="artifacts"><h2>Artifacts and provenance</h2>{% for modality in modalities %}<h3>{{ modality.name }}</h3>{% if modality.artifacts %}<ul>{% for artifact in modality.artifacts %}<li><a class="download" href="{{ artifact.path }}">{{ artifact.key }}</a></li>{% endfor %}</ul>{% else %}<p class="note">No additional file artifacts were found in stored metadata.</p>{% endif %}{% endfor %}</section>
<footer class="footer">Report manifest: <a class="download" href="report_manifest.json">report_manifest.json</a>. Complete result tables are linked as CSV files. Save the corresponding H5AD objects to preserve the in-object provenance.</footer>
</main></body></html>""")


def _section_data(modalities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs = [("qc", "Quality control", "Recorded per-sample QC and alignment metrics."), ("alignment", "Alignment", "Recorded alignment summaries."), ("expression", "Expression and composition", "Expression layers, PCA, correlation, and RNA composition."), ("differential", "Differential expression", "Stored differential-expression results and figures."), ("enrichment", "Enrichment", "Stored enrichment results and figures."), ("fragmentomics", "Fragmentomics", "Fragment-specific feature families from the independent fragmentomics modality."), ("target", "Target network", "Cached target tables and network figures."), ("classification", "Classification", "Stored classification performance."), ("cox", "Cox survival", "Stored Cox results and validation."), ("artifacts", "Artifacts", "Files recorded by upstream tools.")]
    result = []
    for category, title, note in specs:
        present = any(any(plot.get("category") == category for plot in modality["plots"]) or category == "artifacts" and modality["artifacts"] for modality in modalities)
        result.append({"id": category, "category": category, "title": title, "note": note, "empty": not present})
    return result


@register_function(
    aliases=["html_report", "analysis_report", "generate_report", "分析报告", "HTML报告"],
    category="report",
    description="Build a result-only, traceable HTML report from one or more independent AnnData modalities and already generated plots.",
    examples=[
        "adata = sa.report.html(adata, output_dir='results/report', group_col='condition')",
        "sa.report.html(srna_adata=srna, fragmentomics_adata=fragmentomics, output_dir='results/report')",
    ],
    produces={"uns": [REPORT_UNS_KEY]},
)
def html(
    adata: Optional[AnnData] = None,
    *,
    srna_adata: Optional[AnnData] = None,
    fragmentomics_adata: Optional[AnnData] = None,
    isomir_adata: Optional[AnnData] = None,
    rna_adata: Optional[AnnData] = None,
    output_dir: str = "results/report",
    title: str = "sRNAgent analysis report",
    group_col: Optional[str] = None,
    level: str = "standard",
    top_n: int = 15,
    include_existing_plots: bool = True,
) -> AnnData | dict[str, Any]:
    """Generate an HTML report without executing any analysis tool."""
    supplied: dict[str, AnnData] = {}
    if adata is not None:
        supplied["srna"] = adata
    for name, value in (("srna", srna_adata), ("fragmentomics", fragmentomics_adata), ("isomir", isomir_adata), ("rna", rna_adata)):
        if value is not None:
            supplied[name] = value
    if not supplied:
        raise ValueError("Provide adata or at least one modality AnnData")
    level = str(level).lower()
    if level not in {"minimal", "standard", "publication"}:
        raise ValueError("level must be 'minimal', 'standard', or 'publication'")
    report_dir = Path(output_dir).expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    modalities = [_collect_modality(value, name, report_dir, group_col, int(top_n)) for name, value in supplied.items()]
    if not include_existing_plots:
        for modality in modalities:
            modality["plots"] = []
    sections = _section_data(modalities)
    if level == "minimal":
        sections = [section for section in sections if section["category"] in {"expression", "differential", "classification", "cox", "artifacts"}]
    elif level == "publication":
        sections = [section for section in sections if section["category"] in {"differential", "enrichment", "target", "classification", "cox", "artifacts"}]
    totals = {"samples": sum(item["n_samples"] for item in modalities), "features": sum(item["n_features"] for item in modalities), "plots": sum(len(item["plots"]) for item in modalities), "tables": sum(len(item["tables"]) for item in modalities)}
    context = {"title": str(title), "generated_at": _utc_now(), "modalities": modalities, "sections": [{"id": "overview", "title": "Analysis overview"}] + sections, "totals": totals}
    html_path = report_dir / "report.html"
    html_path.write_text(_TEMPLATE.render(**context), encoding="utf-8")
    manifest = {"title": str(title), "generated_at": context["generated_at"], "level": level, "group_col": group_col or "", "result_only": True, "modalities": [{key: value for key, value in item.items() if key not in {"samples"}} for item in modalities], "sections": sections, "totals": totals, "html": str(html_path), "manifest": str(report_dir / "report_manifest.json")}
    (report_dir / "report_manifest.json").write_text(json.dumps(_json_safe(manifest), indent=2, ensure_ascii=False), encoding="utf-8")
    record = {"html": str(html_path), "manifest": str(report_dir / "report_manifest.json"), "level": level, "group_col": group_col or "", "generated_at": context["generated_at"], "sections": [section["id"] for section in sections], "n_plots": totals["plots"], "n_tables": totals["tables"]}
    for value in supplied.values():
        value.uns[REPORT_UNS_KEY] = record
    if len(supplied) == 1:
        return next(iter(supplied.values()))
    return {"report": record, "modalities": modalities}


__all__ = ["html", "REPORT_UNS_KEY"]
