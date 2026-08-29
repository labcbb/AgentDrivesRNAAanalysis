"""Tests for local miRanda target prediction and AnnData persistence."""

from pathlib import Path
import importlib

import anndata as ad
import numpy as np
import pandas as pd

from sRNAgent.Tools.target import miranda, parse_miranda_output

miranda_module = importlib.import_module("sRNAgent.Tools.target.miranda")


def _write_fasta(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_parse_miranda_output_adds_utr_gene_metadata(tmp_path):
    utr = _write_fasta(tmp_path / "utr.fa", ">TX1|gene=GENE1\nACGTACGT\n")
    output = tmp_path / "predictions.txt"
    output.write_text(
        "Read Sequence: iso-1\n"
        ">> TX1|gene=GENE1\n"
        "Query: iso-1\n"
        "Scores for this duplex: 151.5\n"
        "Energy: -22.4 kcal/mol\n",
        encoding="utf-8",
    )

    result = parse_miranda_output(output, utr)

    assert len(result) == 1
    assert result.loc[0, "mirna_id"] == "iso-1"
    assert result.loc[0, "transcript_id"] == "TX1"
    assert result.loc[0, "gene_name"] == "GENE1"
    assert result.loc[0, "score"] == 151.5
    assert result.loc[0, "energy"] == -22.4


def test_parse_miranda_33a_tabular_hit_format(tmp_path):
    """The local miRanda 3.3a binary emits a one-``>`` tabular hit row."""
    utr = _write_fasta(tmp_path / "utr.fa", ">ENST1|gene=GENE1\nACGTACGT\n")
    output = tmp_path / "predictions.txt"
    output.write_text(
        "Read Sequence:iso-1|parent=hsa-miR-21-3p (22 nt)\n"
        "Read Sequence:ENST1|gene=GENE1 (8 nt)\n"
        "Performing Scan: iso-1|parent=hsa-miR-21-3p vs ENST1|gene=GENE1\n"
        "   Forward: Score: 151.000000\n"
        "   Energy: -26.910000 kCal/Mol\n"
        "Scores for this hit:\n"
        ">iso-1|parent=hsa-miR-21-3p\tENST1|gene=GENE1\t151.00\t-26.91\t2 22\n"
        "Score for this Scan:\n"
        ">>iso-1|parent=hsa-miR-21-3p\tENST1|gene=GENE1\t151.00\t-26.91\n",
        encoding="utf-8",
    )

    result = parse_miranda_output(output, utr)

    assert len(result) == 1
    assert result.loc[0, ["mirna_id", "target_id", "transcript_id", "gene_name"]].to_dict() == {
        "mirna_id": "iso-1|parent=hsa-miR-21-3p",
        "target_id": "ENST1|gene=GENE1",
        "transcript_id": "ENST1",
        "gene_name": "GENE1",
    }
    assert result.loc[0, "score"] == 151.0
    assert result.loc[0, "energy"] == -26.91


def test_miranda_runs_local_command_and_persists_isomir_results(tmp_path, monkeypatch):
    mirna = _write_fasta(tmp_path / "isomir.fa", ">iso-1\nAUGCUACG\n")
    utr = _write_fasta(tmp_path / "utr.fa", ">TX1|gene=GENE1\nACGTACGT\n")
    adata = ad.AnnData(X=np.array([[1]]), obs=pd.DataFrame(index=["S1"]), var=pd.DataFrame(index=["iso-1"]))
    commands = []

    def fake_run(command):
        commands.append(command)
        output_path = Path(command[command.index("-out") + 1])
        output_path.write_text(
            "Read Sequence: iso-1\n>> TX1|gene=GENE1\n"
            "Scores for this duplex: 151\nEnergy: -21 kcal/mol\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(miranda_module, "run_cli_cmd", fake_run)
    result = miranda(
        adata, str(mirna), str(utr), output_dir=str(tmp_path / "out"), feature_ids=["iso-1"],
    )

    assert commands[0][0] == "miranda"
    assert commands[0][commands[0].index("-sc") + 1] == "140.0"
    assert commands[0][commands[0].index("-en") + 1] == "-20.0"
    state = result.uns["miranda_targets"]
    assert state["prediction_file"].endswith("miranda_predictions.txt")
    assert state["results"].loc[0, "gene_name"] == "GENE1"


def test_miranda_can_preserve_a_named_comparison_result(tmp_path, monkeypatch):
    mirna = _write_fasta(tmp_path / "parent.fa", ">parent-1\nAUGCUACG\n")
    utr = _write_fasta(tmp_path / "utr.fa", ">TX1|gene=GENE1\nACGTACGT\n")
    adata = ad.AnnData(X=np.array([[1]]), obs=pd.DataFrame(index=["S1"]), var=pd.DataFrame(index=["parent-1"]))

    def fake_run(command):
        Path(command[command.index("-out") + 1]).write_text(
            "Read Sequence: parent-1\n>> TX1|gene=GENE1\nScores for this duplex: 151\nEnergy: -21\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(miranda_module, "run_cli_cmd", fake_run)
    result = miranda(
        adata, str(mirna), str(utr), output_dir=str(tmp_path / "out"), result_key="parent_mirna_miranda_targets",
        feature_ids=["parent-1"],
    )

    assert "parent_mirna_miranda_targets" in result.uns
    assert "miranda_targets" not in result.uns
