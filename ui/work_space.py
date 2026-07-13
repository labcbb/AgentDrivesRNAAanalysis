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
