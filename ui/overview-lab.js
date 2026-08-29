/**
 * Overview page — embed an isolated JupyterLab (separate from Agent chat kernels).
 * Root directory = UI work_space. Lab listens on its own port (default 8766).
 */
(function initOverviewLab() {
  const frame = document.getElementById("overview-lab-frame");
  const statusEl = document.getElementById("overview-lab-status");
  const metaEl = document.getElementById("overview-lab-meta");
  const startBtn = document.getElementById("overview-lab-start");
  const openBtn = document.getElementById("overview-lab-open");
  const restartBtn = document.getElementById("overview-lab-restart");
  if (!frame || !statusEl) return;

  let lastUrl = "";
  let booting = false;
  let loadTimer = null;

  function setStatus(text, kind) {
    statusEl.textContent = text || "";
    statusEl.dataset.kind = kind || "";
    statusEl.hidden = !text;
  }

  function setMeta(status) {
    if (!metaEl) return;
    if (!status?.ok && status?.error) {
      metaEl.textContent = status.error;
      return;
    }
    const parts = [];
    if (status?.workspace) parts.push(`工作目录 ${status.workspace}`);
    if (status?.port) parts.push(`端口 ${status.port}`);
    if (status?.ready) parts.push("独立内核已就绪");
    else if (status?.running) parts.push("启动中…");
    else parts.push("未运行");
    metaEl.textContent = parts.join(" · ");
  }

  function buildLabUrl(status) {
    if (!status?.ready || !status.port || !status.token) return "";
    const protocol = window.location.protocol === "https:" ? "https:" : "http:";
    const host = window.location.hostname || "127.0.0.1";
    return `${protocol}//${host}:${status.port}/lab?token=${encodeURIComponent(status.token)}`;
  }

  function clearLoadTimer() {
    if (loadTimer) {
      window.clearTimeout(loadTimer);
      loadTimer = null;
    }
  }

  function showFrame(url, { forceReload = false } = {}) {
    if (!url) return false;
    const same = url === lastUrl && Boolean(frame.getAttribute("src"));
    if (!same || forceReload) {
      lastUrl = url;
      frame.src = url;
    }
    frame.hidden = false;
    setStatus("");
    if (openBtn) openBtn.disabled = false;
    clearLoadTimer();
    // iframe onload is unreliable cross-origin; give a soft hint if still blank-looking.
    loadTimer = window.setTimeout(() => {
      if (frame.hidden) return;
      // Keep quiet when Lab is up — user can use「新窗口打开」if embed is blocked.
    }, 8000);
    return true;
  }

  async function fetchStatus() {
    const resp = await fetch("/api/jupyterlab/status", { cache: "no-store" });
    return resp.json();
  }

  async function startLab({ restart = false } = {}) {
    if (booting) return null;
    booting = true;
    startBtn && (startBtn.disabled = true);
    restartBtn && (restartBtn.disabled = true);
    setStatus(restart ? "正在重启 JupyterLab…" : "正在启动 JupyterLab（独立内核）…", "loading");
    if (restart) {
      lastUrl = "";
      frame.removeAttribute("src");
      frame.hidden = true;
    }
    try {
      // Already running: mount immediately, skip the blocking start round-trip UX.
      if (!restart) {
        const existing = await fetchStatus();
        setMeta(existing);
        if (existing?.ready) {
          const url = buildLabUrl(existing);
          if (url && showFrame(url)) return existing;
        }
      }

      const resp = await fetch("/api/jupyterlab/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ restart }),
      });
      const data = await resp.json();
      setMeta(data);
      if (!data?.ok || !data?.ready) {
        const detail = data?.logTail ? `\n\n日志尾部：\n${data.logTail}` : "";
        setStatus((data?.error || "启动失败") + detail, "error");
        frame.hidden = true;
        return data;
      }
      const url = buildLabUrl(data);
      if (!url) {
        setStatus("已启动但缺少 token/端口", "error");
        return data;
      }
      showFrame(url, { forceReload: restart });
      return data;
    } catch (error) {
      setStatus(`启动失败：${error instanceof Error ? error.message : String(error)}`, "error");
      return null;
    } finally {
      booting = false;
      startBtn && (startBtn.disabled = false);
      restartBtn && (restartBtn.disabled = false);
    }
  }

  async function ensureLab() {
    try {
      const status = await fetchStatus();
      setMeta(status);
      if (status?.ready) {
        const url = buildLabUrl(status);
        if (url) {
          showFrame(url);
          return status;
        }
      }
      return startLab();
    } catch (error) {
      setStatus(`无法连接 UI 后端：${error instanceof Error ? error.message : String(error)}`, "error");
      return null;
    }
  }

  startBtn?.addEventListener("click", () => {
    void startLab();
  });
  restartBtn?.addEventListener("click", () => {
    void startLab({ restart: true });
  });
  openBtn?.addEventListener("click", async () => {
    let status = null;
    try {
      status = await fetchStatus();
    } catch {
      status = null;
    }
    if (!status?.ready) status = await startLab();
    const url = buildLabUrl(status || {});
    if (url) window.open(url, "_blank", "noopener,noreferrer");
  });

  window.OverviewLab = {
    ensure: ensureLab,
    restart: () => startLab({ restart: true }),
  };
})();
