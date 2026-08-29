"""Shared workspace directory for Agent file I/O (configured at serve.py startup)."""
from __future__ import annotations

from pathlib import Path

_WORK_SPACE: Path | None = None


class WorkSpacePathError(ValueError):
    """Raised when a path escapes the configured workspace."""


def configure_work_space(path: Path | str) -> Path:
    global _WORK_SPACE
    resolved = Path(path).expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    _WORK_SPACE = resolved
    return resolved


def get_work_space() -> Path:
    if _WORK_SPACE is not None:
        return _WORK_SPACE
    return Path.cwd()


def resolve_work_space_path(relative_path: str = "") -> Path:
    root = get_work_space()
    target = (root / str(relative_path or "").strip()).resolve()
    if target != root and root not in target.parents:
        raise WorkSpacePathError("路径必须在 work_space 内")
    return target


def list_work_space_files(
    relative_path: str = "",
    *,
    pattern: str = "*",
    recursive: bool = False,
) -> dict:
    """Fast filesystem listing without going through Jupyter."""
    root = get_work_space()
    target = resolve_work_space_path(relative_path)
    if not target.exists():
        return {
            "path": str(target.relative_to(root)) if target != root else ".",
            "exists": False,
            "entries": [],
            "fileCount": 0,
            "totalBytes": 0,
            "totalLabel": "0 B",
        }

    entries = []
    total_bytes = 0

    if recursive:
        iterator = target.rglob(pattern) if pattern not in ("*", "**") else target.rglob("*")
        for item in sorted(iterator, key=lambda p: str(p).lower()):
            if not item.is_file():
                continue
            size = item.stat().st_size
            total_bytes += size
            rel = item.relative_to(root)
            entries.append(
                {
                    "name": item.name,
                    "path": str(rel),
                    "type": "file",
                    "sizeBytes": size,
                    "sizeLabel": _format_bytes(size),
                }
            )
    elif target.is_file():
        size = target.stat().st_size
        entries.append(
            {
                "name": target.name,
                "path": str(target.relative_to(root)),
                "type": "file",
                "sizeBytes": size,
                "sizeLabel": _format_bytes(size),
            }
        )
        total_bytes = size
    else:
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_file(), p.name.lower())):
            if item.is_file() and pattern not in ("*", "**") and not item.match(pattern):
                continue
            rel = item.relative_to(root)
            if item.is_file():
                size = item.stat().st_size
                total_bytes += size
                entries.append(
                    {
                        "name": item.name,
                        "path": str(rel),
                        "type": "file",
                        "sizeBytes": size,
                        "sizeLabel": _format_bytes(size),
                    }
                )
            elif item.is_dir():
                entries.append({"name": item.name, "path": str(rel), "type": "dir"})

    return {
        "path": str(target.relative_to(root)) if target != root else ".",
        "exists": True,
        "entries": entries,
        "fileCount": sum(1 for entry in entries if entry.get("type") == "file"),
        "totalBytes": total_bytes,
        "totalLabel": _format_bytes(total_bytes),
    }


def _format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(max(value, 0))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_PLOT_DIR_CANDIDATES = (
    "results/plots",
    "plots",
    "figures",
    "output/plots",
    "outputs/plots",
    "results",
)
_FORMAT_RANK = {
    ".png": 0,
    ".jpg": 1,
    ".jpeg": 1,
    ".webp": 2,
    ".gif": 3,
    ".svg": 4,
}


def _image_digest(path: Path) -> str:
    import hashlib

    digest = hashlib.sha1()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 256)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def collect_workspace_figures(*, limit: int = 24) -> list[dict]:
    """Collect recent image files under work_space for the Visualization panel.

    Agent pipelines usually ``savefig`` to disk rather than emitting inline
    Jupyter display data, so Visualization must also scan the workspace.
    Dedupes identical bytes and prefers PNG over SVG for the same stem.
    """
    from datetime import datetime
    from urllib.parse import quote

    root = get_work_space().resolve()
    if not root.is_dir():
        return []

    search_roots: list[Path] = []
    seen_roots: set[Path] = set()
    for rel in _PLOT_DIR_CANDIDATES:
        candidate = (root / rel).resolve()
        if not candidate.is_dir():
            continue
        if candidate != root and root not in candidate.parents:
            continue
        # Skip a parent dir if a more specific child plot dir is already included.
        if any(candidate == other or candidate in other.parents for other in seen_roots):
            continue
        # Drop parents that contain an already-selected child.
        seen_roots = {
            other
            for other in seen_roots
            if other == candidate or candidate not in other.parents
        }
        seen_roots.add(candidate)
    search_roots = sorted(seen_roots, key=lambda p: len(p.parts), reverse=True)
    if not search_roots:
        search_roots = [root]

    found: dict[str, Path] = {}
    for base in search_roots:
        try:
            iterator = base.rglob("*")
        except OSError:
            continue
        for item in iterator:
            try:
                if not item.is_file():
                    continue
                if item.suffix.lower() not in _IMAGE_SUFFIXES:
                    continue
                resolved = item.resolve()
                if resolved != root and root not in resolved.parents:
                    continue
                rel = resolved.relative_to(root)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                key = str(rel).replace("\\", "/")
                found[key] = resolved
            except OSError:
                continue

    # Collapse same-folder stem (+ identical bytes across folders).
    best_by_stem: dict[tuple[str, str], tuple[str, Path, float]] = {}
    best_by_digest: dict[str, tuple[str, Path, float]] = {}
    for rel, path in found.items():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        suffix = path.suffix.lower()
        stem_key = (str(path.parent.resolve()), path.stem.lower())
        rank = _FORMAT_RANK.get(suffix, 9)
        prev = best_by_stem.get(stem_key)
        if prev is None or rank < _FORMAT_RANK.get(prev[1].suffix.lower(), 9) or (
            rank == _FORMAT_RANK.get(prev[1].suffix.lower(), 9) and mtime >= prev[2]
        ):
            best_by_stem[stem_key] = (rel, path, mtime)

    for rel, path, mtime in best_by_stem.values():
        digest = _image_digest(path)
        if digest:
            prev = best_by_digest.get(digest)
            if prev is not None:
                prev_rank = _FORMAT_RANK.get(prev[1].suffix.lower(), 9)
                cur_rank = _FORMAT_RANK.get(path.suffix.lower(), 9)
                if cur_rank > prev_rank or (cur_rank == prev_rank and mtime <= prev[2]):
                    continue
            best_by_digest[digest] = (rel, path, mtime)
        else:
            best_by_digest[f"path:{rel}"] = (rel, path, mtime)

    # Same logical plot often lands in nested folders (…/pca.png vs …/expression/pca.png).
    # Keep the newest file per stem for the Visualization gallery.
    best_by_name: dict[str, tuple[str, Path, float]] = {}
    for rel, path, mtime in best_by_digest.values():
        stem = path.stem.lower()
        prev = best_by_name.get(stem)
        if prev is None or mtime >= prev[2]:
            best_by_name[stem] = (rel, path, mtime)

    ranked = sorted(best_by_name.values(), key=lambda item: item[2], reverse=True)[
        : max(1, int(limit))
    ]

    figures: list[dict] = []
    for rel, path, mtime in ranked:
        suffix = path.suffix.lower().lstrip(".")
        fmt = "jpeg" if suffix == "jpg" else suffix
        figures.append(
            {
                "format": fmt or "png",
                "path": rel,
                "url": f"/api/work_space/raw?path={quote(rel, safe='/')}",
                "title": path.stem,
                "timestamp": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
                "mtime": mtime,
                "source": "workspace",
            }
        )
    return figures
