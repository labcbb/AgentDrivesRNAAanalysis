"""Per-chat Visualization board preferences (hide / pin figures)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from session_store import _read_json, _write_json, ensure_session_dir, sanitize_chat_id

_PREFS_FILE = "viz_prefs.json"
_LOCK = threading.RLock()
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _prefs_path(chat_id: str) -> Path:
    return ensure_session_dir(chat_id) / _PREFS_FILE


def _normalize_path(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def empty_prefs() -> Dict[str, Any]:
    return {
        "hidden": [],
        "pinned": [],
        "order": [],
        "updatedAt": _utc_now(),
    }


def load_viz_prefs(chat_id: str) -> Dict[str, Any]:
    chat_id = sanitize_chat_id(chat_id)
    with _LOCK:
        raw = _read_json(_prefs_path(chat_id))
    if not isinstance(raw, dict):
        return empty_prefs()
    hidden = [
        _normalize_path(item)
        for item in (raw.get("hidden") or [])
        if _normalize_path(str(item))
    ]
    pinned = [
        _normalize_path(item)
        for item in (raw.get("pinned") or [])
        if _normalize_path(str(item))
    ]
    order = [
        _normalize_path(item)
        for item in (raw.get("order") or [])
        if _normalize_path(str(item))
    ]
    # Keep order, drop dups.
    hidden = list(dict.fromkeys(hidden))
    pinned = list(dict.fromkeys(pinned))
    order = list(dict.fromkeys(order))
    return {
        "hidden": hidden,
        "pinned": pinned,
        "order": order,
        "updatedAt": str(raw.get("updatedAt") or ""),
    }


def save_viz_prefs(chat_id: str, prefs: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = sanitize_chat_id(chat_id)
    payload = {
        "hidden": list(dict.fromkeys(
            _normalize_path(item) for item in (prefs.get("hidden") or []) if _normalize_path(str(item))
        )),
        "pinned": list(dict.fromkeys(
            _normalize_path(item) for item in (prefs.get("pinned") or []) if _normalize_path(str(item))
        )),
        "order": list(dict.fromkeys(
            _normalize_path(item) for item in (prefs.get("order") or []) if _normalize_path(str(item))
        )),
        "updatedAt": _utc_now(),
    }
    with _LOCK:
        _write_json(_prefs_path(chat_id), payload)
    return payload


def hide_figures(chat_id: str, paths: List[str]) -> Dict[str, Any]:
    prefs = load_viz_prefs(chat_id)
    hidden = list(prefs.get("hidden") or [])
    pinned = list(prefs.get("pinned") or [])
    order = list(prefs.get("order") or [])
    for path in resolve_plot_paths(paths):
        if not path:
            continue
        if path not in hidden:
            hidden.append(path)
        if path in pinned:
            pinned = [item for item in pinned if item != path]
        if path in order:
            order = [item for item in order if item != path]
    return save_viz_prefs(chat_id, {"hidden": hidden, "pinned": pinned, "order": order})


def show_figures(chat_id: str, paths: List[str], *, pin: bool = True) -> Dict[str, Any]:
    prefs = load_viz_prefs(chat_id)
    hidden = list(prefs.get("hidden") or [])
    pinned = list(prefs.get("pinned") or [])
    order = list(prefs.get("order") or [])
    for path in resolve_plot_paths(paths):
        if not path:
            continue
        hidden = [item for item in hidden if item != path]
        if pin and path not in pinned:
            pinned.insert(0, path)
        if path in order:
            order = [item for item in order if item != path]
        order.insert(0, path)
    return save_viz_prefs(chat_id, {"hidden": hidden, "pinned": pinned, "order": order})


def clear_hidden_figures(chat_id: str) -> Dict[str, Any]:
    prefs = load_viz_prefs(chat_id)
    return save_viz_prefs(
        chat_id,
        {
            "hidden": [],
            "pinned": prefs.get("pinned") or [],
            "order": prefs.get("order") or [],
        },
    )


def reorder_figures(chat_id: str, paths: List[str]) -> Dict[str, Any]:
    """Persist the user-dragged Visualization order."""
    prefs = load_viz_prefs(chat_id)
    hidden = set(prefs.get("hidden") or [])
    ordered = []
    for path in resolve_plot_paths(paths):
        if not path or path in hidden:
            continue
        if path not in ordered:
            ordered.append(path)
    # Keep any previous ordered paths that were not in this payload (e.g. race).
    for path in prefs.get("order") or []:
        if path not in hidden and path not in ordered:
            ordered.append(path)
    return save_viz_prefs(
        chat_id,
        {
            "hidden": prefs.get("hidden") or [],
            "pinned": prefs.get("pinned") or [],
            "order": ordered,
        },
    )


def apply_viz_prefs(figures: List[Dict[str, Any]], prefs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Filter hidden paths and apply user/agent order."""
    hidden = set(prefs.get("hidden") or [])
    pinned_order = [_normalize_path(item) for item in (prefs.get("pinned") or [])]
    pinned_rank = {path: index for index, path in enumerate(pinned_order)}
    custom_order = [_normalize_path(item) for item in (prefs.get("order") or [])]
    custom_rank = {path: index for index, path in enumerate(custom_order)}

    visible: List[Dict[str, Any]] = []
    for item in figures:
        path = _normalize_path(str(item.get("path") or ""))
        if path and path in hidden:
            continue
        entry = dict(item)
        if path and path in pinned_rank:
            entry["pinned"] = True
        visible.append(entry)

    def sort_key(item: Dict[str, Any]) -> tuple:
        path = _normalize_path(str(item.get("path") or ""))
        if path in custom_rank:
            return (0, custom_rank[path], 0.0)
        if path in pinned_rank:
            return (1, pinned_rank[path], 0.0)
        mtime = item.get("mtime")
        try:
            mtime_val = -float(mtime)
        except (TypeError, ValueError):
            mtime_val = 0.0
        return (2, 0, mtime_val)

    visible.sort(key=sort_key)
    return visible


def resolve_plot_paths(paths: List[str], *, workspace: Optional[Path] = None) -> List[str]:
    """Resolve user/agent path hints to workspace-relative plot paths when possible."""
    from work_space import get_work_space

    root = (workspace or get_work_space()).resolve()
    resolved: List[str] = []
    for raw in paths:
        text = _normalize_path(raw)
        if not text:
            continue
        candidate = Path(text)
        if candidate.is_absolute():
            try:
                text = str(candidate.resolve().relative_to(root)).replace("\\", "/")
            except Exception:
                text = _normalize_path(candidate.name)
        # Allow bare stem / filename matches under results/plots.
        direct = (root / text).resolve()
        if direct.is_file() and (direct == root or root in direct.parents):
            resolved.append(str(direct.relative_to(root)).replace("\\", "/"))
            continue
        matches: List[Path] = []
        search_root = root / "results" / "plots"
        if not search_root.is_dir():
            search_root = root
        needle = Path(text).name.lower()
        stem = Path(text).stem.lower()
        requested_suffix = Path(text).suffix.lower()
        try:
            for item in search_root.rglob("*"):
                if not item.is_file():
                    continue
                if item.suffix.lower() not in _IMAGE_SUFFIXES and item.suffix.lower() != requested_suffix:
                    # Prefer real plot images; allow exact suffix match when user asks for it.
                    if requested_suffix:
                        continue
                    if item.suffix.lower() not in _IMAGE_SUFFIXES:
                        continue
                if item.name.lower() == needle or item.stem.lower() == stem:
                    resolved_item = item.resolve()
                    if resolved_item == root or root in resolved_item.parents:
                        matches.append(resolved_item)
        except OSError:
            matches = []
        if matches:
            def rank(path: Path) -> tuple:
                suffix = path.suffix.lower()
                exact = 0 if path.name.lower() == needle else 1
                fmt = 0 if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"} else 2
                if requested_suffix and suffix == requested_suffix:
                    fmt = -1
                try:
                    mtime = -path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                return (exact, fmt, mtime)

            matches.sort(key=rank)
            resolved.append(str(matches[0].relative_to(root)).replace("\\", "/"))
        else:
            resolved.append(text)
    return list(dict.fromkeys(resolved))
