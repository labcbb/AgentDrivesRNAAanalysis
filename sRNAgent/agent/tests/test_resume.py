"""End-to-end test: interrupted tool-loop writes a checkpoint; resume continues it.

Runnable with pytest or directly (``python -m sRNAgent.agent.tests.test_resume``).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.agent_config import ExecutionConfig
from sRNAgent.agent.llm_client import ChatCompletion, LLMConfig, ToolCall
from sRNAgent.agent.srn_agent import AgentCancelledError, SRNAgent


class _FakeLLM:
    """Scripted LLM. interrupt=True: turn1 → search_functions, turn2 → raise.
    interrupt=False: turn1 → answer immediately."""

    def __init__(self, *, interrupt: bool, answer: str = "final answer"):
        self.interrupt = interrupt
        self.answer = answer
        self.calls = 0
        self.seen_messages: list = []

    def complete(self, messages, tools=None, enable_thinking=None):
        self.calls += 1
        self.seen_messages.append(list(messages))
        if self.interrupt and self.calls >= 2:
            raise AgentCancelledError("cancelled by test")
        if self.interrupt:
            return ChatCompletion(
                content="",
                tool_calls=[
                    ToolCall(id="t1", name="search_functions", arguments={"query": "fastq"})
                ],
            )
        return ChatCompletion(content=self.answer)


def _make_agent(tmp: Path, chat_id: str, llm: _FakeLLM) -> SRNAgent:
    agent = SRNAgent(
        llm_config=LLMConfig(
            api_key="test-key",
            base_url="http://127.0.0.1:1",
            model="test-model",
            protocol="openai-completions",
        ),
        cwd=Path.cwd(),
        max_turns=10,
        execution_config=ExecutionConfig(
            use_notebook=False,
            enable_checkpoint=True,
            checkpoint_dir=tmp,
            max_context_tokens=48000,
        ),
    )
    agent.llm = llm  # replace real client; ChatClient is never invoked
    return agent


def test_interrupted_run_writes_checkpoint_and_resumes():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        chat_id = "chat-resume"

        # --- Run 1: one tool turn, then interrupted ---------------------------
        llm1 = _FakeLLM(interrupt=True)
        agent1 = _make_agent(tmp, chat_id, llm1)
        try:
            agent1.run_with_history(
                [{"role": "user", "content": "定量 miRNA"}],
                chat_id=chat_id,
            )
            raise AssertionError("expected AgentCancelledError")
        except AgentCancelledError:
            pass

        ckpt = agent1._load_run_checkpoint(chat_id)
        assert ckpt is not None, "checkpoint must exist after interruption"
        msgs = ckpt["messages"]
        assert any(m.get("role") == "tool" for m in msgs), "tool result checkpointed"
        assert any(m.get("role") == "assistant" for m in msgs)
        assert llm1.calls == 2

        # --- Run 2: resume from checkpoint, answer immediately ----------------
        llm2 = _FakeLLM(interrupt=False)
        agent2 = _make_agent(tmp, chat_id, llm2)
        text = agent2.run_with_history(
            [{"role": "user", "content": "定量 miRNA"}],
            chat_id=chat_id,
            resume=True,
        )
        assert text.startswith("final answer"), f"unexpected text: {text!r}"
        assert "耗时" in text, "elapsed time should be appended to the answer"
        assert llm2.calls == 1
        assert agent2._load_run_checkpoint(chat_id) is None, "completed runs must not remain resumable"
        resumed = llm2.seen_messages[0]
        resumed_text = "\n".join(str(m.get("content") or "") for m in resumed)
        assert "fastq" in resumed_text or any(
            "search_functions" in str(m.get("tool_calls") or "") for m in resumed
        ), "resumed loop reused checkpointed transcript"


def test_no_checkpoint_when_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        agent = SRNAgent(
            llm_config=LLMConfig(
                api_key="k", base_url="http://127.0.0.1:1", model="m"
            ),
            cwd=Path.cwd(),
            execution_config=ExecutionConfig(
                use_notebook=False,
                enable_checkpoint=False,
                checkpoint_dir=Path(tmp),
            ),
        )
        assert agent._checkpoint_base_dir() is None


def test_code_execution_events_share_the_tool_call_id():
    class _ExecuteThenFinishLLM:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools=None, enable_thinking=None):
            self.calls += 1
            if self.calls == 1:
                return ChatCompletion(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="execute-1",
                            name="execute_code",
                            arguments={"description": "检查输入", "code": "print('ok')"},
                        )
                    ],
                )
            return ChatCompletion(
                content="",
                tool_calls=[ToolCall(id="finish-1", name="finish", arguments={"message": "完成"})],
            )

    with tempfile.TemporaryDirectory() as tmp:
        llm = _ExecuteThenFinishLLM()
        agent = _make_agent(Path(tmp), "chat-code-events", llm)
        agent.dispatch_tool = lambda name, arguments, **kwargs: "ok"  # type: ignore[method-assign]
        events = []
        agent.run_with_history(
            [{"role": "user", "content": "检查输入"}],
            chat_id="chat-code-events",
            on_progress=events.append,
        )

    correlated = [
        event for event in events
        if event.get("type") in {"tool_call", "code_execution_started", "tool_result"}
        and event.get("name", "execute_code") == "execute_code"
    ]
    assert len(correlated) == 3
    assert {event.get("toolCallId") for event in correlated} == {"execute-1"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")
