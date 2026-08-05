"""Tests for session memory retention and orphan session detection."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from session_memory import append_work_log, load_session_memory, record_stream_event  # noqa: E402
from session_store import ensure_session_dir, is_orphan_session  # noqa: E402
from work_space import configure_work_space  # noqa: E402


CHAT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_session_memory_extracts_facts_and_artifacts():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        record_stream_event(
            CHAT_ID,
            {
                "type": "tool_result",
                "name": "execute_code",
                "summary": "FASTQ QC 已完成",
                "content": (
                    "adapter 确认为 TGGAATTCTCGG\n"
                    "jobs=4\n"
                    "结果文件 results/run1/report.html\n"
                ),
            },
        )
        memory = load_session_memory(CHAT_ID)
        assert any("adapter" in item.lower() for item in memory.get("facts") or [])
        assert any("jobs=4" in item.lower() for item in memory.get("facts") or [])
        assert any("report.html" in item for item in memory.get("artifacts") or [])


def test_is_orphan_session_keeps_memory_and_work_log():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        path = ensure_session_dir(CHAT_ID)
        (path / "session_memory.json").write_text(
            json.dumps(
                {
                    "chatId": CHAT_ID,
                    "steps": [{"summary": "已完成下载"}],
                    "artifacts": ["results/a.h5ad"],
                    "facts": ["adapter 确认 TGGAATTCTCGG"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert is_orphan_session(CHAT_ID) is False

        empty_chat = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        empty_path = ensure_session_dir(empty_chat)
        append_work_log(empty_chat, kind="run_done", label="完成", paths=["results/a.h5ad"])
        assert (empty_path / "work_log.jsonl").exists()
        assert is_orphan_session(empty_chat) is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
