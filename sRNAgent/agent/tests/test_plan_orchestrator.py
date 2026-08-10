"""Tests for plan prerequisite expansion."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.plan_orchestrator import (  # noqa: E402
    _apply_analysis_policy,
    _apply_skill_plan_contracts,
    _apply_deliverables_policy,
    _bind_execution_skill_from_registry,
    _apply_modality_boundaries,
    _build_executor_system_prompt,
    _build_approval_request,
    _build_plan_review_system_prompt,
    _approval_value_from_context,
    _format_confirmed_approvals,
    approval_response_is_actionable,
    _build_planner_system_prompt,
    _context_has_counts_for_modality,
    _expand_plan_prerequisites,
    _load_planning_skill_guidance,
    _load_skill_plan_contracts,
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


def test_mirna_pirna_and_trna_share_one_srna_modality():
    steps = [
        {"id": "1", "title": "miRNA 定量", "goal": "运行 miRDeep2", "skill": "mirdeep2-mirna"},
        {"id": "2", "title": "piRNA 定量", "goal": "运行 idxstats", "skill": "samtools_idxstats"},
        {"id": "3", "title": "tRNA 定量", "goal": "运行 tRAX", "skill": "trax_quantification"},
        {"id": "4", "title": "片段组学定量", "goal": "提取 fragmentomics 特征", "skill": "fragment-analysis"},
    ]
    query = "完成 miRNA、piRNA、tRNA 定量和片段组学定量"
    goal, bounded = _apply_modality_boundaries(
        "完成 miRNA、piRNA、tRNA 与片段组学四模态定量", steps, user_query=query,
    )
    analysis = _resolve_analysis_policy(query, "", bounded)

    assert analysis["modalities"] == ["srna", "fragmentomics"]
    assert all("srna AnnData" in step["goal"] for step in bounded[:3])
    assert "四模态" not in goal


def test_execution_binds_mirna_default_from_skill_metadata_not_plan_method():
    from sRNAgent.skill_registry import SkillRegistry

    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()
    step = {
        "title": "miRNA 定量（samtools idxstats）",
        "goal": "运行 samtools idxstats",
        "skill": "samtools_idxstats",
    }

    _bind_execution_skill_from_registry(
        step,
        registry,
        [{"role": "user", "content": "完成 miRNA/piRNA/tRNA 定量"}],
    )

    assert step["skill"] == "mirdeep2-mirna"
    assert "miRDeep2" in step["title"]
    assert step["skillBoundAtExecution"] == "mirdeep2-mirna"


def test_llm_plan_reviewer_corrects_dependency_order_before_execution():
    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    class Completion:
        def __init__(self, content):
            self.content = content

    class FakeAgent:
        system_prompt = "system"
        skill_registry = None

        def __init__(self):
            self.responses = [
                Completion(
                    '{"goal":"analysis","steps":[{"id":"1","title":"quantify","goal":"make counts","skill":""},{"id":"2","title":"report","goal":"summarize DE","skill":""},{"id":"3","title":"DE","goal":"run differential analysis","skill":"differential-analysis"}]}'
                ),
                Completion(
                    '{"goal":"analysis","steps":[{"id":"1","title":"quantify","goal":"make counts","skill":""},{"id":"2","title":"DE","goal":"run differential analysis","skill":"differential-analysis"},{"id":"3","title":"report","goal":"summarize DE","skill":""}]}'
                ),
            ]

        def _llm_complete_cancellable(self, *args, **kwargs):
            return self.responses.pop(0)

    orchestrator = PlanOrchestrator.__new__(PlanOrchestrator)
    orchestrator.agent = FakeAgent()
    orchestrator.skill_overview = ""
    plan = orchestrator._create_plan("quantify, run DE, and write a report", "")

    assert [step["title"] for step in plan["steps"]][-1] == "report"


def test_planner_and_reviewer_describe_user_decisions_as_blocking_approvals():
    planner = _build_planner_system_prompt("skills")
    reviewer = _build_plan_review_system_prompt()

    assert "blocking state" in planner
    assert "review.fields" in planner
    assert "MUST carry an `approval`" in reviewer
    assert "review.fields" in reviewer


def test_plain_approval_cannot_close_a_group_gate_with_unknown_configuration():
    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    plan = {
        "steps": [{
            "id": "2",
            "title": "确认差异分析样本分组",
            "status": "awaiting_approval",
            "approval": {"id": "confirm-groups-before-de", "reviewed": {}},
        }]
    }

    restored = PlanOrchestrator._prepare_restored_plan(plan, approval_response="可以")

    assert restored["steps"][0]["status"] == "awaiting_approval"
    assert restored["steps"][0]["approval"]["lastResponse"] == "可以"


def test_group_gate_hydrates_preflight_values_and_accepts_natural_confirmation():
    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    plan = {
        "steps": [
            {
                "id": "1",
                "title": "读取差异分析分组与设计",
                "status": "done",
                "result": "GROUP_COLUMN: group\nGROUP_COUNTS: Tumor=15, Normal=15\nCONTROL_GROUP: Normal\nDESIGN: unpaired",
            },
            {
                "id": "2",
                "title": "确认差异分析样本分组",
                "status": "awaiting_approval",
                "approval": {"id": "confirm-groups-before-de", "reviewed": {}},
            },
        ]
    }

    restored = PlanOrchestrator._prepare_restored_plan(
        plan, approval_response="可以根据这个分组来，normal是对照组",
    )

    approval = restored["steps"][1]["approval"]
    assert restored["steps"][1]["status"] == "done"
    assert approval["reviewed"] == {
        "group_column": "group",
        "group_counts": "Tumor=15, Normal=15",
        "control_group": "Normal",
        "analysis_design": "unpaired",
    }


def test_isomir_parallel_rebuild_is_preserved_as_a_hard_requirement():
    query = "我已清空 mirtop 内容，从 hairpin 比对开始重建 isomiR；剩余样本八个同时运行。"
    requirements = _resolve_requirements_policy(query, "")

    assert requirements["isomir_rebuild_from_alignment"] is True
    assert requirements["isomir_parallel_workers"] == 8
    prompt = _build_executor_system_prompt(
        "base system",
        step={"title": "并行 isomiR 定量", "goal": "完成 mirtop", "skill": "isomir-quantification"},
        step_index=1,
        step_total=1,
        plan_goal="重建 isomiR",
        requirements=requirements,
    )
    assert "Do not call `sa.quant.mirtop` once over all BAMs" in prompt
    assert "progress: N / TOTAL <sample>" in prompt
    assert "8" in prompt


def test_isomir_trimmed_fastq_rebuild_inserts_collapse_before_alignment():
    from sRNAgent.skill_registry import SkillRegistry

    query = "基于已有的 trimmed FASTQ 重建 hairpin 比对并完成 isomiR 定量（30 样本，jobs=8）"
    steps = [
        {
            "id": "1",
            "title": "重建 hairpin 比对并完成 isomiR 定量",
            "goal": "从 trimmed FASTQ 完成 hairpin 比对和 mirtop 定量",
            "skill": "isomir-quantification",
        }
    ]

    expanded = _expand_plan_prerequisites(
        steps, extra_context="ref/hairpin_hsa.fa", user_query=query,
    )
    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()
    planned = _apply_skill_plan_contracts(
        expanded,
        _load_skill_plan_contracts(registry, query, step_skills=["isomir-quantification"]),
        user_query=query,
        extra_context="ref/hairpin_hsa.fa",
    )
    requirements = _resolve_requirements_policy(query, "")
    prompt = _build_executor_system_prompt(
        "base system",
        step=planned[0],
        step_index=1,
        step_total=len(planned),
        plan_goal=query,
        requirements=requirements,
    )

    assert planned[0]["title"] == "折叠 trimmed FASTQ 供 isomiR hairpin 比对"
    assert "seqcluster_collapse" in planned[0]["goal"]
    assert requirements["isomir_parallel_workers"] == 8
    assert "Do not call `sa.quant.mirtop` once over all BAMs" in prompt


def test_isomir_collapse_is_moved_before_hairpin_bowtie_when_planner_orders_it_late():
    from sRNAgent.skill_registry import SkillRegistry

    query = "基于已有 trimmed FASTQ 重建 hairpin 比对并完成 isomiR 定量"
    steps = [
        {"id": "1", "title": "修复 cutadapt 缺失样本", "goal": "补齐 trimmed FASTQ", "skill": "fastq-qc"},
        {"id": "2", "title": "hairpin Bowtie 比对重建", "goal": "生成 hairpin BAM", "skill": "alignment-srna"},
        {"id": "3", "title": "运行 mirtop", "goal": "isomiR 定量", "skill": "isomir-quantification"},
        {"id": "4", "title": "折叠 trimmed FASTQ", "goal": "seqcluster collapse", "skill": "isomir-quantification"},
    ]

    expanded = _expand_plan_prerequisites(
        steps, extra_context="ref/hairpin_hsa.fa", user_query=query,
    )
    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()
    planned = _apply_skill_plan_contracts(
        expanded,
        _load_skill_plan_contracts(registry, query, step_skills=["isomir-quantification"]),
        user_query=query,
        extra_context="ref/hairpin_hsa.fa",
    )
    titles = [step["title"] for step in planned]

    assert titles == [
        "修复 cutadapt 缺失样本",
        "折叠 trimmed FASTQ 供 isomiR hairpin 比对",
        "hairpin Bowtie 比对重建",
        "运行 mirtop",
    ]


def test_isomir_skill_contract_places_collapse_before_hairpin_bowtie():
    from sRNAgent.skill_registry import SkillRegistry

    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()
    query = "基于已有 trimmed FASTQ 重建 hairpin 比对并完成 isomiR 定量"
    contracts = _load_skill_plan_contracts(registry, query)
    planned = _apply_skill_plan_contracts(
        [
            {"id": "1", "title": "hairpin Bowtie 比对重建", "goal": "生成 BAM", "skill": "alignment-srna"},
            {"id": "2", "title": "运行 mirtop", "goal": "isomiR 定量", "skill": "isomir-quantification"},
        ],
        contracts,
        user_query=query,
        extra_context="",
    )

    assert contracts
    assert [step["title"] for step in planned] == [
        "折叠 trimmed FASTQ 供 isomiR hairpin 比对",
        "hairpin Bowtie 比对重建",
        "运行 mirtop",
    ]


def test_fragment_skill_contract_adds_genome_bam_preflight():
    from sRNAgent.skill_registry import SkillRegistry

    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()
    query = "运行片段组学分析"
    contracts = _load_skill_plan_contracts(registry, query, step_skills=["fragment-analysis"])
    planned = _apply_skill_plan_contracts(
        [{"id": "1", "title": "运行片段组学分析", "goal": "提取 FSD", "skill": "fragment-analysis"}],
        contracts,
        user_query=query,
        extra_context="",
    )

    assert [step["title"] for step in planned] == [
        "核验 fragmentomics 全基因组 BAM 与参考 FASTA",
        "运行片段组学分析",
    ]


def test_fragmentomics_preparation_precedes_genome_alignment():
    from sRNAgent.skill_registry import SkillRegistry

    expanded = _expand_plan_prerequisites(
        [{"id": "1", "title": "运行片段组学分析", "goal": "提取 FSD", "skill": "fragment-analysis"}],
        extra_context="",
        user_query="运行片段组学分析",
    )
    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()
    planned = _apply_skill_plan_contracts(
        expanded,
        _load_skill_plan_contracts(registry, "运行片段组学分析", step_skills=["fragment-analysis"]),
        user_query="运行片段组学分析",
        extra_context="",
    )
    titles = [step["title"] for step in planned]

    assert titles.index("准备参考基因组 FASTA") < titles.index("完成参考基因组比对")


def test_trax_skill_contract_adds_trimming_for_raw_fastq_only():
    from sRNAgent.skill_registry import SkillRegistry

    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()
    query = "用 tRAX 做 tRNA 定量"
    contracts = _load_skill_plan_contracts(registry, query, step_skills=["trax_quantification"])
    planned = _apply_skill_plan_contracts(
        [{"id": "1", "title": "运行 tRAX 定量", "goal": "计数 tRF", "skill": "trax_quantification"}],
        contracts,
        user_query=query,
        extra_context="adata.obs['fastq_path'] = 'raw/S1.fastq.gz'",
    )

    assert [step["title"] for step in planned] == [
        "完成 tRAX 前的 FASTQ 质控与修剪",
        "运行 tRAX 定量",
    ]


def test_approval_contract_inserts_persisted_gate_before_feature_count():
    from sRNAgent.skill_registry import SkillRegistry

    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()
    query = "运行 featureCounts 定量"
    contracts = _load_skill_plan_contracts(registry, query, step_skills=["feature-count"])
    planned = _apply_skill_plan_contracts(
        [{"id": "1", "title": "运行 featureCounts", "goal": "生成表达矩阵", "skill": "feature-count"}],
        contracts,
        user_query=query,
        extra_context="",
    )

    assert [step["title"] for step in planned[:2]] == [
        "读取 featureCounts 已知配置",
        "确认 featureCounts 链特异性",
    ]
    assert "不得运行 featureCounts" in planned[0]["goal"]
    assert "approval" in planned[1]
    assert "链特异性" in planned[1]["approval"]["prompt"]


def test_execution_contract_is_bound_to_mirtop_executor_prompt():
    from sRNAgent.skill_registry import SkillRegistry

    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()
    query = "运行 mirtop 做 isomiR 定量"
    contracts = _load_skill_plan_contracts(registry, query, step_skills=["isomir-quantification"])
    planned = _apply_skill_plan_contracts(
        [{"id": "1", "title": "运行 mirtop", "goal": "isomiR 定量", "skill": "isomir-quantification"}],
        contracts,
        user_query=query,
        extra_context="",
    )
    prompt = _build_executor_system_prompt(
        "base", step=planned[0], step_index=1, step_total=1, plan_goal=query,
    )

    assert planned[0]["executionContracts"]
    assert "Bound execution contract" in prompt
    assert "独立 staging 输出目录" in prompt


def test_approval_response_unblocks_only_the_waiting_plan_step():
    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    plan = {
        "steps": [
            {"id": "1", "title": "确认 adapter", "status": "awaiting_approval", "approval": {"id": "adapter"}},
            {"id": "2", "title": "运行 cutadapt", "status": "pending"},
        ]
    }
    restored = PlanOrchestrator._prepare_restored_plan(plan, approval_response="adapter=TGGA")

    assert restored["steps"][0]["status"] == "done"
    assert restored["steps"][0]["approval"]["response"] == "adapter=TGGA"
    assert restored["steps"][1]["status"] == "pending"


def test_non_confirmation_keeps_approval_gate_closed():
    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    plan = {
        "steps": [
            {"id": "1", "title": "确认 adapter", "status": "awaiting_approval", "approval": {"id": "adapter"}},
            {"id": "2", "title": "运行 cutadapt", "status": "pending"},
        ]
    }
    restored = PlanOrchestrator._prepare_restored_plan(plan, approval_response="不可以，先解释 adapter 的来源")

    assert restored["steps"][0]["status"] == "awaiting_approval"
    assert restored["steps"][0]["approval"]["lastResponse"] == "不可以，先解释 adapter 的来源"
    assert restored["steps"][1]["status"] == "pending"
    assert approval_response_is_actionable("可以") is True
    assert approval_response_is_actionable("adapter_3=ACGTACGTACGT") is True
    assert approval_response_is_actionable("不可以，先解释") is False


def test_approval_request_prints_known_values_and_edit_instructions():
    approved_step = {
        "id": "1",
        "title": "核查既有 cutadapt 配置",
        "status": "done",
        "result": "Existing adapter_3=TGGAATTCTCGGGTGCCAAGG; 30 FASTQ files found.",
    }
    gate = {
        "id": "2",
        "title": "确认 sRNA-seq 3' adapter",
        "status": "awaiting_approval",
        "approval": {
            "prompt": "需要确认 3' adapter。",
            "review": {
                "fields": [
                    {"label": "3' adapter", "source": "adapter", "unknown": "未记录"},
                    {"label": "长度范围", "value": "18-40 nt"},
                ],
                "edit_hint": "回复 adapter_3=新序列 可更改。",
            },
        },
    }
    plan = {"steps": [approved_step, gate]}

    message = _build_approval_request(plan, gate, history=[], extra_context="")

    assert "当前已知信息" in message
    assert "TGGAATTCTCGGGTGCCAAGG" in message
    assert "18-40 nt" in message
    assert "adapter_3=新序列" in message
    assert gate["approval"]["reviewed"]["adapter"] == "TGGAATTCTCGGGTGCCAAGG"


def test_group_approval_renders_actual_group_counts_and_control():
    preflight = {
        "id": "1",
        "title": "读取差异分析分组与设计",
        "status": "done",
        "result": "GROUP_COLUMN: group\nGROUP_COUNTS: CRC=15, Normal=15\nCONTROL_GROUP: Normal\nDESIGN: unpaired",
    }
    gate = {
        "id": "2",
        "title": "确认差异分析样本分组",
        "status": "awaiting_approval",
        "approval": {
            "id": "confirm-groups-before-de",
            "review": {
                "fields": [
                    {"label": "目前分组列", "source": "group_column", "unknown": "未记录"},
                    {"label": "目前各组样本数", "source": "group_counts", "unknown": "未记录"},
                    {"label": "拟使用对照组", "source": "control_group", "unknown": "未记录"},
                    {"label": "统计设计", "source": "analysis_design", "unknown": "未记录"},
                ]
            }
        },
    }
    plan = {"analysis": {"design": "unpaired"}, "steps": [preflight, gate]}

    message = _build_approval_request(plan, gate, history=[], extra_context="")

    assert "目前分组列: group" in message
    assert "目前各组样本数: CRC=15, Normal=15" in message
    assert "拟使用对照组: Normal" in message
    assert "统计设计: unpaired" in message
    assert "摘要：目前分组列为 `group`（CRC=15, Normal=15）；拟使用 `Normal` 作为对照组，统计设计为 `unpaired`。" in message


def test_group_approval_reads_legacy_group_summary_from_completed_check():
    context = 'SAMPLE_COUNT: 30（adata.obs_names: SRR1–SRR30；group: 15 Tumor / 15 Normal）'

    assert _approval_value_from_context("group_column", context, {}) == "group"
    assert _approval_value_from_context("group_counts", context, {}) == "15 Tumor / 15 Normal"


def test_adapter_approval_renders_actual_input_adapter_and_length_values():
    preflight = {
        "id": "1",
        "title": "读取现有 FASTQ 修剪配置",
        "status": "done",
        "result": "INPUT_FASTQ: adata.obs['fastq_path']\nSAMPLE_COUNT: 30\nADAPTER_3: TGGAATTCTCGGGTGCCAAGG\nMIN_LENGTH: 18\nMAX_LENGTH: 40",
    }
    gate = {
        "id": "2",
        "title": "确认 sRNA-seq 3' adapter",
        "status": "awaiting_approval",
        "approval": {
            "id": "confirm-adapter-before-trimming",
            "review": {
                "fields": [
                    {"label": "输入", "source": "input_fastq", "unknown": "未记录"},
                    {"label": "样本数", "source": "sample_count", "unknown": "未记录"},
                    {"label": "adapter", "source": "adapter_3", "unknown": "未记录"},
                    {"label": "最小长度", "source": "min_length", "unknown": "未记录"},
                    {"label": "最大长度", "source": "max_length", "unknown": "未记录"},
                ]
            },
        },
    }
    plan = {"steps": [preflight, gate]}

    message = _build_approval_request(plan, gate, history=[], extra_context="")

    assert "摘要：将对 `adata.obs['fastq_path']` 中的 30 个样本使用 3' adapter `TGGAATTCTCGGGTGCCAAGG`，长度过滤为 `18`-40 nt。" in message


def test_executor_receives_confirmed_approval_and_user_override_rule():
    plan = {
        "steps": [
            {
                "id": "1",
                "title": "确认 3' adapter",
                "status": "done",
                "approval": {
                    "response": "adapter_3=ACGTACGTACGT",
                    "reviewed": {"adapter": "TGGAATTCTCGGGTGCCAAGG"},
                },
            }
        ]
    }
    prompt = _build_executor_system_prompt(
        "base",
        step={"title": "运行 cutadapt", "goal": "修剪", "skill": "fastq-qc"},
        step_index=1,
        step_total=1,
        plan_goal="QC",
        confirmed_approvals=_format_confirmed_approvals(plan),
    )

    assert "adapter_3=ACGTACGTACGT" in prompt
    assert "overrides any earlier detected value" in prompt


def test_planner_receives_matched_skill_body_as_bound_guidance():
    from sRNAgent.skill_registry import SkillDefinition, SkillMetadata

    class Registry:
        skill_metadata = {
            "isomir-quantification": SkillMetadata(
                name="isomiR", slug="isomir-quantification", description="isomiR mirtop", path=Path("."),
            )
        }

        @staticmethod
        def load_full_skill(_slug):
            return SkillDefinition(
                name="isomiR",
                slug="isomir-quantification",
                description="isomiR mirtop",
                path=Path("."),
                body="MUST collapse trimmed FASTQ before Bowtie.",
            )

    guidance = _load_planning_skill_guidance(Registry(), "从 trimmed FASTQ 做 isomiR 定量")
    prompt = _build_planner_system_prompt("isomir-quantification", guidance)

    assert "MUST collapse trimmed FASTQ before Bowtie" in prompt
    assert "source of truth" in prompt


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


def test_counts_evidence_is_scoped_to_its_modality():
    context = "srna_adata_miRNA.h5ad has adata.layers['counts'] for miRNA and tRNA."

    assert _context_has_counts_for_modality(context, "srna") is True
    assert _context_has_counts_for_modality(context, "fragmentomics") is False
    assert _context_has_counts_for_modality(context, "isomir") is False


def test_fragmentomics_quantification_precedes_its_count_validation_and_de():
    steps = [
        {
            "id": "1",
            "title": "fragmentomics 差异分析",
            "goal": "运行 limma-voom 差异分析",
            "skill": "differential-analysis",
        },
        {
            "id": "2",
            "title": "fragmentomics 定量",
            "goal": "提取 FSD/FSC/RCD/EDM/BPM 特征",
            "skill": "fragment-analysis",
        },
    ]

    expanded = _expand_plan_prerequisites(steps, extra_context="")
    ordered = _order_de_workflow_steps(expanded)
    titles = [step["title"] for step in ordered]

    assert titles.index("fragmentomics 定量") < titles.index("核查 fragmentomics 定量矩阵")
    assert titles.index("核查 fragmentomics 定量矩阵") < titles.index("fragmentomics 差异分析")


def test_replan_matches_completed_steps_by_identity_not_shifted_id():
    class Completion:
        content = '{"goal":"new plan","steps":[{"id":"1","title":"isomiR 定量","goal":"run mirtop","skill":"isomir-quantification"},{"id":"2","title":"miRNA 定量","goal":"run miRDeep2","skill":"mirdeep2-mirna"}]}'

    class FakeAgent:
        system_prompt = "system"

        def _emit_progress(self, callback, event_type, **payload):
            return None

        def _llm_complete_cancellable(self, *args, **kwargs):
            return Completion()

    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    orchestrator = PlanOrchestrator.__new__(PlanOrchestrator)
    orchestrator.agent = FakeAgent()
    orchestrator.skill_overview = ""
    old_plan = {
        "goal": "old plan",
        "version": 1,
        "steps": [
            {
                "id": "1",
                "title": "miRNA 定量",
                "goal": "run miRDeep2",
                "skill": "mirdeep2-mirna",
                "status": "done",
                "result": "old completed result",
            }
        ],
    }

    revised = orchestrator._replan(
        old_plan,
        user_query="完成 miRNA 和 isomiR 定量",
        extra_context="",
    )
    by_title = {step["title"]: step for step in revised["steps"]}

    assert by_title["miRNA 定量"]["status"] == "done"
    assert by_title["miRNA 定量"]["result"] == "old completed result"
    assert by_title["isomiR 定量"]["status"] == "pending"


def test_existing_result_summary_is_read_only_and_skips_new_pipeline():
    assert _is_read_only_query("总结下片段组学的不同类型特征的数目分布") is True
    assert _is_read_only_query("重新运行片段组学并总结不同类型特征") is False


def test_english_rewrite_html_request_is_a_required_deliverable():
    query = "Re-style, re-write the HTML, and finalize the fragmentomics artifacts"
    deliverables = _resolve_deliverables_policy(query, "", [])
    planned = _apply_deliverables_policy([], deliverables=deliverables)

    assert deliverables["html_report_requested"] is True
    assert len(planned) == 1
    assert "HTML" in planned[0]["title"]


def test_empty_plan_is_rejected_instead_of_reported_complete():
    class FakeAgent:
        system_prompt = "system"

        def __init__(self):
            self.events = []

        def _emit_progress(self, callback, event_type, **payload):
            self.events.append(event_type)

        def _persist_checkpoint(self, payload, chat_id):
            return None

        def _check_cancelled(self, cancel_event):
            return None

    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    agent = FakeAgent()
    orchestrator = PlanOrchestrator.__new__(PlanOrchestrator)
    orchestrator.agent = agent
    orchestrator.chat_id = "chat-empty-plan"
    orchestrator._save_plan = lambda chat_id, plan: None
    orchestrator._load_plan = None
    orchestrator.max_replan_attempts = 0
    orchestrator.skill_overview = ""
    orchestrator._create_plan = lambda *args, **kwargs: {"goal": "写 HTML", "steps": []}

    try:
        orchestrator.run([{"role": "user", "content": "重写 HTML 报告"}])
        raise AssertionError("expected an empty plan error")
    except ValueError as exc:
        assert "未生成任何可执行步骤" in str(exc)

    assert "plan_failed" in agent.events
    assert "plan_complete" not in agent.events


def test_step_exception_is_persisted_as_failed_not_left_running():
    class FakeAgent:
        system_prompt = "system"

        def __init__(self):
            self.events = []

        def _emit_progress(self, callback, event_type, **payload):
            self.events.append(event_type)

        def _persist_checkpoint(self, payload, chat_id):
            return None

        def _clear_run_checkpoint(self, chat_id):
            return None

        def _check_cancelled(self, cancel_event):
            return None

    from sRNAgent.agent.plan_orchestrator import PlanOrchestrator

    agent = FakeAgent()
    saved = []
    orchestrator = PlanOrchestrator.__new__(PlanOrchestrator)
    orchestrator.agent = agent
    orchestrator.chat_id = "chat-step-error"
    orchestrator._save_plan = lambda chat_id, plan: saved.append(plan)
    orchestrator._load_plan = None
    orchestrator.max_replan_attempts = 0
    orchestrator.skill_overview = ""
    orchestrator._create_plan = lambda *args, **kwargs: {
        "goal": "重写 HTML",
        "steps": [{"id": "1", "title": "写报告", "goal": "写 report.html", "status": "pending"}],
    }
    orchestrator._execute_step = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("writer crashed")
    )
    orchestrator._ensure_user_facing_reply = lambda user_query, text, **kwargs: text

    result = orchestrator.run([{"role": "user", "content": "重写 HTML 报告"}])

    assert "任务未完成" in result
    assert saved[-1]["steps"][0]["status"] == "failed"
    assert "plan_incomplete" in agent.events
    assert "plan_complete" not in agent.events


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
