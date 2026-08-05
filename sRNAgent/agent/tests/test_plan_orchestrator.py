"""Tests for plan prerequisite expansion."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.plan_orchestrator import (  # noqa: E402
    _apply_analysis_policy,
    _apply_deliverables_policy,
    _expand_plan_prerequisites,
    _resolve_analysis_policy,
    _resolve_deliverables_policy,
    _resolve_requirements_policy,
)


def test_fragment_analysis_plan_expands_missing_prerequisites():
    steps = [
        {
            "id": "1",
            "title": "做片段组学分析",
            "goal": "运行 fragment-analysis 提取片段特征",
            "skill": "fragment-analysis",
            "status": "pending",
            "result": "",
        }
    ]
    context = """
    ### 已知产物路径
    - srna_fastq/SRR100.fastq.gz
    """

    expanded = _expand_plan_prerequisites(steps, extra_context=context)

    assert [step["skill"] for step in expanded] == [
        "fastq-qc",
        "alignment-srna",
        "reference-download",
        "fragment-analysis",
    ]
    assert expanded[-1]["title"] == "做片段组学分析"


def test_fragment_analysis_plan_skips_known_completed_inputs():
    steps = [
        {
            "id": "1",
            "title": "做片段组学分析",
            "goal": "运行 fragment-analysis 提取片段特征",
            "skill": "fragment-analysis",
            "status": "pending",
            "result": "",
        }
    ]
    context = """
    ### 已知产物路径
    - trimmed/SRR100.trimmed.fastq.gz
    - aligned/SRR100.sorted.bam
    - ref/GRCh38.primary_assembly.genome.fa
    ### 已完成步骤
    - 已完成 fastq 质控
    - 已完成比对
    """

    expanded = _expand_plan_prerequisites(steps, extra_context=context)

    assert len(expanded) == 1
    assert expanded[0]["skill"] == "fragment-analysis"


def test_fragment_analysis_plan_preserves_non_fragment_steps():
    steps = [
        {
            "id": "1",
            "title": "检查结果",
            "goal": "查看已有结果",
            "skill": "",
            "status": "pending",
            "result": "",
        },
        {
            "id": "2",
            "title": "做片段组学分析",
            "goal": "运行 fragment-analysis 提取片段特征",
            "skill": "fragment-analysis",
            "status": "pending",
            "result": "",
        },
    ]

    expanded = _expand_plan_prerequisites(steps, extra_context="")

    assert expanded[0]["title"] == "检查结果"
    assert expanded[-1]["skill"] == "fragment-analysis"


def test_feature_count_plan_expands_alignment_prerequisites():
    steps = [
        {
            "id": "1",
            "title": "对已知特征做计数",
            "goal": "运行 feature-count 生成表达矩阵",
            "skill": "feature-count",
            "status": "pending",
            "result": "",
        }
    ]
    context = """
    ### 已知产物路径
    - srna_fastq/SRR100.fastq.gz
    """

    expanded = _expand_plan_prerequisites(steps, extra_context=context)

    assert [step["skill"] for step in expanded] == [
        "fastq-qc",
        "reference-download",
        "alignment-srna",
        "feature-count",
    ]


def test_differential_analysis_plan_expands_counts_and_group_confirmation():
    steps = [
        {
            "id": "1",
            "title": "做差异分析",
            "goal": "运行 differential-analysis 得到 DE 结果",
            "skill": "differential-analysis",
            "status": "pending",
            "result": "",
        }
    ]
    context = """
    ### 已知产物路径
    - trimmed/SRR100.trimmed.fastq.gz
    - aligned/SRR100.sorted.bam
    """

    expanded = _expand_plan_prerequisites(steps, extra_context=context)

    assert [step["skill"] for step in expanded] == [
        "feature-count",
        "",
        "differential-analysis",
    ]
    assert expanded[1]["title"] == "确认样本分组信息"


def test_differential_analysis_defaults_to_unpaired_when_user_unspecified():
    steps = [
        {
            "id": "1",
            "title": "做差异分析",
            "goal": "运行 differential-analysis 得到 DE 结果",
            "skill": "differential-analysis",
            "status": "pending",
            "result": "",
        }
    ]
    analysis = _resolve_analysis_policy("做 miRNA 差异分析", "", steps)
    filtered = _apply_analysis_policy(steps, analysis=analysis)

    assert analysis["design"] == "unpaired"
    assert filtered[0]["goal"]
    assert "unpaired" in filtered[0]["goal"].lower() or "非配对" in filtered[0]["goal"]


def test_differential_analysis_drops_conflicting_paired_step_when_unpaired_confirmed():
    steps = [
        {
            "id": "1",
            "title": "做 miRNA 配对差异分析",
            "goal": "对 miRNA 做 paired differential-analysis，并使用 patient blocking",
            "skill": "differential-analysis",
            "status": "pending",
            "result": "",
        },
        {
            "id": "2",
            "title": "做 miRNA 非配对差异分析",
            "goal": "对 miRNA 做 unpaired differential-analysis",
            "skill": "differential-analysis",
            "status": "pending",
            "result": "",
        },
    ]
    analysis = _resolve_analysis_policy("做非配对差异分析", "", steps)
    filtered = _apply_analysis_policy(steps, analysis=analysis)

    assert len(filtered) == 1
    assert "非配对" in filtered[0]["title"] or "unpaired" in filtered[0]["title"].lower()
    assert "patient blocking" not in filtered[0]["goal"].lower()


def test_differential_analysis_requires_confirmation_when_paired_infeasible():
    steps = [
        {
            "id": "1",
            "title": "做 miRNA 配对差异分析",
            "goal": "运行 paired differential-analysis",
            "skill": "differential-analysis",
            "status": "pending",
            "result": "",
        }
    ]
    context = "paired_feasible=false"
    analysis = _resolve_analysis_policy("做配对差异分析", context, steps)
    filtered = _apply_analysis_policy(steps, analysis=analysis)

    assert analysis["design"] == "needs_confirmation"
    assert filtered[0]["id"] == "analysis-confirmation"
    assert "paired" in filtered[0]["goal"]


def test_html_report_request_appends_report_step_when_missing():
    steps = [
        {
            "id": "1",
            "title": "过滤低表达特征并跑 limma-voom DE",
            "goal": "完成 fragmentomics 差异分析",
            "skill": "differential-analysis",
            "status": "pending",
            "result": "",
        }
    ]
    deliverables = _resolve_deliverables_policy(
        "在两组间做片段组学差异分析并生成 HTML 报告",
        "",
        steps,
    )
    filtered = _apply_deliverables_policy(steps, deliverables=deliverables)

    assert deliverables["html_report_requested"] is True
    assert filtered[-1]["title"] == "汇总差异结果并生成 HTML 报告"
    assert "report.html" in filtered[-1]["goal"] or ".html" in filtered[-1]["goal"]


def test_requirements_policy_keeps_high_priority_user_requirements():
    analysis = {
        "design": "unpaired",
    }
    deliverables = {
        "html_report_requested": True,
    }
    requirements = _resolve_requirements_policy(
        "如果已经有小RNA定量，片段组学要放在MuData下面，并且最后生成HTML报告",
        "片段组学需要 whole-genome BAM",
        analysis=analysis,
        deliverables=deliverables,
    )

    assert requirements["default_unpaired"] is True
    assert requirements["html_report_requested"] is True
    assert requirements["mudata_required"] is True
    assert requirements["whole_genome_bam_required"] is True
    items = requirements["items"]
    assert any("MuData" in item or "mudata" in item.lower() for item in items)
    assert any("HTML" in item or ".html" in item for item in items)
