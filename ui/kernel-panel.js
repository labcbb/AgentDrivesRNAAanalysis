/**
 * Right rail: Jupyter kernel Environment + Visualization panels.
 */
(function initKernelPanel() {
  const envBody = document.getElementById("kernel-env-body");
  const envTable = document.getElementById("kernel-env-table");
  const envTbody = document.getElementById("kernel-env-tbody");
  const envRefreshBtn = document.getElementById("kernel-env-refresh");
  const vizBody = document.getElementById("kernel-viz-body");
  const vizGallery = document.getElementById("kernel-viz-gallery");
  const vizRefreshBtn = document.getElementById("kernel-viz-refresh");
  const vizRestoreBtn = document.getElementById("kernel-viz-restore");
  const sideRail = document.getElementById("agent-side-rail");
  const railSplitter = document.getElementById("kernel-rail-splitter");

  let pollTimer = null;
  let refreshInFlight = false;
  let lastRefreshAt = 0;
  const MIN_REFRESH_MS = 5000;
  const RAIL_SPLIT_KEY = "srnagent-rail-env-pct";
  let lastFiguresFingerprint = "";
  let lastEnvFingerprint = "";

  function figuresFingerprint(figures, hiddenCount = 0) {
    return `${Number(hiddenCount) || 0}#${(Array.isArray(figures) ? figures : [])
      .map((figure) => {
        if (figure?.path) return `p:${figure.path}:${figure.mtime || figure.timestamp || ""}:${figure.pinned ? 1 : 0}`;
        if (figure?.url) return `u:${figure.url}`;
        const data = String(figure?.data || "");
        return `d:${data.length}:${data.slice(0, 48)}`;
      })
      .join("|")}`;
  }

  function envFingerprint(payload) {
    const variables = Array.isArray(payload?.variables) ? payload.variables : [];
    return `${payload?.ready ? 1 : 0}|${payload?.message || ""}|${variables
      .map((item) => `${item?.name}:${item?.type}:${item?.detail || ""}:${String(item?.preview || "").slice(0, 24)}`)
      .join(";")}`;
  }

  function applyRailEnvPct(pct) {
    if (!sideRail) return;
    const next = Math.min(82, Math.max(18, Number(pct) || 48));
    sideRail.style.setProperty("--rail-env-pct", `${next}%`);
    try {
      localStorage.setItem(RAIL_SPLIT_KEY, String(Math.round(next)));
    } catch {
      // ignore
    }
    return next;
  }

  function restoreRailSplit() {
    let saved = 48;
    try {
      const raw = localStorage.getItem(RAIL_SPLIT_KEY);
      if (raw != null && raw !== "") saved = Number(raw);
    } catch {
      // ignore
    }
    applyRailEnvPct(Number.isFinite(saved) ? saved : 48);
  }

  function initRailSplitter() {
    if (!sideRail || !railSplitter) return;
    restoreRailSplit();
    let dragging = false;

    const onMove = (event) => {
      if (!dragging) return;
      const rect = sideRail.getBoundingClientRect();
      if (!rect.height) return;
      const pct = ((event.clientY - rect.top) / rect.height) * 100;
      applyRailEnvPct(pct);
    };

    const stopDrag = (event) => {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove("is-rail-resizing");
      try {
        railSplitter.releasePointerCapture(event.pointerId);
      } catch {
        // ignore
      }
    };

    railSplitter.addEventListener("pointerdown", (event) => {
      if (event.button != null && event.button !== 0) return;
      dragging = true;
      document.body.classList.add("is-rail-resizing");
      railSplitter.setPointerCapture(event.pointerId);
      onMove(event);
      event.preventDefault();
    });
    railSplitter.addEventListener("pointermove", onMove);
    railSplitter.addEventListener("pointerup", stopDrag);
    railSplitter.addEventListener("pointercancel", stopDrag);
    railSplitter.addEventListener("keydown", (event) => {
      const current = Number.parseFloat(getComputedStyle(sideRail).getPropertyValue("--rail-env-pct")) || 48;
      if (event.key === "ArrowUp") {
        applyRailEnvPct(current - 3);
        event.preventDefault();
      } else if (event.key === "ArrowDown") {
        applyRailEnvPct(current + 3);
        event.preventDefault();
      }
    });
  }

  function envEmptyEl() {
    return envBody?.querySelector(".kernel-panel__empty");
  }

  function vizEmptyEl() {
    return vizBody?.querySelector(".kernel-panel__empty");
  }

  function formatVariableType(item) {
    const mod = item?.module ? String(item.module) : "";
    const typ = item?.type ? String(item.type) : "object";
    if (mod && mod !== "builtins" && mod !== typ) {
      return `${mod}.${typ}`;
    }
    return typ;
  }

  function isBinaryLikePreview(text) {
    if (!text) return false;
    if (text === "empty" || text.startsWith("hex ") || text.includes("gzip") || text === "binary data") {
      return false;
    }
    if (text.includes("\uFFFD")) return true;
    const sample = text.slice(0, 160);
    let bad = 0;
    for (let i = 0; i < sample.length; i += 1) {
      const code = sample.charCodeAt(i);
      if (code === 0xfffd) bad += 1;
      else if (code < 32 && code !== 9 && code !== 10 && code !== 13) bad += 1;
      else if (code >= 127 && code < 160) bad += 1;
    }
    return bad >= Math.max(2, sample.length * 0.12);
  }

  function formatBytesPreview(item) {
    const detail = item?.detail ? String(item.detail) : "";
    const preview = item?.preview ? String(item.preview) : "";
    if (preview === "empty" || preview.startsWith("hex ") || preview.includes("gzip compressed")) {
      return [detail, preview].filter(Boolean).join(" · ") || preview || "empty";
    }
    if (preview === "binary data" || preview === "zip archive") {
      return [detail, preview].filter(Boolean).join(" · ") || preview;
    }
    if (!preview) return detail || "empty";
    if (isBinaryLikePreview(preview)) {
      return detail ? `${detail} · binary data` : "binary data";
    }
    return [detail, preview.length > 80 ? `${preview.slice(0, 80)}…` : preview].filter(Boolean).join(" · ");
  }

  function formatVariableInfo(item) {
    const typ = String(item?.type || "");
    if (typ === "bytes") {
      return formatBytesPreview(item);
    }
    const detail = item?.detail ? String(item.detail) : "";
    const preview = item?.preview ? String(item.preview) : "";
    const parts = [];
    if (detail) parts.push(detail);
    if (preview) parts.push(preview);
    return parts.join(" · ") || "—";
  }

  function renderEnvironment(payload) {
    if (!envTbody || !envTable) return;
    const fingerprint = envFingerprint(payload);
    if (fingerprint === lastEnvFingerprint) return;
    lastEnvFingerprint = fingerprint;

    const variables = Array.isArray(payload?.variables) ? payload.variables : [];
    const emptyEl = envEmptyEl();

    if (!payload?.ready) {
      if (emptyEl) {
        emptyEl.hidden = false;
        emptyEl.textContent = payload?.message || "内核尚未启动，执行一次代码后将显示变量";
      }
      envTable.hidden = true;
      envTbody.innerHTML = "";
      return;
    }

    if (variables.length === 0) {
      if (emptyEl) {
        emptyEl.hidden = false;
        emptyEl.textContent = "当前内核中没有可显示的用户变量";
      }
      envTable.hidden = true;
      envTbody.innerHTML = "";
      return;
    }

    if (emptyEl) emptyEl.hidden = true;
    envTable.hidden = false;
    envTbody.innerHTML = "";

    variables.forEach((item) => {
      const row = document.createElement("tr");
      row.className = "kernel-env-row";
      if (formatVariableType(item).includes("AnnData")) {
        row.classList.add("kernel-env-row--highlight");
      }

      const nameCell = document.createElement("td");
      nameCell.className = "kernel-env-name";
      nameCell.textContent = item.name || "—";
      nameCell.title = item.name || "";

      const typeCell = document.createElement("td");
      typeCell.className = "kernel-env-type";
      typeCell.textContent = formatVariableType(item);

      const infoCell = document.createElement("td");
      infoCell.className = "kernel-env-info";
      infoCell.textContent = formatVariableInfo(item);
      infoCell.title = formatVariableInfo(item);

      row.appendChild(nameCell);
      row.appendChild(typeCell);
      row.appendChild(infoCell);
      envTbody.appendChild(row);
    });
  }

  function updateRestoreButton(payload) {
    if (!vizRestoreBtn) return;
    const hiddenCount = Number(payload?.hiddenCount) || 0;
    vizRestoreBtn.hidden = hiddenCount <= 0;
    vizRestoreBtn.textContent = hiddenCount > 0 ? `恢复(${hiddenCount})` : "恢复";
    vizRestoreBtn.title = hiddenCount > 0
      ? `恢复已关闭的 ${hiddenCount} 张图`
      : "没有已关闭的图";
  }

  async function hideFigure(path) {
    const chatId = window.getActiveChatId?.() || "";
    if (!chatId || !path || !window.updateKernelVizPrefs) return;
    try {
      await window.updateKernelVizPrefs({
        chatId,
        action: "hide",
        paths: [path],
      });
      lastFiguresFingerprint = "";
      await refresh({ force: true });
    } catch (error) {
      console.warn("hide figure failed", error);
    }
  }

  async function restoreHiddenFigures() {
    const chatId = window.getActiveChatId?.() || "";
    if (!chatId || !window.updateKernelVizPrefs) return;
    try {
      await window.updateKernelVizPrefs({
        chatId,
        action: "clear_hidden",
      });
      lastFiguresFingerprint = "";
      await refresh({ force: true });
    } catch (error) {
      console.warn("restore figures failed", error);
    }
  }

  async function persistGalleryOrder() {
    if (!vizGallery || !window.updateKernelVizPrefs) return;
    const chatId = window.getActiveChatId?.() || "";
    if (!chatId) return;
    const paths = [...vizGallery.querySelectorAll(".kernel-viz-card[data-path]")]
      .map((card) => String(card.dataset.path || "").trim())
      .filter(Boolean);
    if (!paths.length) return;
    try {
      await window.updateKernelVizPrefs({
        chatId,
        action: "reorder",
        paths,
      });
      lastFiguresFingerprint = figuresFingerprint(
        paths.map((path) => ({ path })),
        Number(vizRestoreBtn?.textContent?.match(/\d+/)?.[0] || 0),
      );
    } catch (error) {
      console.warn("reorder figures failed", error);
    }
  }

  function bindGalleryDrag(card) {
    if (!card?.dataset?.path || !vizGallery) return;
    const handle = card.querySelector(".kernel-viz-drag");
    if (!handle) return;

    handle.addEventListener("pointerdown", (event) => {
      if (event.button != null && event.button !== 0) return;
      card.draggable = true;
    });
    card.addEventListener("dragstart", (event) => {
      if (!card.draggable) {
        event.preventDefault();
        return;
      }
      card.classList.add("kernel-viz-card--dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", card.dataset.path || "");
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("kernel-viz-card--dragging");
      card.draggable = false;
      vizGallery.querySelectorAll(".kernel-viz-card--drop-target").forEach((item) => {
        item.classList.remove("kernel-viz-card--drop-target");
      });
      void persistGalleryOrder();
    });
    card.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      const dragging = vizGallery.querySelector(".kernel-viz-card--dragging");
      if (!dragging || dragging === card) return;
      const rect = card.getBoundingClientRect();
      const before = event.clientY < rect.top + rect.height / 2;
      if (before) vizGallery.insertBefore(dragging, card);
      else vizGallery.insertBefore(dragging, card.nextSibling);
      card.classList.add("kernel-viz-card--drop-target");
    });
    card.addEventListener("dragleave", () => {
      card.classList.remove("kernel-viz-card--drop-target");
    });
    card.addEventListener("drop", (event) => {
      event.preventDefault();
      card.classList.remove("kernel-viz-card--drop-target");
    });
  }

  function renderFigures(payload) {
    if (!vizGallery) return;
    const figures = Array.isArray(payload?.figures) ? payload.figures : [];
    const fingerprint = figuresFingerprint(figures, payload?.hiddenCount);
    // Avoid wiping <img> nodes on every poll — that causes visible flicker/reload.
    if (fingerprint === lastFiguresFingerprint) {
      updateRestoreButton(payload);
      return;
    }
    lastFiguresFingerprint = fingerprint;
    updateRestoreButton(payload);

    const emptyEl = vizEmptyEl();

    if (!figures.length) {
      if (emptyEl) {
        emptyEl.hidden = false;
        emptyEl.textContent = payload?.message || "暂无图表输出";
      }
      vizGallery.hidden = true;
      vizGallery.innerHTML = "";
      return;
    }

    if (emptyEl) emptyEl.hidden = true;
    vizGallery.hidden = false;
    vizGallery.innerHTML = "";

    figures.forEach((figure, index) => {
      const format = figure?.format === "jpeg" ? "jpeg" : (figure?.format || "png");
      const data = figure?.data || "";
      const url = String(figure?.url || "").trim();
      const path = String(figure?.path || "").trim();
      // Cache-bust only when mtime changes; stable URLs keep the browser cache warm.
      const src = url
        ? (figure?.mtime ? `${url}${url.includes("?") ? "&" : "?"}v=${encodeURIComponent(String(figure.mtime))}` : url)
        : (data ? `data:image/${format};base64,${data}` : "");
      if (!src) return;

      const card = document.createElement("figure");
      card.className = "kernel-viz-card";
      if (path) card.dataset.path = path;

      if (path) {
        const dragHandle = document.createElement("button");
        dragHandle.type = "button";
        dragHandle.className = "kernel-viz-drag";
        dragHandle.title = "拖动调整顺序";
        dragHandle.setAttribute("aria-label", "拖动调整顺序");
        dragHandle.textContent = "⋮⋮";
        card.appendChild(dragHandle);

        const closeBtn = document.createElement("button");
        closeBtn.type = "button";
        closeBtn.className = "kernel-viz-close";
        closeBtn.title = "关闭此图";
        closeBtn.setAttribute("aria-label", `关闭 ${figure?.title || path}`);
        closeBtn.textContent = "×";
        closeBtn.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          void hideFigure(path);
        });
        card.appendChild(closeBtn);
      }

      const img = document.createElement("img");
      img.className = "kernel-viz-image";
      img.alt = figure?.title || path || `Plot ${figures.length - index}`;
      img.loading = "lazy";
      img.draggable = false;
      img.src = src;
      img.addEventListener("click", () => {
        window.open(src, "_blank", "noopener,noreferrer");
      });

      const caption = document.createElement("figcaption");
      caption.className = "kernel-viz-caption";
      caption.textContent = figure?.title || path || figure?.timestamp || `Plot ${figures.length - index}`;
      caption.title = path || caption.textContent;

      card.appendChild(img);
      card.appendChild(caption);
      vizGallery.appendChild(card);
      bindGalleryDrag(card);
    });
  }

  async function refresh(options = {}) {
    if (!window.llmIsLocalServer?.()) return;
    // Agent 流式进行中默认不轮询；但 force=true（例如每步 execute_code 结束后）仍刷新
    if (window.isAgentSending?.() && !options.force) return;
    if (refreshInFlight && !options.force) return;
    const now = Date.now();
    if (!options.force && now - lastRefreshAt < MIN_REFRESH_MS) return;
    refreshInFlight = true;
    const chatId = window.getActiveChatId?.() || "";
    try {
      const [envPayload, figPayload] = await Promise.all([
        chatId
          ? window.fetchKernelEnvironment?.(chatId).catch(() => ({ ready: false, variables: [], message: "无法读取环境" }))
          : Promise.resolve({ ready: false, variables: [], message: "未选择对话" }),
        chatId
          ? window.fetchKernelFigures?.(chatId).catch(() => ({ ready: false, figures: [], message: "无法读取图表" }))
          : Promise.resolve({ ready: false, figures: [], message: "未选择对话" }),
      ]);
      renderEnvironment(envPayload || {});
      renderFigures(figPayload || {});
    } finally {
      refreshInFlight = false;
      lastRefreshAt = Date.now();
    }
  }

  function startPolling(intervalMs = 12000) {
    stopPolling();
    pollTimer = window.setInterval(() => {
      if (window.isAgentSending?.()) return;
      refresh();
    }, intervalMs);
  }

  function stopPolling() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  envRefreshBtn?.addEventListener("click", () => refresh({ force: true }));
  vizRefreshBtn?.addEventListener("click", () => refresh({ force: true }));
  vizRestoreBtn?.addEventListener("click", () => {
    void restoreHiddenFigures();
  });
  initRailSplitter();

  window.KernelPanel = {
    refresh,
    startPolling,
    stopPolling,
  };

  if (window.llmIsLocalServer?.()) {
    refresh();
    startPolling(12000);
  }
})();
