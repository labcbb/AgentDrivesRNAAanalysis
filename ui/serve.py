#!/usr/bin/env python3
"""Local static server + LLM proxy + sRNAgent backend for AgentDrivesRNAAanalysis/ui."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from work_space import configure_work_space

DEFAULT_PORT = int(os.environ.get("UI_PORT", "8765"))
HOST = os.environ.get("UI_HOST", "0.0.0.0")
ROOT = os.path.dirname(os.path.abspath(__file__))


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def build_anthropic_url(base_url: str) -> str:
    base = normalize_base_url(base_url)
    if base.endswith("/v1"):
        return f"{base}/messages"
    if base.endswith("/anthropic"):
        return f"{base}/v1/messages"
    return f"{base}/v1/messages"


def build_openai_url(base_url: str) -> str:
    base = normalize_base_url(base_url)
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def uses_bearer_auth(account: dict[str, Any], vendor: dict[str, Any] | None) -> bool:
    vendor_id = str(account.get("vendorId") or "")
    base_url = str(account.get("baseUrl") or "")
    if "minimax" in vendor_id or "minimax" in base_url:
        return True
    return False


def build_headers(account: dict[str, Any], vendor: dict[str, Any] | None, protocol: str) -> dict[str, str]:
    api_key = str(account.get("apiKey") or "").strip()
    headers = {"Content-Type": "application/json"}

    if protocol == "anthropic-messages":
        headers["anthropic-version"] = "2023-06-01"
        vendor_id = str(account.get("vendorId") or "")
        base_url = str(account.get("baseUrl") or "")
        if "minimax" in vendor_id or "minimax" in base_url:
            headers["Authorization"] = f"Bearer {api_key}"
        elif uses_bearer_auth(account, vendor):
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers["x-api-key"] = api_key
    else:
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    return headers


def build_payload(
    protocol: str,
    account: dict[str, Any],
    agent: dict[str, Any],
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    max_tokens = int(agent.get("maxTokens") or 4096)
    temperature = float(agent.get("temperature") or 0.3)
    top_p = float(agent.get("topP") or 1)
    system_prompt = str(agent.get("systemPrompt") or "").strip()

    if protocol == "anthropic-messages":
        payload: dict[str, Any] = {
            "model": account.get("model"),
            "max_tokens": max_tokens,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] in ("user", "assistant")],
            "temperature": temperature,
            "top_p": top_p,
        }
        if system_prompt:
            payload["system"] = system_prompt
        return payload

    oa_messages: list[dict[str, str]] = []
    if system_prompt:
        oa_messages.append({"role": "system", "content": system_prompt})
    for m in messages:
        if m["role"] in ("user", "assistant", "system"):
            oa_messages.append({"role": m["role"], "content": m["content"]})

    return {
        "model": account.get("model"),
        "max_tokens": max_tokens,
        "messages": oa_messages,
        "temperature": temperature,
        "top_p": top_p,
        "stream": False,
    }


def extract_text(protocol: str, data: dict[str, Any]) -> str:
    if protocol == "anthropic-messages":
        parts: list[str] = []
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts).strip() or str(data)

    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        return str(message.get("content") or "").strip()
    return str(data)


def forward_chat(body: dict[str, Any]) -> dict[str, Any]:
    account = body.get("account") or {}
    vendor = body.get("vendor") or {}
    agent = body.get("agent") or {}
    messages = body.get("messages") or []

    auth_mode = account.get("authMode") or "api_key"
    if auth_mode == "api_key" and not str(account.get("apiKey") or "").strip():
        return {"ok": False, "error": "API Key 未配置"}

    protocol = account.get("apiProtocol") or vendor.get("apiProtocol") or "openai-completions"
    base_url = account.get("baseUrl") or vendor.get("defaultBaseUrl") or ""

    if protocol == "anthropic-messages":
        url = build_anthropic_url(base_url)
    else:
        url = build_openai_url(base_url)

    headers = build_headers(account, vendor, protocol)
    payload = build_payload(protocol, account, agent, messages)

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            text = extract_text(protocol, data)
            return {"ok": True, "text": text, "raw": data}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            msg = parsed.get("error", {}).get("message") or parsed.get("message") or detail
        except json.JSONDecodeError:
            msg = detail or str(exc)
        return {"ok": False, "error": f"HTTP {exc.code}: {msg}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def forward_agent_chat(body: dict[str, Any]) -> dict[str, Any]:
    try:
        from agent_bridge import run_agent_chat

        return run_agent_chat(body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"sRNAgent backend error: {exc}"}


def _client_ip(handler: Any) -> str:
    return str(handler.client_address[0] if handler.client_address else "unknown")


def forward_agent_cancel(body: dict[str, Any]) -> dict[str, Any]:
    try:
        from agent_bridge import cancel_run

        run_id = str(body.get("runId") or "").strip()
        chat_id = str(body.get("chatId") or "").strip()
        if not run_id and not chat_id:
            return {"ok": False, "error": "runId 或 chatId 不能为空"}
        # Explicit stop (runId present) may interrupt a busy kernel; chat-only cancel stops LLM only.
        cancelled = cancel_run(
            run_id,
            chat_id,
            interrupt_kernel=bool(run_id) or None,
            force_interrupt=bool(run_id),
        )
        return {"ok": True, "cancelled": cancelled}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"sRNAgent cancel error: {exc}"}


def forward_agent_approve(body: dict[str, Any]) -> dict[str, Any]:
    try:
        from agent_bridge import approve_code

        run_id = str(body.get("runId") or "").strip()
        request_id = str(body.get("requestId") or "").strip()
        if not run_id or not request_id:
            return {"ok": False, "error": "runId 和 requestId 不能为空"}
        approved = bool(body.get("approved"))
        ok = approve_code(run_id, request_id, approved)
        if not ok:
            return {"ok": False, "error": "用户拒绝了代码执行"}
        return {"ok": True, "approved": approved}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"sRNAgent approve error: {exc}"}


def iter_agent_chat_stream(body: dict[str, Any]):
    from agent_bridge import run_agent_chat_stream

    yield from run_agent_chat_stream(body)


def iter_agent_live_stream(chat_id: str, after_seq: int = 0):
    from agent_bridge import run_agent_live_stream

    yield from run_agent_live_stream(chat_id, after_seq=after_seq)


def forward_agent_run_status(chat_id: str) -> dict[str, Any]:
    try:
        from agent_bridge import agent_run_status

        if not chat_id:
            return {"ok": False, "error": "chatId 不能为空"}
        return agent_run_status(chat_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"agent run status error: {exc}"}


def forward_agent_status() -> dict[str, Any]:
    try:
        from agent_bridge import agent_status

        return {"ok": True, **agent_status()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"sRNAgent status error: {exc}"}


def forward_kernel_environment(chat_id: str) -> dict[str, Any]:
    try:
        from agent_bridge import kernel_environment

        return kernel_environment(chat_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"kernel environment error: {exc}"}


def forward_kernel_figures(chat_id: str) -> dict[str, Any]:
    try:
        from agent_bridge import kernel_figures

        return kernel_figures(chat_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"kernel figures error: {exc}"}


def forward_kernel_release(body: dict[str, Any]) -> dict[str, Any]:
    try:
        from agent_bridge import release_kernel

        chat_id = str(body.get("chatId") or "").strip()
        if not chat_id:
            return {"ok": False, "error": "chatId 不能为空"}
        return release_kernel(chat_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"kernel release error: {exc}"}


def forward_session_delete(body: dict[str, Any]) -> dict[str, Any]:
    try:
        from agent_bridge import delete_session_api

        return delete_session_api(body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"session delete error: {exc}"}


def forward_run_report(chat_id: str) -> dict[str, Any]:
    try:
        from agent_bridge import get_run_report

        return get_run_report(chat_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"run report error: {exc}"}


def forward_clear_run_report(body: dict[str, Any]) -> dict[str, Any]:
    try:
        from agent_bridge import clear_run_report_api

        return clear_run_report_api(body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"clear report error: {exc}"}


def iter_supervisor_chat_stream(body: dict[str, Any]):
    from agent_bridge import stream_supervisor_chat

    yield from stream_supervisor_chat(body)


def forward_work_space_files(query: dict[str, list[str]]) -> dict[str, Any]:
    try:
        from agent_bridge import work_space_files

        relative_path = str((query.get("path") or [""])[0]).strip()
        pattern = str((query.get("pattern") or ["*"])[0]).strip() or "*"
        recursive = str((query.get("recursive") or ["0"])[0]).strip().lower() in {"1", "true", "yes"}
        return work_space_files(relative_path, pattern=pattern, recursive=recursive)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"work_space files error: {exc}"}


def forward_sessions_list() -> dict[str, Any]:
    try:
        from agent_bridge import list_sessions

        return list_sessions()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"sessions list error: {exc}"}


def forward_session_detail(chat_id: str) -> dict[str, Any]:
    try:
        from agent_bridge import get_session

        return get_session(chat_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"session detail error: {exc}"}


def forward_session_replay(chat_id: str) -> dict[str, Any]:
    try:
        from agent_bridge import session_replay_code

        return session_replay_code(chat_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"session replay error: {exc}"}


def forward_session_save(body: dict[str, Any]) -> dict[str, Any]:
    try:
        from agent_bridge import save_session

        return save_session(body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"session save error: {exc}"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=ROOT, **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def _write_json(self, result: dict[str, Any], ok_status: int = 200, err_status: int = 502) -> None:
        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(ok_status if result.get("ok", True) else err_status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _write_sse_stream(self, event_iter, *, cancel_on_disconnect: bool = True) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
        except Exception:
            pass
        run_id = ""
        try:
            for event in event_iter:
                if isinstance(event, dict) and event.get("type") == "run_start":
                    run_id = str(event.get("runId") or "")
                if isinstance(event, dict) and event.get("type") == "live_joined" and event.get("runId"):
                    run_id = str(event.get("runId") or run_id)
                payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            if cancel_on_disconnect and run_id:
                try:
                    from agent_bridge import cancel_run, record_sse_disconnect, resolve_chat_id_for_run

                    chat_id = resolve_chat_id_for_run(run_id)
                    if chat_id:
                        record_sse_disconnect(chat_id, run_id=run_id)
                    cancel_run(run_id, interrupt_kernel=False)
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            try:
                payload = json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
            except Exception:
                pass
        else:
            try:
                payload = json.dumps({"type": "stream_end"}, ensure_ascii=False).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
            except Exception:
                pass

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        chat_id = str((query.get("chatId") or [""])[0]).strip()
        if path == "/api/agent/status":
            self._write_json(forward_agent_status())
            return
        if path == "/api/agent/run-status":
            if not chat_id:
                self._write_json({"ok": False, "error": "chatId 不能为空"}, err_status=400)
                return
            self._write_json(forward_agent_run_status(chat_id))
            return
        if path == "/api/agent/events/stream":
            if not chat_id:
                self._write_json({"ok": False, "error": "chatId 不能为空"}, err_status=400)
                return
            after_raw = str((query.get("afterSeq") or ["0"])[0]).strip() or "0"
            try:
                after_seq = max(0, int(after_raw))
            except ValueError:
                after_seq = 0
            # 旁观端断开绝不能 cancel 主任务
            self._write_sse_stream(
                iter_agent_live_stream(chat_id, after_seq=after_seq),
                cancel_on_disconnect=False,
            )
            return
        if path == "/api/kernel/environment":
            if not chat_id:
                self._write_json({"ok": False, "error": "chatId 不能为空"}, err_status=400)
                return
            self._write_json(forward_kernel_environment(chat_id))
            return
        if path == "/api/kernel/figures":
            if not chat_id:
                self._write_json({"ok": False, "error": "chatId 不能为空"}, err_status=400)
                return
            self._write_json(forward_kernel_figures(chat_id))
            return
        if path == "/api/work_space/files":
            self._write_json(forward_work_space_files(query))
            return
        if path == "/api/sessions":
            self._write_json(forward_sessions_list())
            return
        if path == "/api/sessions/detail":
            detail_id = str((query.get("chatId") or [""])[0]).strip()
            if not detail_id:
                self._write_json({"ok": False, "error": "chatId 不能为空"}, err_status=400)
                return
            self._write_json(forward_session_detail(detail_id))
            return
        if path == "/api/sessions/replay":
            replay_id = str((query.get("chatId") or [""])[0]).strip()
            if not replay_id:
                self._write_json({"ok": False, "error": "chatId 不能为空"}, err_status=400)
                return
            self._write_json(forward_session_replay(replay_id))
            return
        if path == "/api/supervisor/report":
            report_id = str((query.get("chatId") or [""])[0]).strip()
            if not report_id:
                self._write_json({"ok": False, "error": "chatId 不能为空"}, err_status=400)
                return
            self._write_json(forward_run_report(report_id))
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in (
            "/api/llm/chat",
            "/api/agent/chat",
            "/api/agent/chat/stream",
            "/api/agent/cancel",
            "/api/agent/approve",
            "/api/kernel/release",
            "/api/sessions/save",
            "/api/sessions/delete",
            "/api/supervisor/chat/stream",
            "/api/supervisor/report/clear",
        ):
            self.send_error(404, "Not Found")
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        if path == "/api/agent/chat/stream":
            self._write_sse_stream(iter_agent_chat_stream(body))
            return

        if path == "/api/supervisor/chat/stream":
            self._write_sse_stream(iter_supervisor_chat_stream(body), cancel_on_disconnect=False)
            return

        if path == "/api/supervisor/report/clear":
            self._write_json(forward_clear_run_report(body))
            return

        if path == "/api/agent/cancel":
            self._write_json(forward_agent_cancel(body))
            return

        if path == "/api/agent/approve":
            self._write_json(forward_agent_approve(body))
            return

        if path == "/api/kernel/release":
            self._write_json(forward_kernel_release(body))
            return

        if path == "/api/sessions/save":
            result = forward_session_save(body)
            if result.get("conflict"):
                self._write_json(result, ok_status=409, err_status=409)
            else:
                self._write_json(result)
            return

        if path == "/api/sessions/delete":
            self._write_json(forward_session_delete(body))
            return

        if path == "/api/agent/chat":
            self._write_json(forward_agent_chat(body))
            return

        if path == "/api/llm/chat":
            result = forward_chat(body)
            self._write_json(result)
            return

        self.send_error(404, "Not Found")

    def log_message(self, fmt: str, *args: Any) -> None:
        req = str(args[0])
        if req.startswith("POST /api/") or req.startswith("GET /api/"):
            sys.stdout.write("[api] %s\n" % (fmt % args))
        elif not req.startswith("GET /"):
            super().log_message(fmt, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="sRNAgent UI server")
    parser.add_argument("--host", type=str, default=HOST,
                        help="Bind address (default: 0.0.0.0). Use 127.0.0.1 for localhost only")
    parser.add_argument("--local", action="store_true",
                        help="Shorthand for --host 127.0.0.1 (localhost only)")
    parser.add_argument("--lan", action="store_true",
                        help="Shorthand for --host 0.0.0.0 (allow remote/LAN access)")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"HTTP port (default: {DEFAULT_PORT}, or UI_PORT env)",
    )
    parser.add_argument(
        "--work_space",
        type=str,
        default=os.environ.get("UI_WORK_SPACE", ""),
        metavar="PATH",
        help="Agent 工作区目录：下载、数据处理等文件操作均限制在此路径下（默认：启动 serve 时的当前目录）",
    )
    args = parser.parse_args()

    if args.local:
        host = "127.0.0.1"
    elif args.lan:
        host = "0.0.0.0"
    else:
        host = args.host

    launch_cwd = Path(os.getcwd()).resolve()
    workspace_input = (args.work_space or "").strip() or str(launch_cwd)
    workspace = configure_work_space(workspace_input)
    os.chdir(workspace)

    port = int(args.port)
    server = ThreadingHTTPServer((host, port), Handler)
    if host == "0.0.0.0":
        print(f"OpenClaw UI → http://0.0.0.0:{port}/index.html  (use http://<server-ip>:{port}/index.html)")
    else:
        print(f"OpenClaw UI → http://127.0.0.1:{port}/index.html")
    print(f"Work space  → {workspace}")
    print("LLM proxy   → POST /api/llm/chat")
    print("sRNAgent    → POST /api/agent/chat  /api/agent/chat/stream  /api/agent/cancel  /api/agent/approve  GET /api/agent/status  GET /api/agent/run-status?chatId=  GET /api/agent/events/stream?chatId=  GET /api/kernel/environment?chatId=  GET /api/kernel/figures?chatId=  GET /api/work_space/files?path=  GET /api/sessions  GET /api/sessions/detail?chatId=  POST /api/sessions/save  POST /api/sessions/delete  POST /api/kernel/release")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
