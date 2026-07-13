"""Per-chat live event bus — fan-out Agent SSE events to multiple browser clients."""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Iterator, List, Optional

from session_store import sanitize_chat_id

_MAX_BUFFER = 500
_SUBSCRIBER_QUEUE_SIZE = 512
_HEARTBEAT_SEC = 3.0
_TERMINAL_TYPES = frozenset({"done", "cancelled", "error", "stream_end"})
_PROGRESS_TYPES = frozenset({"code_execution_progress"})

_LOCK = threading.RLock()
_BUSES: Dict[str, "_ChatLiveBus"] = {}


class _ChatLiveBus:
    def __init__(self, chat_id: str, run_id: str) -> None:
        self.chat_id = chat_id
        self.run_id = run_id
        self.seq = 0
        self.buffer: Deque[Dict[str, Any]] = deque(maxlen=_MAX_BUFFER)
        self.subscribers: List[queue.Queue] = []
        self.closed = False
        self.updated_at = time.time()
        self._lock = threading.Lock()

    def publish(self, event: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(event or {})
        with self._lock:
            if self.closed:
                return payload
            self.seq += 1
            payload["_seq"] = self.seq
            payload.setdefault("chatId", self.chat_id)
            payload.setdefault("runId", self.run_id)
            self._append_buffer(payload)
            self.updated_at = time.time()
            dead: List[queue.Queue] = []
            for q in self.subscribers:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(payload)
                    except queue.Full:
                        dead.append(q)
            if dead:
                self.subscribers = [q for q in self.subscribers if q not in dead]
        return payload

    def _append_buffer(self, payload: Dict[str, Any]) -> None:
        event_type = str(payload.get("type") or "")
        # Coalesce progress frames in the replay buffer so late joiners get the latest %.
        if event_type in _PROGRESS_TYPES:
            for idx in range(len(self.buffer) - 1, -1, -1):
                if str(self.buffer[idx].get("type") or "") in _PROGRESS_TYPES:
                    self.buffer[idx] = payload
                    return
        if event_type == "heartbeat":
            for idx in range(len(self.buffer) - 1, -1, -1):
                if str(self.buffer[idx].get("type") or "") == "heartbeat":
                    self.buffer[idx] = payload
                    return
        self.buffer.append(payload)

    def snapshot(self, after_seq: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self.buffer if int(item.get("_seq") or 0) > after_seq]

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        with self._lock:
            if self.closed:
                q.put_nowait({"type": "stream_end", "chatId": self.chat_id, "runId": self.run_id})
                return q
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self.subscribers = [item for item in self.subscribers if item is not q]

    def close(self, final_event: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            if self.closed:
                return
            if final_event is not None:
                self.seq += 1
                payload = dict(final_event)
                payload["_seq"] = self.seq
                payload.setdefault("chatId", self.chat_id)
                payload.setdefault("runId", self.run_id)
                self.buffer.append(payload)
                for q in self.subscribers:
                    try:
                        q.put_nowait(payload)
                    except queue.Full:
                        pass
            end_event = {
                "type": "stream_end",
                "chatId": self.chat_id,
                "runId": self.run_id,
                "_seq": self.seq + 1,
            }
            self.seq += 1
            self.buffer.append(end_event)
            for q in list(self.subscribers):
                try:
                    q.put_nowait(end_event)
                except queue.Full:
                    pass
            self.closed = True
            self.subscribers.clear()


def start_live_bus(chat_id: str, run_id: str) -> None:
    chat_id = sanitize_chat_id(chat_id)
    run_id = str(run_id or "").strip()
    if not chat_id or not run_id:
        return
    with _LOCK:
        old = _BUSES.get(chat_id)
        if old is not None and old.run_id != run_id and not old.closed:
            old.close({"type": "status", "message": "会话已开始新的 Agent 运行"})
        bus = _ChatLiveBus(chat_id, run_id)
        _BUSES[chat_id] = bus


def publish_live_event(chat_id: str, event: Dict[str, Any]) -> None:
    chat_id = sanitize_chat_id(chat_id)
    if not chat_id or not event:
        return
    with _LOCK:
        bus = _BUSES.get(chat_id)
    if bus is None or bus.closed:
        return
    bus.publish(event)
    if str(event.get("type") or "") in _TERMINAL_TYPES:
        close_live_bus(chat_id, run_id=bus.run_id, final_event=None)


def close_live_bus(
    chat_id: str,
    *,
    run_id: Optional[str] = None,
    final_event: Optional[Dict[str, Any]] = None,
) -> None:
    chat_id = sanitize_chat_id(chat_id)
    if not chat_id:
        return
    with _LOCK:
        bus = _BUSES.get(chat_id)
        if bus is None:
            return
        if run_id and bus.run_id != run_id:
            return
        bus.close(final_event=final_event)
        # Keep closed bus briefly so late joiners can still replay buffer.
        # A later start_live_bus replaces it.


def get_live_run_id(chat_id: str) -> str:
    chat_id = sanitize_chat_id(chat_id)
    with _LOCK:
        bus = _BUSES.get(chat_id)
        if bus is None or bus.closed:
            return ""
        return bus.run_id


def has_live_bus(chat_id: str) -> bool:
    chat_id = sanitize_chat_id(chat_id)
    with _LOCK:
        bus = _BUSES.get(chat_id)
        return bool(bus and not bus.closed)


def iter_live_events(chat_id: str, after_seq: int = 0) -> Iterator[Dict[str, Any]]:
    """SSE iterator for secondary clients. Disconnect must NOT cancel the agent run."""
    chat_id = sanitize_chat_id(chat_id)
    if not chat_id:
        yield {"type": "error", "message": "chatId 不能为空"}
        return

    with _LOCK:
        bus = _BUSES.get(chat_id)

    if bus is None:
        yield {
            "type": "status",
            "message": "当前会话没有正在广播的实时事件",
            "chatId": chat_id,
            "live": False,
        }
        yield {"type": "stream_end", "chatId": chat_id}
        return

    yield {
        "type": "live_joined",
        "chatId": chat_id,
        "runId": bus.run_id,
        "afterSeq": after_seq,
        "message": "已加入实时同步",
    }

    for item in bus.snapshot(after_seq=after_seq):
        yield item
        if str(item.get("type") or "") in _TERMINAL_TYPES | {"stream_end"}:
            return

    if bus.closed:
        yield {"type": "stream_end", "chatId": chat_id, "runId": bus.run_id}
        return

    q = bus.subscribe()
    try:
        while True:
            try:
                item = q.get(timeout=_HEARTBEAT_SEC)
            except queue.Empty:
                if bus.closed:
                    yield {"type": "stream_end", "chatId": chat_id, "runId": bus.run_id}
                    return
                yield {
                    "type": "heartbeat",
                    "chatId": chat_id,
                    "runId": bus.run_id,
                    "hasActiveRun": True,
                    "message": "实时同步中…",
                }
                continue

            yield item
            event_type = str(item.get("type") or "")
            if event_type in _TERMINAL_TYPES or event_type == "stream_end":
                return
    finally:
        bus.unsubscribe(q)
