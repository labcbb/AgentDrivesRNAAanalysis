"""Tests for session memory retention and orphan session detection."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from session_memory import append_work_log, build_session_memory_context, build_workspace_manifest, load_session_memory, record_stream_event, remember_user_query, save_session_memory  # noqa: E402
from session_plan import save_plan  # noqa: E402
from session_plan import normalize_plan, plan_progress_summary  # noqa: E402
from session_store import ensure_session_dir, is_orphan_session, load_chat_record, save_chat_record  # noqa: E402
from work_space import configure_work_space  # noqa: E402


CHAT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_chat_persistence_canonicalizes_replayed_thinking_steps():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        repeated = {
            "kind": "tool",
            "title": "execute_code — 检查输入",
            "body": "",
        }
        plan_old = {"kind": "plan", "title": "计划 (0/2)", "body": "○ A\n○ B"}
        plan_new = {"kind": "plan", "title": "计划 (1/2)", "body": "✓ A\n▶ B"}
        saved = save_chat_record(
            CHAT_ID,
            {
                "messages": [{
                    "role": "assistant",
                    "thinkingSteps": [repeated, repeated, plan_old, plan_new, repeated],
                    "thinkingRoundCount": 999,
                }],
            },
        )
        steps = saved["messages"][0]["thinkingSteps"]
        assert len(steps) == 2
        assert saved["messages"][0]["thinkingRoundCount"] == 1
        assert all(step.get("id") for step in steps)
        loaded = load_chat_record(CHAT_ID)
        assert len(loaded["messages"][0]["thinkingSteps"]) == 2


def test_chat_persistence_counts_legacy_tool_decisions_after_reload():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        run_id = "run-123"
        call_one = "call_111"
        call_two = "call_222"
        saved = save_chat_record(
            CHAT_ID,
            {
                "messages": [{
                    "role": "assistant",
                    "thinkingSteps": [
                        {"kind": "tool", "id": f"execution:{run_id}:{call_one}:tool", "turn": 1},
                        {"kind": "result", "id": f"execution:{run_id}:{call_one}:result", "turn": 1},
                        {"kind": "tool", "id": f"execution:{run_id}:{call_two}:tool", "turn": 1},
                        {"kind": "plan", "id": "current-plan"},
                    ],
                }],
            },
        )
        message = saved["messages"][0]
        assert message["thinkingRoundCount"] == 2
        assert all("roundId" in step for step in message["thinkingSteps"])
        assert message["thinkingSteps"][0]["roundId"] == message["thinkingSteps"][1]["roundId"]


def test_chat_persistence_bounds_oversized_assistant_messages_for_ui():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        save_chat_record(
            CHAT_ID,
            {
                "id": CHAT_ID,
                "title": "large reply",
                "messages": [{"role": "assistant", "content": "x" * 50_000}],
            },
            force=True,
        )

        loaded = load_chat_record(CHAT_ID)

        content = loaded["messages"][0]["content"]
        assert len(content) <= 16_000
        assert "回复已截断" in content


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


def test_execute_code_memory_detail_is_compacted():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        record_stream_event(
            CHAT_ID,
            {
                "type": "tool_result",
                "name": "execute_code",
                "summary": "片段组学统计完成",
                "content": (
                    "DataFrame(10000x20)\n"
                    "random verbose line\n"
                    "jobs=8\n"
                    "saved: results/fragmentomics_raw.tsv\n"
                    "another huge table row\n"
                ),
            },
        )
        memory = load_session_memory(CHAT_ID)
        step = (memory.get("steps") or [])[-1]
        assert "DataFrame(10000x20)" not in str(step.get("detail") or "")
        assert "random verbose line" not in str(step.get("detail") or "")
        assert "jobs=8" in str(step.get("detail") or "")
        assert "fragmentomics_raw.tsv" in str(step.get("detail") or "")


def test_execute_code_memory_keeps_fragmentomics_feature_distribution():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        record_stream_event(
            CHAT_ID,
            {
                "type": "tool_result",
                "name": "execute_code",
                "summary": "片段组学特征统计完成",
                "content": "FSD 10\nFSC 4\nRCD 8\n",
                "fullContent": (
                    "feature_type,count\n"
                    "FSD,10\nFSC,4\nRCD,8\nEDM_5P,12\nBPM_START,6\n"
                ),
            },
        )
        step = (load_session_memory(CHAT_ID).get("steps") or [])[-1]
        detail = str(step.get("detail") or "")
        assert "FSD,10" in detail
        assert "BPM_START,6" in detail


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


def test_session_memory_context_includes_unfinished_plan():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        save_plan(
            CHAT_ID,
            {
                "goal": "完成片段组学分析并写回 work.h5ad",
                "steps": [
                    {"id": "1", "title": "清查输入", "status": "done", "result": "30/30 样本齐备"},
                    {
                        "id": "2",
                        "title": "运行 fragment-analysis",
                        "goal": "基于全基因组 BAM 计算 FSD/FSC/RCD/EDM/BPM",
                        "skill": "fragment-analysis",
                        "status": "running",
                    },
                    {
                        "id": "3",
                        "title": "保存 fragmentomics 结果",
                        "goal": "写回 adata 或 MuData 并落盘",
                        "status": "pending",
                    },
                ],
            },
        )
        context = build_session_memory_context(CHAT_ID)
        assert "当前未完成计划" in context
        assert "完成片段组学分析并写回 work.h5ad" in context
        assert "运行 fragment-analysis" in context
        assert "保存 fragmentomics 结果" in context
        assert "步骤 2/3" in context


def test_session_memory_records_structured_analysis_from_plan_event():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        record_stream_event(
            CHAT_ID,
            {
                "type": "plan_created",
                "message": "计划已生成",
                "plan": {
                    "goal": "做 miRNA 和 fragmentomics 差异分析",
                    "analysis": {
                        "design": "unpaired",
                        "source": "explicit_unpaired",
                        "paired_feasible": False,
                        "modalities": ["srna", "fragmentomics"],
                        "reason": "用户已明确要求非配对，且当前 paired 不可行。",
                    },
                    "steps": [],
                },
            },
        )
        memory = load_session_memory(CHAT_ID)
        analysis = memory.get("analysis") or {}
        assert analysis.get("design") == "unpaired"
        assert analysis.get("paired_feasible") is False
        assert analysis.get("modalities") == ["srna", "fragmentomics"]

        context = build_session_memory_context(CHAT_ID)
        assert "### 高优先级分析设计" in context
        assert "analysis.design = unpaired" in context
        assert "analysis.modalities = [srna, fragmentomics]" in context
        assert "analysis.paired_feasible = false" in context


def test_session_memory_records_html_report_deliverable_from_plan_event():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        record_stream_event(
            CHAT_ID,
            {
                "type": "plan_created",
                "message": "计划已生成",
                "plan": {
                    "goal": "做片段组学差异分析并生成 HTML 报告",
                    "deliverables": {
                        "html_report_requested": True,
                        "has_report_step": True,
                    },
                    "steps": [],
                },
            },
        )
        memory = load_session_memory(CHAT_ID)
        deliverables = memory.get("deliverables") or {}
        assert deliverables.get("html_report_requested") is True
        assert deliverables.get("has_report_step") is True

        context = build_session_memory_context(CHAT_ID)
        assert "### 高优先级交付要求" in context
        assert "deliverables.html_report_requested = true" in context


def test_session_memory_records_high_priority_requirements_from_plan_event():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        record_stream_event(
            CHAT_ID,
            {
                "type": "plan_created",
                "message": "计划已生成",
                "plan": {
                    "goal": "继续片段组学任务",
                    "requirements": {
                        "default_unpaired": True,
                        "html_report_requested": True,
                        "mudata_required": True,
                        "whole_genome_bam_required": True,
                        "items": [
                            "如果已经有小RNA定量，片段组学结果必须放在 MuData 下。",
                            "最后必须生成真实 HTML 报告文件。",
                        ],
                    },
                    "steps": [],
                },
            },
        )
        memory = load_session_memory(CHAT_ID)
        requirements = memory.get("requirements") or {}
        assert requirements.get("mudata_required") is True
        assert requirements.get("whole_genome_bam_required") is True
        assert len(requirements.get("items") or []) == 2

        context = build_session_memory_context(CHAT_ID)
        assert "### 高优先级用户要求" in context
        assert "requirements.mudata_required = true" in context
        assert "requirements.whole_genome_bam_required = true" in context
        assert "片段组学结果必须放在 MuData 下" in context


def test_new_chat_can_inherit_previous_session_handoff_on_continuation_query():
    previous_chat_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    current_chat_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        save_chat_record(
            previous_chat_id,
            {
                "id": previous_chat_id,
                "title": "片段组学任务",
                "messages": [{"role": "user", "content": "做片段组学并放到MuData"}],
                "updatedAt": 1000,
            },
            active_chat_id=previous_chat_id,
            force=True,
        )
        save_chat_record(
            current_chat_id,
            {
                "id": current_chat_id,
                "title": "继续刚才任务",
                "messages": [{"role": "user", "content": "继续前面一个对话"}],
                "updatedAt": 2000,
            },
            active_chat_id=current_chat_id,
            force=True,
        )
        save_plan(
            previous_chat_id,
            {
                "goal": "完成片段组学并写出 h5mu",
                "requirements": {
                    "mudata_required": True,
                    "whole_genome_bam_required": True,
                    "items": [
                        "如果已经有小RNA定量，片段组学结果必须放在 MuData 下。",
                    ],
                },
                "steps": [
                    {"id": "1", "title": "检查输入", "status": "done"},
                    {"id": "2", "title": "运行 fragment-analysis", "status": "running", "goal": "继续片段组学"},
                ],
            },
        )

        context = build_session_memory_context(
            current_chat_id,
            user_query="继续前面一个对话的片段组学任务",
        )
        assert "### 最近相关会话继承" in context
        assert "上一会话目标：完成片段组学并写出 h5mu" in context
        assert "previous.requirements.mudata_required = true" in context
        assert "上一会话仍有未完成计划" in context


def test_remember_user_query_persists_high_priority_requirements_before_plan():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        remember_user_query(
            CHAT_ID,
            "如果已经有小RNA定量，片段组学结果要放在MuData下面，并且最后生成HTML报告；差异分析默认非配对",
        )
        memory = load_session_memory(CHAT_ID)
        requirements = memory.get("requirements") or {}
        assert requirements.get("mudata_required") is True
        assert requirements.get("html_report_requested") is True
        assert requirements.get("default_unpaired") is True
        assert any("MuData" in item or "mudata" in item.lower() for item in (requirements.get("items") or []))


def test_new_request_clears_previous_report_requirement():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        remember_user_query(CHAT_ID, "对 fragmentomics 做差异分析并生成 HTML 报告")
        remember_user_query(
            CHAT_ID,
            "完成 isomiR 的定量和差异分析",
            reset_request_scope=True,
        )

        memory = load_session_memory(CHAT_ID)
        assert memory.get("deliverables") == {}
        assert memory.get("requirements", {}).get("html_report_requested") is not True
        assert memory.get("requirements", {}).get("default_unpaired") is True


def test_cross_session_handoff_prefers_more_relevant_previous_session():
    chat_a = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    chat_b = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    current_chat_id = "99999999-9999-4999-8999-999999999999"
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        save_chat_record(
            chat_a,
            {
                "id": chat_a,
                "title": "miRNA 差异分析",
                "messages": [{"role": "user", "content": "做miRNA DE"}],
                "updatedAt": 1000,
            },
            active_chat_id=chat_a,
            force=True,
        )
        save_chat_record(
            chat_b,
            {
                "id": chat_b,
                "title": "片段组学 MuData 任务",
                "messages": [{"role": "user", "content": "片段组学放在MuData下面"}],
                "updatedAt": 2000,
            },
            active_chat_id=chat_b,
            force=True,
        )
        save_chat_record(
            current_chat_id,
            {
                "id": current_chat_id,
                "title": "继续任务",
                "messages": [{"role": "user", "content": "继续刚才的片段组学"}],
                "updatedAt": 3000,
            },
            active_chat_id=current_chat_id,
            force=True,
        )
        save_plan(chat_a, {"goal": "完成 miRNA 差异分析", "steps": [{"id": "1", "title": "跑 miRNA DE", "status": "running"}]})
        save_plan(
            chat_b,
            {
                "goal": "完成片段组学并写出 h5mu",
                "requirements": {"mudata_required": True, "items": ["片段组学结果必须放在 MuData 下。"]},
                "steps": [{"id": "1", "title": "运行 fragment-analysis", "status": "running"}],
            },
        )

        context = build_session_memory_context(current_chat_id, user_query="继续刚才的片段组学 MuData 任务")
        assert "完成片段组学并写出 h5mu" in context
        assert "片段组学结果必须放在 MuData 下" in context
        assert "完成 miRNA 差异分析" not in context


def test_completed_plan_is_not_presented_as_unfinished_context():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        save_plan(
            CHAT_ID,
            {
                "goal": "完成片段组学定量",
                "steps": [{"id": "1", "title": "运行 fragmentomics", "status": "done"}],
            },
        )
        context = build_session_memory_context(CHAT_ID, user_query="继续在刚才的片段组学结果上做总结")
        assert "当前未完成计划" not in context


def test_normalized_plan_preserves_approval_and_dependency_metadata():
    raw = {
        "goal": "质控",
        "steps": [{
            "id": "trim",
            "title": "确认 adapter",
            "status": "awaiting_approval",
            "depends_on": ["download"],
            "approval": {"id": "confirm-adapter-before-trimming"},
            "reviewed": {"adapter_3": "TGGAATTCTCGGGTGCCAAGG"},
        }],
    }

    normalized = normalize_plan(raw)

    step = normalized["steps"][0]
    assert step["status"] == "awaiting_approval"
    assert step["depends_on"] == ["download"]
    assert step["approval"]["id"] == "confirm-adapter-before-trimming"
    assert plan_progress_summary(normalized) == "等待确认：确认 adapter"


def test_session_memory_context_has_a_hard_budget_and_keeps_latest_intent():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        save_session_memory(
            CHAT_ID,
            {
                "steps": [{"summary": "step " + ("x" * 700)} for _ in range(20)],
                "facts": ["fact " + ("y" * 700) for _ in range(20)],
                "artifacts": [f"results/file_{index}.tsv" for index in range(30)],
            },
        )

        context = build_session_memory_context(CHAT_ID, user_query="继续当前任务并只确认接头")

        assert len(context) <= 10_000
        assert "会话记忆已截断" in context
        assert "继续当前任务并只确认接头" in context


def test_workspace_manifest_has_a_file_scan_budget():
    with tempfile.TemporaryDirectory() as tmp:
        configure_work_space(tmp)
        result_dir = Path(tmp) / "results"
        result_dir.mkdir()
        for index in range(5):
            (result_dir / f"result_{index}.tsv").write_text("x", encoding="utf-8")

        manifest = build_workspace_manifest(max_files=10, max_scan_files=3)

        assert "工作区清单扫描在 3 个文件处停止" in manifest


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
