"""Tests for plan prerequisite expansion."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.plan_orchestrator import (  # noqa: E402
    _apply_analysis_policy,
    _apply_deliverables_policy,
    _apply_modality_boundaries,
    _expand_plan_prerequisites,
    _order_de_workflow_steps,
    _resolve_analysis_policy,
    _resolve_deliverables_policy,
    _resolve_requirements_policy,
    _is_read_only_query,
    _strip_unrequested_html_report,
)


def test_mirna_trna_and_fragmentomics_are_two_modalities():
    steps = [
        {"id": "1", "title": "miRNA 定量", "goal": "运行 miRDeep2", "skill": "mirdeep2-mirna"},
        {"id": "2", "title": "tRNA 定量", "goal": "运行 tRAX", "skill": "trax_quantification"},
        {"id": "3", "title": "片段组学定量", "goal": "运行 fragment-analysis", "skill": "fragment-analysis"},
    ]
    query = "基于 trimmed FASTQ 完成 miRNA 和 tRNA 定量、片段组学定量"
    goal, bounded = _apply_modality_boundaries(
        "完成 miRNA、tRNA 与片段组学三模态定量", steps, user_query=query,
    )
    analysis = _resolve_analysis_policy(query, "", bounded)

    assert "两种模态" in goal
    assert "三模态" not in goal
    assert analysis["modalities"] == ["srna", "fragmentomics"]
    assert all("srna AnnData" in step["goal"] for step in bounded[:2])
    assert "fragmentomics AnnData" in bounded[2]["goal"]


def test_old_mudata_note_does_not_become_a_current_requirement():
    analysis = _resolve_analysis_policy("完成 miRNA、tRNA 和片段组学定量", "", [])
    requirements = _resolve_requirements_policy(
        "完成 miRNA、tRNA 和片段组学定量",
        "上一会话要求 MuData 并写出 h5mu",
        analysis=analysis,
        deliverables={"html_report_requested": False},
    )
    assert requirements["mudata_required"] is False
    assert requirements["default_unpaired"] is False


def test_fragmentomics_plan_drops_unrequested_mudata_packaging_step():
    steps = [
        {"id": "1", "title": "运行 fragmentomics 定量", "goal": "生成 FSD/FSC/RCD/EDM/BPM", "skill": "fragment-analysis"},
        {"id": "2", "title": "封装 MuData 并最终核验交付", "goal": "写出 h5mu", "skill": ""},
    ]
    goal, bounded = _apply_modality_boundaries(
        "完成片段组学定量 → MuData", steps, user_query="完成片段组学定量",
    )

    assert "MuData" not in goal
    assert len(bounded) == 1
    assert bounded[0]["skill"] == "fragment-analysis"
    assert "fragmentomics AnnData" in bounded[0]["goal"]


def test_existing_html_artifact_does_not_request_a_new_report():
    steps = [
        {"id": "1", "title": "miRNA 定量", "goal": "运行 miRDeep2", "skill": "mirdeep2-mirna"},
        {"id": "2", "title": "生成 HTML 报告", "goal": "写 report.html", "skill": ""},
    ]
    deliverables = _resolve_deliverables_policy(
        "基于 trimmed FASTQ 完成 miRNA 定量",
        "已有产物：multiqc_out/multiqc_report.html",
        steps,
    )
    filtered = _apply_deliverables_policy(steps, deliverables=deliverables)

    assert deliverables["html_report_requested"] is False
    assert [step["title"] for step in filtered] == ["miRNA 定量"]


def test_unrequested_html_report_is_removed_from_plan_goal():
    goal = _strip_unrequested_html_report(
        "完成 isomiR 定量与差异分析（limma-voom），并生成 HTML 报告",
        requested=False,
    )

    assert "HTML" not in goal
    assert "isomiR 定量与差异分析" in goal


def test_isomir_quantification_is_ordered_before_de_without_feature_count():
    steps = [
        {
            "id": "1",
            "title": "核查 isomiR 模态前置条件（hairpin BAM / 参考 / 分组）",
            "goal": "核查 hairpin BAM、参考与分组",
            "skill": "",
        },
        {
            "id": "2",
            "title": "isomiR 模态差异分析（unpaired limma-voom）",
            "goal": "在独立 isomir AnnData 上运行差异分析",
            "skill": "differential-analysis",
        },
        {
            "id": "3",
            "title": "用 mirtop 跑 isomiR 定量，独立 isomir_adata",
            "goal": "运行 mirtop 并持久化独立 isomir AnnData",
            "skill": "isomir-quantification",
        },
    ]

    expanded = _expand_plan_prerequisites(steps, extra_context="")
    ordered = _order_de_workflow_steps(expanded)
    titles = [step["title"] for step in ordered]

    assert not any(step.get("skill") == "feature-count" for step in expanded)
    assert titles.index("用 mirtop 跑 isomiR 定量，独立 isomir_adata") < titles.index(
        "isomiR 模态差异分析（unpaired limma-voom）"
    )


def test_existing_result_summary_is_read_only_and_skips_new_pipeline():
    assert _is_read_only_query("总结下片段组学的不同类型特征的数目分布") is True
    assert _is_read_only_query("重新运行片段组学并总结不同类型特征") is False


def test_read_only_summary_bypasses_planner():
    class FakeAgent:
        system_prompt = "system"

        def __init__(self):
            self.events = []

        def _emit_progress(self, callback, event_type, **payload):
            self.events.append(event_type)
            if callback:
                callback({"type": event_type, **payload})

        def run_with_history(self, history, **kwargs):
            return "已有结果"

    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    agent = FakeAgent()
    orchestrator = PlanOrchestrator.__new__(PlanOrchestrator)
    orchestrator.agent = agent
    orchestrator.chat_id = ""
    result = orchestrator.run([
        {"role": "user", "content": "总结下片段组学的不同类型特征的数目分布"},
    ])

    assert result == "已有结果"
    assert "plan_created" not in agent.events


def test_resume_uses_persisted_plan_when_checkpoint_was_lost():
    """A crash must not turn a bare "continue" into a fresh one-step plan."""
    class FakeAgent:
        system_prompt = "system"

        def __init__(self):
            self.events = []
            self.executed_steps = []

        def _emit_progress(self, callback, event_type, **payload):
            self.events.append(event_type)
            if callback:
                callback({"type": event_type, **payload})

        def _load_run_checkpoint(self, chat_id):
            return None

        def _persist_checkpoint(self, payload, chat_id):
            return None

        def _clear_run_checkpoint(self, chat_id):
            return None

        def _check_cancelled(self, cancel_event):
            return None

        def _tool_loop(self, messages, **kwargs):
            self.executed_steps.append(messages[-1]["content"])
            return "HTML report written"

    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    stored_plan = {
        "goal": "对 fragmentomics 做差异分析并生成 HTML 报告",
        "analysis": {"design": "unpaired", "modalities": ["fragmentomics"]},
        "deliverables": {"html_report_requested": True, "has_report_step": True},
        "requirements": {"html_report_requested": True},
        "steps": [
            {
                "id": "1",
                "title": "fragmentomics 差异分析（limma-voom）",
                "goal": "运行 unpaired limma-voom",
                "skill": "differential-analysis",
                "status": "done",
                "result": "DE 已完成",
            },
            {
                "id": "2",
                "title": "汇总差异结果并生成 HTML 报告",
                "goal": "写出 fragmentomics_DE_report.html",
                "skill": "",
                "status": "running",
                "result": "",
            },
        ],
    }
    saved = []
    agent = FakeAgent()
    orchestrator = PlanOrchestrator.__new__(PlanOrchestrator)
    orchestrator.agent = agent
    orchestrator.chat_id = "chat-resume"
    orchestrator._save_plan = lambda chat_id, plan: saved.append(plan)
    orchestrator._load_plan = lambda chat_id: stored_plan
    orchestrator.max_replan_attempts = 0
    orchestrator.skill_overview = ""
    orchestrator._create_plan = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("resume must not create a new plan")
    )

    result = orchestrator.run(
        [{"role": "user", "content": "继续"}],
        resume=True,
    )

    assert result.startswith("HTML report written")
    assert "plan_restored" in agent.events
    assert len(agent.executed_steps) == 1
    assert "fragmentomics_DE_report.html" in agent.executed_steps[0]
    assert saved[-1]["steps"][0]["status"] == "done"
    assert saved[-1]["steps"][1]["status"] == "done"


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
