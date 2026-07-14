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

  let pollTimer = null;
  let refreshInFlight = false;
  let lastRefreshAt = 0;
  const MIN_REFRESH_MS = 5000;

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

  function renderFigures(payload) {
    if (!vizGallery) return;
    const figures = Array.isArray(payload?.figures) ? payload.figures : [];
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

    figures.slice().reverse().forEach((figure, index) => {
      const format = figure?.format === "jpeg" ? "jpeg" : "png";
      const data = figure?.data || "";
      if (!data) return;

      const card = document.createElement("figure");
      card.className = "kernel-viz-card";

      const img = document.createElement("img");
      img.className = "kernel-viz-image";
      img.alt = `Plot ${figures.length - index}`;
      img.loading = "lazy";
      img.src = `data:image/${format};base64,${data}`;

      const caption = document.createElement("figcaption");
      caption.className = "kernel-viz-caption";
      caption.textContent = figure?.timestamp || `Plot ${figures.length - index}`;

      card.appendChild(img);
      card.appendChild(caption);
      vizGallery.appendChild(card);
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
