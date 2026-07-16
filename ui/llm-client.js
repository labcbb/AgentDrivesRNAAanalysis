/**
 * Browser-side LLM + sRNAgent client — calls local serve.py proxy (port 8765).
 */
(function initLlmClient() {
  const PROXY_PORT = window.LLM_PROXY_PORT || "8765";

  function resolveProxyBase() {
    const { protocol, host } = window.location;
    if (protocol === "http:" || protocol === "https:") {
      return `${protocol}//${host}`;
    }
    return `http://127.0.0.1:${PROXY_PORT}`;
  }

  const PROXY_BASE = resolveProxyBase();

  let proxyProbeCache = null;
  let proxyProbePromise = null;

  /** 检测当前页面能否访问 serve.py 后端（不依赖固定端口） */
  async function probeProxyServer(force = false) {
    if (window.location.protocol === "file:") {
      proxyProbeCache = false;
      return false;
    }
    if (!force && proxyProbeCache !== null) return proxyProbeCache;
    if (!force && proxyProbePromise) return proxyProbePromise;

    proxyProbePromise = fetch(`${PROXY_BASE}/api/agent/status`, { method: "GET" })
      .then((response) => {
        proxyProbeCache = response.ok;
        window.__proxyServerReady = proxyProbeCache;
        return proxyProbeCache;
      })
      .catch(() => {
        proxyProbeCache = false;
        window.__proxyServerReady = false;
        return false;
      })
      .finally(() => {
        proxyProbePromise = null;
      });
    return proxyProbePromise;
  }

  function isPageOnProxyServer() {
    const { protocol } = window.location;
    if (protocol === "file:") return false;
    if (protocol !== "http:" && protocol !== "https:") return false;
    return proxyProbeCache === true;
  }

  function trimHistory(messages, maxCount) {
    const limit = Math.max(4, Number(maxCount) || 40);
    if (messages.length <= limit) return messages;
    return messages.slice(-limit);
  }

  async function postJson(path, body, options = {}) {
    if (window.location.protocol === "file:") {
      throw new Error(
        "当前是 file:// 打开，无法调用 API。请运行：\n\ncd ui && python3 serve.py\n\n然后打开 http://<服务器IP>:8765/index.html",
      );
    }

    let response;
    try {
      response = await fetch(`${PROXY_BASE}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: options.signal,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw error;
      }
      throw new Error(
        `无法连接 UI 服务 ${PROXY_BASE}。请先运行：cd ui && python3 serve.py\n\n(${error instanceof Error ? error.message : String(error)})`,
      );
    }

    if (response.status === 405) {
      throw new Error(
        `请求失败 (405)：请直接打开 http://<服务器IP>:${PROXY_PORT}/index.html`,
      );
    }

    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      const errText = data.error || data.message || `请求失败 (HTTP ${response.status})`;
      throw new Error(typeof errText === "string" ? errText : JSON.stringify(errText));
    }
    return data;
  }

  function parseSseChunk(buffer) {
    const events = [];
    const parts = buffer.split("\n\n");
    const rest = parts.pop() || "";
    for (const part of parts) {
      const line = part.split("\n").find((entry) => entry.startsWith("data: "));
      if (!line) continue;
      try {
        events.push(JSON.parse(line.slice(6)));
      } catch {
        // ignore malformed chunks
      }
    }
    return { events, rest };
  }

  async function chatCompletion({ account, vendor, agent, messages }) {
    const data = await postJson("/api/llm/chat", {
      account,
      vendor,
      agent,
      messages: trimHistory(messages, agent?.maxHistoryMessages),
    });
    return data.text || "";
  }

  async function agentChatCompletion({ account, vendor, agent, messages, chatId, autoApproveCode, signal }) {
    const data = await postJson("/api/agent/chat", {
      account,
      vendor,
      agent,
      messages: trimHistory(messages, agent?.maxHistoryMessages),
      chatId,
      autoApproveCode: Boolean(autoApproveCode),
    }, { signal });
    return {
      text: data.text || "",
      meta: data.meta || {},
    };
  }

  async function agentChatStream({
    account,
    vendor,
    agent,
    messages,
    executionContext,
    runId,
    chatId,
    deviceId,
    autoApproveCode,
    approvalMode,
    signal,
    onEvent,
  }) {
    if (window.location.protocol === "file:") {
      throw new Error(
        "当前是 file:// 打开，无法调用 API。请运行：\n\ncd ui && python3 serve.py\n\n然后打开 http://<服务器IP>:8765/index.html",
      );
    }

    const mode = String(approvalMode || "").trim().toLowerCase()
      || (autoApproveCode ? "auto" : "manual");

    let response;
    try {
      response = await fetch(`${PROXY_BASE}/api/agent/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account,
          vendor,
          agent,
          messages: trimHistory(messages, agent?.maxHistoryMessages),
          executionContext: String(executionContext || "").trim(),
          runId,
          chatId,
          deviceId: deviceId || undefined,
          approvalMode: mode,
          autoApproveCode: mode === "auto",
        }),
        signal,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw error;
      }
      throw new Error(
        `无法连接 UI 服务 ${PROXY_BASE}。请先运行：cd ui && python3 serve.py\n\n(${error instanceof Error ? error.message : String(error)})`,
      );
    }

    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || data.message || `请求失败 (HTTP ${response.status})`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalResult = null;

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parsed = parseSseChunk(buffer);
        buffer = parsed.rest;
        for (const event of parsed.events) {
          onEvent?.(event);
          if (event.type === "final" && event.content && !finalResult) {
            finalResult = { text: event.content, meta: {} };
          }
          if (event.type === "done") {
            finalResult = {
              text: event.text || finalResult?.text || "",
              meta: event.meta || finalResult?.meta || {},
            };
          }
          if (event.type === "cancelled") {
            const err = new Error(event.message || "已停止生成");
            err.name = "AgentCancelledError";
            throw err;
          }
          if (event.type === "error") {
            throw new Error(event.message || "Agent 执行失败");
          }
          if (event.type === "stream_end") {
            finalResult = finalResult || { text: "", meta: {} };
            return finalResult;
          }
        }
      }
    } catch (error) {
      if (finalResult) return finalResult;
      throw error;
    }

    if (finalResult) return finalResult;
    throw new Error("Agent 流式响应异常结束（可能是 LLM API 配置错误或 serve.py 已断开，请检查 Config 中的 API Key / Base URL）");
  }

  async function cancelAgentRun(runId, chatId) {
    if (!runId && !chatId) return;
    try {
      await fetch(`${PROXY_BASE}/api/agent/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ runId, chatId }),
      });
    } catch {
      // best-effort cancel
    }
  }

  async function approveAgentCode(runId, requestId, approved) {
    const data = await postJson("/api/agent/approve", {
      runId,
      requestId,
      approved: Boolean(approved),
    });
    return data;
  }

  async function fetchAgentRunStatus(chatId) {
    if (!chatId) return { ok: false };
    const qs = `?chatId=${encodeURIComponent(chatId)}`;
    const response = await fetch(`${PROXY_BASE}/api/agent/run-status${qs}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      return { ok: false, error: data.error || `run-status HTTP ${response.status}` };
    }
    return data;
  }

  async function fetchAgentStatus() {
    const response = await fetch(`${PROXY_BASE}/api/agent/status`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(data.error || `status HTTP ${response.status}`);
    }
    return data;
  }

  async function fetchKernelEnvironment(chatId) {
    const qs = chatId ? `?chatId=${encodeURIComponent(chatId)}` : "";
    const response = await fetch(`${PROXY_BASE}/api/kernel/environment${qs}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `environment HTTP ${response.status}`);
    }
    return data;
  }

  async function fetchKernelFigures(chatId) {
    const qs = chatId ? `?chatId=${encodeURIComponent(chatId)}` : "";
    const response = await fetch(`${PROXY_BASE}/api/kernel/figures${qs}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `figures HTTP ${response.status}`);
    }
    return data;
  }

  async function releaseChatKernel(chatId) {
    if (!chatId) return { ok: true, released: false };
    try {
      return await postJson("/api/kernel/release", { chatId });
    } catch {
      return { ok: true, released: false };
    }
  }

  async function deleteChatSession(chatId) {
    if (!chatId) return { ok: true, deleted: false };
    try {
      return await postJson("/api/sessions/delete", { chatId });
    } catch (error) {
      return {
        ok: false,
        deleted: false,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }

  async function fetchRunReport(chatId) {
    if (!chatId) return { ok: false, error: "chatId 不能为空" };
    const qs = `?chatId=${encodeURIComponent(chatId)}`;
    const response = await fetch(`${PROXY_BASE}/api/supervisor/report${qs}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      return { ok: false, error: data.error || `report HTTP ${response.status}` };
    }
    return data;
  }

  async function clearRunReport(chatId) {
    if (!chatId) return { ok: false, error: "chatId 不能为空" };
    const response = await fetch(`${PROXY_BASE}/api/supervisor/report/clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chatId }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      return { ok: false, error: data.error || `clear report HTTP ${response.status}` };
    }
    return data;
  }

  async function supervisorChatStream({
    account,
    vendor,
    agent,
    chatId,
    parentChatId,
    messages,
    question,
    signal,
    onEvent,
  }) {
    const targetChatId = chatId || parentChatId;
    if (!targetChatId) throw new Error("chatId 不能为空");
    if (window.location.protocol === "file:") {
      throw new Error("file:// 无法调用监管者 API，请通过 serve.py 打开页面");
    }
    let response;
    try {
      response = await fetch(`${PROXY_BASE}/api/supervisor/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account,
          vendor,
          agent,
          chatId: targetChatId,
          parentChatId: parentChatId || targetChatId,
          messages: Array.isArray(messages) ? messages : undefined,
          question,
        }),
        signal,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new Error(
        `无法连接监管者通道 ${PROXY_BASE}。（${error instanceof Error ? error.message : String(error)}）`,
      );
    }
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || data.message || `监管者请求失败 (HTTP ${response.status})`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalResult = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSseChunk(buffer);
      buffer = parsed.rest;
      for (const event of parsed.events) {
        onEvent?.(event);
        if (event.type === "final" && event.content) {
          finalResult = { text: event.content, meta: {} };
        }
        if (event.type === "done") {
          finalResult = {
            text: event.text || finalResult?.text || "",
            meta: event.meta || finalResult?.meta || {},
          };
        }
        if (event.type === "error") {
          throw new Error(event.message || "监管者执行失败");
        }
        if (event.type === "stream_end") {
          return finalResult || { text: "", meta: {} };
        }
      }
    }
    return finalResult || { text: "", meta: {} };
  }

  async function fetchChatSessions() {
    const response = await fetch(`${PROXY_BASE}/api/sessions`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `sessions HTTP ${response.status}`);
    }
    return data;
  }

  async function saveChatSession({
    chatId,
    chat,
    activeChatId,
    deviceId,
    expectedUpdatedAt,
    updateGlobalActive = false,
    force = false,
  }) {
    // Dedicated fetch: 409 conflict must not throw like generic postJson failures.
    let response;
    try {
      response = await fetch(`${PROXY_BASE}/api/sessions/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chatId,
          chat,
          activeChatId: activeChatId || chatId,
          deviceId: deviceId || undefined,
          expectedUpdatedAt:
            expectedUpdatedAt == null || expectedUpdatedAt === ""
              ? undefined
              : Number(expectedUpdatedAt),
          updateGlobalActive: Boolean(updateGlobalActive),
          force: Boolean(force),
        }),
      });
    } catch (error) {
      throw new Error(
        `无法连接 UI 服务 ${PROXY_BASE}。（${error instanceof Error ? error.message : String(error)}）`,
      );
    }
    const data = await response.json().catch(() => ({}));
    if (data?.conflict || response.status === 409) {
      return {
        ok: false,
        conflict: true,
        error: data.error || "会话已被其他设备更新",
        chat: data.chat || null,
        lease: data.lease || null,
      };
    }
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || data.message || `session save HTTP ${response.status}`);
    }
    return data;
  }

  /** 旁观端实时同步：加入已有 Agent 运行的事件广播（断开不会 cancel 任务） */
  async function agentLiveEventStream({ chatId, afterSeq = 0, signal, onEvent }) {
    if (!chatId) throw new Error("chatId 不能为空");
    if (window.location.protocol === "file:") {
      throw new Error("file:// 无法订阅实时事件，请通过 serve.py 打开页面");
    }
    const qs = `?chatId=${encodeURIComponent(chatId)}&afterSeq=${encodeURIComponent(String(afterSeq || 0))}`;
    let response;
    try {
      response = await fetch(`${PROXY_BASE}/api/agent/events/stream${qs}`, {
        method: "GET",
        headers: { Accept: "text/event-stream" },
        signal,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw error;
      }
      throw new Error(
        `无法连接实时同步通道 ${PROXY_BASE}。（${error instanceof Error ? error.message : String(error)}）`,
      );
    }
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || data.message || `实时同步失败 (HTTP ${response.status})`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseSseChunk(buffer);
      buffer = parsed.rest;
      for (const event of parsed.events) {
        onEvent?.(event);
        if (event.type === "stream_end" || event.type === "done" || event.type === "cancelled" || event.type === "error") {
          return;
        }
      }
    }
  }

  window.llmChatCompletion = chatCompletion;
  window.agentChatCompletion = agentChatCompletion;
  window.agentChatStream = agentChatStream;
  window.agentLiveEventStream = agentLiveEventStream;
  window.cancelAgentRun = cancelAgentRun;
  window.approveAgentCode = approveAgentCode;
  window.fetchAgentRunStatus = fetchAgentRunStatus;
  window.fetchAgentStatus = fetchAgentStatus;
  window.fetchKernelEnvironment = fetchKernelEnvironment;
  window.fetchKernelFigures = fetchKernelFigures;
  window.releaseChatKernel = releaseChatKernel;
  window.deleteChatSession = deleteChatSession;
  window.fetchRunReport = fetchRunReport;
  window.clearRunReport = clearRunReport;
  window.supervisorChatStream = supervisorChatStream;
  window.fetchChatSessions = fetchChatSessions;
  window.saveChatSession = saveChatSession;
  window.llmIsLocalServer = isPageOnProxyServer;
  window.probeProxyServer = probeProxyServer;
  window.llmProxyBase = PROXY_BASE;

  function notifyProxyProbeDone() {
    window.dispatchEvent(new CustomEvent("proxy-server-probed", {
      detail: { ok: proxyProbeCache === true },
    }));
    window.updateComposerStatus?.();
  }

  probeProxyServer().then(notifyProxyProbeDone);
})();
