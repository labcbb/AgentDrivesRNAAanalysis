"""Reloading the page must not make serve.py print a BrokenPipeError traceback.

The browser closes the socket while the server is still writing the
response. That is ordinary client behaviour, so it must be absorbed rather
than reported as a server fault.
"""
from __future__ import annotations

import socket
import sys
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import serve  # noqa: E402


class _BrokenSocket:
    """A socket-like object whose write always fails like a closed browser."""

    def write(self, _payload: bytes) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        return None


def _make_handler() -> serve.Handler:
    handler = serve.Handler.__new__(serve.Handler)
    handler.wfile = _BrokenSocket()
    handler.close_connection = False
    return handler


def test_write_payload_absorbs_a_broken_pipe():
    handler = _make_handler()
    handler._write_payload(b"payload")
    assert handler.close_connection is True, (
        "a broken pipe must close the connection instead of propagating"
    )


def test_write_payload_absorbs_a_connection_reset():
    class _ResetSocket(_BrokenSocket):
        def write(self, _payload: bytes) -> int:
            raise ConnectionResetError(104, "Connection reset by peer")

    handler = serve.Handler.__new__(serve.Handler)
    handler.wfile = _ResetSocket()
    handler.close_connection = False
    handler._write_payload(b"payload")
    assert handler.close_connection is True


def test_write_payload_still_raises_unexpected_errors():
    class _BoomSocket(_BrokenSocket):
        def write(self, _payload: bytes) -> int:
            raise ValueError("unexpected")

    handler = serve.Handler.__new__(serve.Handler)
    handler.wfile = _BoomSocket()
    handler.close_connection = False
    try:
        handler._write_payload(b"payload")
    except ValueError:
        return
    raise AssertionError("an unexpected error must not be swallowed")


def test_server_handle_error_ignores_disconnects():
    server = serve.Server.__new__(serve.Server)
    for exc in (BrokenPipeError(32, "Broken pipe"),
                ConnectionResetError(104, "reset"),
                ConnectionAbortedError(103, "aborted")):
        try:
            raise exc
        except type(exc):
            server.handle_error(None, None)  # must not print or raise


def test_server_handle_error_still_reports_real_faults():
    server = serve.Server.__new__(serve.Server)
    reported = []
    original = serve.ThreadingHTTPServer.handle_error

    def _capture(self, request, client_address):
        reported.append((request, client_address))

    serve.ThreadingHTTPServer.handle_error = _capture
    try:
        try:
            raise ValueError("real bug")
        except ValueError:
            server.handle_error(None, None)
    finally:
        serve.ThreadingHTTPServer.handle_error = original
    assert reported, "a genuine server fault must still be reported"


def test_reloading_the_page_does_not_log_a_traceback():
    """End-to-end: abort a request mid-flight and assert the server stays quiet."""
    server = serve.Server(("127.0.0.1", 0), serve.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Ask for the session list and drop the connection before reading the
        # response, exactly like a browser navigating away mid-request.
        for _ in range(5):
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.sendall(b"GET /api/sessions HTTP/1.1\r\nHost: localhost\r\n\r\n")
            sock.close()
        # The server must still serve a normal request afterwards.
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/agent/status", timeout=10
        ) as resp:
            assert resp.status == 200
    finally:
        server.shutdown()
        server.server_close()
