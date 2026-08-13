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


def test_html_report_is_result_only_and_copies_assets(tmp_path):
    adata = _adata(tmp_path, "source")
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


def test_multi_modality_report_preserves_separate_ann_data(tmp_path):
    srna = _adata(tmp_path, "srna")
    fragment = _adata(tmp_path, "fragment")
    output = html(srna_adata=srna, fragmentomics_adata=fragment, output_dir=str(tmp_path / "multi"), title="Combined report")
    assert isinstance(output, dict)
    assert (tmp_path / "multi" / "report.html").exists()
    assert srna.uns["report"]["html"] == fragment.uns["report"]["html"]
