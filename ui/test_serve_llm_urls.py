"""Tests for the UI proxy's LLM endpoint construction and error reporting."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import serve  # noqa: E402


def test_anthropic_url_targets_minimax_anthropic_endpoint():
    # A root-style or /v1 base must land on MiniMax's /anthropic prefix,
    # otherwise the gateway answers 404 "page not found".
    assert serve.build_anthropic_url("https://api.minimaxi.com") == (
        "https://api.minimaxi.com/anthropic/v1/messages"
    )
    assert serve.build_anthropic_url("https://api.minimaxi.com/v1") == (
        "https://api.minimaxi.com/anthropic/v1/messages"
    )
    assert serve.build_anthropic_url("https://api.minimaxi.com/anthropic") == (
        "https://api.minimaxi.com/anthropic/v1/messages"
    )
    assert serve.build_anthropic_url("https://api.minimax.io/v1") == (
        "https://api.minimax.io/anthropic/v1/messages"
    )


def test_openai_url_targets_minimax_openai_endpoint():
    assert serve.build_openai_url("https://api.minimaxi.com") == (
        "https://api.minimaxi.com/v1/chat/completions"
    )
    assert serve.build_openai_url("https://api.minimaxi.com/v1") == (
        "https://api.minimaxi.com/v1/chat/completions"
    )
    assert serve.build_openai_url("https://api.minimaxi.com/anthropic") == (
        "https://api.minimaxi.com/v1/chat/completions"
    )


def test_non_minimax_hosts_are_untouched():
    assert serve.build_anthropic_url("https://api.anthropic.com") == (
        "https://api.anthropic.com/v1/messages"
    )
    assert serve.build_openai_url("https://api.openai.com/v1") == (
        "https://api.openai.com/v1/chat/completions"
    )


def test_safe_url_strips_credentials_and_query():
    assert serve._safe_url("https://user:secret@example.com:8443/v1/messages?api_key=abc") == (
        "https://example.com:8443/v1/messages"
    )


def test_forward_chat_error_reports_the_endpoint():
    import urllib.error

    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("https://api.minimaxi.com/v1/messages", 404, "Not Found", {}, None)

        def read(self):
            return b"404 page not found"

    original = serve.urllib.request.urlopen

    def boom(*args, **kwargs):
        raise FakeHTTPError()

    serve.urllib.request.urlopen = boom
    try:
        result = serve.forward_chat({
            "account": {
                "apiKey": "k",
                "baseUrl": "https://api.minimaxi.com",
                "apiProtocol": "anthropic-messages",
            },
            "messages": [{"role": "user", "content": "hi"}],
        })
    finally:
        serve.urllib.request.urlopen = original

    assert result["ok"] is False
    assert "url=https://api.minimaxi.com/anthropic/v1/messages" in result["error"]
