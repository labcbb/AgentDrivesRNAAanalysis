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
# Short confirms, plus common gate replies like「好的，执行吧」「确认运行」.
_APPROVE_RE = re.compile(
    r"^\s*(?:可以(?:的|啊|呀)?|可(?:以|行)|好(?:的|啊|呀)?|同意|确认|按(?:此|上述)|采用|"
    r"ok(?:ay)?(?:\s+go\s+ahead)?|yes|yep|sure|go\s+ahead)"
    r"(?:\s*[，,。.!！]?\s*(?:开始|继续|执行|运行|吧|啦|了|please|it|now)?(?:\s*(?:执行|运行|开始|继续|吧|啦|了|it|now))?)*"
    r"\s*$",
    re.I,
)
# Gate-only: short confirm + one action verb, no new-task object/method payload.
_GATE_APPROVE_RE = re.compile(
    r"^\s*(?:可以(?:的|啊|呀)?|好(?:的|啊|呀)?|同意|确认|ok(?:ay)?|yes|yep|sure)"
    r"(?:\s*[，,。.!！])?\s*"
    r"(?:执行|运行|开始|继续|proceed|run|execute|go)(?:\s*(?:吧|啦|了|it|now))?"
    r"\s*$",
    re.I,
)
# Bare action at a gate: 「执行」「运行吧」— keep plan, do not NEW_WORKFLOW.
_GATE_BARE_ACTION_RE = re.compile(
    r"^\s*(?:执行|运行|开始|继续|proceed|run|execute|go)(?:\s*(?:吧|啦|了|it|now))?\s*$",
    re.I,
)
_QUESTION_HINT_RE = re.compile(
    r"[?？]|^(?:why|what|where|how|which|who|is|are|can|could|does|do|did|will)\b|"
    r"(?:吗|呢|么|什么|怎么|如何|为何|为什么|哪|是否|对不对|准不准|在哪里|能不能|可不可以)",
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
# Gate parameter replies — must resume the plan, not ANSWER-only.
_GATE_ASSIGNMENT_RE = re.compile(
    r"(?:adapter(?:_3)?|strandedness|group(?:_col)?|control_group|design|min_length|max_length|"
    r"quality_cutoff|error_rate|min_overlap|no_indels|times|trim_n|poly_a|output_dir|json_report)\s*=\s*\S+",
    re.I,
)
_GATE_EXPLICIT_VALUE_RE = re.compile(
    r"\b(?:unstranded|forward|reverse|paired|unpaired)\b|\b[ACGTUN]{8,}\b",
    re.I,
)
_GATE_GROUP_CONFIRM_RE = re.compile(
    r"(?:确认|同意|采用|按|根据|使用).{0,24}(?:分组|组别|group)|"
    r"(?:分组|组别|group).{0,24}(?:确认|同意|采用|继续)",
    re.I,
)
_GATE_NATURAL_CONTROL_RE = re.compile(
    r"(?:对照组|control(?:\s*group)?)\s*(?:为|是|=|:|：)\s*`?([A-Za-z][\w.-]*)`?|"
    r"\b([A-Za-z][\w.-]*)\s*(?:是|作为|as)\s*(?:对照组|control(?:\s*group)?)",
    re.I,
)
_GATE_NATURAL_DESIGN_RE = re.compile(
    r"(?:design|设计)\s*(?:为|是|=|:|：)?\s*`?((?:un)?paired|配对|非配对|不配对)`?",
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


def _plan_awaiting_approval(plan: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(plan, Mapping):
        return False
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return False
    return any(
        isinstance(step, Mapping)
        and str(step.get("status") or "").strip().lower() == "awaiting_approval"
        for step in steps
    )


def is_plan_approval_confirm(message: str, *, awaiting_gate: bool = False) -> bool:
    """Shared short-confirm vocabulary for IntentRouter and plan_orchestrator."""
    text = str(message or "").strip()
    if not text:
        return False
    if _APPROVE_RE.fullmatch(text):
        return True
    if awaiting_gate and _GATE_APPROVE_RE.fullmatch(text):
        return True
    if awaiting_gate and _GATE_BARE_ACTION_RE.fullmatch(text):
        return True
    return False


def is_gate_actionable_reply(message: str, *, awaiting_gate: bool = False) -> bool:
    """True for confirms AND concrete gate edits (adapter=/DNA/group prose).

    Used by both IntentRouter (must CONTINUE/resume) and
    ``approval_response_is_actionable`` (must close or hydrate the gate).
    """
    text = str(message or "").strip()
    if not text:
        return False
    if is_plan_approval_confirm(text, awaiting_gate=awaiting_gate):
        return True
    if not awaiting_gate:
        return False
    return bool(
        _GATE_ASSIGNMENT_RE.search(text)
        or _GATE_EXPLICIT_VALUE_RE.search(text)
        or _GATE_GROUP_CONFIRM_RE.search(text)
        or _GATE_NATURAL_CONTROL_RE.search(text)
        or _GATE_NATURAL_DESIGN_RE.search(text)
    )


def _looks_like_question(message: str) -> bool:
    text = str(message or "").strip()
    return bool(text and _QUESTION_HINT_RE.search(text))


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
        awaiting_gate = _plan_awaiting_approval(active_plan)
        if not text:
            return RouteDecision(RouteIntent.ANSWER, "high", "empty message")

        if explicit_resume:
            return RouteDecision(RouteIntent.CONTINUE, "high", "explicit resume flag")
        # Gate parameter replies (adapter_3=… / DNA / unstranded / group prose)
        # must resume so _prepare_restored_plan can consume them.
        if awaiting_gate and is_gate_actionable_reply(text, awaiting_gate=True):
            return RouteDecision(RouteIntent.CONTINUE, "high", "actionable approval-gate reply")
        if active and is_plan_approval_confirm(text, awaiting_gate=False):
            return RouteDecision(RouteIntent.CONTINUE, "high", "approval of active plan")
        if active and _CONTINUE_RE.search(text):
            return RouteDecision(RouteIntent.CONTINUE, "high", "explicit continuation request")

        if active and _AMEND_RE.search(text):
            method = _METHOD_RE.search(text)
            change = {"method": method.group(0)} if method else None
            return RouteDecision(RouteIntent.AMEND_PLAN, "high", "explicit change to active workflow", change)

        # At an approval gate, only exact soft confirms (above) keep the plan.
        # Do NOT use startswith("可以"/"确认") — that swallowed real new work like
        # 「可以开始下载数据了」 / 「确认运行 miRanda …」.
        if _NEW_WORKFLOW_RE.search(text):
            # Side questions that mention 运行/预测 must not clear_plan.
            if active and _looks_like_question(text):
                return RouteDecision(RouteIntent.ANSWER, "high", "question about active workflow")
            return RouteDecision(RouteIntent.NEW_WORKFLOW, "high", "explicit workflow operation")
        if _EXPLICIT_METHOD_USE_RE.search(text):
            if awaiting_gate:
                method = _METHOD_RE.search(text)
                change = {"method": method.group(0)} if method else None
                return RouteDecision(
                    RouteIntent.AMEND_PLAN,
                    "high",
                    "method choice at approval gate",
                    change,
                )
            return RouteDecision(RouteIntent.NEW_WORKFLOW, "high", "explicit workflow method selection")

        # Fallback is deliberately answer, not plan. It covers declarative,
        # interrogative, and mixed-language factual questions without needing
        # per-language punctuation or question-word rules.
        return RouteDecision(RouteIntent.ANSWER, "high", "no explicit workflow operation")


__all__ = [
    "IntentRouter",
    "RouteDecision",
    "RouteIntent",
    "is_plan_approval_confirm",
    "is_gate_actionable_reply",
]
