"""Language-agnostic routing between answers and scientific workflows.

The router is intentionally conservative: an analysis workflow is started only
by an explicit operational request. Everything else, including incomplete or
unrecognised prose in any language, is a normal answer and cannot mutate a
persisted plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any, Mapping, Optional


class RouteIntent(StrEnum):
    ANSWER = "answer"
    CONTINUE = "continue"
    AMEND_PLAN = "amend_plan"
    NEW_WORKFLOW = "new_workflow"


@dataclass(frozen=True)
class RouteDecision:
    intent: RouteIntent
    confidence: str
    reason: str
    plan_change: Optional[dict[str, str]] = None


_CONTINUE_RE = re.compile(
    r"(?:^|\s)(?:continue|resume|go\s+on|proceed|carry\s+on)(?:\s|$)|"
    r"(?:继续|接着|继续刚才|继续任务|继续对话|从断的地方|从上次)",
    re.I,
)
_APPROVE_RE = re.compile(
    r"^\s*(?:可以(?:的|啊|呀)?|可(?:以|行)|好(?:的|啊|呀)?|同意|确认|按(?:此|上述)|采用|"
    r"ok(?:ay)?|yes|yep|sure|go\s+ahead)\s*[，,。.!！]?\s*$",
    re.I,
)
_NEW_WORKFLOW_RE = re.compile(
    r"(?:下载|重跑|重算|重写|改写|运行|执行|生成|创建|开始|比对|定量|质控|修剪|预测|富集|建模)|"
    r"\b(?:run|execute|download|rerun|recompute|generate|create|start|align|quantif(?:y|ication)|"
    r"trim|predict|enrich|model)\b",
    re.I,
)
_AMEND_RE = re.compile(
    r"(?:改用|改成|替换为|换成|不要.*(?:用|使用)|使用.*(?:替代|代替)|参数改为|方法改为)|"
    r"\b(?:switch|change|replace|instead\s+of|use\s+.+\s+instead)\b",
    re.I,
)
_METHOD_RE = re.compile(
    r"feature[-_ ]?counts?|mirdeep(?:2)?|mirtop|miranda|starbase|encori|idxstats?|trax|"
    r"cutadapt|bowtie|limma(?:-voom)?",
    re.I,
)
_EXPLICIT_METHOD_USE_RE = re.compile(
    r"(?:使用|用|采用).{0,32}(?:feature[-_ ]?counts?|mirdeep(?:2)?|mirtop|miranda|starbase|"
    r"encori|idxstats?|trax)|"
    r"\b(?:please\s+)?(?:use|apply)\s+(?:feature[-_ ]?counts?|mirdeep(?:2)?|mirtop|miranda|"
    r"starbase|encori|idxstats?|trax)\b|"
    r"\b(?:can|could)\s+you\s+(?:use|apply)\s+(?:feature[-_ ]?counts?|mirdeep(?:2)?|mirtop|"
    r"miranda|starbase|encori|idxstats?|trax)\b",
    re.I,
)


def _has_unfinished_plan(plan: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(plan, Mapping):
        return False
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return False
    return any(
        isinstance(step, Mapping)
        and str(step.get("status") or "pending").strip().lower()
        in {"pending", "running", "failed", "awaiting_approval"}
        for step in steps
    )


class IntentRouter:
    """Classify one user turn without creating or modifying a workflow."""

    @classmethod
    def route(
        cls,
        message: str,
        *,
        active_plan: Optional[Mapping[str, Any]] = None,
        explicit_resume: bool = False,
    ) -> RouteDecision:
        text = str(message or "").strip()
        active = _has_unfinished_plan(active_plan)
        if not text:
            return RouteDecision(RouteIntent.ANSWER, "high", "empty message")

        if explicit_resume:
            return RouteDecision(RouteIntent.CONTINUE, "high", "explicit resume flag")
        if active and _APPROVE_RE.fullmatch(text):
            return RouteDecision(RouteIntent.CONTINUE, "high", "approval of active plan")
        if active and _CONTINUE_RE.search(text):
            return RouteDecision(RouteIntent.CONTINUE, "high", "explicit continuation request")

        if active and _AMEND_RE.search(text):
            method = _METHOD_RE.search(text)
            change = {"method": method.group(0)} if method else None
            return RouteDecision(RouteIntent.AMEND_PLAN, "high", "explicit change to active workflow", change)

        if _NEW_WORKFLOW_RE.search(text):
            return RouteDecision(RouteIntent.NEW_WORKFLOW, "high", "explicit workflow operation")
        if _EXPLICIT_METHOD_USE_RE.search(text):
            return RouteDecision(RouteIntent.NEW_WORKFLOW, "high", "explicit workflow method selection")

        # Fallback is deliberately answer, not plan. It covers declarative,
        # interrogative, and mixed-language factual questions without needing
        # per-language punctuation or question-word rules.
        return RouteDecision(RouteIntent.ANSWER, "high", "no explicit workflow operation")


__all__ = ["IntentRouter", "RouteDecision", "RouteIntent"]
