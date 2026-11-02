"""Regression tests for the UI startup races that forced a manual page refresh.

Covered defects:
  1. kernel-panel.js started its polling under a *synchronous* check of the
     async backend probe, so the Environment/Visualization panels never
     appeared until the page was reloaded.
  2. llm-client.js cached a failed probe forever, so a page opened before
     serve.py was up kept reporting "not connected" even after the server came
     up.
  3. Static assets were served without ETag / Cache-Control, so browsers
     heuristically cached a stale app.js.
  4. The first /api/agent/status request paid a multi-second cold import,
     which the browser's startup probe hit head-on.
"""
from __future__ import annotations

import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import serve  # noqa: E402

UI_DIR = Path(__file__).resolve().parent


def _read(name: str) -> str:
    return (UI_DIR / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 1. kernel-panel must await the probe instead of reading it synchronously
# --------------------------------------------------------------------------
def test_kernel_panel_does_not_gate_polling_on_a_synchronous_probe_check():
    source = _read("kernel-panel.js")
    # The old bug: `if (window.llmIsLocalServer?.()) { ... startPolling ... }`
    # evaluated before the probe promise had settled, so it was always false.
    assert "if (window.llmIsLocalServer?.()) {" not in source, (
        "kernel-panel.js must not gate startPolling on a synchronous probe check"
    )
    assert "whenProxyServerReady" in source, (
        "kernel-panel.js must await the settled probe before starting polling"
    )


def test_kernel_panel_restarts_polling_when_the_backend_comes_back():
    source = _read("kernel-panel.js")
    assert '"proxy-server-probed"' in source, (
        "kernel-panel.js must re-arm polling when the backend is probed again"
    )


# --------------------------------------------------------------------------
# 2. the probe cache must expire and retry instead of pinning a failure
# --------------------------------------------------------------------------
def test_probe_cache_expires_so_a_failed_probe_is_not_permanent():
    source = _read("llm-client.js")
    assert "PROBE_CACHE_TTL_MS" in source, "probe cache entries must have a TTL"
    assert "proxyProbeCacheAt" in source, "the probe cache must record when it was written"


def test_failed_probe_schedules_a_retry():
    source = _read("llm-client.js")
    assert "scheduleProbeRetry" in source, "a failed probe must schedule a retry"
    # The retry must re-probe with force=true, otherwise it re-reads the
    # stale cached "false" and the retry loop never recovers.
    retry_block = source.split("function scheduleProbeRetry()", 1)[1].split("\n  }", 1)[0]
    assert "probeProxyServer(true)" in retry_block, (
        "the retry must force a fresh probe, not reuse the cached failure"
    )


def test_whenProxyServerReady_is_exported():
    source = _read("llm-client.js")
    assert "window.whenProxyServerReady = whenProxyServerReady;" in source


def test_app_waits_for_the_settled_probe_before_choosing_a_persistence_mode():
    source = _read("app.js")
    init_block = source.split("async function initChatSessions()", 1)[1].split("\n}", 1)[0]
    assert "whenProxyServerReady" in init_block, (
        "initChatSessions must await the settled probe, not read a racing cache"
    )


def test_load_agent_backend_status_forces_a_fresh_probe():
    source = _read("app.js")
    status_block = source.split("async function loadAgentBackendStatus()", 1)[1].split("\n}", 1)[0]
    assert "probeProxyServer?.(true)" in status_block, (
        "backend status must force a re-probe so a late-starting server is seen"
    )


# --------------------------------------------------------------------------
# 3. static assets must be revalidatable (ETag + Cache-Control)
# --------------------------------------------------------------------------
def test_handler_computes_an_etag_for_static_files():
    handler = serve.Handler
    assert hasattr(handler, "_compute_etag"), "Handler must compute an ETag"
    etag = handler._compute_etag(handler, str(UI_DIR / "app.js"))
    assert etag.startswith('"') and etag.endswith('"'), "ETag must be a quoted string"


def test_handler_etag_is_empty_for_a_missing_file():
    handler = serve.Handler
    assert handler._compute_etag(handler, str(UI_DIR / "does-not-exist.js")) == ""


def test_static_assets_are_served_with_cache_control_and_etag():
    """Boot a real server and assert the headers a browser relies on."""
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}/app.js?v=115"
        with urllib.request.urlopen(url, timeout=10) as resp:
            assert resp.status == 200
            assert resp.headers.get("ETag"), "static assets must carry an ETag"
            assert "no-cache" in (resp.headers.get("Cache-Control") or ""), (
                "static assets must be revalidated on every load"
            )
            etag = resp.headers["ETag"]
            body = resp.read()
        assert body, "app.js body should not be empty"

        # A conditional request with the same ETag must revalidate to 304.
        request = urllib.request.Request(url, headers={"If-None-Match": etag})
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        assert status == 304, f"unchanged asset should revalidate to 304, got {status}"
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------
# 4. the backend must be warmed up off the request path
# --------------------------------------------------------------------------
def test_warm_up_backend_exists_and_reports_status():
    assert hasattr(serve, "warm_up_backend"), "serve.py must expose a warm-up helper"
    # It must not raise even when the backend is unavailable.
    serve.warm_up_backend()


def test_warm_up_thread_is_started_in_main():
    source = _read("serve.py")
    assert "warm_up_backend" in source
    assert re.search(r"threading\.Thread\([^)]*warm_up_backend", source), (
        "main() must start the warm-up in a background thread"
    )


def test_registry_hydration_is_not_half_visible_to_concurrent_callers():
    """_ensure_hydrated must not mark itself hydrated before the scan ends."""
    source = (UI_DIR.parent / "sRNAgent" / "_registry.py").read_text(encoding="utf-8")
    block = source.split("def _ensure_hydrated(self)", 1)[1].split("\n    def ", 1)[0]
    assert "_hydrate_lock" in block, "hydration must be guarded by a lock"
    # The flag must be set after the hydration call, not before it.
    hydrate_call = block.index("_hydrate_registry_for_export()")
    flag_set = block.index("self._hydrated = True")
    assert flag_set > hydrate_call, (
        "self._hydrated must be set after hydration completes, not before"
    )
