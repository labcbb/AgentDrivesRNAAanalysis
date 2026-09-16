"""Tests for MCP client tool namespacing and pool assembly (s14 pattern)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sRNAgent.agent.mcp_client import (  # noqa: E402
    MCPClient,
    MCPManager,
    _namespaced,
    _safe_name,
    get_default_manager,
)


def test_namespacing():
    assert _namespaced("my-server", "search") == "mcp__my_server__search"
    assert _namespaced("ena_api", "fetch") == "mcp__ena_api__fetch"


def test_safe_name_strips_special_chars():
    assert _safe_name("my-server.v2") == "my_server_v2"
    assert _safe_name("") == "unnamed"


def test_manager_is_singleton():
    a = get_default_manager()
    b = get_default_manager()
    assert a is b


def test_manager_assembles_empty_pool_without_servers():
    mgr = MCPManager()
    builtin = [{"type": "function", "function": {"name": "bash"}}]
    handlers = {"bash": lambda **_: "ok"}
    tools, h = mgr.assemble_tool_pool(builtin, handlers)
    assert tools == builtin
    assert h == handlers


def test_manager_is_mcp_tool():
    mgr = MCPManager()
    assert mgr.is_mcp_tool("mcp__ena__search") is True
    assert mgr.is_mcp_tool("bash") is False


def test_manager_call_mcp_tool_not_connected():
    mgr = MCPManager()
    result = mgr.call_mcp_tool("mcp__nonexistent__search", {})
    assert "not connected" in result


def test_mcp_client_raw_tool_name():
    client = MCPClient("test", ["echo"])
    client._connected = True
    assert client.raw_tool_name("mcp__test__search") == "search"
    assert client.raw_tool_name("mcp__other__search") is None
