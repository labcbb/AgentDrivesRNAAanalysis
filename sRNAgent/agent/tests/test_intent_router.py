"""Intent routing must preserve plans for factual turns in every language."""

import pytest

from sRNAgent.agent.intent_router import IntentRouter, RouteIntent


@pytest.mark.parametrize(
    "message",
    [
        "这个isomir的序列，是真实的不同于miRNA的mature序列的吧",
        "Are these observed isomiR sequences rather than mature miRNA sequences",
        "I wonder whether the reported isomiR sequence is the observed read sequence",
        "isomiR 的 sequence source 是什么",
    ],
)
def test_factual_text_defaults_to_answer_without_language_specific_question_rules(message):
    decision = IntentRouter.route(message, active_plan={"steps": [{"status": "pending"}]})

    assert decision.intent == RouteIntent.ANSWER


def test_explicit_workflow_operations_are_the_only_new_workflow_route():
    assert IntentRouter.route("Run miRanda for selected isomiRs").intent == RouteIntent.NEW_WORKFLOW
    assert IntentRouter.route("重新运行 isomiR 定量").intent == RouteIntent.NEW_WORKFLOW
    assert IntentRouter.route("Could you use miRanda for the selected isomiRs").intent == RouteIntent.NEW_WORKFLOW


def test_continue_and_amend_require_an_active_plan():
    active = {"steps": [{"status": "awaiting_approval"}]}

    assert IntentRouter.route("continue", active_plan=active).intent == RouteIntent.CONTINUE
    assert IntentRouter.route("改用 miRanda 替代 starBase", active_plan=active).intent == RouteIntent.AMEND_PLAN
    assert IntentRouter.route("continue").intent == RouteIntent.ANSWER


def test_approval_gate_confirms_with_action_verbs_keep_plan():
    active = {"steps": [{"status": "awaiting_approval"}]}

    for message in ("好的，执行吧", "确认运行", "可以，开始", "ok run it", "ok go ahead", "执行", "运行吧"):
        decision = IntentRouter.route(message, active_plan=active)
        assert decision.intent == RouteIntent.CONTINUE, message

    # Concrete gate edits must CONTINUE so the orchestrator can consume them.
    for message in (
        "adapter_3=ACGTACGTACGT",
        "unstranded",
        "TGGAATTCTCGGGTGCCAAGG",
        "可以根据这个分组来，normal是对照组",
    ):
        decision = IntentRouter.route(message, active_plan=active)
        assert decision.intent == RouteIntent.CONTINUE, message

    # Soft-confirm prefixes must NOT swallow real new work.
    assert (
        IntentRouter.route("可以开始下载数据了", active_plan=active).intent
        == RouteIntent.NEW_WORKFLOW
    )
    assert (
        IntentRouter.route("确认运行 miRanda 靶标预测", active_plan=active).intent
        == RouteIntent.NEW_WORKFLOW
    )
    # Side questions that mention workflow verbs must not clear_plan.
    assert (
        IntentRouter.route("这个运行结果对吗", active_plan=active).intent
        == RouteIntent.ANSWER
    )
    # Explicit new workflow still wins when it is not an approval-style confirm.
    assert (
        IntentRouter.route("重新运行 isomiR 定量", active_plan=active).intent
        == RouteIntent.NEW_WORKFLOW
    )
    assert (
        IntentRouter.route("使用 miRanda", active_plan=active).intent
        == RouteIntent.AMEND_PLAN
    )
