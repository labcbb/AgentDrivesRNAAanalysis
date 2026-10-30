"""Tests for LLM endpoint URL construction."""
from __future__ import annotations

import pytest

from sRNAgent.agent.llm_client import (
    _anthropic_url, _is_minimax_host, _minimax_base, _openai_url, _safe_url,
)


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.minimaxi.com/anthropic", "https://api.minimaxi.com/anthropic/v1/messages"),
        ("https://api.minimaxi.com/anthropic/", "https://api.minimaxi.com/anthropic/v1/messages"),
        # An OpenAI-style base with the Anthropic protocol must land on /anthropic,
        # otherwise MiniMax's gateway answers 404 "page not found".
        ("https://api.minimaxi.com", "https://api.minimaxi.com/anthropic/v1/messages"),
        ("https://api.minimaxi.com/v1", "https://api.minimaxi.com/anthropic/v1/messages"),
        ("https://api.minimax.io", "https://api.minimax.io/anthropic/v1/messages"),
        ("https://api.minimax.io/v1", "https://api.minimax.io/anthropic/v1/messages"),
        ("https://api.minimax.cn", "https://api.minimax.cn/anthropic/v1/messages"),
        ("https://api.minimax.cn/v1", "https://api.minimax.cn/anthropic/v1/messages"),
        ("https://api.minimax.cn/anthropic", "https://api.minimax.cn/anthropic/v1/messages"),
    ],
)
def test_anthropic_url_targets_minimax_anthropic_endpoint(base_url, expected):
    assert _anthropic_url(base_url) == expected


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.minimaxi.com/v1", "https://api.minimaxi.com/v1/chat/completions"),
        ("https://api.minimaxi.com", "https://api.minimaxi.com/v1/chat/completions"),
        ("https://api.minimaxi.com/anthropic", "https://api.minimaxi.com/v1/chat/completions"),
        ("https://api.minimax.io/anthropic", "https://api.minimax.io/v1/chat/completions"),
    ],
)
def test_openai_url_targets_minimax_openai_endpoint(base_url, expected):
    assert _openai_url(base_url) == expected


def test_full_endpoint_url_is_not_rewritten_twice():
    assert _anthropic_url("https://api.minimaxi.com/anthropic/v1/messages") == (
        "https://api.minimaxi.com/anthropic/v1/messages"
    )
    assert _openai_url("https://api.minimaxi.com/v1/chat/completions") == (
        "https://api.minimaxi.com/v1/chat/completions"
    )


def test_non_minimax_hosts_are_untouched():
    assert _anthropic_url("https://api.anthropic.com") == "https://api.anthropic.com/v1/messages"
    assert _openai_url("https://api.openai.com/v1") == "https://api.openai.com/v1/chat/completions"
    assert _minimax_base("https://llm.internal.example/v1", "anthropic-messages") == (
        "https://llm.internal.example/v1"
    )


def test_safe_url_strips_credentials_and_query():
    assert _safe_url("https://user:secret@example.com:8443/v1/messages?api_key=abc") == (
        "https://example.com:8443/v1/messages"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://api.minimaxi.com/anthropic/v1/messages",
        "https://api.minimax.io/anthropic/v1/messages",
        "https://api.minimax.cn/anthropic/v1/messages",
    ],
)
def test_minimax_hosts_are_recognised(url):
    assert _is_minimax_host(url) is True


def test_non_minimax_host_is_not_recognised():
    assert _is_minimax_host("https://api.anthropic.com/v1/messages") is False


def test_serve_and_client_agree_on_the_minimax_host_set():
    """serve.py keeps its own copy of the host set; they must not drift."""
    import importlib.util
    from pathlib import Path

    import sRNAgent.agent.llm_client as client

    spec = importlib.util.spec_from_file_location(
        "ui_serve", Path(__file__).resolve().parents[3] / "ui" / "serve.py",
    )
    serve = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(serve)

    assert serve._MINIMAX_HOSTS == client._MINIMAX_HOSTS
    assert serve._MINIMAX_KEY_REJECTED_HINT == client._MINIMAX_KEY_REJECTED_HINT
    assert serve._MINIMAX_KEY_UNRECOGNISED_HINT == client._MINIMAX_KEY_UNRECOGNISED_HINT


def test_auth_hint_distinguishes_a_rejected_key_from_an_unrecognised_one():
    from sRNAgent.agent.llm_client import (
        _MINIMAX_KEY_REJECTED_HINT, _MINIMAX_KEY_UNRECOGNISED_HINT, _minimax_auth_hint,
    )

    rejected = _minimax_auth_hint('{"error":{"message":"invalid api key"}}')
    assert rejected == _MINIMAX_KEY_REJECTED_HINT
    assert "platform.minimaxi.com" in rejected

    unrecognised = _minimax_auth_hint(
        '{"error":{"message":"login fail: Please carry the API secret key"}}'
    )
    assert unrecognised == _MINIMAX_KEY_UNRECOGNISED_HINT
