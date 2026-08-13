import numpy as np
import pandas as pd
from anndata import AnnData
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.Tools.plot import available, generate
from sRNAgent.Tools.plot.differential import volcano
from sRNAgent.Tools.plot.expression import pca
from sRNAgent.Tools.plot.fragmentomics import fragment_profile
from sRNAgent.Tools.plot.target import target_network


def _adata():
    adata = AnnData(
        np.array([[3, 1], [4, 2], [1, 5], [2, 4]], dtype=float),
        obs=pd.DataFrame({"group": ["A", "A", "B", "B"]}),
        var=pd.DataFrame({"rna_type": ["miRNA", "tRNA"]}, index=["miR", "tRF"]),
    )
    adata.layers["logcpm"] = np.log1p(adata.X)
    adata.obsm["X_pca"] = np.array([[-2, 0], [-1, .2], [1, -.2], [2, 0]])
    adata.uns["pca"] = {"variance_ratio": [0.7, 0.2]}
    adata.uns["de_results"] = pd.DataFrame({"log_fc": [2.0, -1.5], "adj_p_value": [.01, .02]}, index=adata.var_names)
    return adata


def test_result_driven_plot_registration_and_generation(tmp_path):
    adata = _adata()
    assert available(adata)["pca"]["available"] is True
    assert available(adata)["fragment_profile"]["available"] is False
    pca(adata, group_col="group", output_dir=str(tmp_path))
    volcano(adata, output_dir=str(tmp_path))
    assert (tmp_path / "expression" / "pca.png").exists()
    assert (tmp_path / "differential" / "volcano.pdf").exists()
    assert {"pca", "volcano"} <= set(adata.uns["plots"])
    report = generate(adata, plots=["pca", "fragment_profile"], group_col="group", output_dir=str(tmp_path))
    assert report["generated"] == ["pca"]
    assert "fragment_profile" in report["skipped"]


def test_fragment_profile_and_target_network_use_existing_results(tmp_path):
    fragment = AnnData(
        np.array([[4, 1, 2], [3, 2, 1], [1, 4, 2], [2, 3, 1]], dtype=float),
        obs=pd.DataFrame({"group": ["A", "A", "B", "B"]}),
        var=pd.DataFrame({"type": ["FSD", "FSD", "BPM_START"]}, index=["FSD::len=20", "FSD::len=21", "BPM::ACTG"]),
    )
    fragment.layers["CPM"] = fragment.X.copy()
    fragment_profile(fragment, feature_type="FSD", group_col="group", output_dir=str(tmp_path))
    assert (tmp_path / "fragmentomics" / "fragment_fsd_profile.png").exists()

    adata = _adata()
    tsv = tmp_path / "targets.tsv"
    pd.DataFrame({"miRNAname": ["hsa-miR-1", "hsa-miR-1", "hsa-miR-2"], "geneName": ["TP53", "EGFR", "TP53"]}).to_csv(tsv, sep="\t", index=False)
    adata.uns["starbase_mirna_targets"] = {"last_run": {"records": [{"miRNA": "hsa-miR-1", "tsv": str(tsv)}]}}
    target_network(adata, output_dir=str(tmp_path))
    assert (tmp_path / "target" / "mirna_target_network.graphml").exists()
