"""Tests for the memory pipeline (s09 pattern)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.memory import (  # noqa: E402
    consolidate_memories,
    extract_memories,
    load_all_memories,
    memory_dir,
    select_relevant_memories,
)


def test_load_empty(tmp_path):
    assert load_all_memories(tmp_path) == []


def test_select_empty_returns_empty(tmp_path):
    assert select_relevant_memories("anything", tmp_path) == ""


def test_select_keyword_fallback(tmp_path):
    mdir = memory_dir(tmp_path)
    mdir.mkdir(parents=True)
    (mdir / "001_path.md").write_text("[path] Results saved to results/DE/adata.h5ad\n")
    (mdir / "002_param.md").write_text("[param] adapter: TGGAATTCTCGG\n")
    (mdir / "003_decision.md").write_text("[decision] Use bowtie for alignment\n")

    block = select_relevant_memories("Where are the DE results saved?", tmp_path)
    assert "adata.h5ad" in block
    assert "bowtie" not in block  # not relevant to query


def test_select_returns_relevant_when_few(tmp_path):
    mdir = memory_dir(tmp_path)
    mdir.mkdir(parents=True)
    (mdir / "001.md").write_text("[path] /tmp/foo\n")
    (mdir / "002.md").write_text("[param] bar=1\n")

    block = select_relevant_memories("foo bar", tmp_path)
    assert "/tmp/foo" in block
    assert "bar=1" in block


def test_extract_memories_with_llm(tmp_path):
    calls = []

    def fake_llm(messages, *, system=""):
        calls.append(system)
        return (
            "[path] Results saved to data/raw/fastq/SRP181693\n"
            "[param] adapter: TGGAATTCTCGG\n"
            "[decision] Use bowtie for sRNA alignment\n"
        )

    msgs = [
        {"role": "user", "content": "Download SRP181693 and align with bowtie"},
        {"role": "assistant", "content": "Done. Results in data/raw/fastq/SRP181693"},
    ]
    count = extract_memories(msgs, tmp_path, llm_complete=fake_llm)
    assert count == 3
    loaded = load_all_memories(tmp_path)
    assert len(loaded) == 3
    assert any("SRP181693" in m["content"] for m in loaded)


def test_extract_memories_none(tmp_path):
    def fake_llm(messages, *, system=""):
        return "NONE"

    count = extract_memories([{"role": "user", "content": "hi"}], tmp_path, llm_complete=fake_llm)
    assert count == 0


def test_extract_memories_no_llm(tmp_path):
    count = extract_memories([{"role": "user", "content": "hi"}], tmp_path, llm_complete=None)
    assert count == 0


def test_consolidate_under_threshold(tmp_path):
    mdir = memory_dir(tmp_path)
    mdir.mkdir(parents=True)
    for i in range(5):
        (mdir / f"{i:03d}.md").write_text(f"[path] file{i}\n")
    result = consolidate_memories(tmp_path, llm_complete=None, max_memories=10)
    assert result == 5  # no change


def test_consolidate_over_threshold_fallback(tmp_path):
    mdir = memory_dir(tmp_path)
    mdir.mkdir(parents=True)
    for i in range(40):
        (mdir / f"{i:03d}.md").write_text(f"[path] file{i}\n")
    result = consolidate_memories(tmp_path, llm_complete=None, max_memories=30)
    assert result == 30
    assert len(load_all_memories(tmp_path)) == 30


def test_consolidate_with_llm(tmp_path):
    mdir = memory_dir(tmp_path)
    mdir.mkdir(parents=True)
    for i in range(35):
        (mdir / f"{i:03d}.md").write_text(f"[path] file{i}\n")

    def fake_llm(messages, *, system=""):
        return "[path] consolidated entry 1\n[path] consolidated entry 2\n"

    result = consolidate_memories(tmp_path, llm_complete=fake_llm, max_memories=30)
    assert result == 2
    loaded = load_all_memories(tmp_path)
    assert len(loaded) == 2
    assert "consolidated entry 1" in loaded[0]["content"]
