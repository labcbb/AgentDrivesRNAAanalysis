"""Tests for MSigDB symbols GMT discovery and selection."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.Tools.download import msigdb  # noqa: E402


def test_list_msigdb_collections_only_returns_symbols_gmt(monkeypatch):
    page = "\n".join([
        '<a href="c2.cp.kegg_legacy.v2026.1.Hs.symbols.gmt">KEGG</a>',
        '<a href="c5.go.bp.v2026.1.Hs.symbols.gmt">GO BP</a>',
        '<a href="c5.go.bp.v2026.1.Hs.entrez.gmt">Entrez</a>',
        '<a href="msigdb_v2026.1.Hs.db.zip">Database</a>',
    ])
    monkeypatch.setattr(msigdb, "_fetch_directory", lambda url, timeout: page)

    collections = msigdb.list_msigdb_collections("human")

    assert [item["collection"] for item in collections] == ["c2.cp.kegg_legacy", "c5.go.bp"]
    assert all(item["filename"].endswith(".symbols.gmt") for item in collections)


def test_msigdb_default_prefers_kegg_and_mouse_falls_back_to_go_bp(tmp_path, monkeypatch):
    human = [
        {"collection": "c2.cp.kegg_legacy", "filename": "c2.cp.kegg_legacy.v2026.1.Hs.symbols.gmt", "url": "https://example/human-kegg"},
        {"collection": "c5.go.bp", "filename": "c5.go.bp.v2026.1.Hs.symbols.gmt", "url": "https://example/human-go"},
    ]
    mouse = [
        {"collection": "m2.cp.reactome", "filename": "m2.cp.reactome.v2026.1.Mm.symbols.gmt", "url": "https://example/mouse-reactome"},
        {"collection": "m5.go.bp", "filename": "m5.go.bp.v2026.1.Mm.symbols.gmt", "url": "https://example/mouse-go"},
    ]
    monkeypatch.setattr(msigdb, "_available_collections", lambda species, release, timeout: human if species == "human" else mouse)

    downloaded = []
    def fake_download(url, destination, jobs, force):
        downloaded.append((url, Path(destination), jobs, force))
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_text("set\tdesc\tGENE\n", encoding="utf-8")
        return str(destination)

    monkeypatch.setattr(msigdb, "resumable_download", fake_download)
    human_result = msigdb.download_msigdb("human", output_dir=str(tmp_path))
    mouse_result = msigdb.download_msigdb("mouse", output_dir=str(tmp_path))

    assert human_result["collection"] == "c2.cp.kegg_legacy"
    assert human_result["selection"] == "default_kegg"
    assert mouse_result["collection"] == "m5.go.bp"
    assert mouse_result["selection"] == "fallback_go_bp"
    assert all(path.name.endswith(".symbols.gmt") for _, path, _, _ in downloaded)
