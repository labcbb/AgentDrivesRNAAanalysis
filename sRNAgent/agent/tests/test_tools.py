"""Tests for skill matching and reply compliance helpers."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import anndata as ad  # noqa: E402
import mudata as md  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import sRNAgent as sa  # noqa: E402
from sRNAgent.agent.llm_client import ChatCompletion  # noqa: E402
from sRNAgent.agent.srn_agent import SRNAgent  # noqa: E402
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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
