"""Download selected MSigDB gene-set GMT files from Broad Institute releases."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Dict, List, Optional
from urllib.request import Request, urlopen

from ..._registry import register_function
from ..reference.util import resumable_download


MSIGDB_BASE_URL = "https://data.broadinstitute.org/gsea-msigdb/msigdb/release"
DEFAULT_RELEASE = "2026.1"
_SPECIES = {
    "human": "Hs",
    "hs": "Hs",
    "homo_sapiens": "Hs",
    "mouse": "Mm",
    "mm": "Mm",
    "mus_musculus": "Mm",
}
_SYMBOLS_GMT_RE = re.compile(r'href=["\']([^"\']+\.symbols\.gmt)["\']', re.I)


def _resolve_species(species: str) -> str:
    value = str(species or "").strip().lower()
    if value not in _SPECIES:
        raise ValueError("species must be human or mouse")
    return _SPECIES[value]


def _directory_url(release: str, code: str) -> str:
    return f"{MSIGDB_BASE_URL}/{release}.{code}/"


def _fetch_directory(url: str, timeout: int = 60) -> str:
    request = Request(url, headers={"User-Agent": "sRNAgent/0.1 MSigDB downloader"})
    with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed HTTPS Broad URL
        return response.read().decode("utf-8", errors="replace")


def _collection_from_filename(filename: str, release: str, code: str) -> str:
    suffix = f".v{release}.{code}.symbols.gmt"
    return filename[:-len(suffix)] if filename.endswith(suffix) else filename.removesuffix(".symbols.gmt")


def _available_collections(species: str, release: str, timeout: int = 60) -> List[Dict[str, str]]:
    code = _resolve_species(species)
    url = _directory_url(release, code)
    page = _fetch_directory(url, timeout=timeout)
    filenames = sorted({html.unescape(match) for match in _SYMBOLS_GMT_RE.findall(page)})
    expected = f".v{release}.{code}.symbols.gmt"
    filenames = [name for name in filenames if name.endswith(expected)]
    return [
        {
            "collection": _collection_from_filename(filename, release, code),
            "filename": filename,
            "url": f"{url}{filename}",
        }
        for filename in filenames
    ]


def _choose_collection(available: List[Dict[str, str]], collection: Optional[str]) -> tuple[Dict[str, str], str]:
    if not available:
        raise FileNotFoundError("No MSigDB symbols.gmt collections were found for this release and species")
    if collection:
        wanted = str(collection).strip().lower().removesuffix(".symbols.gmt")
        matches = [
            item for item in available
            if item["collection"].lower() == wanted or item["filename"].lower().removesuffix(".symbols.gmt") == wanted
        ]
        if not matches:
            choices = ", ".join(item["collection"] for item in available)
            raise ValueError(f"MSigDB collection {collection!r} is unavailable. Available: {choices}")
        return matches[0], "user_selected"

    kegg = [item for item in available if ".kegg_legacy" in item["collection"].lower()]
    kegg += [item for item in available if ".kegg_" in item["collection"].lower() and item not in kegg]
    kegg += [item for item in available if "kegg" in item["collection"].lower() and item not in kegg]
    if kegg:
        return kegg[0], "default_kegg"
    go_bp = [item for item in available if ".go.bp" in item["collection"].lower()]
    if go_bp:
        return go_bp[0], "fallback_go_bp"
    raise FileNotFoundError("Neither a KEGG nor a GO biological-process symbols.gmt collection is available")


@register_function(
    aliases=["list_msigdb_collections", "msigdb_collections", "msigdb可下载数据集"],
    category="download",
    description=(
        "List symbols.gmt MSigDB collections available from the current Broad Institute release page for human or mouse. "
        "Use this before download_msigdb when the user wants a collection other than the default KEGG/GO BP choice."
    ),
    examples=[
        'collections = sa.download.list_msigdb_collections("human")',
        'collections = sa.download.list_msigdb_collections("mouse", release="2026.1")',
    ],
    related=["download.download_msigdb"],
)
def list_msigdb_collections(
    species: str = "human",
    *,
    release: str = DEFAULT_RELEASE,
    timeout: int = 60,
) -> List[Dict[str, str]]:
    """List downloadable ``symbols.gmt`` collections for human or mouse."""
    return _available_collections(species, str(release).strip(), timeout=int(timeout))


@register_function(
    aliases=["download_msigdb", "msigdb", "download_msigdb_gmt", "下载MSigDB"],
    category="download",
    description=(
        "Download exactly one MSigDB symbols.gmt gene-set collection for human or mouse. "
        "The default chooses KEGG (preferring kegg_legacy); when KEGG is absent, it downloads GO biological process. "
        "No Entrez GMT, JSON, ZIP, database, XML, or other MSigDB files are downloaded."
    ),
    examples=[
        'result = sa.download.download_msigdb("human", output_dir="msigdb")',
        'result = sa.download.download_msigdb("mouse", collection="m2.cp.reactome", output_dir="msigdb")',
    ],
    related=["download.list_msigdb_collections"],
)
def download_msigdb(
    species: str = "human",
    output_dir: str = "references/msigdb",
    *,
    collection: Optional[str] = None,
    release: str = DEFAULT_RELEASE,
    jobs: int = 4,
    force: bool = False,
    timeout: int = 60,
) -> Dict[str, str]:
    """Download one MSigDB ``symbols.gmt`` collection.

    With no ``collection``, KEGG is selected where present. Mouse MSigDB 2026.1
    has no KEGG symbols GMT, so its default falls back to GO biological process.
    """
    release = str(release).strip()
    code = _resolve_species(species)
    selected, selected_by = _choose_collection(
        _available_collections(species, release, timeout=int(timeout)), collection,
    )
    destination = Path(output_dir).expanduser().resolve() / selected["filename"]
    gmt = resumable_download(selected["url"], destination, jobs=int(jobs), force=force)
    return {
        "species": "human" if code == "Hs" else "mouse",
        "release": release,
        "collection": selected["collection"],
        "selection": selected_by,
        "gmt": str(gmt),
        "url": selected["url"],
    }
