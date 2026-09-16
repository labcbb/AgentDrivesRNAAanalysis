"""Goal gate — independent evaluator on the stop boundary (Claude Code s17).

When the agent tries to finish (calls ``finish`` or produces a no-tool reply),
the goal gate independently evaluates whether the user's original goal has been
achieved.  If not, it injects a ``[Goal still active]`` message and forces
another turn, up to ``max_blocks`` times.

The evaluator is deliberately lightweight: a single LLM call with **no tools**
and a short system prompt, reading only the transcript.  It cannot be prompt-
injected by tool results because it never sees tool definitions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoalDecision:
    action: str  # "achieved" | "block" | "impossible" | "limit"
    reason: str = ""


class GoalGate:
    """Evaluate whether a user goal is achieved at the stop boundary."""

    def __init__(
        self,
        condition: str,
        *,
        max_blocks: int = 8,
    ) -> None:
        self.condition = str(condition or "").strip()
        self.max_blocks = max(1, int(max_blocks))
        self._blocks = 0

    @property
    def active(self) -> bool:
        return bool(self.condition)

    def reset(self) -> None:
        self._blocks = 0

    def evaluate(
        self,
        messages: List[Dict[str, Any]],
        final_text: str,
        llm_complete: Optional[Any] = None,
    ) -> GoalDecision:
        """Return a decision.  ``llm_complete(messages, *, system) -> text``."""
        if not self.active:
            return GoalDecision("achieved")

        if self._blocks >= self.max_blocks:
            return GoalDecision(
                "limit",
                f"Goal gate reached max blocks ({self.max_blocks}).",
            )

        if llm_complete is None:
            # No evaluator available — trust the agent's finish.
            return GoalDecision("achieved")

        system = (
            "You are a goal-completion evaluator. Read the conversation transcript "
            "and decide whether the user's original goal has been fully achieved.\n\n"
            f"User goal: {self.condition}\n\n"
            "Reply with EXACTLY one line:\n"
            "  ACHIEVED — if the goal is fully met\n"
            "  BLOCK — if the goal is not yet met and the agent should continue\n"
            "  IMPOSSIBLE — if the goal cannot be achieved (missing data, contradiction)\n"
            "Do not call tools. Do not explain. One word only."
        )

        transcript = _build_transcript(messages, max_chars=12000)
        try:
            raw = llm_complete(
                [{"role": "user", "content": transcript}],
                system=system,
            )
            verdict = str(raw or "").strip().upper()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Goal gate evaluator failed: %s", exc)
            return GoalDecision("achieved")

        if verdict.startswith("IMPOSSIBLE"):
            return GoalDecision("impossible", "Evaluator: goal impossible.")
        if verdict.startswith("BLOCK"):
            self._blocks += 1
            return GoalDecision("block", f"Goal not yet achieved (block {self._blocks}/{self.max_blocks}).")
        return GoalDecision("achieved")

    def block_message(self, decision: GoalDecision) -> str:
        return (
            "[Goal still active]\n"
            f"Condition: {self.condition}\n"
            f"Evaluator: {decision.reason}\n"
            "Continue working toward the goal. Do not call finish until it is truly done."
        )


def _build_transcript(messages: List[Dict[str, Any]], *, max_chars: int = 12000) -> str:
    """Flatten messages into a compact transcript for the evaluator."""
    lines: List[str] = []
    total = 0
    for msg in messages:
        role = str(msg.get("role") or "").strip()
        if role not in {"user", "assistant", "tool"}:
            continue
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                    elif block.get("type") == "tool_result":
                        parts.append(f"[tool_result: {str(block.get('content',''))[:200]}]")
            content = " ".join(parts)
        else:
            content = str(content or "")
        content = content.strip()
        if not content:
            continue
        line = f"{role}: {content[:800]}"
        if total + len(line) > max_chars:
            line = line[: max(0, max_chars - total)]
        if line:
            lines.append(line)
            total += len(line) + 1
        if total >= max_chars:
            break
    return "\n".join(lines)
