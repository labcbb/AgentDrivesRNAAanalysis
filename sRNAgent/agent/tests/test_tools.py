"""Tests for skill matching and reply compliance helpers."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import anndata as ad  # noqa: E402
import mudata as md  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import sRNAgent as sa  # noqa: E402
from sRNAgent.agent.llm_client import ChatCompletion  # noqa: E402
from sRNAgent.agent.srn_agent import SRNAgent, _audit_execute_code_policy  # noqa: E402
from sRNAgent.agent.tools import rank_skill_matches, resolve_skill_query, search_skills  # noqa: E402
from sRNAgent.skill_registry import SkillDefinition, SkillMetadata  # noqa: E402


class _FakeSkillRegistry:
    def __init__(self):
        self.skill_metadata = {
            "fastq-qc": SkillMetadata(
                name="FASTQ QC",
                slug="fastq-qc",
                description="质控 small RNA FASTQ，并确认 adapter 与 QC 异常。",
                path=Path("."),
            ),
            "differential-analysis": SkillMetadata(
                name="Differential Analysis",
                slug="differential-analysis",
                description="进行 miRNA 差异分析并确认分组。",
                path=Path("."),
            ),
        }
        self._skills = {
            "fastq-qc": SkillDefinition(
                name="FASTQ QC",
                slug="fastq-qc",
                description="质控",
                path=Path("."),
                body="必须先确认 adapter。",
            ),
            "differential-analysis": SkillDefinition(
                name="Differential Analysis",
                slug="differential-analysis",
                description="差异分析",
                path=Path("."),
                body="必须先确认分组。",
            ),
        }

    def load_full_skill(self, slug: str):
        return self._skills.get(str(slug).lower())


def test_rank_skill_matches_prefers_exact_slug():
    registry = _FakeSkillRegistry()
    matches = rank_skill_matches(registry, "fastq-qc")
    assert matches
    assert matches[0][0].slug == "fastq-qc"


def test_resolve_skill_query_supports_chinese_description_match():
    registry = _FakeSkillRegistry()
    skill = resolve_skill_query(registry, "差异分析 分组")
    assert skill is not None
    assert skill.slug == "differential-analysis"


def test_search_skills_returns_best_skill_body():
    registry = _FakeSkillRegistry()
    text = search_skills(registry, "adapter 质控")
    assert "FASTQ QC" in text
    assert "必须先确认 adapter" in text


class _DummyAgent:
    def __init__(self):
        self.rewrite_calls = 0

    def _emit_progress(self, on_progress, event_type, **payload):
        return None

    def _llm_complete_cancellable(
        self,
        messages,
        *,
        tools=None,
        cancel_event=None,
        on_progress=None,
        enable_thinking=None,
    ):
        self.rewrite_calls += 1
        return ChatCompletion(content="请直接告诉我你希望我下一步继续做什么。")


def test_ensure_user_facing_reply_rewrites_internal_report():
    dummy = _DummyAgent()
    reply = SRNAgent._ensure_user_facing_reply(
        dummy,
        [{"role": "user", "content": "继续分析 miRNA"}],
        "已向用户发送结果，等待用户下一步。",
    )
    assert "等待用户下一步" not in reply
    assert "请直接告诉我" in reply
    assert dummy.rewrite_calls == 1


def test_registered_adata_tool_accepts_mudata_default_srna_mod():
    adata = ad.AnnData(
        X=np.array([[0.0, 3.0], [0.0, 5.0]], dtype=float),
        obs=pd.DataFrame(index=["S1", "S2"]),
        var=pd.DataFrame(index=["gene_low", "gene_high"]),
    )
    mdata = md.MuData({"srna": adata})

    result = sa.diff.filter_low_expression(mdata)

    assert isinstance(result, md.MuData)
    assert result.mod["srna"].n_vars == 1
    assert list(result.mod["srna"].var_names) == ["gene_high"]


def test_registered_adata_tool_accepts_mudata_explicit_mod():
    srna = ad.AnnData(
        X=np.array([[1.0, 2.0], [3.0, 4.0]], dtype=float),
        obs=pd.DataFrame(index=["S1", "S2"]),
        var=pd.DataFrame(index=["a", "b"]),
    )
    fragmentomics = ad.AnnData(
        X=np.array([[10.0, 20.0], [30.0, 40.0]], dtype=float),
        obs=pd.DataFrame(index=["S1", "S2"]),
        var=pd.DataFrame(index=["frag1", "frag2"]),
    )
    mdata = md.MuData({"srna": srna, "fragmentomics": fragmentomics})

    result = sa.quant.normalize_cpm(mdata, mod="fragmentomics")

    assert isinstance(result, md.MuData)
    assert "logcpm" in result.mod["fragmentomics"].layers
    assert "logcpm" not in result.mod["srna"].layers


def test_execute_code_policy_blocks_paired_code_when_design_is_unpaired():
    messages = [
        {
            "role": "system",
            "content": (
                "## Structured analysis policy\n"
                "- analysis.design = unpaired\n"
                "- analysis.paired_feasible = false\n"
            ),
        }
    ]
    arguments = {
        "description": "运行 miRNA 差异分析",
        "code": (
            "design = '~0 + group + patient_id'\n"
            "sa.diff.de_analysis(adata, control_group='Normal')\n"
        ),
    }
    result = _audit_execute_code_policy(messages, arguments)
    assert "POLICY_VIOLATION" in result
    assert "unpaired" in result


def test_execute_code_policy_allows_unpaired_de_code():
    messages = [
        {
            "role": "system",
            "content": (
                "## Structured analysis policy\n"
                "- analysis.design = unpaired\n"
                "- analysis.paired_feasible = false\n"
            ),
        }
    ]
    arguments = {
        "description": "运行 miRNA 差异分析",
        "code": "sa.diff.de_analysis(adata, control_group='Normal')\n",
    }
    result = _audit_execute_code_policy(messages, arguments)
    assert result == ""


def test_execute_code_policy_blocks_report_step_without_html_output():
    messages = [
        {
            "role": "system",
            "content": (
                "## Required deliverables\n"
                "- deliverables.html_report_requested = true\n"
                "## Current subtask (5/5)\n"
                "Title: 汇总差异结果并生成 HTML 报告\n"
                "Goal: 生成 report.html 并写回路径\n"
            ),
        }
    ]
    arguments = {
        "description": "汇总结果",
        "code": "print('summary only')\n",
    }
    result = _audit_execute_code_policy(messages, arguments)
    assert "POLICY_VIOLATION" in result
    assert ".html" in result


def test_execute_code_policy_blocks_fragomics_without_safe_result_handling():
    messages = [
        {
            "role": "system",
            "content": (
                "## High priority user requirements\n"
                "- requirements.mudata_required = true\n"
                "- requirement: 如果已经有小RNA定量，片段组学结果必须放在 MuData 下。\n"
            ),
        }
    ]
    arguments = {
        "description": "运行片段组学",
        "code": (
            "adata = sa.fragment.fragomics(adata, genome_fasta='ref/genome.fa')\n"
            "print(adata.X.shape)\n"
        ),
    }
    result = _audit_execute_code_policy(messages, arguments)
    assert "POLICY_VIOLATION" in result
    assert "MuData" in result


def test_execute_code_policy_respects_default_unpaired_requirement_flag():
    messages = [
        {
            "role": "system",
            "content": (
                "## High priority user requirements\n"
                "- requirements.default_unpaired = true\n"
                "- requirement: 差异分析默认非配对。\n"
            ),
        }
    ]
    arguments = {
        "description": "运行差异分析",
        "code": (
            "design = '~0 + group + patient_id'\n"
            "sa.diff.de_analysis(adata, control_group='normal')\n"
        ),
    }
    result = _audit_execute_code_policy(messages, arguments)
    assert "POLICY_VIOLATION" in result
    assert "unpaired" in result


def test_de_analysis_stores_default_unpaired_design_metadata():
    adata = ad.AnnData(
        X=np.array([[10.0, 0.0], [12.0, 1.0], [2.0, 11.0], [1.0, 10.0]], dtype=float),
        obs=pd.DataFrame({"group": ["tumor", "tumor", "normal", "normal"]}, index=["S1", "S2", "S3", "S4"]),
        var=pd.DataFrame(index=["gene1", "gene2"]),
    )
    adata.layers["counts"] = adata.X.copy()
    with tempfile.TemporaryDirectory() as tmpdir:
        result = sa.diff.de_analysis(adata, group_col="group", control_group="normal", output_dir=tmpdir, force=True)

    params = result.uns["de_params"]
    assert params["design"] == "unpaired"
    assert params["paired"] is False
    assert params["design_formula"] == "~0+group"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
