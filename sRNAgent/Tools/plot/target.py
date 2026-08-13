"""Target-enrichment and miRNA-target network visualisation."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd
from anndata import AnnData

from ..._registry import register_function
from .common import PALETTE, apply_style, save_figure


def _edges(adata: AnnData, top_targets: int) -> pd.DataFrame:
    state = adata.uns.get("starbase_mirna_targets") or {}
    records = ((state.get("last_run") or {}).get("records") or []) if isinstance(state, dict) else []
    frames = []
    for record in records:
        path = Path(str(record.get("tsv") or ""))
        if not path.exists():
            continue
        frame = pd.read_csv(path, sep="\t", dtype=str)
        mirna = str(record.get("miRNA") or "")
        if "miRNAname" in frame.columns:
            frame["miRNA"] = frame["miRNAname"].fillna(mirna)
        else:
            frame["miRNA"] = mirna
        gene = "geneName" if "geneName" in frame.columns else "geneID"
        if gene not in frame.columns:
            continue
        frames.append(frame[["miRNA", gene]].rename(columns={gene: "gene"}).dropna())
    if not frames:
        raise KeyError("No readable starBase TSV target records were found; run starbase_mirna_targets first")
    edges = pd.concat(frames, ignore_index=True).drop_duplicates()
    return edges.groupby("miRNA", group_keys=False).head(int(top_targets))


@register_function(aliases=["plot_target_network", "mirna_target_network", "靶标网络图"], category="plot", description="Build a bounded miRNA-target bipartite network from already cached starBase TSV files; does not call the API.", examples=["sa.plot.target_network(adata, top_mirnas=10, top_targets=15)"], requires={"uns": ["starbase_mirna_targets"]}, produces={"uns": ["plots"]})
def target_network(adata: AnnData, *, top_mirnas: int = 10, top_targets: int = 15, output_dir: str = "results/plots") -> AnnData:
    """Render a bounded bipartite network from cached starBase target tables."""
    edges = _edges(adata, top_targets)
    selected = edges["miRNA"].value_counts().head(int(top_mirnas)).index
    edges = edges[edges["miRNA"].isin(selected)]
    graph = nx.from_pandas_edgelist(edges, "miRNA", "gene")
    mirnas = set(edges["miRNA"])
    pos = nx.spring_layout(graph, seed=0, k=1.1 / max(len(graph) ** .5, 1))
    apply_style()
    fig, ax = plt.subplots(figsize=(7.4, 5.6))
    nx.draw_networkx_edges(graph, pos, ax=ax, width=.45, alpha=.32, edge_color="#8A8A8A")
    nx.draw_networkx_nodes(graph, pos, nodelist=list(mirnas), node_color=PALETTE[1], node_size=135, edgecolors="white", linewidths=.55, ax=ax)
    genes = [node for node in graph if node not in mirnas]
    nx.draw_networkx_nodes(graph, pos, nodelist=genes, node_color=PALETTE[4], node_size=46, edgecolors="white", linewidths=.35, ax=ax)
    labels = {node: node for node in mirnas}
    labels.update({node: node for node in sorted(genes, key=lambda n: graph.degree[n], reverse=True)[:20]})
    nx.draw_networkx_labels(graph, pos, labels=labels, font_size=6, ax=ax)
    ax.set(title="miRNA-target network"); ax.axis("off")
    record = save_figure(adata, fig, "mirna_target_network", category="target", output_dir=output_dir, parameters={"top_mirnas": top_mirnas, "top_targets_per_mirna": top_targets}, source="cached starBase TSV files")
    edges.to_csv(Path(record["path_png"]).with_suffix(".edges.tsv"), sep="\t", index=False)
    nx.write_graphml(graph, Path(record["path_png"]).with_suffix(".graphml"))
    return adata


__all__ = ["target_network"]
