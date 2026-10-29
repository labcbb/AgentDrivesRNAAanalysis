"""Tests for LLM endpoint URL construction."""
from __future__ import annotations

import pytest

from sRNAgent.agent.llm_client import _anthropic_url, _minimax_base, _openai_url, _safe_url


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
