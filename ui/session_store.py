"""Persist chat records and kernel artifacts under work_space/sessions/{chatId}/."""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from work_space import get_work_space

logger = logging.getLogger(__name__)

_CHAT_ID_RE = re.compile(r"^[a-f0-9-]{8,64}$", re.IGNORECASE)
_SESSIONS_DIR_NAME = "sessions"
_LEGACY_KERNELS_ROOT = Path.home() / ".srnagent" / "chat_kernels"
_STORE_LOCK = threading.Lock()

CHAT_FILE = "chat.json"
KERNEL_STATE_FILE = "kernel_state.json"
INDEX_FILE = "index.json"


class SessionStoreError(ValueError):
    """Invalid session id or payload."""


def sanitize_chat_id(chat_id: str) -> str:
    value = str(chat_id or "").strip()
    if not value or not _CHAT_ID_RE.match(value):
        raise SessionStoreError("无效的 chatId")
    return value


def sessions_root() -> Path:
    root = get_work_space() / _SESSIONS_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def session_dir(chat_id: str) -> Path:
    return sessions_root() / sanitize_chat_id(chat_id)


def ensure_session_dir(chat_id: str) -> Path:
    path = session_dir(chat_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except Exception as exc:
        logger.warning("Failed to read JSON %s: %s", path, exc)
    return None


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_index() -> Dict[str, Any]:
    payload = _read_json(sessions_root() / INDEX_FILE)
    if payload is None:
        return {"activeChatId": None, "updatedAt": None}
    return {
        "activeChatId": payload.get("activeChatId"),
        "updatedAt": payload.get("updatedAt"),
    }


def _save_index(active_chat_id: Optional[str] = None) -> None:
    current = _load_index()
    if active_chat_id is not None:
        current["activeChatId"] = active_chat_id
    current["updatedAt"] = _utc_now_iso()
    _write_json(sessions_root() / INDEX_FILE, current)


def migrate_legacy_session(chat_id: str) -> bool:
    """Copy kernel artifacts from ~/.srnagent/chat_kernels/{id} if needed."""
    chat_id = sanitize_chat_id(chat_id)
    target = session_dir(chat_id)
    if target.exists() and any(target.iterdir()):
        return False
    legacy = _LEGACY_KERNELS_ROOT / chat_id
    if not legacy.exists():
        return False
    try:
        target.mkdir(parents=True, exist_ok=True)
        for item in legacy.iterdir():
            dest = target / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        logger.info("Migrated legacy session %s -> %s", legacy, target)
        return True
    except Exception as exc:
        logger.warning("Failed to migrate legacy session %s: %s", chat_id, exc)
        return False


def save_chat_record(chat_id: str, chat: Dict[str, Any], *, active_chat_id: Optional[str] = None) -> Dict[str, Any]:
    chat_id = sanitize_chat_id(chat_id)
    normalized = {
        "id": chat_id,
        "title": str(chat.get("title") or "New Chat"),
        "messages": chat.get("messages") if isinstance(chat.get("messages"), list) else [],
        "codePanel": chat.get("codePanel") if isinstance(chat.get("codePanel"), list) else [],
        "createdAt": int(chat.get("createdAt") or 0) or int(datetime.now().timestamp() * 1000),
        "updatedAt": int(chat.get("updatedAt") or 0) or int(datetime.now().timestamp() * 1000),
        "savedAt": _utc_now_iso(),
    }
    with _STORE_LOCK:
        ensure_session_dir(chat_id)
        _write_json(session_dir(chat_id) / CHAT_FILE, normalized)
        if active_chat_id is not None:
            _save_index(active_chat_id)
    return normalized


def load_chat_record(chat_id: str) -> Optional[Dict[str, Any]]:
    chat_id = sanitize_chat_id(chat_id)
    migrate_legacy_session(chat_id)
    payload = _read_json(session_dir(chat_id) / CHAT_FILE)
    if payload is None:
        return None
    payload.setdefault("id", chat_id)
    return payload


def list_chat_records() -> List[Dict[str, Any]]:
    root = sessions_root()
    chats: List[Dict[str, Any]] = []
    if not root.exists():
        return chats
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not _CHAT_ID_RE.match(entry.name):
            continue
        chat = _read_json(entry / CHAT_FILE)
        if chat is None:
            continue
        chat.setdefault("id", entry.name)
        chats.append(chat)
    chats.sort(key=lambda item: int(item.get("updatedAt") or 0), reverse=True)
    return chats


def load_chat_store() -> Dict[str, Any]:
    index = _load_index()
    chats = list_chat_records()
    active = index.get("activeChatId")
    if active and not any(chat.get("id") == active for chat in chats):
        active = chats[0]["id"] if chats else None
    return {
        "activeChatId": active,
        "chats": chats,
        "sessionsRoot": str(sessions_root()),
    }


def save_kernel_state(chat_id: str, payload: Dict[str, Any]) -> None:
    chat_id = sanitize_chat_id(chat_id)
    body = {
        "chatId": chat_id,
        "updatedAt": _utc_now_iso(),
        **payload,
    }
    with _STORE_LOCK:
        ensure_session_dir(chat_id)
        _write_json(session_dir(chat_id) / KERNEL_STATE_FILE, body)


def load_kernel_state(chat_id: str) -> Optional[Dict[str, Any]]:
    chat_id = sanitize_chat_id(chat_id)
    return _read_json(session_dir(chat_id) / KERNEL_STATE_FILE)


def delete_session(chat_id: str) -> bool:
    chat_id = sanitize_chat_id(chat_id)
    target = session_dir(chat_id)
    if not target.exists():
        return False
    with _STORE_LOCK:
        shutil.rmtree(target, ignore_errors=True)
        index = _load_index()
        if index.get("activeChatId") == chat_id:
            remaining = list_chat_records()
            _save_index(remaining[0]["id"] if remaining else None)
    return True


def load_replay_chunks(chat_id: str) -> List[str]:
    chat_id = sanitize_chat_id(chat_id)
    replay = session_dir(chat_id) / "replay.py"
    if not replay.exists():
        return []
    try:
        text = replay.read_text(encoding="utf-8")
    except Exception as exc:
        logger.debug("Failed to read replay %s: %s", replay, exc)
        return []
    marker = "# --- replay chunk ---"
    chunks: List[str] = []
    for part in text.split(marker):
        snippet = str(part or "").strip()
        if snippet:
            chunks.append(snippet)
    return chunks


def session_artifacts(chat_id: str) -> Dict[str, Any]:
    chat_id = sanitize_chat_id(chat_id)
    directory = session_dir(chat_id)
    if not directory.exists():
        return {"chatId": chat_id, "exists": False, "files": []}
    files = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name):
        if not path.is_file():
            continue
        files.append(
            {
                "name": path.name,
                "sizeBytes": path.stat().st_size,
            }
        )
    return {
        "chatId": chat_id,
        "exists": True,
        "path": str(directory),
        "files": files,
    }
