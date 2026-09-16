"""MCP plugin support — dynamic external tool pool (Claude Code s14 pattern).

Connects to Model Context Protocol servers, discovers their tools, and
merges them into the agent's tool pool under a namespaced key
(``mcp__<server>__<tool>``).  The built-in tools always stay first; MCP tools
are appended.  Each turn the pool is re-assembled so a newly connected server
is picked up without restarting the agent.

This implementation uses a lightweight stdio transport: it spawns the server
process, communicates via JSON-RPC over stdin/stdout.  No external MCP SDK
dependency is required — the protocol is small enough to implement inline.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MCP_ID_SAFE = re.compile(r"[^a-zA-Z0-9_]")


def _safe_name(name: str) -> str:
    return _MCP_ID_SAFE.sub("_", name).strip("_") or "unnamed"


def _namespaced(server: str, tool: str) -> str:
    return f"mcp__{_safe_name(server)}__{_safe_name(tool)}"


class MCPClient:
    """A single MCP server connection (stdio transport)."""

    def __init__(self, name: str, command: List[str], *, env: Optional[Dict[str, str]] = None) -> None:
        self.name = name
        self.command = list(command)
        self.env = dict(env or {})
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._tools: List[Dict[str, Any]] = []
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self._proc is not None and self._proc.poll() is None

    def connect(self) -> bool:
        """Spawn the server process and discover its tools."""
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env={**os.environ, **self.env},
            )
            self._connected = True
            self._initialize()
            self._tools = self._list_tools()
            logger.info("MCP server %s connected: %d tools", self.name, len(self._tools))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP connect %s failed: %s", self.name, exc)
            self._connected = False
            return False

    def disconnect(self) -> None:
        self._connected = False
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self._proc = None

    def _send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("MCP server not connected")
        with self._lock:
            self._request_id += 1
            req_id = self._request_id
            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }
            line = json.dumps(request) + "\n"
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
            # Read until we get a response with matching id
            while True:
                raw = self._proc.stdout.readline()
                if not raw:
                    raise RuntimeError("MCP server closed stdout")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(f"MCP error: {msg['error']}")
                    return msg.get("result") or {}

    def _initialize(self) -> None:
        self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "sRNAgent", "version": "1.0"},
        })
        self._send("notifications/initialized", {})

    def _list_tools(self) -> List[Dict[str, Any]]:
        result = self._send("tools/list", {})
        return list(result.get("tools") or [])

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        result = self._send("tools/call", {"name": tool_name, "arguments": arguments})
        content = result.get("content") or []
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)

    def tool_schemas(self) -> List[Dict[str, Any]]:
        """Return OpenAI-style function schemas for this server's tools."""
        schemas = []
        for tool in self._tools:
            raw_name = str(tool.get("name") or "")
            namespaced = _namespaced(self.name, raw_name)
            schemas.append({
                "type": "function",
                "function": {
                    "name": namespaced,
                    "description": str(tool.get("description") or f"MCP tool {raw_name}"),
                    "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                },
            })
        return schemas

    def raw_tool_name(self, namespaced: str) -> Optional[str]:
        prefix = f"mcp__{_safe_name(self.name)}__"
        if namespaced.startswith(prefix):
            return namespaced[len(prefix):]
        return None


class MCPManager:
    """Manages multiple MCP server connections and assembles the tool pool."""

    def __init__(self) -> None:
        self._servers: Dict[str, MCPClient] = {}
        self._lock = threading.Lock()

    def add_server(self, name: str, command: List[str], *, env: Optional[Dict[str, str]] = None) -> bool:
        with self._lock:
            if name in self._servers:
                return True
            client = MCPClient(name, command, env=env)
            if client.connect():
                self._servers[name] = client
                return True
            return False

    def remove_server(self, name: str) -> None:
        with self._lock:
            client = self._servers.pop(name, None)
        if client:
            client.disconnect()

    def disconnect_all(self) -> None:
        with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for client in servers:
            client.disconnect()

    def list_servers(self) -> List[str]:
        with self._lock:
            return [name for name, client in self._servers.items() if client.connected]

    def assemble_tool_pool(
        self,
        builtin_tools: List[Dict[str, Any]],
        builtin_handlers: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Return (tools, handlers) with MCP tools appended to built-ins."""
        tools = list(builtin_tools)
        handlers = dict(builtin_handlers)
        with self._lock:
            servers = list(self._servers.values())
        for client in servers:
            if not client.connected:
                continue
            for schema in client.tool_schemas():
                namespaced = schema["function"]["name"]
                tools.append(schema)
                handlers[namespaced] = lambda *, c=client, **kwargs: c.call_tool(
                    c.raw_tool_name(namespaced) or "", dict(kwargs),
                )
        return tools, handlers

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name.startswith("mcp__")

    def call_mcp_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        """Route an MCP-namespaced tool call. Returns None if not an MCP tool."""
        if not self.is_mcp_tool(tool_name):
            return None
        parts = tool_name.split("__", 2)
        if len(parts) < 3:
            return None
        server_name = parts[1]
        raw_name = parts[2]
        with self._lock:
            client = self._servers.get(server_name)
        if client is None or not client.connected:
            return f"MCP server '{server_name}' not connected"
        return client.call_tool(raw_name, arguments)


# ---------------------------------------------------------------------- #
# Process-global default manager
# ---------------------------------------------------------------------- #
_default_manager: Optional[MCPManager] = None
_default_lock = threading.Lock()


def get_default_manager() -> MCPManager:
    global _default_manager
    with _default_lock:
        if _default_manager is None:
            _default_manager = MCPManager()
        return _default_manager
