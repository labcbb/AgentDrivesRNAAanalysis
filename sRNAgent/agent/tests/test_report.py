from pathlib import Path
import sys

import numpy as np
import pandas as pd
from anndata import AnnData

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.Tools.report import html


def _adata(tmp_path: Path, name: str) -> AnnData:
    adata = AnnData(
        np.ones((3, 2)),
        obs=pd.DataFrame({"condition": ["control", "treated", "treated"]}),
        var=pd.DataFrame(index=["f1", "f2"]),
    )
    image = tmp_path / f"{name}.png"
    image.write_bytes(b"png-placeholder")
    pdf = tmp_path / f"{name}.pdf"
    pdf.write_bytes(b"pdf-placeholder")
    svg = tmp_path / f"{name}.svg"
    svg.write_text("<svg/>", encoding="utf-8")
    adata.uns["plots"] = {"volcano": {"category": "differential", "path_png": str(image), "path_pdf": str(pdf), "path_svg": str(svg), "source": "de_results", "parameters": {}}}
    adata.uns["de_results"] = pd.DataFrame({"log_fc": [2.0, -1.0], "adj_p_value": [.01, .03]}, index=["f1", "f2"])
    return adata


def test_html_report_is_result_only_and_records_artifact_source_paths(tmp_path):
    adata = _adata(tmp_path, "source")
    fastq = tmp_path / "sample.fastq.gz"
    fastq.write_bytes(b"fastq")
    fasta = tmp_path / "reference.fa"
    fasta.write_text(">chr1\nACGT\n", encoding="utf-8")
    adata.obs["fastq_path"] = [str(fastq)] * adata.n_obs
    adata.uns["genome_fasta"] = str(fasta)
    result = html(adata, output_dir=str(tmp_path / "report"), group_col="condition")
    assert result is adata
    report_dir = tmp_path / "report"
    assert (report_dir / "report.html").exists()
    assert (report_dir / "report_manifest.json").exists()
    assert (report_dir / "tables" / "srna_de_results.csv").exists()
    assert "report" in adata.uns
    text = (report_dir / "report.html").read_text(encoding="utf-8")
    assert "Differential expression" in text
    assert "assets/plots/srna/source.png" in text
    assert str(fastq.resolve()) in text
    assert str(fasta.resolve()) in text
    assert not (report_dir / "assets" / "artifacts").exists()


def test_html_report_includes_candidate_priority_audit_and_artifacts(tmp_path):
    adata = _adata(tmp_path, "priority")
    audit_path = tmp_path / "candidate_priority_audit.csv"
    audit_path.write_text("candidate,eligible\nmiR-1,True\n", encoding="utf-8")
    manifest_path = tmp_path / "candidate_priority_manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    adata.uns["candidate_prioritization"] = {
        "recommended": pd.DataFrame({"candidate": ["miR-1"], "priority_score": [0.8], "eligible": [True]}),
        "audit": pd.DataFrame({"candidate": ["miR-1", "miR-2"], "priority_score": [0.8, 0.3], "eligible": [True, False], "exclusion_reasons": ["", "DE FDR exceeds 0.05"], "evidence_gaps": ["", "replication unavailable"]}),
        "artifacts": {"audit_csv": str(audit_path), "manifest": str(manifest_path)},
    }

    html(adata, output_dir=str(tmp_path / "priority-report"), level="publication")
    report_dir = tmp_path / "priority-report"
    text = (report_dir / "report.html").read_text(encoding="utf-8")

    assert "Candidate prioritization" in text
    assert "Candidate prioritization audit" in text
    assert (report_dir / "tables" / "srna_candidate_prioritization.audit.csv").exists()
    assert str(audit_path.resolve()) in text
    assert not (report_dir / "assets" / "artifacts").exists()


def test_multi_modality_report_preserves_separate_ann_data(tmp_path):
    srna = _adata(tmp_path, "srna")
    fragment = _adata(tmp_path, "fragment")
    output = html(srna_adata=srna, fragmentomics_adata=fragment, output_dir=str(tmp_path / "multi"), title="Combined report")
    assert isinstance(output, dict)
    assert (tmp_path / "multi" / "report.html").exists()
    assert srna.uns["report"]["html"] == fragment.uns["report"]["html"]
