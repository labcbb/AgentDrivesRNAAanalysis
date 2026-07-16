"""Persist chat records and kernel artifacts under work_space/sessions/{chatId}/."""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from work_space import get_work_space

logger = logging.getLogger(__name__)

_CHAT_ID_RE = re.compile(r"^[a-f0-9-]{8,64}$", re.IGNORECASE)
_SESSIONS_DIR_NAME = "sessions"
_LEGACY_KERNELS_ROOT = Path.home() / ".srnagent" / "chat_kernels"
# RLock: lease helpers may nest under save_chat_record.
_STORE_LOCK = threading.RLock()

CHAT_FILE = "chat.json"
KERNEL_STATE_FILE = "kernel_state.json"
INDEX_FILE = "index.json"
OPERATOR_LEASE_FILE = "operator_lease.json"

# Soft write lease TTL; renewed while an operator run is alive.
LEASE_TTL_SEC = 180


class SessionStoreError(ValueError):
    """Invalid session id or payload."""


class SessionSaveConflict(Exception):
    """Optimistic concurrency / operator lease rejected a write."""

    def __init__(self, chat: Optional[Dict[str, Any]] = None, lease: Optional[Dict[str, Any]] = None, message: str = ""):
        super().__init__(message or "会话写入冲突")
        self.chat = chat
        self.lease = lease


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


def _lease_path(chat_id: str) -> Path:
    return session_dir(chat_id) / OPERATOR_LEASE_FILE


def _normalize_lease(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    device_id = str(payload.get("deviceId") or "").strip()
    if not device_id:
        return None
    try:
        expires_at = float(payload.get("expiresAt") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    return {
        "deviceId": device_id,
        "runId": str(payload.get("runId") or ""),
        "acquiredAt": payload.get("acquiredAt"),
        "heartbeatAt": payload.get("heartbeatAt"),
        "expiresAt": expires_at,
    }


def is_lease_valid(lease: Optional[Dict[str, Any]]) -> bool:
    normalized = _normalize_lease(lease)
    if not normalized:
        return False
    return time.time() < float(normalized.get("expiresAt") or 0)


def get_operator_lease(chat_id: str) -> Optional[Dict[str, Any]]:
    chat_id = sanitize_chat_id(chat_id)
    with _STORE_LOCK:
        lease = _normalize_lease(_read_json(_lease_path(chat_id)))
        if not is_lease_valid(lease):
            return None
        return lease


def acquire_operator_lease(
    chat_id: str,
    device_id: str,
    *,
    run_id: str = "",
    force: bool = False,
    ttl_sec: int = LEASE_TTL_SEC,
) -> Dict[str, Any]:
    chat_id = sanitize_chat_id(chat_id)
    device_id = str(device_id or "").strip()
    if not device_id:
        raise SessionStoreError("deviceId 不能为空")
    now = time.time()
    expires = now + max(30, int(ttl_sec or LEASE_TTL_SEC))
    with _STORE_LOCK:
        current = _normalize_lease(_read_json(_lease_path(chat_id)))
        if (
            is_lease_valid(current)
            and current
            and current.get("deviceId") != device_id
            and not force
        ):
            raise SessionSaveConflict(
                load_chat_record(chat_id),
                current,
                "该会话正由其他设备操作",
            )
        lease = {
            "deviceId": device_id,
            "runId": str(run_id or ""),
            "acquiredAt": _utc_now_iso(),
            "heartbeatAt": _utc_now_iso(),
            "expiresAt": expires,
        }
        ensure_session_dir(chat_id)
        _write_json(_lease_path(chat_id), lease)
        return lease


def renew_operator_lease(
    chat_id: str,
    device_id: str,
    *,
    run_id: str = "",
    ttl_sec: int = LEASE_TTL_SEC,
) -> Optional[Dict[str, Any]]:
    chat_id = sanitize_chat_id(chat_id)
    device_id = str(device_id or "").strip()
    if not device_id:
        return None
    now = time.time()
    with _STORE_LOCK:
        current = _normalize_lease(_read_json(_lease_path(chat_id)))
        if not current or current.get("deviceId") != device_id:
            return None
        current["heartbeatAt"] = _utc_now_iso()
        current["expiresAt"] = now + max(30, int(ttl_sec or LEASE_TTL_SEC))
        if run_id:
            current["runId"] = str(run_id)
        _write_json(_lease_path(chat_id), current)
        return current


def clear_operator_lease(chat_id: str, device_id: Optional[str] = None) -> bool:
    chat_id = sanitize_chat_id(chat_id)
    with _STORE_LOCK:
        path = _lease_path(chat_id)
        current = _normalize_lease(_read_json(path))
        if device_id and current and current.get("deviceId") != str(device_id).strip():
            return False
        if path.exists():
            try:
                path.unlink()
            except OSError:
                return False
        return True


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


def save_chat_record(
    chat_id: str,
    chat: Dict[str, Any],
    *,
    active_chat_id: Optional[str] = None,
    expected_updated_at: Optional[int] = None,
    device_id: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    chat_id = sanitize_chat_id(chat_id)
    device_id = str(device_id or "").strip() or None
    with _STORE_LOCK:
        existing = _read_json(session_dir(chat_id) / CHAT_FILE)
        lease = _normalize_lease(_read_json(_lease_path(chat_id)))
        if is_lease_valid(lease) and lease and device_id and lease.get("deviceId") != device_id and not force:
            raise SessionSaveConflict(existing, lease, "该会话正由其他设备写入")

        if (
            not force
            and expected_updated_at is not None
            and existing is not None
            and int(existing.get("updatedAt") or 0) != int(expected_updated_at)
        ):
            # Same operator renewing after local edits: allow if they hold the lease.
            same_operator = bool(
                device_id
                and is_lease_valid(lease)
                and lease
                and lease.get("deviceId") == device_id
            )
            if not same_operator:
                raise SessionSaveConflict(existing, lease if is_lease_valid(lease) else None, "会话版本冲突")

        prev_revision = int((existing or {}).get("revision") or 0)
        now_ms = int(datetime.now().timestamp() * 1000)
        normalized = {
            "id": chat_id,
            "title": str(chat.get("title") or "New Chat"),
            "messages": chat.get("messages") if isinstance(chat.get("messages"), list) else [],
            "codePanel": chat.get("codePanel") if isinstance(chat.get("codePanel"), list) else [],
            "createdAt": int(chat.get("createdAt") or 0) or int((existing or {}).get("createdAt") or 0) or now_ms,
            "updatedAt": int(chat.get("updatedAt") or 0) or now_ms,
            "revision": prev_revision + 1,
            "lastWriterDeviceId": device_id or (existing or {}).get("lastWriterDeviceId"),
            "savedAt": _utc_now_iso(),
        }
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
    lease = get_operator_lease(chat_id)
    if lease:
        payload["operatorLease"] = lease
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
        lease = _normalize_lease(_read_json(entry / OPERATOR_LEASE_FILE))
        if is_lease_valid(lease):
            chat["operatorLease"] = lease
        chats.append(chat)
    chats.sort(key=lambda item: int(item.get("updatedAt") or 0), reverse=True)
    return chats


def load_chat_store() -> Dict[str, Any]:
    index = _load_index()
    chats = list_chat_records()
    # Soft hint only — clients must prefer per-device activeChatId.
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


def is_orphan_session(chat_id: str) -> bool:
    """True for empty shell dirs left by New Chat / kernel probes with no real history."""
    try:
        chat_id = sanitize_chat_id(chat_id)
    except SessionStoreError:
        return False
    path = session_dir(chat_id)
    if not path.exists() or not path.is_dir():
        return False
    # Don't purge a live operator lease.
    lease = _normalize_lease(_read_json(path / OPERATOR_LEASE_FILE))
    if is_lease_valid(lease):
        return False
    chat = _read_json(path / CHAT_FILE)
    messages = chat.get("messages") if isinstance(chat, dict) else None
    if isinstance(messages, list) and len(messages) > 0:
        return False
    plan = _read_json(path / "plan.json")
    if isinstance(plan, dict) and isinstance(plan.get("steps"), list) and plan["steps"]:
        return False
    memory = _read_json(path / "session_memory.json")
    if isinstance(memory, dict) and (memory.get("events") or memory.get("entries")):
        return False
    replay = path / "replay.py"
    if replay.exists() and replay.stat().st_size > 0:
        return False
    # Empty dir, chat.json with 0 messages, or kernel-only leftovers → orphan.
    return True


def purge_orphan_sessions() -> List[str]:
    """Remove empty shell session directories. Returns deleted chat ids."""
    root = sessions_root()
    deleted: List[str] = []
    if not root.exists():
        return deleted
    with _STORE_LOCK:
        for entry in list(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if not _CHAT_ID_RE.match(entry.name):
                continue
            try:
                if not is_orphan_session(entry.name):
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                deleted.append(entry.name)
            except Exception as exc:
                logger.warning("Failed to purge orphan session %s: %s", entry.name, exc)
        if deleted:
            index = _load_index()
            active = index.get("activeChatId")
            if active in deleted:
                remaining = list_chat_records()
                _save_index(remaining[0]["id"] if remaining else None)
    return deleted


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
