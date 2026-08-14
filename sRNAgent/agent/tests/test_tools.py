"""Tests for skill matching and reply compliance helpers."""
from __future__ import annotations

import sys
import tempfile
import importlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import anndata as ad  # noqa: E402
import mudata as md  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import scipy.sparse as sp  # noqa: E402
import sRNAgent as sa  # noqa: E402
from sRNAgent.agent.llm_client import ChatCompletion  # noqa: E402
from sRNAgent.agent.srn_agent import SRNAgent, _audit_execute_code_policy  # noqa: E402
from sRNAgent.agent.tools import rank_skill_matches, resolve_skill_query, search_skills  # noqa: E402
from sRNAgent.skill_registry import SkillDefinition, SkillMetadata  # noqa: E402
from sRNAgent.Tools.quant.tRAX import store_count_matrix  # noqa: E402


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


class _DefaultQuantSkillRegistry:
    def __init__(self):
        self.skill_metadata = {
            "samtools_idxstats": SkillMetadata(
                name="Default piRNA quantification", slug="samtools_idxstats",
                description="Default piRNA quantification with idxstats", path=Path("."),
            ),
            "mirdeep2-mirna": SkillMetadata(
                name="miRNA quantification", slug="mirdeep2-mirna",
                description="miRNA quantification with miRDeep2", path=Path("."),
            ),
            "feature-count": SkillMetadata(
                name="featureCounts", slug="feature-count",
                description="Count explicitly requested genomic features", path=Path("."),
            ),
        }

    def load_full_skill(self, _slug: str):
        return None


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


def test_default_quantification_skill_ranking_prefers_idxstats_and_mirdeep2():
    registry = _DefaultQuantSkillRegistry()

    assert rank_skill_matches(registry, "piRNA 定量")[0][0].slug == "samtools_idxstats"
    assert rank_skill_matches(registry, "miRNA 定量")[0][0].slug == "mirdeep2-mirna"
    assert rank_skill_matches(registry, "用 featureCounts 做 piRNA 定量")[0][0].slug == "feature-count"


def test_combined_small_rna_query_boosts_both_default_quantification_skills():
    from sRNAgent.skill_registry import SkillRegistry

    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()
    ranked = rank_skill_matches(registry, "完成 miRNA/piRNA/tRNA 定量")
    top_slugs = {metadata.slug for metadata, _score in ranked[:2]}

    assert {"mirdeep2-mirna", "samtools_idxstats"}.issubset(top_slugs)


def test_rank_skill_matches_recognizes_chinese_workflow_aliases():
    from sRNAgent.skill_registry import SkillRegistry

    registry = SkillRegistry(Path(__file__).resolve().parents[2] / "skills")
    registry.load()

    assert rank_skill_matches(registry, "我想先去接头再做质控")[0][0].slug == "fastq-qc"
    assert rank_skill_matches(registry, "查询 miRNA 靶基因")[0][0].slug == "starbase-mirna-targets"


def test_rank_skill_matches_prefers_explicit_method_over_biological_default():
    registry = _DefaultQuantSkillRegistry()

    assert rank_skill_matches(registry, "用 featureCounts 做 miRNA 定量")[0][0].slug == "feature-count"


def test_search_skills_returns_best_skill_body():
    registry = _FakeSkillRegistry()
    text = search_skills(registry, "adapter 质控")
    assert "FASTQ QC" in text
    assert "必须先确认 adapter" in text


def test_fastq_dl_metadata_only_keeps_existing_fastq_paths(tmp_path: Path, monkeypatch):
    module = importlib.import_module("sRNAgent.Tools.fastq.fastq_dl")
    adata = ad.AnnData(obs=pd.DataFrame({"fastq_path": ["existing/S1.fastq.gz"]}, index=["S1"]))

    def fake_run_cli(cmd, **_kwargs):
        out_dir = Path(cmd[cmd.index("--outdir") + 1])
        accession = cmd[cmd.index("--accession") + 1]
        (out_dir / f"fastq-{accession}-run-info.tsv").write_text(
            "run_accession\tfastq_ftp\n"
            f"{accession}\tftp.sra.ebi.ac.uk/example.fastq.gz\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(module, "run_cli_cmd", fake_run_cli)
    result = module.fastq_dl(adata, accessions="SRR000001", output_dir=str(tmp_path), only_metadata=True)

    assert result.obs.loc["S1", "fastq_path"] == "existing/S1.fastq.gz"
    assert "SRR000001" in result.uns["fastq_dl_runs"]


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


def test_store_count_matrix_accepts_sparse_existing_counts():
    adata = ad.AnnData(
        X=np.array([[1.0, 2.0], [3.0, 4.0]], dtype=float),
        obs=pd.DataFrame(index=["S1", "S2"]),
        var=pd.DataFrame({"rna_type": ["miRNA", "miRNA"]}, index=["m1", "m2"]),
    )
    adata.layers["counts"] = adata.X.copy()
    adata.X = sp.csr_matrix(adata.X)
    adata.layers["counts"] = sp.csr_matrix(adata.layers["counts"])

    result = store_count_matrix(
        adata,
        sp.csr_matrix([[5.0], [6.0]]),
        pd.DataFrame(index=["t1"]),
        rna_type="tRNA",
    )
    assert result.shape == (2, 3)
    assert result.layers["counts"].shape == (2, 3)


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


def test_execute_code_policy_requires_fragomics_result_assignment():
    messages = [
        {
            "role": "system",
            "content": (
                "## High priority user requirements\n"
                "- requirements.mudata_required = false\n"
            ),
        }
    ]
    arguments = {
        "description": "运行片段组学",
        "code": (
            "sa.fragment.fragomics(adata, genome_fasta='ref/genome.fa')\n"
        ),
    }
    result = _audit_execute_code_policy(messages, arguments)
    assert "POLICY_VIOLATION" in result
    assert "fragmentomics AnnData" in result


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
