"""Checkpoint persistence for agent runs (interruption-safe resume).

Each checkpoint is a small JSON file keyed by ``chat_id``. A run writes it
after every completed LLM turn, so a cancelled/crashed run can be resumed by
reloading the message transcript (plus any plan / step context) and continuing
the tool loop. Writes are atomic (tmp file + rename).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")


def sanitize_chat_id(chat_id: str) -> str:
    """Sanitize a chat id for safe use as a filename component."""
    cleaned = _SAFE_ID_RE.sub("_", chat_id).strip("._")
    return cleaned or "default"


def checkpoint_path(base_dir: Path, chat_id: str) -> Path:
    return Path(base_dir) / f"{sanitize_chat_id(chat_id)}.checkpoint.json"


def save_checkpoint(base_dir: Path, chat_id: str, payload: Dict[str, Any]) -> None:
    """Atomically persist a checkpoint payload."""
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    path = checkpoint_path(base, chat_id)
    payload = dict(payload or {})
    payload["chat_id"] = chat_id
    payload["updated_at"] = time.time()
    fd, tmp_path = tempfile.mkstemp(dir=str(base), prefix=".ckpt-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, default=str)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def load_checkpoint(base_dir: Path, chat_id: str) -> Optional[Dict[str, Any]]:
    path = checkpoint_path(base_dir, chat_id)
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def clear_checkpoint(base_dir: Path, chat_id: str) -> None:
    path = checkpoint_path(base_dir, chat_id)
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass
