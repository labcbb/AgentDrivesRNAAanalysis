"""LangChain chat-model adapter from sRNAgent LLMConfig.

Uses langchain-openai's ChatOpenAI against OpenAI-compatible endpoints
(MiniMax, local gateways, etc.). The legacy urllib client in llm_client.py
remains available for the existing tool-loop.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_openai import ChatOpenAI

if TYPE_CHECKING:
    from .llm_client import LLMConfig


def chat_model_from_config(config: "LLMConfig") -> ChatOpenAI:
    """Build a ChatOpenAI client that talks to the same endpoint as LLMConfig."""
    return ChatOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        top_p=config.top_p,
    )
