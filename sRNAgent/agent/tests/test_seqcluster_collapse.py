"""Tests for the seqcluster FASTQ-collapse wrapper."""

from __future__ import annotations

import gzip
import sys
from pathlib import Path

import pandas as pd
from anndata import AnnData

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.Tools.fastq import seqcluster as seqcluster_module  # noqa: E402


def test_seqcluster_collapse_compresses_output_and_removes_log(tmp_path: Path, monkeypatch):
    input_fastq = tmp_path / "input_clean.fastq.gz"
    with gzip.open(input_fastq, "wt", encoding="utf-8") as handle:
        handle.write("@r1\nACGT\n+\nIIII\n")

    monkeypatch.setattr(seqcluster_module.shutil, "which", lambda _: "/usr/bin/seqcluster")

    def fake_run_cli(command):
        out_dir = Path(command[command.index("-o") + 1])
        fastq = Path(command[command.index("-f") + 1])
        raw = out_dir / seqcluster_module._collapse_output_name(fastq)
        raw.write_text("@seq_1_x2\nACGT\n+\nIIII\n", encoding="utf-8")
        (out_dir / "log").mkdir()

    monkeypatch.setattr(seqcluster_module, "run_cli_cmd", fake_run_cli)
    adata = AnnData(obs=pd.DataFrame(index=["S1"]))
    adata.obs["trimmed_path"] = [str(input_fastq)]

    result = seqcluster_module.seqcluster_collapse(adata, output_dir=str(tmp_path / "collapsed"))

    output = Path(result.obs.loc["S1", "collapsed_path"])
    assert output.name == "input_clean_trimmed.fastq.gz"
    assert output.is_file()
    assert not output.with_suffix("").exists()
    assert not (output.parent / "log").exists()
    with gzip.open(output, "rt", encoding="utf-8") as handle:
        assert handle.readline().strip() == "@seq_1_x2"
