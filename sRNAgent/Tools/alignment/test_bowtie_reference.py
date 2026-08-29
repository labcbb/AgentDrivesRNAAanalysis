import importlib
import json

from sRNAgent.Tools.alignment.bowtie import bowtie_build, normalize_rna_fasta_to_dna

bowtie_module = importlib.import_module("sRNAgent.Tools.alignment.bowtie")


def test_normalize_rna_fasta_to_dna_keeps_source_and_coordinates(tmp_path):
    source = tmp_path / "hairpin_hsa.fa"
    source.write_text(">hsa-mir-21\nUGUCGGGUAGCUU\n", encoding="utf-8")

    derived, normalized = normalize_rna_fasta_to_dna(source)

    assert normalized is True
    assert derived == tmp_path / "hairpin_hsa.dna.fa"
    assert source.read_text(encoding="utf-8") == ">hsa-mir-21\nUGUCGGGUAGCUU\n"
    assert derived.read_text(encoding="utf-8") == ">hsa-mir-21\nTGTCGGGTAGCTT\n"


def test_normalize_rna_fasta_to_dna_reuses_dna_source(tmp_path):
    source = tmp_path / "genome.fa"
    source.write_text(">chrU_name\nACGTN\n", encoding="utf-8")

    prepared, normalized = normalize_rna_fasta_to_dna(source)

    assert normalized is False
    assert prepared == source
    assert not (tmp_path / "genome.dna.fa").exists()


def test_bowtie_build_records_the_normalized_reference(tmp_path, monkeypatch):
    source = tmp_path / "hairpin.fa"
    source.write_text(">hairpin\nAUGCU\n", encoding="utf-8")
    captured = []
    monkeypatch.setattr(bowtie_module, "run_cli_cmd", lambda command: captured.append(command))

    result = bowtie_build(str(source), str(tmp_path / "hairpin_index"))

    assert captured[0][-2] == str(tmp_path / "hairpin.dna.fa")
    assert result["reference_used"] == str(tmp_path / "hairpin.dna.fa")
    assert result["source_references"] == [str(source)]
    assert result["rna_to_dna_normalized"] is True
    manifest = json.loads((tmp_path / "hairpin_index.reference.json").read_text(encoding="utf-8"))
    assert manifest["reference_used"] == str(tmp_path / "hairpin.dna.fa")
