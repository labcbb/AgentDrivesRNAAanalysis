"""Plan-and-Execute orchestrator for sRNAgent.

Planner creates/revises a structured plan; each step runs in an isolated tool-loop
with its own turn budget (max_turns per step).
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from .tools import list_available_skills

if TYPE_CHECKING:
    from .srn_agent import SRNAgent, ProgressCallback, CodeApprovalCallback

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

_MAX_REPLAN_ATTEMPTS = 8
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_PIPELINE_KEYWORDS_RE = re.compile(
    r"\b(SRR|ERR|DRR|SRP|GSE|GSM)\d+\b|fastq|fasta|bam|sam|mirdeep|bowtie|cutadapt|"
    r"annadata|adata|multiqc|fastqc|ena|sra|mirbase|ensembl",
    re.I,
)
_ACTION_KEYWORDS_RE = re.compile(r"下载|比对|比对|定量|质控|运行|执行|处理|分析|align|download|quant|trim", re.I)
_INTERNAL_REPORT_RE = re.compile(
    r"已向用户|已向用户发送|已向.*发送|等待.{0,8}下一步|等待用户|"
    r"已发送问候|已介绍|介绍.*功能.*等待|"
    r"task completed|step (is )?done|waiting for (the )?user|"
    r"回复用户|向用户回复|发送问候",
    re.I,
)


def _extract_user_query(history: List[Dict[str, str]]) -> str:
    for item in reversed(history):
        if item.get("role") == "user":
            content = str(item.get("content") or "").strip()
            if content:
                return content
    return ""


def _is_conversational_query(query: str) -> bool:
    """Short chat / greetings — skip plan mode and reply directly."""
    q = (query or "").strip()
    if not q or len(q) > 160:
        return False
    if _PIPELINE_KEYWORDS_RE.search(q) or _ACTION_KEYWORDS_RE.search(q):
        return False
    if re.match(r"^(你好|您好|hi|hello|hey|谢谢|感谢|再见|好的|ok|okay)[!！。.~～\s]*$", q, re.I):
        return True
    if re.search(r"(你|您)(能|可以|会).*(做什么|干什么|什么功能|怎么用|如何使用)", q):
        return True
    if re.match(r"^(介绍|说明|帮助|help)\b", q, re.I):
        return True
    if len(q) <= 48 and ("?" in q or "？" in q):
        return True
    return False


def _looks_like_internal_report(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _INTERNAL_REPORT_RE.search(t):
        return True
    if re.search(r"(已向|已对|已向)(用户|您)", t):
        return True
    return False


def _parse_plan_json(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Planner returned empty response.")

    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    block_match = _JSON_BLOCK_RE.search(raw)
    if block_match:
        payload = json.loads(block_match.group(1))
        if isinstance(payload, dict):
            return payload

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        payload = json.loads(raw[start : end + 1])
        if isinstance(payload, dict):
            return payload

    raise ValueError(f"Could not parse plan JSON from planner response: {raw[:400]}")


def _normalize_steps(raw_steps: Any, *, goal: str) -> List[Dict[str, Any]]:
    if not isinstance(raw_steps, list) or not raw_steps:
        return [
            {
                "id": "1",
                "title": "完成任务",
                "goal": goal or "Complete the user request.",
                "skill": "",
                "status": STEP_PENDING,
                "result": "",
            }
        ]

    steps: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or f"Step {index}").strip()
        step_goal = str(item.get("goal") or title).strip()
        steps.append(
            {
                "id": str(item.get("id") or index),
                "title": title,
                "goal": step_goal,
                "skill": str(item.get("skill") or "").strip(),
                "status": STEP_PENDING,
                "result": "",
            }
        )

    if not steps:
        return _normalize_steps([], goal=goal)
    return steps


def _build_planner_system_prompt(skill_overview: str) -> str:
    skills_block = skill_overview or "(no skills loaded)"
    return (
        "You are the Planner for sRNAgent, a small RNA-seq analysis assistant.\n"
        "Your job is to break the user's request into clear, sequential subtasks.\n"
        "You do NOT execute tools yourself — only output a JSON plan.\n\n"
        "## Output format (strict JSON only, no markdown fences)\n"
        "{\n"
        '  "goal": "one-line summary of the overall task",\n'
        '  "steps": [\n'
        "    {\n"
        '      "id": "1",\n'
        '      "title": "short step title",\n'
        '      "goal": "specific objective for this step only",\n'
        '      "skill": "optional skill slug from registered skills, or empty string"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "## Planning rules\n"
        "1. For simple questions (no code/pipeline), use a single step.\n"
        "2. For multi-step pipelines, split by natural phases: download → QC → "
        "reference → alignment → quantification.\n"
        "3. Each step must be independently completable in one focused execution session.\n"
        "4. Do not duplicate work already marked done in session context.\n"
        "5. Prefer skill slugs when a step matches a registered skill.\n"
        "6. Keep 1–8 steps; split oversized steps rather than one giant step.\n\n"
        "## Registered skills\n"
        f"{skills_block}\n"
    )


def _build_replanner_system_prompt(skill_overview: str) -> str:
    base = _build_planner_system_prompt(skill_overview)
    return (
        f"{base}\n"
        "## Replanning mode\n"
        "You are revising an existing plan based on step results or failures.\n"
        "- Keep completed steps as status \"done\" with their results.\n"
        "- Mark failed steps as \"failed\" or replace them with smaller retry steps.\n"
        "- Add new steps only if needed; remove redundant pending steps.\n"
        "- Output the FULL updated plan JSON (all steps with status).\n"
    )


def _build_executor_system_prompt(
    agent_system_prompt: str,
    *,
    step: Dict[str, Any],
    step_index: int,
    step_total: int,
    plan_goal: str,
) -> str:
    skill_hint = ""
    if step.get("skill"):
        skill_hint = (
            f"\nRecommended skill for this step: `{step['skill']}` "
            "(call search_skills first if you need workflow guidance)."
        )
    step_block = (
        f"\n## Current subtask ({step_index}/{step_total})\n"
        f"Title: {step.get('title') or 'Subtask'}\n"
        f"Goal: {step.get('goal') or plan_goal}\n"
        f"{skill_hint}\n\n"
        "IMPORTANT: Complete ONLY this subtask in this session.\n"
        "- Do not start later pipeline stages.\n"
        "- When done, call `finish` with your message TO THE USER.\n"
        "- The finish message is shown directly in chat — reply naturally in second person.\n"
        "- NEVER write internal status reports (e.g. '已向用户…', '等待用户下一步', "
        "'Task completed', 'Step done').\n"
        "- The Jupyter kernel state (e.g. adata) persists across steps.\n"
    )
    return f"{agent_system_prompt}\n{step_block}"


def _build_step_user_message(
    *,
    user_query: str,
    step: Dict[str, Any],
    step_total: int = 1,
) -> str:
    # Single-step conversational tasks: pass user message through directly.
    if step_total == 1 and not str(step.get("skill") or "").strip():
        return user_query
    return (
        f"Execute this subtask now:\n\n"
        f"**{step.get('title') or 'Subtask'}**\n"
        f"{step.get('goal') or ''}\n\n"
        f"Original user request for context:\n{user_query}"
    )


def _step_failed(result: str) -> bool:
    lowered = (result or "").lower()
    if "max turns" in lowered or "reached max turns" in lowered:
        return True
    if "agent stopped without" in lowered:
        return True
    if "cancelled" in lowered or "canceled" in lowered:
        return True
    return False


def _format_plan_for_planner(plan: Dict[str, Any]) -> str:
    lines = [f"Goal: {plan.get('goal') or '(unspecified)'}", "Steps:"]
    for step in plan.get("steps") or []:
        status = step.get("status") or STEP_PENDING
        title = step.get("title") or step.get("goal") or step.get("id")
        result = str(step.get("result") or "").strip()
        line = f"  - [{status}] {step.get('id')}: {title}"
        if step.get("skill"):
            line += f" (skill: {step['skill']})"
        if result:
            preview = result[:300] + ("…" if len(result) > 300 else "")
            line += f"\n    result: {preview}"
        lines.append(line)
    return "\n".join(lines)


def _build_final_summary(plan: Dict[str, Any]) -> str:
    goal = str(plan.get("goal") or "任务").strip()
    steps = plan.get("steps") or []
    done = [s for s in steps if s.get("status") == STEP_DONE]
    failed = [s for s in steps if s.get("status") == STEP_FAILED]

    # Single-step tasks: show executor reply directly (no task-report wrapper).
    if len(steps) == 1 and len(done) == 1 and not failed:
        result = str(done[0].get("result") or "").strip()
        return result or "完成。"

    # Multi-step success: lead with the last step's user-facing result.
    if done and not failed:
        last_result = str(done[-1].get("result") or "").strip()
        if last_result:
            if len(done) == 1:
                return last_result
            titles = "、".join(str(s.get("title") or s.get("id")) for s in done)
            return f"{last_result}\n\n---\n已完成：{titles}"

    lines = [f"## 任务完成：{goal}", ""]
    if done:
        lines.append(f"已完成 {len(done)}/{len(steps)} 个步骤：")
        for step in done:
            title = step.get("title") or step.get("id")
            result = str(step.get("result") or "").strip()
            if result:
                preview = result[:500] + ("…" if len(result) > 500 else "")
                lines.append(f"- **{title}**：{preview}")
            else:
                lines.append(f"- **{title}**：完成")
    if failed:
        lines.append("")
        lines.append("以下步骤未完成：")
        for step in failed:
            lines.append(f"- {step.get('title') or step.get('id')}")
    return "\n".join(lines).strip()


PlanStore = Callable[[str, Dict[str, Any]], None]
PlanLoader = Callable[[str], Optional[Dict[str, Any]]]


class PlanOrchestrator:
    """Orchestrates plan → execute → replan loops."""

    def __init__(
        self,
        agent: "SRNAgent",
        *,
        chat_id: str = "",
        save_plan: Optional[PlanStore] = None,
        load_plan: Optional[PlanLoader] = None,
        max_replan_attempts: int = _MAX_REPLAN_ATTEMPTS,
    ) -> None:
        self.agent = agent
        self.chat_id = chat_id
        self._save_plan = save_plan
        self._load_plan = load_plan
        self.max_replan_attempts = max_replan_attempts
        self.skill_overview = list_available_skills(agent.skill_registry)

    def _persist_plan(self, plan: Dict[str, Any]) -> None:
        if self._save_plan and self.chat_id:
            self._save_plan(self.chat_id, plan)

    def _emit(
        self,
        on_progress: Optional["ProgressCallback"],
        event_type: str,
        **payload: Any,
    ) -> None:
        self.agent._emit_progress(on_progress, event_type, **payload)

    def _create_plan(
        self,
        user_query: str,
        extra_context: str,
        *,
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
    ) -> Dict[str, Any]:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _build_planner_system_prompt(self.skill_overview)},
            {
                "role": "user",
                "content": (
                    f"User request:\n{user_query}\n\n"
                    f"Session context:\n{extra_context or '(none)'}"
                ),
            },
        ]
        completion = self.agent._llm_complete_cancellable(
            messages,
            tools=None,
            cancel_event=cancel_event,
            on_progress=on_progress,
            enable_thinking=False,
        )
        raw = _parse_plan_json(str(completion.content or ""))
        plan = {
            "goal": str(raw.get("goal") or user_query).strip(),
            "steps": _normalize_steps(raw.get("steps"), goal=user_query),
            "version": 1,
        }
        return plan

    def _replan(
        self,
        plan: Dict[str, Any],
        *,
        user_query: str,
        extra_context: str,
        failed_step: Optional[Dict[str, Any]] = None,
        failure_reason: str = "",
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
    ) -> Dict[str, Any]:
        current_plan_text = _format_plan_for_planner(plan)
        failure_block = ""
        if failed_step:
            failure_block = (
                f"\n\nFailed step:\n"
                f"  id={failed_step.get('id')} title={failed_step.get('title')}\n"
                f"  reason: {failure_reason or failed_step.get('result') or 'unknown'}\n"
            )

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _build_replanner_system_prompt(self.skill_overview)},
            {
                "role": "user",
                "content": (
                    f"User request:\n{user_query}\n\n"
                    f"Session context:\n{extra_context or '(none)'}\n\n"
                    f"Current plan:\n{current_plan_text}"
                    f"{failure_block}\n\n"
                    "Revise the plan. Output full updated JSON."
                ),
            },
        ]
        self._emit(on_progress, "plan_revising", message="根据执行结果修正计划…")
        completion = self.agent._llm_complete_cancellable(
            messages,
            tools=None,
            cancel_event=cancel_event,
            on_progress=on_progress,
            enable_thinking=False,
        )
        raw = _parse_plan_json(str(completion.content or ""))
        new_steps = _normalize_steps(raw.get("steps"), goal=plan.get("goal") or user_query)

        # Preserve done results from old plan when replanner omits them
        old_by_id = {str(s.get("id")): s for s in (plan.get("steps") or [])}
        for step in new_steps:
            old = old_by_id.get(str(step.get("id")))
            replanner_status = str(
                next(
                    (
                        item.get("status")
                        for item in (raw.get("steps") or [])
                        if isinstance(item, dict)
                        and str(item.get("id") or "") == str(step.get("id"))
                    ),
                    "",
                )
            ).strip()
            if replanner_status in {STEP_DONE, STEP_FAILED, STEP_SKIPPED, STEP_PENDING, STEP_RUNNING}:
                step["status"] = replanner_status
            elif old and old.get("status") == STEP_DONE:
                step["status"] = STEP_DONE
                step["result"] = old.get("result") or step.get("result") or ""
            elif old and old.get("status") == STEP_FAILED:
                step["status"] = STEP_FAILED
                step["result"] = old.get("result") or step.get("result") or ""

        revised = {
            "goal": str(raw.get("goal") or plan.get("goal") or user_query).strip(),
            "steps": new_steps,
            "version": int(plan.get("version") or 1) + 1,
        }
        return revised

    def _ensure_user_facing_reply(
        self,
        user_query: str,
        text: str,
        *,
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
    ) -> str:
        text = (text or "").strip()
        if not _looks_like_internal_report(text):
            return text
        self._emit(on_progress, "status", message="正在整理回复…")
        messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are sRNAgent. Output ONLY the message to show the user in chat. "
                    "Use the same language as the user. Reply directly in second person. "
                    "Never describe your actions in third person (no '已向用户', '等待下一步')."
                ),
            },
            {"role": "user", "content": user_query},
        ]
        completion = self.agent._llm_complete_cancellable(
            messages,
            tools=None,
            cancel_event=cancel_event,
            on_progress=on_progress,
            enable_thinking=False,
        )
        reply = str(completion.content or "").strip()
        return reply or text

    def _execute_step(
        self,
        step: Dict[str, Any],
        *,
        step_index: int,
        step_total: int,
        plan_goal: str,
        user_query: str,
        history: List[Dict[str, str]],
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
        code_approval_callback: Optional["CodeApprovalCallback"] = None,
    ) -> str:
        # Single-step chat-like tasks: use normal conversation loop (no subtask framing).
        if step_total == 1 and not str(step.get("skill") or "").strip():
            result = self.agent.run_with_history(
                history,
                on_progress=on_progress,
                cancel_event=cancel_event,
                code_approval_callback=code_approval_callback,
            )
            return self._ensure_user_facing_reply(
                user_query,
                result,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )

        executor_system = _build_executor_system_prompt(
            self.agent.system_prompt,
            step=step,
            step_index=step_index,
            step_total=step_total,
            plan_goal=plan_goal,
        )
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": executor_system},
            {"role": "user", "content": _build_step_user_message(
                user_query=user_query, step=step, step_total=step_total
            )},
        ]
        return self._ensure_user_facing_reply(
            user_query,
            self.agent._tool_loop(
                messages,
                on_progress=on_progress,
                cancel_event=cancel_event,
                code_approval_callback=code_approval_callback,
            ),
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    @staticmethod
    def _next_pending_step(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for step in plan.get("steps") or []:
            if step.get("status") == STEP_PENDING:
                return step
        return None

    def run(
        self,
        history: List[Dict[str, str]],
        *,
        extra_context: str = "",
        on_progress: Optional["ProgressCallback"] = None,
        cancel_event: Optional[Any] = None,
        code_approval_callback: Optional["CodeApprovalCallback"] = None,
    ) -> str:
        user_query = _extract_user_query(history)
        if not user_query:
            raise ValueError("No user message in history")

        # Greetings / short chat: skip planning, reply like normal agent.
        if _is_conversational_query(user_query):
            self._emit(on_progress, "status", message="正在回复…")
            result = self.agent.run_with_history(
                history,
                on_progress=on_progress,
                cancel_event=cancel_event,
                code_approval_callback=code_approval_callback,
            )
            result = self._ensure_user_facing_reply(
                user_query,
                result,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            self._emit(on_progress, "final", content=result)
            return result

        self._emit(on_progress, "status", message="正在制定执行计划…")
        plan = self._create_plan(
            user_query,
            extra_context,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        self._persist_plan(plan)
        self._emit(
            on_progress,
            "plan_created",
            plan=plan,
            message=f"计划已生成：{len(plan.get('steps') or [])} 个步骤",
        )

        replan_attempts = 0
        steps_list = plan.get("steps") or []
        step_total = len(steps_list)

        while True:
            self.agent._check_cancelled(cancel_event)
            pending = self._next_pending_step(plan)
            if pending is None:
                break

            step_index = steps_list.index(pending) + 1
            pending["status"] = STEP_RUNNING
            self._persist_plan(plan)
            self._emit(
                on_progress,
                "plan_step_start",
                plan=plan,
                stepId=pending.get("id"),
                stepIndex=step_index,
                stepTotal=step_total,
                title=pending.get("title"),
                message=f"正在执行步骤 {step_index}/{step_total}：{pending.get('title')}",
            )

            result = self._execute_step(
                pending,
                step_index=step_index,
                step_total=step_total,
                plan_goal=str(plan.get("goal") or ""),
                user_query=user_query,
                history=history,
                on_progress=on_progress,
                cancel_event=cancel_event,
                code_approval_callback=code_approval_callback,
            )

            if _step_failed(result):
                pending["status"] = STEP_FAILED
                pending["result"] = result
                self._persist_plan(plan)
                self._emit(
                    on_progress,
                    "plan_step_failed",
                    plan=plan,
                    stepId=pending.get("id"),
                    stepIndex=step_index,
                    message=f"步骤 {step_index} 未在轮次上限内完成",
                )

                if replan_attempts >= self.max_replan_attempts:
                    summary = self._ensure_user_facing_reply(
                        user_query,
                        _build_final_summary(plan),
                        on_progress=on_progress,
                        cancel_event=cancel_event,
                    )
                    self._emit(on_progress, "plan_complete", plan=plan, message=summary)
                    self._emit(on_progress, "final", content=summary)
                    return summary

                replan_attempts += 1
                plan = self._replan(
                    plan,
                    user_query=user_query,
                    extra_context=extra_context,
                    failed_step=pending,
                    failure_reason=result,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                )
                steps_list = plan.get("steps") or []
                step_total = len(steps_list)
                self._persist_plan(plan)
                self._emit(
                    on_progress,
                    "plan_revised",
                    plan=plan,
                    message=f"计划已更新（第 {plan.get('version')} 版）",
                )
                continue

            pending["status"] = STEP_DONE
            pending["result"] = result
            self._persist_plan(plan)
            self._emit(
                on_progress,
                "plan_step_done",
                plan=plan,
                stepId=pending.get("id"),
                stepIndex=step_index,
                result=result[:600] if result else "",
                message=f"步骤 {step_index}/{step_total} 完成",
            )

        summary = self._ensure_user_facing_reply(
            user_query,
            _build_final_summary(plan),
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        self._emit(on_progress, "plan_complete", plan=plan, message=summary)
        self._emit(on_progress, "final", content=summary)
        return summary
