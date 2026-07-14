const shell = document.querySelector(".shell");
const navToggle = document.getElementById("nav-toggle");
const navBackdrop = document.getElementById("nav-backdrop");
const collapseToggle = null;
const themeToggle = document.getElementById("theme-toggle");
const composerForm = document.getElementById("composer-form");
const composer = document.getElementById("composer");
const sendBtn = document.getElementById("send-btn");
const chatScroll = document.getElementById("chat-scroll");
const threadInner = document.getElementById("chat-thread-inner");
const agentCodePanel = document.getElementById("agent-code-panel");
const agentCodeInner = document.getElementById("agent-code-inner");
const autoApproveToggle = document.getElementById("auto-approve-toggle");
const breadcrumbCurrent = document.getElementById("breadcrumb-current");
const agentSessions = document.getElementById("agent-sessions");
const newChatBtn = document.getElementById("new-chat-btn");
const chatRecentList = document.getElementById("chat-recent-list");
const parameterHint = document.getElementById("parameter-hint");
const parameterForm = document.getElementById("parameter-form");
const analysisLog = document.getElementById("analysis-log");
const runAnalysisBtn = document.getElementById("run-analysis-btn");

/** HTTP + 非 localhost 时 crypto.randomUUID 不可用，需 fallback */
function createId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    try {
      return crypto.randomUUID();
    } catch {
      // insecure context (e.g. http://123.x.x.x)
    }
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

let isComposing = false;
let pendingExecutionCode = "";
let imeEnterStroke = false;
let activeCodeExecutionId = null;
let currentPage = "agent";
/** @type {Map<string, { generation: number, runId: string, abortController: AbortController, messages: Array<any>, assistantEntry: any, pending: Element|null, idleTimer: number|null, lastStreamEventAt: number, codeExecutionId: string|null }>} */
const chatStreams = new Map();
let nextStreamGeneration = 0;
const STREAM_IDLE_MS = 3600000;
const STREAM_STATUS_POLL_MS = 4000;
/** @type {Map<string, { abortController: AbortController, runId: string, codeExecutionId: string|null, lastSeq: number }>} */
const liveFollows = new Map();
const BACKGROUND_WATCH_POLL_MS = 3000;
const BACKGROUND_EXECUTION_ID = "background-kernel-run";
/** @type {Map<string, { timer: number, startedAt: number, pending: Element|null, assistantEntry: any|null }>} */
const backgroundWatches = new Map();

function isChatStreaming(chatId) {
  if (!chatId) return false;
  const stream = chatStreams.get(chatId);
  if (!stream) return false;
  // 旁观伪 stream 不算「本机主发送流」
  return !stream.isFollower;
}

function isLiveFollowing(chatId = activeChatId) {
  return Boolean(chatId && liveFollows.has(chatId));
}

function isActiveChatSending() {
  return isChatStreaming(activeChatId) || isLiveFollowing(activeChatId);
}

function getChatStream(chatId) {
  return chatStreams.get(chatId || activeChatId) || null;
}

function syncComposerForActiveChat() {
  setComposerMode(isActiveChatSending() ? "stop" : "send");
  renderRecentChats();
}

function getChatRecord(chatId) {
  return chatStore.chats.find((chat) => chat.id === chatId) || null;
}

function ensureChatRecord(chatId) {
  if (!chatId) return null;
  let chat = getChatRecord(chatId);
  if (!chat) {
    chat = {
      id: chatId,
      title: "New Chat",
      messages: [],
      codePanel: [],
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
    chatStore.chats.unshift(chat);
  }
  if (!Array.isArray(chat.codePanel)) chat.codePanel = [];
  return chat;
}

function persistChatMessages(chatId, messages) {
  if (!chatId) return;
  const hasMessages = messages.some(
    (item) => item.role === "user" || (item.role === "assistant" && assistantHasPersistableContent(item)),
  );
  if (!hasMessages) {
    removeEmptyChat(chatId);
    saveChatStore();
    renderRecentChats();
    return;
  }

  const chat = ensureChatRecord(chatId);
  chat.messages = messages
    .map((item) => normalizeMessage(item))
    .filter(
      (item) =>
        item.role === "user" ||
        (item.role === "assistant" && assistantHasPersistableContent(item)),
    );
  chat.updatedAt = Date.now();
  chat.title = deriveChatTitle(chat.messages);
  if (chatStore.chats.length > MAX_STORED_CHATS) {
    chatStore.chats.sort((a, b) => b.createdAt - a.createdAt);
    chatStore.chats = chatStore.chats.slice(0, MAX_STORED_CHATS);
  }
  if (chatId === activeChatId) {
    chatStore.activeChatId = activeChatId;
  }
  saveChatStore();
  scheduleServerSessionSave(chatId);
  renderRecentChats();
}

function cancelAllChatStreams() {
  for (const [chatId, stream] of chatStreams.entries()) {
    stream.generation = -1;
    stream.abortController?.abort();
    void window.cancelAgentRun?.(stream.runId, chatId);
  }
  chatStreams.clear();
  renderRecentChats();
}

function finishChatStream(chatId, options = {}) {
  const stream = chatStreams.get(chatId);
  if (stream?.idleTimer) {
    window.clearInterval(stream.idleTimer);
    stream.idleTimer = null;
  }
  if (stream?.statusPollTimer) {
    window.clearInterval(stream.statusPollTimer);
    stream.statusPollTimer = null;
  }
  chatStreams.delete(chatId);
  renderRecentChats();
  if (chatId !== activeChatId) return;
  syncComposerForActiveChat();
  if (!options.keepBackgroundWatch && window.llmIsLocalServer?.()) {
    if (isActiveChatSending()) {
      window.KernelPanel?.stopPolling?.();
    } else {
      window.KernelPanel?.refresh?.({ force: true });
      window.KernelPanel?.startPolling?.(12000);
    }
  }
}
let chatHistory = [];
let activeChatId = null;
let currentAnalysisCategory = "";

const CHAT_STORE_KEY = "srnagent-chat-sessions";
const AUTO_APPROVE_KEY = "srnagent-auto-approve-code";
const MAX_STORED_CHATS = 40;

/** "server" = read/write sessions only via serve.py; "local" = offline localStorage fallback */
let chatPersistenceMode = "pending";

let chatStore = { activeChatId: null, chats: [] };
let autoApproveCode = loadAutoApproveSetting();
let serverSessionSaveTimer = null;

const categoryLabels = {
  normalization: "Normalization",
  qc: "Quality Control",
  feature: "Feature Selection",
  dimreduction: "Dimensionality Reduction",
  clustering: "Clustering",
  deg: "Differential Expression",
  dct: "Differential Cell Type",
  annotation: "Cell Annotation",
  trajectory: "Trajectory Analysis",
  "env-install": "Python Package Install",
  "env-info": "Python Env Info",
};

function setTheme(mode) {
  document.documentElement.setAttribute("data-theme-mode", mode);
  localStorage.setItem("ui-mock-theme", mode);
}

function initTheme() {
  const saved = localStorage.getItem("ui-mock-theme");
  setTheme(saved === "light" ? "light" : "dark");
}

function scrollThreadToBottom() {
  if (!chatScroll) return;
  requestAnimationFrame(() => {
    chatScroll.scrollTop = chatScroll.scrollHeight;
  });
}

function scrollCodePanelToBottom() {
  const inner = getCodePanelInner();
  if (!inner) return;
  requestAnimationFrame(() => {
    inner.scrollTop = inner.scrollHeight;
  });
}

function isProgressNoiseLine(line) {
  const text = String(line || "");
  if (!text) return true;
  if (text.includes("__SRNAGENT_DL__")) return true;
  if (/\d+%\|[\s█▏▎▍▌▋▊▉]+\|/.test(text)) return true;
  if (/overallPct/i.test(text) && /bytesTotal/i.test(text)) return true;
  if (/\[\d{2}:\d{2}<\d{2}:\d{2},\s*\d+[kMG]?B\/s\]/.test(text)) return true;
  return false;
}

function filterExecutionHighlights(highlights, artifact) {
  const items = Array.isArray(highlights) ? highlights.filter(Boolean) : [];
  const filtered = items.filter((line) => !isProgressNoiseLine(line));
  if (artifact?.isDownloadTask) return [];
  return filtered;
}

function isDownloadProgressArtifact(artifact) {
  if (!artifact || !artifact.isDownloadTask) return false;
  return Boolean(
    artifact.progressRun
    || artifact.progressFileTotal
    || artifact.progressBytesTotal
    || artifact.progressLabel
    || Number(artifact.progressBytes) > 0
    || artifact.progressIndeterminate,
  );
}

function stripDownloadProgressFields(artifact) {
  if (!artifact) return artifact;
  const next = { ...artifact };
  delete next.progressOverallPct;
  delete next.progressFilePct;
  delete next.progressRun;
  delete next.progressFileIndex;
  delete next.progressFileTotal;
  delete next.progressBytes;
  delete next.progressBytesTotal;
  delete next.progressLabel;
  delete next.progressIndeterminate;
  next.isDownloadTask = false;
  return next;
}

function normalizeExecutionArtifact(artifact) {
  if (!artifact || artifact.type !== "execution") return artifact;
  if (artifact.done || artifact.stopped) {
    return stripDownloadProgressFields(artifact);
  }
  return artifact;
}

function welcomeCardHtml() {
  return `
    <div class="welcome-card">
      <h2>sRNA Agent</h2>
      <p>Welcome! This is an agent for small RNA analysis</p>
    </div>
  `;
}

function loadAutoApproveSetting() {
  try {
    return localStorage.getItem(AUTO_APPROVE_KEY) === "true";
  } catch (_error) {
    return false;
  }
}

function setAutoApproveCode(enabled) {
  autoApproveCode = Boolean(enabled);
  try {
    localStorage.setItem(AUTO_APPROVE_KEY, autoApproveCode ? "true" : "false");
  } catch (_error) {
    // ignore storage failures
  }
  updateAutoApproveUi();
  if (autoApproveCode) {
    approvePendingCodeCards({ auto: true });
  }
}

function updateAutoApproveUi() {
  if (!autoApproveToggle) return;
  autoApproveToggle.setAttribute("aria-pressed", autoApproveCode ? "true" : "false");
  autoApproveToggle.textContent = autoApproveCode ? "自动批准：开" : "自动批准：关";
  autoApproveToggle.classList.toggle("code__auto-approve-btn--active", autoApproveCode);
}

function normalizeMessage(item) {
  const role = item?.role === "user" ? "user" : "assistant";
  const message = {
    role,
    content: role === "assistant"
      ? stripExecutionMemoryBlock(item?.content)
      : String(item?.content || ""),
  };
  if (role === "assistant" && Array.isArray(item?.thinkingSteps)) {
    message.thinkingSteps = item.thinkingSteps.map((step) => ({
      kind: String(step?.kind || "tool"),
      title: String(step?.title || ""),
      body: step?.body ? String(step.body) : "",
    }));
  }
  if (role === "assistant" && Array.isArray(item?.executionLog)) {
    message.executionLog = item.executionLog.map((entry) => ({
      tool: String(entry?.tool || ""),
      title: String(entry?.title || ""),
      summary: String(entry?.summary || ""),
    }));
  }
  if (role === "assistant" && item?.stopped === true) {
    message.stopped = true;
  }
  return message;
}

function normalizeChat(chat) {
  return {
    id: chat.id,
    title: chat.title || "New Chat",
    messages: Array.isArray(chat.messages) ? chat.messages.map(normalizeMessage) : [],
    codePanel: Array.isArray(chat.codePanel) ? chat.codePanel : [],
    createdAt: chat.createdAt || Date.now(),
    updatedAt: chat.updatedAt || Date.now(),
  };
}

function loadChatStoreFromLocal() {
  try {
    const raw = localStorage.getItem(CHAT_STORE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && Array.isArray(parsed.chats)) {
        return {
          activeChatId: parsed.activeChatId || null,
          chats: parsed.chats.map(normalizeChat),
        };
      }
    }
  } catch (_error) {
    // ignore corrupt storage
  }
  return { activeChatId: null, chats: [] };
}

function saveChatStore() {
  if (chatPersistenceMode === "local") {
    localStorage.setItem(CHAT_STORE_KEY, JSON.stringify(chatStore));
  }
  if (chatPersistenceMode === "server") {
    scheduleServerSessionSave();
  }
}

function scheduleServerSessionSave() {
  if (chatPersistenceMode !== "server" || !window.saveChatSession) return;
  if (serverSessionSaveTimer) {
    window.clearTimeout(serverSessionSaveTimer);
  }
  serverSessionSaveTimer = window.setTimeout(() => {
    serverSessionSaveTimer = null;
    void flushServerSessionSave();
  }, 400);
}

async function flushServerSessionSave() {
  if (chatPersistenceMode !== "server" || !window.saveChatSession) return;
  const chatsToSave = chatStore.chats.filter(
    (chat) => Array.isArray(chat.messages) && chat.messages.length > 0,
  );
  if (!chatsToSave.length) return;
  try {
    await Promise.all(
      chatsToSave.map((chat) =>
        window.saveChatSession({
          chatId: chat.id,
          chat,
          activeChatId: activeChatId || chatStore.activeChatId || chat.id,
        }),
      ),
    );
  } catch {
    // best-effort server persistence
  }
}

function getActiveChatRecord() {
  return chatStore.chats.find((chat) => chat.id === activeChatId) || null;
}

function deriveChatTitle(messages) {
  const firstUser = messages.find((item) => item.role === "user" && String(item.content || "").trim());
  if (!firstUser) return "New Chat";
  const text = String(firstUser.content).trim().replace(/\s+/g, " ");
  return text.length > 42 ? `${text.slice(0, 42)}…` : text;
}

function ensureActiveChatRecord() {
  if (!activeChatId) activeChatId = createId();
  let chat = getActiveChatRecord();
  if (!chat) {
    chat = {
      id: activeChatId,
      title: "New Chat",
      messages: [],
      codePanel: [],
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
    chatStore.chats.unshift(chat);
  }
  if (!Array.isArray(chat.codePanel)) chat.codePanel = [];
  chatStore.activeChatId = activeChatId;
  return chat;
}

function recordThinkingStep(step) {
  const chat = ensureActiveChatRecord();
  const entry = chatHistory[chatHistory.length - 1];
  if (!entry || entry.role !== "assistant") return;
  if (!Array.isArray(entry.thinkingSteps)) entry.thinkingSteps = [];
  entry.thinkingSteps.push({
    kind: step.kind || "tool",
    title: step.title || "",
    body: step.body || "",
  });
  persistActiveChat();
}

function recordCodeArtifact(artifact) {
  const chat = ensureActiveChatRecord();
  if (!Array.isArray(chat.codePanel)) chat.codePanel = [];

  if (artifact.type === "execution") {
    const index = artifact.id != null
      ? chat.codePanel.findIndex((item) => item.type === "execution" && item.id === artifact.id)
      : chat.codePanel.findIndex((item) => item.type === "execution" && !item.done && !item.stopped);
    if (index >= 0) {
      const prev = chat.codePanel[index];
      const merged = { ...prev, ...artifact };
      if (prev.done && artifact.done === false) merged.done = true;
      if (prev.stopped && artifact.stopped === false) merged.stopped = true;
      if (merged.done || merged.stopped) {
        Object.assign(merged, stripDownloadProgressFields(merged));
      }
      chat.codePanel[index] = merged;
    } else {
      chat.codePanel.push(artifact);
    }
  } else {
    const existingIndex = chat.codePanel.findIndex(
      (item) => item.type === "approval" && item.id === artifact.id,
    );
    if (existingIndex >= 0) {
      chat.codePanel[existingIndex] = { ...chat.codePanel[existingIndex], ...artifact };
    } else {
      chat.codePanel.push(artifact);
    }
  }
  persistActiveChat();
}

function isPlaceholderAssistantContent(content) {
  const text = String(content || "").trim();
  if (!text) return true;
  return (
    text === "思考中…"
    || text.startsWith("正在")
    || text.startsWith("Agent ")
    || text.startsWith("调用失败：")
    || text === "（已停止生成）"
    || text === "（流式连接已结束，未检测到后台任务）"
    || text.startsWith("（流式连接已结束")
    || text.startsWith("（后台运行中")
    || text.includes("CODE 面板仍有任务执行中")
    || text.includes("任务仍在进行")
    || text.includes("任务已中断")
  );
}

async function fetchRunStatus(chatId) {
  if (!chatId || !window.fetchAgentRunStatus) return null;
  try {
    return await window.fetchAgentRunStatus(chatId);
  } catch {
    return null;
  }
}

function hasRunningCodePanelExecution(chatId = activeChatId) {
  const chat = chatId === activeChatId ? getActiveChatRecord() : getChatRecord(chatId);
  if (!chat?.codePanel) return false;
  return chat.codePanel.some(
    (item) => item.type === "execution" && !item.done && !item.stopped,
  );
}

function isTaskLikelyActive(status, chatId = activeChatId) {
  if (status?.ok) {
    return Boolean(status.hasActiveRun || status.kernelBusy);
  }
  return false;
}

function isStaleTaskState(status) {
  if (!status?.ok) return false;
  return Boolean(
    status.stalePlanStep
    || status.staleCodePanel
    || ((status.planStepRunning || status.codePanelRunning) && !status.hasActiveRun && !status.kernelBusy),
  );
}

function buildInterruptedStatusMessage(status) {
  const parts = [];
  if (status?.planSummary) parts.push(status.planSummary);
  parts.push("（任务已中断，可继续对话或重新发送指令）");
  return parts.join(" · ");
}

function buildRunStatusMessage(status) {
  if (!status?.ok) return "";
  const parts = [];
  if (status.planSummary) parts.push(status.planSummary);
  else if (status.kernelBusy) parts.push("Jupyter 内核正在执行代码");
  else if (status.hasActiveRun) parts.push("Agent 正在运行");

  if (isStaleTaskState(status)) {
    parts.push("（任务已中断，计划/CODE 状态未同步）");
  } else if (status.backgroundActive && !isChatStreaming(status.chatId || activeChatId)) {
    parts.push("（流式连接已断开，内核仍在执行）");
  } else if ((status.hasActiveRun || status.kernelBusy) && isChatStreaming(status.chatId || activeChatId)) {
    parts.push("（长任务执行中，请稍候）");
  }
  return parts.join(" · ") || "任务运行中…";
}

function reconcileStaleTaskUi(chatId, status, options = {}) {
  if (!isStaleTaskState(status)) return false;
  const { pending = null, assistantEntry = null, persist = true } = options;
  const message = buildInterruptedStatusMessage(status);

  markRunningExecutionsStopped();
  finishBackgroundExecutionCard();
  stopBackgroundRunWatch(chatId);

  if (assistantEntry) {
    assistantEntry.content = message;
  }
  if (chatId === activeChatId) {
    const target = resolvePendingGroup(pending) || getLastAssistantGroup();
    if (target) {
      const textEl = target.querySelector(".chat-text");
      if (textEl) {
        textEl.classList.remove("chat-text--loading");
        textEl.textContent = message;
      }
    }
    renderCodePanel(getActiveChatRecord()?.codePanel || [], { interactive: false });
  }
  if (persist && chatId) {
    persistChatMessages(chatId, chatHistory);
  }
  return true;
}

function ensureBackgroundExecutionCard(status, { showStop = false } = {}) {
  // 已有真实 execute_code 进度卡片时，不要再叠一张「Agent 运行中」导致 CODE 区混乱
  if (activeCodeExecutionId) return null;
  const codeInner = getCodePanelInner();
  if (!codeInner) return null;

  const runningRealCard = codeInner.querySelector(
    `.code-execution-progress--running:not([data-execution-id="${BACKGROUND_EXECUTION_ID}"])`,
  );
  if (runningRealCard) return null;

  showCodePanel();
  let card = getExecutionCard(BACKGROUND_EXECUTION_ID);
  if (!card) {
    card = createExecutionCardElement(BACKGROUND_EXECUTION_ID, { showStop });
    codeInner.appendChild(card);
  }

  const stage = status?.planSummary
    || (status?.runningStepTitle ? `执行：${status.runningStepTitle}` : "")
    || (status?.kernelBusy ? "内核执行中" : "等待中");
  const title = status?.hasActiveRun ? "Agent 运行中" : "后台代码运行中";
  const hint = status?.backgroundActive
    ? "流式连接已断开，正在轮询后端状态。请勿重复发送消息以免打断任务。"
    : "长任务执行中，界面将自动刷新进度。";

  applyExecutionCardState(
    card,
    {
      type: "execution",
      id: BACKGROUND_EXECUTION_ID,
      title,
      description: status?.plan?.goal || "",
      stage,
      done: false,
      stopped: false,
      hint,
    },
    { showStop },
  );
  recordCodeArtifact({
    type: "execution",
    id: BACKGROUND_EXECUTION_ID,
    title,
    description: status?.plan?.goal || "",
    stage,
    done: false,
    stopped: false,
    hint,
  });
  scrollCodePanelToBottom();
  return card;
}

function removeBackgroundExecutionCard() {
  const card = getExecutionCard(BACKGROUND_EXECUTION_ID);
  if (card) card.remove();
  const chat = getActiveChatRecord();
  if (!chat?.codePanel) return;
  const before = chat.codePanel.length;
  chat.codePanel = chat.codePanel.filter(
    (item) => !(item.type === "execution" && item.id === BACKGROUND_EXECUTION_ID),
  );
  if (chat.codePanel.length !== before) persistActiveChat();
  syncCodePanelVisibility();
}

function finishBackgroundExecutionCard() {
  const card = getExecutionCard(BACKGROUND_EXECUTION_ID);
  if (!card) return;
  const chat = getActiveChatRecord();
  const existing = chat?.codePanel?.find(
    (item) => item.type === "execution" && item.id === BACKGROUND_EXECUTION_ID,
  );
  const artifact = stripDownloadProgressFields({
    type: "execution",
    id: BACKGROUND_EXECUTION_ID,
    title: "代码运行完成",
    description: existing?.description || "",
    code: existing?.code || "",
    stage: "已完成",
    done: true,
    stopped: false,
    hint: "",
  });
  applyExecutionCardState(card, artifact);
  recordCodeArtifact(artifact);
}

function syncRunStatusToUI(chatId, status, options = {}) {
  if (!isTaskLikelyActive(status, chatId)) return false;

  const message = buildRunStatusMessage(status);
  const { pending = null, assistantEntry = null, persist = true } = options;

  if (assistantEntry && message) {
    assistantEntry.content = message;
  }

  if (chatId === activeChatId) {
    const target = resolvePendingGroup(pending) || getLastAssistantGroup();
    if (target && message) {
      const textEl = target.querySelector(".chat-text");
      if (textEl) {
        textEl.classList.add("chat-text--loading");
        textEl.textContent = message;
      }
    }
    if (status.plan?.steps?.length && target) {
      const done = status.plan.steps.filter((s) => s.status === "done").length;
      const total = status.plan.steps.length;
      const planTitle = status.plan.goal || "执行计划";
      const stepsEl = getThinkingStepsEl(target);
      const existingPlan = stepsEl?.querySelector(".chat-thinking__step--plan");
      if (!existingPlan) {
        appendThinkingStep(
          target,
          {
            kind: "plan",
            title: `${planTitle} (${done}/${total})`,
            body: status.plan.steps
              .map((s) => {
                const mark =
                  s.status === "done" ? "✓" : s.status === "running" ? "▶" : s.status === "failed" ? "✗" : "○";
                return `${mark} ${s.title || s.goal || s.id}`;
              })
              .join("\n"),
          },
          { persist: false },
        );
      }
    }
    ensureBackgroundExecutionCard(status, { showStop: isActiveChatSending() });
    scrollThreadToBottom();
  }

  if (persist && assistantEntry && chatId) {
    const messages = options.messages || chatHistory;
    persistChatMessages(chatId, messages);
  }
  return true;
}

function stopBackgroundRunWatch(chatId) {
  const watch = backgroundWatches.get(chatId);
  if (!watch) return;
  if (watch.timer) window.clearInterval(watch.timer);
  backgroundWatches.delete(chatId);
}

async function pollBackgroundRunStatus(chatId) {
  const watch = backgroundWatches.get(chatId);
  if (!watch) return;

  const status = await fetchRunStatus(chatId);
  if (isStaleTaskState(status)) {
    reconcileStaleTaskUi(chatId, status, {
      pending: watch.pending,
      assistantEntry: watch.assistantEntry,
    });
    return;
  }
  if (!isTaskLikelyActive(status, chatId)) {
    stopBackgroundRunWatch(chatId);
    if (chatId === activeChatId) {
      finishBackgroundExecutionCard();
      markRunningExecutionsStopped();
      if (watch.assistantEntry) {
        const finalText = status?.planSummary
          ? `${status.planSummary}（任务已结束，可继续对话）`
          : "（任务已结束，可继续对话）";
        watch.assistantEntry.content = finalText;
        updateMessageGroup(resolvePendingGroup(watch.pending), finalText);
        persistChatMessages(chatId, chatHistory);
      }
    }
    return;
  }

  syncRunStatusToUI(chatId, status, {
    pending: watch.pending,
    assistantEntry: watch.assistantEntry,
    messages: watch.assistantEntry ? chatHistory : undefined,
  });
}

function startBackgroundRunWatch(chatId, { pending = null, assistantEntry = null } = {}) {
  if (!chatId) return;
  stopBackgroundRunWatch(chatId);
  backgroundWatches.set(chatId, {
    timer: window.setInterval(() => {
      void pollBackgroundRunStatus(chatId);
    }, BACKGROUND_WATCH_POLL_MS),
    startedAt: Date.now(),
    pending,
    assistantEntry,
  });
  void pollBackgroundRunStatus(chatId);
}

async function resumeBackgroundRunIfNeeded(chatId) {
  if (!chatId || isChatStreaming(chatId)) return;
  const status = await fetchRunStatus(chatId);
  if (isStaleTaskState(status)) {
    const lastAssistant = [...chatHistory].reverse().find((item) => item.role === "assistant");
    reconcileStaleTaskUi(chatId, status, { assistantEntry: lastAssistant || null });
    return;
  }
  if (!isTaskLikelyActive(status, chatId)) return;

  const lastAssistant = (chatId === activeChatId
    ? [...chatHistory]
    : [...(getChatRecord(chatId)?.messages || [])]
  ).reverse().find((item) => item.role === "assistant");

  // 优先接入服务端实时事件广播（思考流 / 下载进度）
  void attachLiveEventStream(chatId, status);

  if (!backgroundWatches.has(chatId)) {
    startBackgroundRunWatch(chatId, { assistantEntry: lastAssistant || null });
  }
  syncRunStatusToUI(chatId, status, {
    assistantEntry: lastAssistant || null,
    persist: false,
  });
}

function detachLiveEventStream(chatId) {
  const follow = liveFollows.get(chatId);
  if (!follow) return;
  try {
    follow.abortController.abort();
  } catch {
    // ignore
  }
  liveFollows.delete(chatId);
}

function ensureFollowerStreamShell(chatId, follow, messages, assistantEntry) {
  if (chatStreams.has(chatId)) return chatStreams.get(chatId);
  const shell = {
    generation: -1,
    runId: follow?.runId || "",
    abortController: follow?.abortController || new AbortController(),
    messages,
    assistantEntry,
    pending: null,
    idleTimer: null,
    statusPollTimer: null,
    lastStreamEventAt: Date.now(),
    codeExecutionId: follow?.codeExecutionId || null,
    isFollower: true,
  };
  chatStreams.set(chatId, shell);
  return shell;
}

function applyLiveFollowEvent(chatId, event) {
  if (!chatId || !event?.type) return;
  const follow = liveFollows.get(chatId);
  if (follow && event._seq != null) {
    follow.lastSeq = Math.max(follow.lastSeq || 0, Number(event._seq) || 0);
  }
  if (follow && (event.runId || event.type === "live_joined")) {
    follow.runId = String(event.runId || follow.runId || "");
  }

  if (event.type === "live_joined") {
    if (chatId === activeChatId) {
      const target = getLastAssistantGroup();
      const textEl = target?.querySelector(".chat-text");
      if (textEl) {
        textEl.classList.add("chat-text--loading");
        textEl.textContent = event.message || "已加入实时同步…";
      }
    }
    return;
  }

  const chat = ensureChatRecord(chatId);
  const messages = chat.messages || [];
  let assistantEntry = [...messages].reverse().find((item) => item.role === "assistant");
  if (!assistantEntry) {
    assistantEntry = { role: "assistant", content: "实时同步中…", thinkingSteps: [], executionLog: [] };
    messages.push(assistantEntry);
    chat.messages = messages;
  }
  if (chatId === activeChatId) {
    chatHistory = messages;
  }

  const isVisible = chatId === activeChatId;
  if (!isVisible) {
    const shell = ensureFollowerStreamShell(chatId, follow, messages, assistantEntry);
    if (follow) {
      shell.runId = follow.runId || shell.runId;
      shell.codeExecutionId = follow.codeExecutionId;
    }
    handleAgentStreamEventBackground(chatId, messages, assistantEntry, event);
    if (follow) {
      follow.codeExecutionId = chatStreams.get(chatId)?.codeExecutionId || follow.codeExecutionId;
    }
    persistChatMessages(chatId, messages);
  } else {
    let target = getLastAssistantGroup();
    if (!target) {
      target = appendMessage("assistant", assistantEntry.content || "实时同步中…", {
        loading: true,
      });
    }
    handleAgentStreamEvent(target, event);
    if (event.type === "final" && event.content) {
      assistantEntry.content = stripExecutionMemoryBlock(event.content);
      persistChatMessages(chatId, messages);
    }
    if (event.type === "done" && event.text) {
      assistantEntry.content = stripExecutionMemoryBlock(event.text);
      updateMessageGroup(target, assistantEntry.content);
      persistChatMessages(chatId, messages);
    }
    if (follow && activeCodeExecutionId) {
      follow.codeExecutionId = activeCodeExecutionId;
    }
  }

  if (event.type === "done" || event.type === "cancelled" || event.type === "error" || event.type === "stream_end") {
    const stream = chatStreams.get(chatId);
    if (stream?.isFollower) {
      chatStreams.delete(chatId);
    }
    if (chatId === activeChatId) {
      window.KernelPanel?.refresh?.({ force: true });
      syncComposerForActiveChat();
    }
  }
}

async function attachLiveEventStream(chatId, status = null) {
  if (!chatId || isChatStreaming(chatId) || liveFollows.has(chatId)) return;
  if (!window.agentLiveEventStream) return;

  let snap = status;
  if (!snap) {
    snap = await fetchRunStatus(chatId);
  }
  if (!snap?.ok) return;
  if (!(snap.hasActiveRun || snap.kernelBusy || snap.liveAvailable)) return;

  const abortController = new AbortController();
  liveFollows.set(chatId, {
    abortController,
    runId: String(snap.runId || ""),
    codeExecutionId: null,
    lastSeq: 0,
  });

  if (chatId === activeChatId) {
    syncComposerForActiveChat();
    const target = getLastAssistantGroup();
    if (target) {
      const textEl = target.querySelector(".chat-text");
      if (textEl && isPlaceholderAssistantContent(textEl.textContent)) {
        textEl.classList.add("chat-text--loading");
        textEl.textContent = "正在接入实时同步…";
      }
    }
  }

  void (async () => {
    try {
      await window.agentLiveEventStream({
        chatId,
        afterSeq: 0,
        signal: abortController.signal,
        onEvent: (event) => applyLiveFollowEvent(chatId, event),
      });
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        console.warn("live event stream error", error);
      }
    } finally {
      liveFollows.delete(chatId);
      const stream = chatStreams.get(chatId);
      if (stream?.isFollower) chatStreams.delete(chatId);
      if (chatId === activeChatId) syncComposerForActiveChat();
    }
  })();
}

function isStoppedAssistantEntry(item) {
  if (!item || item.role !== "assistant") return false;
  if (item.stopped === true) return true;
  const text = String(item.content || "").trim();
  return text === "（已停止生成）";
}

function markAssistantStopped(assistantEntry) {
  if (!assistantEntry) return;
  assistantEntry.stopped = true;
  assistantEntry.content = "（已停止生成）";
}

function truncateExecutionSummary(text, max = 360) {
  const value = String(text || "").trim();
  if (value.length <= max) return value;
  return `${value.slice(0, max - 1)}…`;
}

function stripExecutionMemoryBlock(content) {
  const text = String(content || "");
  const marker = "\n\n[本轮执行记录]\n";
  const idx = text.indexOf(marker);
  if (idx >= 0) return text.slice(0, idx).trim();
  if (text.startsWith("[本轮执行记录]\n")) return "";
  return text.trim();
}

function assistantContentForDisplay(item) {
  return stripExecutionMemoryBlock(item?.content);
}

function formatExecutionLogBlock(logs) {
  if (!Array.isArray(logs) || !logs.length) return "";
  return logs
    .slice(-12)
    .map((log) => {
      const title = String(log?.title || log?.tool || "步骤").trim();
      const summary = String(log?.summary || "").trim();
      return summary ? `- ${title}: ${summary}` : `- ${title}`;
    })
    .filter(Boolean)
    .join("\n");
}

function buildExecutionContextForApi() {
  const sections = [];
  for (const item of chatHistory) {
    if (item.role !== "assistant") continue;
    const block = formatExecutionLogBlock(item.executionLog);
    if (block) sections.push(block);
  }
  return sections.slice(-2).join("\n\n");
}

function appendExecutionLog(entry, assistant = getLastAssistantEntry(), options = {}) {
  if (!assistant) return;
  if (!Array.isArray(assistant.executionLog)) assistant.executionLog = [];
  const tool = String(entry?.tool || "").trim();
  const title = String(entry?.title || tool || "步骤").trim();
  const summary = truncateExecutionSummary(entry?.summary || "");
  const last = assistant.executionLog[assistant.executionLog.length - 1];
  if (last && !last.summary && summary && (last.tool === tool || last.title === title)) {
    last.summary = summary;
    if (options.chatId && options.messages) {
      persistChatMessages(options.chatId, options.messages);
    } else {
      persistActiveChat();
    }
    return;
  }
  if (last && last.title === title && last.summary === summary) return;
  assistant.executionLog.push({ tool, title, summary });
  if (assistant.executionLog.length > 24) {
    assistant.executionLog = assistant.executionLog.slice(-24);
  }
  if (options.chatId && options.messages) {
    persistChatMessages(options.chatId, options.messages);
  } else {
    persistActiveChat();
  }
}

function messagesForAgentApi() {
  const result = [];
  for (let i = 0; i < chatHistory.length; i += 1) {
    const item = chatHistory[i];
    if (item.role === "user") {
      const next = chatHistory[i + 1];
      if (next && isStoppedAssistantEntry(next)) {
        i += 1;
        continue;
      }
      result.push({ role: "user", content: String(item.content || "") });
      continue;
    }
    if (item.role !== "assistant") continue;
    const content = assistantContentForDisplay(item);
    if (isPlaceholderAssistantContent(content)) continue;
    result.push({ role: "assistant", content });
  }
  if (result.length === 0) {
    const lastUser = [...chatHistory].reverse().find((item) => item.role === "user" && String(item.content || "").trim());
    if (lastUser) {
      result.push({ role: "user", content: String(lastUser.content || "") });
    }
  }
  return result;
}

function abandonStaleAssistantPlaceholders(messages) {
  messages.forEach((item) => {
    if (
      item?.role === "assistant"
      && !item.stopped
      && isPlaceholderAssistantContent(item.content)
    ) {
      markAssistantStopped(item);
    }
  });
}

function getLastAssistantEntry() {
  for (let i = chatHistory.length - 1; i >= 0; i -= 1) {
    if (chatHistory[i]?.role === "assistant") return chatHistory[i];
  }
  return null;
}

function removeEmptyChat(chatId) {
  if (!chatId) return;
  const chat = chatStore.chats.find((item) => item.id === chatId);
  if (chat && chat.messages.length === 0) {
    chatStore.chats = chatStore.chats.filter((item) => item.id !== chatId);
  }
}

function assistantHasPersistableContent(item) {
  return (
    String(item?.content || "").trim() ||
    (Array.isArray(item?.thinkingSteps) && item.thinkingSteps.length > 0) ||
    (Array.isArray(item?.executionLog) && item.executionLog.length > 0)
  );
}

function persistActiveChat() {
  if (!activeChatId) return;
  chatStore.activeChatId = activeChatId;
  persistChatMessages(activeChatId, chatHistory);
}

function recordCodeArtifactForChat(chatId, artifact) {
  const chat = ensureChatRecord(chatId);
  if (!chat || !Array.isArray(chat.codePanel)) return;

  if (artifact.type === "execution") {
    const index = chat.codePanel.findIndex(
      (item) => item.type === "execution" && item.id === artifact.id,
    );
    if (index >= 0) {
      const merged = { ...chat.codePanel[index], ...artifact };
      chat.codePanel[index] = merged;
    } else {
      chat.codePanel.push(artifact);
    }
  } else {
    const existingIndex = chat.codePanel.findIndex(
      (item) => item.type === "approval" && item.id === artifact.id,
    );
    if (existingIndex >= 0) {
      chat.codePanel[existingIndex] = { ...chat.codePanel[existingIndex], ...artifact };
    } else {
      chat.codePanel.push(artifact);
    }
  }
  saveChatStore();
  scheduleServerSessionSave(chatId);
  renderRecentChats();
}

function resetCodePanel() {
  if (agentCodeInner) agentCodeInner.innerHTML = "";
  syncCodePanelVisibility();
}

function renderChatThread() {
  if (!threadInner) return;
  threadInner.innerHTML = "";
  if (chatHistory.length === 0) {
    threadInner.innerHTML = welcomeCardHtml();
    renderCodePanel(getActiveChatRecord()?.codePanel || [], { interactive: isActiveChatSending() });
    scrollThreadToBottom();
    return;
  }
  chatHistory.forEach((item) => {
    if (item.role !== "user" && item.role !== "assistant") return;
    const fallback = item.role === "assistant" && item.thinkingSteps?.length ? "" : "（无回复）";
    const group = appendMessage(item.role, item.role === "assistant" ? assistantContentForDisplay(item) || fallback : (item.content || fallback));
    if (item.role === "assistant" && Array.isArray(item.thinkingSteps) && item.thinkingSteps.length) {
      item.thinkingSteps.forEach((step) => appendThinkingStep(group, step, { persist: false }));
      const thinkingEl = group.querySelector(".chat-thinking");
      if (thinkingEl) thinkingEl.open = false;
    }
  });
  renderCodePanel(getActiveChatRecord()?.codePanel || [], { interactive: isActiveChatSending() });
}

function deleteChat(chatId, event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  if (!chatId) return;
  if (isChatStreaming(chatId)) return;

  window.releaseChatKernel?.(chatId);

  const wasActive = chatId === activeChatId;
  chatStore.chats = chatStore.chats.filter((item) => item.id !== chatId);

  if (wasActive) {
    const nextChat = [...chatStore.chats]
      .filter((chat) => chat.messages?.length > 0)
      .sort((a, b) => b.createdAt - a.createdAt)[0];

    if (nextChat) {
      activeChatId = nextChat.id;
      chatHistory = nextChat.messages.map((item) => normalizeMessage(item));
    } else {
      activeChatId = createId();
      chatHistory = [];
    }
    chatStore.activeChatId = activeChatId;
    renderChatThread();
    resetComposer();
    window.KernelPanel?.refresh?.({ force: true });
  }

  saveChatStore();
  renderRecentChats();
}

function renderRecentChats() {
  if (!chatRecentList) return;
  chatRecentList.innerHTML = "";

  const chats = [...chatStore.chats]
    .filter((chat) => chat.messages?.length > 0)
    .sort((a, b) => b.createdAt - a.createdAt);

  if (chats.length === 0) {
    const empty = document.createElement("div");
    empty.className = "sidebar-recent-sessions__empty";
    empty.textContent = "No chats yet";
    chatRecentList.appendChild(empty);
    return;
  }

  chats.forEach((chat) => {
    const item = document.createElement("div");
    item.className = "sidebar-recent-session";
    if (isChatStreaming(chat.id) || liveFollows.has(chat.id)) {
      item.classList.add("sidebar-recent-session--streaming");
    }
    if (chat.id === activeChatId) item.classList.add("sidebar-recent-session--active");

    const link = document.createElement("a");
    link.className = "sidebar-recent-session__link";
    link.href = "#";
    link.textContent = chat.title || "New Chat";
    link.title = chat.title || "New Chat";
    link.addEventListener("click", (event) => {
      event.preventDefault();
      loadChat(chat.id);
    });

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "sidebar-recent-session__delete";
    deleteBtn.setAttribute("aria-label", "Delete chat");
    deleteBtn.title = "Delete chat";
    deleteBtn.textContent = "×";
    if (isChatStreaming(chat.id)) {
      deleteBtn.disabled = true;
    }
    deleteBtn.addEventListener("click", (event) => deleteChat(chat.id, event));

    item.appendChild(link);
    item.appendChild(deleteBtn);
    chatRecentList.appendChild(item);
  });
}

function startNewChat() {
  const previousChatId = activeChatId;
  persistActiveChat();
  removeEmptyChat(activeChatId);

  // 每个 chat 有独立内核：开新对话时不要 cancel/interrupt 旧会话的 Agent / Jupyter。
  // 旧会话若仍在跑，SSE 继续后台接收事件并写入该会话的记录。
  if (previousChatId && isChatStreaming(previousChatId)) {
    renderRecentChats();
  }

  // 切到新会话时，清掉「当前可见」代码执行指针；后台会话用 stream.codeExecutionId
  activeCodeExecutionId = null;

  activeChatId = createId();
  chatHistory = [];
  chatStore.activeChatId = activeChatId;
  ensureChatRecord(activeChatId);
  saveChatStore();

  renderChatThread();
  resetComposer();
  syncComposerForActiveChat();
  setPage("agent");
  window.KernelPanel?.refresh?.({ force: true });
  window.KernelPanel?.startPolling?.(12000);
}

function loadChat(chatId) {
  if (!chatId || chatId === activeChatId) return;
  persistActiveChat();

  const chat = chatStore.chats.find((item) => item.id === chatId);
  if (!chat) return;

  activeChatId = chat.id;
  chatStore.activeChatId = activeChatId;
  chatHistory = chat.messages.map((item) => normalizeMessage(item));
  // 切回仍在跑的会话时，恢复该会话自己的代码执行卡片 ID
  activeCodeExecutionId = chatStreams.get(chatId)?.codeExecutionId || null;
  saveChatStore();

  renderChatThread();
  resetComposer();
  syncComposerForActiveChat();
  setPage("agent");
  void resumeBackgroundRunIfNeeded(chatId);
  if (isActiveChatSending()) {
    window.KernelPanel?.stopPolling?.();
  } else {
    window.KernelPanel?.refresh?.({ force: true });
  }
}

async function syncChatStoreFromServer() {
  if (!window.fetchChatSessions) return false;
  try {
    const data = await window.fetchChatSessions();
    if (!data?.ok || !Array.isArray(data.chats)) return false;
    if (data.chats.length > 0) {
      chatStore = {
        activeChatId: data.activeChatId || data.chats[0]?.id || null,
        chats: data.chats.map(normalizeChat),
      };
    } else {
      chatStore = { activeChatId: null, chats: [] };
    }
    return true;
  } catch {
    return false;
  }
}

async function refreshChatStoreFromServerIfIdle() {
  if (chatPersistenceMode !== "server" || isActiveChatSending()) return;
  const prevActive = activeChatId;
  const synced = await syncChatStoreFromServer();
  if (!synced) return;

  const activeChat = chatStore.chats.find((item) => item.id === prevActive && item.messages?.length > 0);
  if (activeChat) {
    activeChatId = prevActive;
    chatStore.activeChatId = prevActive;
    chatHistory = activeChat.messages.map((item) => normalizeMessage(item));
    renderChatThread();
    renderRecentChats();
    return;
  }
  applyActiveChatFromStore();
}

function applyActiveChatFromStore() {
  const savedActive = chatStore.activeChatId;
  const savedChat = chatStore.chats.find((item) => item.id === savedActive && item.messages?.length > 0);
  if (savedChat) {
    activeChatId = savedChat.id;
    chatHistory = savedChat.messages.map((item) => normalizeMessage(item));
  } else {
    activeChatId = createId();
    chatHistory = [];
    chatStore.activeChatId = activeChatId;
  }
  saveChatStore();
  renderChatThread();
  renderRecentChats();
}

async function initChatSessions() {
  const serverOk = await window.probeProxyServer?.();
  if (serverOk) {
    chatPersistenceMode = "server";
    try {
      localStorage.removeItem(CHAT_STORE_KEY);
    } catch {
      // ignore storage failures
    }
    await syncChatStoreFromServer();
  } else {
    chatPersistenceMode = "local";
    chatStore = loadChatStoreFromLocal();
  }

  applyActiveChatFromStore();
  void resumeBackgroundRunIfNeeded(activeChatId);
}

function appendMessage(role, text, options = {}) {
  if (!threadInner) return null;

  const group = document.createElement("div");
  group.className = `chat-group ${role}`;
  group.innerHTML = `
    <div class="chat-bubble">
      <div class="chat-text"></div>
    </div>
  `;
  const textEl = group.querySelector(".chat-text");
  if (options.loading) {
    textEl.classList.add("chat-text--loading");
    textEl.textContent = "思考中…";
  } else {
    textEl.textContent = text;
  }
  threadInner.appendChild(group);
  scrollThreadToBottom();
  return group;
}

function getLastAssistantGroup() {
  if (!threadInner) return null;
  const groups = threadInner.querySelectorAll(".chat-group.assistant");
  return groups.length ? groups[groups.length - 1] : null;
}

function resolvePendingGroup(pending) {
  return pending || getLastAssistantGroup();
}

function showCodePanel() {
  if (agentCodePanel) agentCodePanel.hidden = false;
}

function syncCodePanelVisibility() {
  if (!agentCodePanel || !agentCodeInner) return;
  agentCodePanel.hidden = agentCodeInner.children.length === 0;
}

function getCodePanelInner() {
  return agentCodeInner;
}

function ensureThinkingPanel(group) {
  if (!group || group.querySelector(".chat-thinking")) return;
  const bubble = group.querySelector(".chat-bubble");
  const textEl = group.querySelector(".chat-text");
  if (!bubble || !textEl) return;

  const panel = document.createElement("details");
  panel.className = "chat-thinking";
  panel.open = true;
  panel.innerHTML = `
    <summary class="chat-thinking__summary">Thinking</summary>
    <div class="chat-thinking__steps"></div>
  `;
  bubble.insertBefore(panel, textEl);
}

function getThinkingStepsEl(group) {
  return group?.querySelector(".chat-thinking__steps") || null;
}

function getThinkingSummaryEl(group) {
  return group?.querySelector(".chat-thinking__summary") || null;
}

function appendThinkingStep(group, step, options = {}) {
  ensureThinkingPanel(group);
  const stepsEl = getThinkingStepsEl(group);
  if (!stepsEl) return;

  const item = document.createElement("div");
  item.className = `chat-thinking__step chat-thinking__step--${step.kind}`;

  const title = document.createElement("div");
  title.className = "chat-thinking__step-title";
  title.textContent = step.title;
  item.appendChild(title);

  if (step.body) {
    const body = document.createElement("pre");
    body.className = "chat-thinking__step-body";
    body.textContent = step.body;
    item.appendChild(body);
  }

  stepsEl.appendChild(item);

  const summaryEl = getThinkingSummaryEl(group);
  const count = stepsEl.children.length;
  if (summaryEl) {
    summaryEl.textContent = `Thinking (${count})`;
  }
  scrollThreadToBottom();

  if (options.persist !== false) {
    recordThinkingStep(step);
  }
}

function approvalStatusLabel(status) {
  if (status === "approved") return "已允许运行";
  if (status === "auto-approved") return "已自动允许运行";
  if (status === "denied") return "已拒绝";
  return "";
}

function renderApprovalArtifact(artifact, interactive) {
  const codeInner = getCodePanelInner();
  if (!codeInner) return null;

  showCodePanel();

  const card = document.createElement("div");
  card.className = "code-approval";
  card.dataset.requestId = artifact.id;
  if (artifact.status === "approved" || artifact.status === "auto-approved") {
    card.classList.add("code-approval--approved");
  }
  if (artifact.status === "denied") {
    card.classList.add("code-approval--denied");
  }

  card.innerHTML = `
    <div class="code-approval__title">Agent 请求运行代码</div>
    <div class="code-approval__desc"></div>
    <pre class="code-approval__code"></pre>
    <div class="code-approval__actions"></div>
  `;

  card.querySelector(".code-approval__desc").textContent =
    artifact.description || "即将在当前 conda / Jupyter 环境中执行以下 Python 代码。";
  card.querySelector(".code-approval__code").textContent = artifact.code || "";

  const actions = card.querySelector(".code-approval__actions");
  if (artifact.status && artifact.status !== "pending") {
    const label = approvalStatusLabel(artifact.status);
    actions.innerHTML = `<span class="code-approval__status ${artifact.status === "denied" ? "code-approval__status--denied" : "code-approval__status--ok"}">${label}</span>`;
  } else if (interactive) {
    actions.innerHTML = `
      <button class="btn btn--outline btn--sm code-approval__deny" type="button">拒绝</button>
      <button class="btn btn--primary btn--sm code-approval__allow" type="button">允许运行</button>
      <button class="btn btn--outline btn--sm code-approval__allow-all" type="button">允许所有操作</button>
    `;
  } else {
    actions.innerHTML = '<span class="code-approval__status">等待批准</span>';
  }

  codeInner.appendChild(card);
  scrollCodePanelToBottom();
  return card;
}

function sanitizeCodePanel(codePanel) {
  const items = Array.isArray(codePanel) ? [...codePanel] : [];
  const runningIndices = items
    .map((item, index) => (item.type === "execution" && !item.done && !item.stopped ? index : -1))
    .filter((index) => index >= 0);

  runningIndices.forEach((index, order) => {
    const keepRunning =
      (isChatStreaming(activeChatId) || backgroundWatches.has(activeChatId))
      && order === runningIndices.length - 1;
    if (!keepRunning) {
      items[index] = stripDownloadProgressFields({
        ...items[index],
        done: true,
        stopped: true,
        title: "代码已停止",
        stage: "已终止",
        hint: "",
      });
    }
  });
  return items;
}

function markRunningExecutionsStopped() {
  const chat = ensureActiveChatRecord();
  if (!Array.isArray(chat.codePanel)) return;

  // 背景监控卡直接移除，不要标成「已停止」干扰真实下载进度卡
  removeBackgroundExecutionCard();

  let changed = false;
  chat.codePanel.forEach((item, index) => {
    if (item.type === "execution" && !item.done && !item.stopped) {
      if (item.id === BACKGROUND_EXECUTION_ID) return;
      chat.codePanel[index] = stripDownloadProgressFields({
        ...item,
        done: true,
        stopped: true,
        title: "代码已停止",
        stage: "已终止",
        hint: "",
      });
      changed = true;
    }
  });
  if (changed) persistActiveChat();

  getCodePanelInner()?.querySelectorAll(".code-execution-progress--running").forEach((card) => {
    if (card.dataset.executionId === BACKGROUND_EXECUTION_ID) {
      card.remove();
      return;
    }
    applyExecutionStoppedCard(card);
  });
  activeCodeExecutionId = null;
}

function getExecutionCard(executionId) {
  if (!executionId) return null;
  return getCodePanelInner()?.querySelector(`[data-execution-id="${executionId}"]`) || null;
}

function wireExecutionStopButton(card) {
  card?.querySelector(".code-execution-progress__stop")?.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    void handleStop();
  });
}

function wireExecutionToggleButton(card) {
  const btn = card?.querySelector(".code-execution-progress__toggle");
  if (!btn || btn.dataset.wired === "true") return;
  btn.dataset.wired = "true";
  btn.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    void (async () => {
      const codeEl = card.querySelector(".code-execution-progress__code");
      let code = card.dataset.executionCode || codeEl?.textContent || "";
      if (!code && card.classList.contains("code-execution-progress--done")) {
        if (codeEl) codeEl.textContent = "正在加载代码…";
        await hydrateExecutionCodes();
        const executionId = card.dataset.executionId;
        const chat = getActiveChatRecord();
        const artifact = chat?.codePanel?.find(
          (item) => item.type === "execution" && item.id === executionId,
        );
        code = artifact?.code || card.dataset.executionCode || "";
      }
      if (codeEl) {
        codeEl.textContent = code || "暂无保存的代码记录";
        if (code) card.dataset.executionCode = code;
      }
      const expanded = card.classList.toggle("code-execution-progress--expanded");
      btn.setAttribute("aria-expanded", expanded ? "true" : "false");
      btn.textContent = expanded ? "收起" : "查看代码";
      if (codeEl) codeEl.hidden = !expanded;
    })();
  });
}

function resolveExecutionCode(event, artifact = {}) {
  if (artifact.code) return String(artifact.code);
  if (event?.code) return String(event.code);
  const chat = getActiveChatRecord();
  if (!chat?.codePanel) return "";
  const desc = event?.description || event?.summary || artifact.description || "";
  const approvals = chat.codePanel.filter(
    (item) =>
      item.type === "approval" &&
      (item.status === "approved" || item.status === "auto-approved"),
  );
  const match = [...approvals].reverse().find((item) => item.description === desc);
  return String((match || approvals[approvals.length - 1])?.code || "");
}

function compactCodePanelForRender(codePanel) {
  const items = Array.isArray(codePanel) ? codePanel : [];
  const completedDescriptions = new Set(
    items
      .filter((item) => item.type === "execution" && item.done && !item.stopped)
      .map((item) => item.description)
      .filter(Boolean),
  );
  return items.filter((item) => {
    if (item.type !== "approval") return true;
    if (item.status !== "approved" && item.status !== "auto-approved") return true;
    return !completedDescriptions.has(item.description);
  });
}

async function fetchReplayChunks(chatId) {
  if (!chatId || !window.llmIsLocalServer?.()) return [];
  try {
    const base = window.llmProxyBase || "";
    const response = await fetch(`${base}/api/sessions/replay?chatId=${encodeURIComponent(chatId)}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) return [];
    return Array.isArray(data.chunks) ? data.chunks.filter(Boolean) : [];
  } catch {
    return [];
  }
}

async function hydrateExecutionCodes() {
  const chat = getActiveChatRecord();
  if (!chat?.codePanel || !activeChatId) return;

  const executions = chat.codePanel.filter((item) => item.type === "execution");
  const missing = executions.filter((item) => !item.code);
  if (!missing.length) return;

  const chunks = await fetchReplayChunks(activeChatId);
  if (!chunks.length) return;

  let changed = false;
  executions.forEach((item, index) => {
    if (!item.code && chunks[index]) {
      item.code = chunks[index];
      changed = true;
      const card = getExecutionCard(item.id);
      if (card) applyExecutionCardState(card, item);
    }
  });
  if (changed) persistActiveChat();
}

function createExecutionCardElement(executionId, { showStop = false } = {}) {
  const card = document.createElement("div");
  card.className = "code-execution-progress code-execution-progress--running";
  card.dataset.executionId = executionId;
  card.innerHTML = `
    <div class="code-execution-progress__header">
      <span class="code-execution-progress__dot"></span>
      <span class="code-execution-progress__title">代码运行中</span>
      <span class="code-execution-progress__elapsed"></span>
      <button class="code-execution-progress__toggle" type="button" aria-expanded="false" aria-label="查看代码" title="查看运行的代码" hidden>查看代码</button>
      <button class="code-execution-progress__stop" type="button" aria-label="停止代码" title="停止运行" ${showStop ? "" : "hidden"}>停止</button>
    </div>
    <div class="code-execution-progress__desc"></div>
    <div class="code-execution-progress__stage-row">
      <span class="code-execution-progress__stage-label">当前阶段</span>
      <span class="code-execution-progress__stage"></span>
    </div>
    <div class="code-execution-progress__bar-wrap" hidden>
      <div class="code-execution-progress__bar-label"></div>
      <div class="code-execution-progress__bar-track">
        <div class="code-execution-progress__bar-fill"></div>
      </div>
      <div class="code-execution-progress__bar-meta"></div>
    </div>
    <ul class="code-execution-progress__highlights"></ul>
    <div class="code-execution-progress__empty">等待任务输出…</div>
    <div class="code-execution-progress__hint"></div>
    <pre class="code-execution-progress__code" hidden></pre>
  `;
  wireExecutionStopButton(card);
  wireExecutionToggleButton(card);
  return card;
}

function applyExecutionCardState(card, artifact, { showStop = false } = {}) {
  if (!card) return;

  card.classList.remove("code-execution-progress--running", "code-execution-progress--done", "code-execution-progress--stopped");
  const dot = card.querySelector(".code-execution-progress__dot");
  dot?.classList.remove("code-execution-progress__dot--done");

  if (artifact.stopped) {
    card.classList.add("code-execution-progress--stopped");
  } else if (artifact.done) {
    card.classList.add("code-execution-progress--done");
    dot?.classList.add("code-execution-progress__dot--done");
  } else {
    card.classList.add("code-execution-progress--running");
  }

  card.querySelector(".code-execution-progress__title").textContent =
    artifact.title || (artifact.stopped ? "代码已停止" : artifact.done ? "代码运行完成" : "代码运行中");
  card.querySelector(".code-execution-progress__elapsed").textContent = artifact.elapsedLabel
    ? `· ${artifact.elapsedLabel}`
    : "";
  card.querySelector(".code-execution-progress__desc").textContent = artifact.description || "";
  card.querySelector(".code-execution-progress__stage").textContent =
    artifact.stage || (artifact.stopped ? "已终止" : artifact.done ? "已完成" : "运行中");

  const highlights = filterExecutionHighlights(artifact.highlights, artifact);
  const listEl = card.querySelector(".code-execution-progress__highlights");
  const emptyEl = card.querySelector(".code-execution-progress__empty");
  listEl.innerHTML = "";

  const isCompact = Boolean(artifact.done || artifact.stopped);
  const isActiveDownload = !isCompact && Boolean(artifact.isDownloadTask);
  const barWrap = card.querySelector(".code-execution-progress__bar-wrap");
  const barFill = card.querySelector(".code-execution-progress__bar-fill");
  const bytes = Number(artifact.progressBytes);
  const bytesTotal = Number(artifact.progressBytesTotal);
  const fileIndex = Number(artifact.progressFileIndex);
  const fileTotal = Number(artifact.progressFileTotal);
  const filePctRaw = Number(artifact.progressFilePct);
  let filePct = Number.isFinite(filePctRaw) ? filePctRaw : null;
  if (Number.isFinite(bytes) && Number.isFinite(bytesTotal) && bytesTotal > 0) {
    filePct = Math.max(0, Math.min(100, (bytes / bytesTotal) * 100));
  }

  let overallPct = Number(artifact.progressOverallPct);
  let hasPct = artifact.progressOverallPct != null && Number.isFinite(overallPct);
  // 整体% 不能用「当前文件序号/总数」冒充完成；有本文件进度时按加权重算
  if (
    Number.isFinite(fileIndex)
    && Number.isFinite(fileTotal)
    && fileTotal > 0
    && filePct != null
  ) {
    const weighted = ((fileIndex - 1) + filePct / 100) / fileTotal * 100;
    if (!hasPct || (overallPct >= 99.5 && filePct < 95) || Math.abs(overallPct - weighted) > 8) {
      overallPct = weighted;
      hasPct = true;
    }
  }
  const hasBytes = Number(artifact.progressBytes) > 0;
  const hasProgress =
    isActiveDownload && isDownloadProgressArtifact(artifact) && (hasPct || hasBytes);
  const hideOutput = isActiveDownload && hasProgress;

  if (highlights.length && !hideOutput) {
    emptyEl.hidden = true;
    listEl.hidden = false;
    highlights.slice(-4).forEach((line) => {
      const item = document.createElement("li");
      item.className = "code-execution-progress__highlight";
      item.textContent = line;
      listEl.appendChild(item);
    });
  } else {
    emptyEl.hidden = Boolean(artifact.done || artifact.stopped || hideOutput);
    listEl.hidden = true;
  }

  card.querySelector(".code-execution-progress__hint").textContent = artifact.hint || "";
  const stopBtn = card.querySelector(".code-execution-progress__stop");
  if (stopBtn) stopBtn.hidden = !(showStop && !artifact.done && !artifact.stopped);

  const barLabel = card.querySelector(".code-execution-progress__bar-label");
  const barMeta = card.querySelector(".code-execution-progress__bar-meta");
  let barPct = hasPct ? overallPct : filePct;
  if (barPct == null || !Number.isFinite(barPct)) barPct = 0;

  const runLabel = artifact.progressRun || "";
  let displayLabel = artifact.progressLabel || artifact.stage || "下载中…";
  if (runLabel && fileIndex && fileTotal && hasPct && filePct != null) {
    displayLabel = `${runLabel} · 本文件 ${filePct.toFixed(1)}% · 整体 ${Number(barPct).toFixed(1)}% (${fileIndex}/${fileTotal})`;
  } else if (runLabel && hasPct && filePct != null) {
    displayLabel = `${runLabel} · 本文件 ${filePct.toFixed(1)}% · 整体 ${Number(barPct).toFixed(1)}%`;
  } else if (runLabel && hasPct) {
    displayLabel = `${runLabel} · 整体 ${Number(barPct).toFixed(1)}%`;
  }

  const isIndeterminate = Boolean(artifact.progressIndeterminate) && !hasPct && hasBytes && filePct == null;
  if (barWrap && barFill) {
    barFill.classList.remove("code-execution-progress__bar-fill--indeterminate");
    if (!hasProgress) {
      barWrap.hidden = true;
      barFill.style.width = "0";
    } else {
      barWrap.hidden = false;
      barFill.classList.toggle(
        "code-execution-progress__bar-fill--indeterminate",
        isIndeterminate,
      );
      if (isIndeterminate) {
        barFill.style.width = "";
      } else {
        const pct = Math.max(0, Math.min(100, barPct));
        barFill.style.width = `${pct}%`;
      }
      if (barLabel) {
        barLabel.textContent = displayLabel;
      }
      if (barMeta) {
        if (Number.isFinite(bytes) && Number.isFinite(bytesTotal) && bytesTotal > 0) {
          const fmt = (value) => `${(value / 1024 / 1024).toFixed(1)} MB`;
          const fileText = filePct != null ? `本文件 ${filePct.toFixed(1)}%` : "";
          const overallText = `整体 ${Number(barPct).toFixed(1)}%`;
          barMeta.textContent = [fmt(bytes) + " / " + fmt(bytesTotal), fileText, overallText]
            .filter(Boolean)
            .join(" · ");
        } else if (Number.isFinite(bytes) && bytes > 0) {
          barMeta.textContent = hasPct
            ? `整体 ${Number(barPct).toFixed(1)}% · ${(bytes / 1024 / 1024).toFixed(1)} MB`
            : `${(bytes / 1024 / 1024).toFixed(1)} MB 已下载`;
        } else if (artifact.progressFileIndex && artifact.progressFileTotal) {
          barMeta.textContent = `文件 ${artifact.progressFileIndex}/${artifact.progressFileTotal}${hasPct ? ` · 整体 ${Number(barPct).toFixed(1)}%` : ""}`;
        } else if (hasPct) {
          barMeta.textContent = `整体 ${Number(barPct).toFixed(1)}%`;
        } else {
          barMeta.textContent = "下载中…";
        }
      }
    }
  }

  const code = resolveExecutionCode(null, artifact) || card.dataset.executionCode || "";
  if (code) card.dataset.executionCode = code;

  const codeEl = card.querySelector(".code-execution-progress__code");
  const toggleBtn = card.querySelector(".code-execution-progress__toggle");
  const stageRow = card.querySelector(".code-execution-progress__stage-row");
  const descEl = card.querySelector(".code-execution-progress__desc");
  const hintEl = card.querySelector(".code-execution-progress__hint");

  if (codeEl) {
    codeEl.textContent = code;
    codeEl.hidden = !card.classList.contains("code-execution-progress--expanded") || !code;
  }
  if (toggleBtn) {
    toggleBtn.hidden = !isCompact;
    if (!card.classList.contains("code-execution-progress--expanded")) {
      toggleBtn.setAttribute("aria-expanded", "false");
      toggleBtn.textContent = "查看代码";
    }
  }

  card.classList.toggle("code-execution-progress--compact", isCompact);
  if (stageRow) stageRow.hidden = isCompact;
  if (descEl) descEl.hidden = isCompact;
  if (hintEl) hintEl.hidden = isCompact || !artifact.hint;
  if (isCompact) {
    emptyEl.hidden = true;
    listEl.hidden = true;
  }
}

function applyExecutionStoppedCard(card) {
  if (!card) return;
  applyExecutionCardState(card, stripDownloadProgressFields({
    stopped: true,
    done: true,
    title: "代码已停止",
    stage: "已终止",
    hint: "",
    highlights: [],
  }));
}

function renderExecutionArtifact(artifact, options = {}) {
  const codeInner = getCodePanelInner();
  if (!codeInner || !artifact?.id) return null;

  showCodePanel();

  let card = getExecutionCard(artifact.id);
  if (!card) {
    card = createExecutionCardElement(artifact.id, {
      showStop: Boolean(options.showStop && !artifact.done && !artifact.stopped),
    });
    codeInner.appendChild(card);
  }
  applyExecutionCardState(card, normalizeExecutionArtifact(artifact), {
    showStop: Boolean(options.showStop && !artifact.done && !artifact.stopped),
  });
  scrollCodePanelToBottom();
  return card;
}

function renderCodePanel(codePanel, options = {}) {
  const interactive = Boolean(options.interactive) || isActiveChatSending();
  resetCodePanel();

  const normalized = (compactCodePanelForRender(codePanel) || []).map((item) => {
    if (item.type === "execution" && !item.id) {
      return { ...item, id: createId() };
    }
    return item;
  });
  const sanitized = sanitizeCodePanel(normalized);
  if (sanitized.length === 0) return;

  const chat = getActiveChatRecord();
  if (chat && JSON.stringify(chat.codePanel) !== JSON.stringify(sanitized)) {
    chat.codePanel = sanitized;
    persistActiveChat();
  }

  sanitized.forEach((artifact) => {
    if (artifact.type === "approval") {
      renderApprovalArtifact(artifact, interactive);
    } else if (artifact.type === "execution") {
      renderExecutionArtifact(normalizeExecutionArtifact(artifact), {
        showStop: interactive && isActiveChatSending() && !artifact.done && !artifact.stopped,
      });
    }
  });
  syncCodePanelVisibility();
  void hydrateExecutionCodes();
  scrollCodePanelToBottom();
}

function markApprovalCard(card, status) {
  if (!card) return;
  card.classList.toggle("code-approval--approved", status === "approved" || status === "auto-approved");
  card.classList.toggle("code-approval--denied", status === "denied");
  const actions = card.querySelector(".code-approval__actions");
  if (!actions) return;
  if (status === "denied") {
    actions.innerHTML = '<span class="code-approval__status code-approval__status--denied">已拒绝</span>';
    return;
  }
  actions.innerHTML = `<span class="code-approval__status code-approval__status--ok">${approvalStatusLabel(status)}</span>`;
}

async function settleCodeApproval(card, event, approved, options = {}) {
  const runId = options.runId || getChatStream(activeChatId)?.runId || "";
  const desc = event.description || "即将在当前 conda / Jupyter 环境中执行以下 Python 代码。";
  const status = approved ? (options.auto ? "auto-approved" : "approved") : "denied";

  const buttons = card.querySelectorAll("button");
  buttons.forEach((btn) => {
    btn.disabled = true;
  });

  try {
    if (!runId) {
      throw new Error("当前 Agent 运行已结束，无法批准代码。请重新发送消息。");
    }
    if (!options.skipBackendApprove) {
      await window.approveAgentCode?.(runId, event.requestId, approved);
    }
    if (approved && event.code) {
      pendingExecutionCode = event.code;
    }
    recordCodeArtifact({
      type: "approval",
      id: event.requestId,
      description: desc,
      code: event.code || "",
      status,
    });
    markApprovalCard(card, status);
  } catch (error) {
    buttons.forEach((btn) => {
      btn.disabled = false;
    });
    const actions = card.querySelector(".code-approval__actions");
    if (actions) {
      actions.innerHTML = `<span class="code-approval__status code-approval__status--denied">${error instanceof Error ? error.message : String(error)}</span>`;
    }
    throw error;
  }

  syncCodePanelVisibility();
  scrollThreadToBottom();
}

function wireApprovalCard(card, event, options = {}) {
  if (!card || card.dataset.approvalWired === "true") return;
  card.dataset.approvalWired = "true";

  card.querySelector(".code-approval__allow")?.addEventListener("click", () => {
    void settleCodeApproval(card, event, true, options);
  });
  card.querySelector(".code-approval__deny")?.addEventListener("click", () => {
    void settleCodeApproval(card, event, false, options);
  });
  card.querySelector(".code-approval__allow-all")?.addEventListener("click", () => {
    setAutoApproveCode(true);
    void settleCodeApproval(card, event, true, { ...options, auto: true });
  });
}

function approvePendingCodeCards(options = {}) {
  const codeInner = getCodePanelInner();
  if (!codeInner) return;
  codeInner.querySelectorAll(".code-approval").forEach((card) => {
    if (card.classList.contains("code-approval--approved") || card.classList.contains("code-approval--denied")) {
      return;
    }
    const requestId = card.dataset.requestId;
    if (!requestId) return;
    const chat = getActiveChatRecord();
    const artifact = chat?.codePanel?.find((item) => item.type === "approval" && item.id === requestId);
    const event = {
      requestId,
      description: artifact?.description || card.querySelector(".code-approval__desc")?.textContent || "",
      code: artifact?.code || card.querySelector(".code-approval__code")?.textContent || "",
    };
    wireApprovalCard(card, event, options);
    if (options.auto || autoApproveCode) {
      void settleCodeApproval(card, event, true, {
        ...options,
        auto: true,
      });
    }
  });
}

function showCodeApproval(_group, event, options = {}) {
  const codeInner = getCodePanelInner();
  if (!codeInner) return;

  const shouldAuto = Boolean(options.autoApprove ?? autoApproveCode);
  const runId = options.runId || getChatStream(activeChatId)?.runId || "";
  const desc = event.description || "即将在当前 conda / Jupyter 环境中执行以下 Python 代码。";

  let card = codeInner.querySelector(`[data-request-id="${event.requestId}"]`);
  if (card) {
    if (card.classList.contains("code-approval--approved") || card.classList.contains("code-approval--denied")) {
      return;
    }
    wireApprovalCard(card, event, { ...options, runId });
    if (shouldAuto) {
      void settleCodeApproval(card, event, true, {
        runId,
        auto: true,
      });
    }
    return;
  }

  recordCodeArtifact({
    type: "approval",
    id: event.requestId,
    description: desc,
    code: event.code || "",
    status: shouldAuto ? "auto-approved" : "pending",
  });

  card = renderApprovalArtifact(
    {
      type: "approval",
      id: event.requestId,
      description: desc,
      code: event.code || "",
      status: shouldAuto ? "auto-approved" : "pending",
    },
    !shouldAuto,
  );
  if (!card) return;

  if (shouldAuto) {
    wireApprovalCard(card, event, { ...options, runId });
    void settleCodeApproval(card, event, true, {
      runId,
      auto: true,
    });
    return;
  }

  wireApprovalCard(card, event, { ...options, runId });
  syncCodePanelVisibility();
  scrollThreadToBottom();
}

function formatWaitTime(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return "";
  if (value < 60) return `${value} 秒`;
  if (value < 3600) {
    const minutes = Math.round(value / 60);
    return `${minutes} 分钟`;
  }
  const hours = Math.floor(value / 3600);
  const minutes = Math.round((value % 3600) / 60);
  return minutes > 0 ? `${hours} 小时 ${minutes} 分钟` : `${hours} 小时`;
}

function ensureCodeExecutionProgress(_group, executionId) {
  const codeInner = getCodePanelInner();
  if (!codeInner || !executionId) return null;

  showCodePanel();

  let card = getExecutionCard(executionId);
  if (!card) {
    card = createExecutionCardElement(executionId, { showStop: isActiveChatSending() });
    codeInner.appendChild(card);
  }
  return card;
}

function updateCodeExecutionProgress(group, event) {
  if (event.type === "code_execution_started") {
    markRunningExecutionsStopped();
    removeBackgroundExecutionCard();
    activeCodeExecutionId = createId();
    const stream = chatStreams.get(activeChatId);
    if (stream) stream.codeExecutionId = activeCodeExecutionId;
  }

  const executionId = activeCodeExecutionId;
  if (!executionId) return;

  const chat = getActiveChatRecord();
  const existing = chat?.codePanel?.find(
    (item) => item.type === "execution" && item.id === executionId,
  );
  if (existing?.done || existing?.stopped) return;

  const card = ensureCodeExecutionProgress(group, executionId);
  if (!card) return;

  const executionCode = resolveExecutionCode(event) || pendingExecutionCode || existing?.code || "";
  if (event.type === "code_execution_started" && executionCode) {
    pendingExecutionCode = "";
  }

  const elapsed = event.elapsedLabel || (event.elapsedSec != null ? `${event.elapsedSec} 秒` : "") || existing?.elapsedLabel || "";
  const title = event.type === "code_execution_started" ? "代码已开始运行" : "代码仍在运行";
  const stageText = event.stage || (event.type === "code_execution_started" ? "已启动，等待输出" : "运行中");

  const hasProgressFields = Boolean(
    event.progressOverallPct != null
    || event.progressFilePct != null
    || event.progressRun
    || event.progressFileTotal
    || event.progressBytesTotal
    || event.progressLabel
    || Number(event.progressBytes) > 0
    || event.progressIndeterminate,
  );
  const isDownloadTask = Boolean(event.isDownloadTask || existing?.isDownloadTask || hasProgressFields);

  const rawHighlights = Array.isArray(event.highlights)
    ? event.highlights.filter(Boolean)
    : String(event.snippet || "")
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
  const highlights = filterExecutionHighlights(rawHighlights, { isDownloadTask });

  let hint = "点击「停止」将中断 Jupyter 内核中的代码。";
  if (isDownloadTask) {
    hint = "下载进度实时更新；点击「停止」可中断内核。";
  } else if (event.type === "code_execution_started") {
    hint = "代码已启动，进度将实时刷新；点击「停止」可中断内核。";
  }

  const artifact = {
    type: "execution",
    id: executionId,
    title,
    description: event.description || event.summary || existing?.description || "",
    code: executionCode,
    stage: stageText,
    highlights: highlights.slice(-4),
    elapsedLabel: elapsed,
    hint,
    done: false,
    stopped: false,
    isDownloadTask,
  };

  if (isDownloadTask) {
    const pick = (key) => (event[key] != null ? event[key] : existing?.[key]);
    Object.assign(artifact, {
      progressOverallPct: pick("progressOverallPct"),
      progressFilePct: pick("progressFilePct"),
      progressRun: pick("progressRun"),
      progressFileIndex: pick("progressFileIndex"),
      progressFileTotal: pick("progressFileTotal"),
      progressBytes: pick("progressBytes"),
      progressBytesTotal: pick("progressBytesTotal"),
      progressLabel: pick("progressLabel"),
      progressIndeterminate: event.progressIndeterminate != null
        ? event.progressIndeterminate
        : existing?.progressIndeterminate,
    });
    // 事件缺字段时 pick 会残留旧整体%；用当前字节纠偏，避免 21MB/218MB 却显示 100%
    const b = Number(artifact.progressBytes);
    const bt = Number(artifact.progressBytesTotal);
    const fi = Number(artifact.progressFileIndex);
    const ft = Number(artifact.progressFileTotal);
    if (Number.isFinite(b) && Number.isFinite(bt) && bt > 0) {
      const fp = Math.max(0, Math.min(100, (b / bt) * 100));
      artifact.progressFilePct = fp;
      if (Number.isFinite(fi) && Number.isFinite(ft) && ft > 0) {
        const weighted = ((fi - 1) + fp / 100) / ft * 100;
        const reported = Number(artifact.progressOverallPct);
        if (!Number.isFinite(reported) || (reported >= 99.5 && fp < 95) || Math.abs(reported - weighted) > 8) {
          artifact.progressOverallPct = Math.round(weighted * 10) / 10;
          artifact.progressLabel = `${artifact.progressRun || "FASTQ"} · 本文件 ${fp.toFixed(1)}% · 整体 ${artifact.progressOverallPct.toFixed(1)}% (${fi}/${ft})`;
        }
      }
    }
  }

  applyExecutionCardState(card, artifact, { showStop: isActiveChatSending() });
  recordCodeArtifact(artifact);

  // 下载进度更新时不要反复把面板滚到底，避免进度条视觉跳动
  if (!isDownloadTask || event.type === "code_execution_started") {
    scrollCodePanelToBottom();
  }
}

function finishCodeExecutionProgress(_group) {
  const executionId = activeCodeExecutionId;
  const card = executionId ? getExecutionCard(executionId) : null;
  if (!card) return;

  const chat = getActiveChatRecord();
  const existing = chat?.codePanel?.find(
    (item) => item.type === "execution" && item.id === executionId,
  );

  const artifact = stripDownloadProgressFields({
    type: "execution",
    id: executionId,
    title: "代码运行完成",
    description: existing?.description || "",
    code: existing?.code || card.dataset.executionCode || "",
    stage: "已完成",
    done: true,
    stopped: false,
    hint: "",
  });

  applyExecutionCardState(card, artifact);
  recordCodeArtifact(artifact);
  activeCodeExecutionId = null;
  const stream = chatStreams.get(activeChatId);
  if (stream) stream.codeExecutionId = null;

  scrollCodePanelToBottom();
  scrollThreadToBottom();
  void hydrateExecutionCodes();
  // 每步代码执行结束后立即刷新右侧 Environment / Visualization
  window.KernelPanel?.refresh?.({ force: true });
}

function buildCodeProgressArtifact(event, existing, executionId) {
  const elapsed = event.elapsedLabel
    || (event.elapsedSec != null ? `${event.elapsedSec} 秒` : "")
    || existing?.elapsedLabel
    || "";
  const title = event.type === "code_execution_started" ? "代码已开始运行" : "代码仍在运行";
  const stageText = event.stage
    || (event.type === "code_execution_started" ? "已启动，等待输出" : "运行中");
  const hasProgressFields = Boolean(
    event.progressOverallPct != null
    || event.progressFilePct != null
    || event.progressRun
    || event.progressFileTotal
    || event.progressBytesTotal
    || event.progressLabel
    || Number(event.progressBytes) > 0
    || event.progressIndeterminate,
  );
  const isDownloadTask = Boolean(event.isDownloadTask || existing?.isDownloadTask || hasProgressFields);
  const executionCode = resolveExecutionCode(event) || existing?.code || "";
  const rawHighlights = Array.isArray(event.highlights)
    ? event.highlights.filter(Boolean)
    : String(event.snippet || "")
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
  const highlights = filterExecutionHighlights(rawHighlights, { isDownloadTask });

  const artifact = {
    type: "execution",
    id: executionId,
    title,
    description: event.description || event.summary || existing?.description || "",
    code: executionCode,
    stage: stageText,
    highlights: highlights.slice(-4),
    elapsedLabel: elapsed,
    hint: isDownloadTask
      ? "下载进度实时更新；点击「停止」可中断内核。"
      : "代码已启动，进度将实时刷新；点击「停止」可中断内核。",
    done: false,
    stopped: false,
    isDownloadTask,
  };

  if (isDownloadTask) {
    const pick = (key) => (event[key] != null ? event[key] : existing?.[key]);
    Object.assign(artifact, {
      progressOverallPct: pick("progressOverallPct"),
      progressFilePct: pick("progressFilePct"),
      progressRun: pick("progressRun"),
      progressFileIndex: pick("progressFileIndex"),
      progressFileTotal: pick("progressFileTotal"),
      progressBytes: pick("progressBytes"),
      progressBytesTotal: pick("progressBytesTotal"),
      progressLabel: pick("progressLabel"),
      progressIndeterminate: event.progressIndeterminate != null
        ? event.progressIndeterminate
        : existing?.progressIndeterminate,
    });
    // 事件缺字段时 pick 会残留旧整体%；用当前字节纠偏，避免 21MB/218MB 却显示 100%
    const b = Number(artifact.progressBytes);
    const bt = Number(artifact.progressBytesTotal);
    const fi = Number(artifact.progressFileIndex);
    const ft = Number(artifact.progressFileTotal);
    if (Number.isFinite(b) && Number.isFinite(bt) && bt > 0) {
      const fp = Math.max(0, Math.min(100, (b / bt) * 100));
      artifact.progressFilePct = fp;
      if (Number.isFinite(fi) && Number.isFinite(ft) && ft > 0) {
        const weighted = ((fi - 1) + fp / 100) / ft * 100;
        const reported = Number(artifact.progressOverallPct);
        if (!Number.isFinite(reported) || (reported >= 99.5 && fp < 95) || Math.abs(reported - weighted) > 8) {
          artifact.progressOverallPct = Math.round(weighted * 10) / 10;
          artifact.progressLabel = `${artifact.progressRun || "FASTQ"} · 本文件 ${fp.toFixed(1)}% · 整体 ${artifact.progressOverallPct.toFixed(1)}% (${fi}/${ft})`;
        }
      }
    }
  }
  return artifact;
}

/** 非当前可见会话：只落盘进度到该 chat 的 codePanel，不改当前 DOM */
function recordBackgroundCodeProgress(streamChatId, event) {
  const stream = chatStreams.get(streamChatId);
  if (!stream || !streamChatId) return;
  const chat = ensureChatRecord(streamChatId);
  if (!Array.isArray(chat.codePanel)) chat.codePanel = [];

  if (event.type === "code_execution_started") {
    chat.codePanel = chat.codePanel.map((item) => {
      if (item.type === "execution" && !item.done && !item.stopped && item.id !== BACKGROUND_EXECUTION_ID) {
        return stripDownloadProgressFields({
          ...item,
          done: true,
          stopped: true,
          title: "代码已停止",
          stage: "已终止",
          hint: "",
        });
      }
      return item;
    });
    stream.codeExecutionId = createId();
  }

  const executionId = stream.codeExecutionId;
  if (!executionId) return;

  const existing = chat.codePanel.find(
    (item) => item.type === "execution" && item.id === executionId,
  );
  if (existing?.done || existing?.stopped) return;

  const artifact = buildCodeProgressArtifact(event, existing, executionId);
  recordCodeArtifactForChat(streamChatId, artifact);
}

function finishBackgroundCodeProgress(streamChatId) {
  const stream = chatStreams.get(streamChatId);
  const executionId = stream?.codeExecutionId;
  if (!streamChatId || !executionId) return;
  const chat = ensureChatRecord(streamChatId);
  const existing = chat?.codePanel?.find(
    (item) => item.type === "execution" && item.id === executionId,
  );
  recordCodeArtifactForChat(
    streamChatId,
    stripDownloadProgressFields({
      type: "execution",
      id: executionId,
      title: "代码运行完成",
      description: existing?.description || "",
      code: existing?.code || "",
      stage: "已完成",
      done: true,
      stopped: false,
      hint: "",
    }),
  );
  stream.codeExecutionId = null;
}

function appendThinkingStepToEntry(assistantEntry, step) {
  if (!assistantEntry) return;
  if (!Array.isArray(assistantEntry.thinkingSteps)) assistantEntry.thinkingSteps = [];
  assistantEntry.thinkingSteps.push({
    kind: step.kind || "tool",
    title: step.title || "",
    body: step.body || "",
  });
}

function handleAgentStreamEventBackground(streamChatId, streamMessages, assistantEntry, event) {
  if (!event?.type || !assistantEntry) return;

  if (event.type === "status" && event.message) {
    assistantEntry.content = event.message;
    return;
  }
  if (
    event.type === "plan_created"
    || event.type === "plan_revised"
    || event.type === "plan_step_start"
    || event.type === "plan_step_done"
    || event.type === "plan_step_failed"
    || event.type === "plan_complete"
  ) {
    const msg = event.message || "";
    if (msg) assistantEntry.content = msg;
    if (event.plan?.steps?.length) {
      appendThinkingStepToEntry(assistantEntry, {
        kind: "plan",
        title: event.plan.goal || "执行计划",
        body: event.plan.steps
          .map((s) => {
            const mark =
              s.status === "done" ? "✓" : s.status === "running" ? "▶" : s.status === "failed" ? "✗" : "○";
            return `${mark} ${s.title || s.goal || s.id}`;
          })
          .join("\n"),
      });
    }
    return;
  }
  if (event.type === "code_execution_started" || event.type === "code_execution_progress") {
    const stage = event.stage || (event.type === "code_execution_started" ? "已启动" : "运行中");
    appendThinkingStepToEntry(assistantEntry, {
      kind: "tool",
      title: event.summary || event.description || "代码执行中",
      body: stage,
    });
    recordBackgroundCodeProgress(streamChatId, event);
    return;
  }
  if (event.type === "done" && event.text) {
    assistantEntry.content = stripExecutionMemoryBlock(event.text);
    return;
  }
  if (event.type === "final" && event.content) {
    assistantEntry.content = stripExecutionMemoryBlock(event.content);
    return;
  }
  if (event.type === "thinking" && String(event.content || "").trim()) {
    appendThinkingStepToEntry(assistantEntry, {
      kind: "thinking",
      title: "Thinking",
      body: event.content,
    });
    return;
  }
  if (event.type === "tool_call" && event.name !== "finish") {
    appendExecutionLog(
      { tool: event.name, title: event.summary || event.name, summary: "" },
      assistantEntry,
      { chatId: streamChatId, messages: streamMessages },
    );
    appendThinkingStepToEntry(assistantEntry, {
      kind: "tool",
      title: event.summary || event.name,
    });
    return;
  }
  if (event.type === "tool_result") {
    appendExecutionLog(
      { tool: event.name, title: event.summary || event.name, summary: event.content || "" },
      assistantEntry,
      { chatId: streamChatId, messages: streamMessages },
    );
    if (event.name === "execute_code") {
      appendThinkingStepToEntry(assistantEntry, {
        kind: "result",
        title: event.summary || "execute_code 完成",
        body: event.content || "",
      });
      finishBackgroundCodeProgress(streamChatId);
      if (streamChatId === activeChatId) {
        window.KernelPanel?.refresh?.({ force: true });
      }
    }
  }
}

function handleAgentStreamEvent(group, event) {
  if (!group || !event?.type) return;

  if (event.type === "status" && event.message) {
    const entry = getLastAssistantEntry();
    if (entry) entry.content = event.message;
    const textEl = group.querySelector(".chat-text");
    if (textEl) {
      textEl.classList.add("chat-text--loading");
      textEl.textContent = event.message;
    }
    scrollThreadToBottom();
    return;
  }

  if (event.type === "heartbeat") {
    const msg = event.message || "任务运行中…";
    const entry = getLastAssistantEntry();
    if (entry) entry.content = msg;
    const textEl = group.querySelector(".chat-text");
    if (textEl) {
      textEl.classList.add("chat-text--loading");
      textEl.textContent = msg;
    }
    if (event.kernelBusy && !activeCodeExecutionId) {
      ensureBackgroundExecutionCard(
        {
          ok: true,
          hasActiveRun: Boolean(event.hasActiveRun),
          kernelBusy: true,
          planSummary: msg,
        },
        { showStop: isActiveChatSending() },
      );
    }
    scrollThreadToBottom();
    return;
  }

  if (
    event.type === "plan_created" ||
    event.type === "plan_revised" ||
    event.type === "plan_step_start" ||
    event.type === "plan_step_done" ||
    event.type === "plan_step_failed" ||
    event.type === "plan_complete"
  ) {
    const entry = getLastAssistantEntry();
    const msg = event.message || "";
    if (entry && msg) entry.content = msg;
    const textEl = group.querySelector(".chat-text");
    if (textEl && msg) {
      textEl.classList.add("chat-text--loading");
      textEl.textContent = msg;
    }
    if (event.plan?.steps?.length) {
      const done = event.plan.steps.filter((s) => s.status === "done").length;
      const total = event.plan.steps.length;
      const planTitle = event.plan.goal || "执行计划";
      appendThinkingStep(group, {
        kind: "plan",
        title: `${planTitle} (${done}/${total})`,
        body: event.plan.steps
          .map((s) => {
            const mark =
              s.status === "done" ? "✓" : s.status === "running" ? "▶" : s.status === "failed" ? "✗" : "○";
            return `${mark} ${s.title || s.goal || s.id}`;
          })
          .join("\n"),
      });
    } else if (msg) {
      appendThinkingStep(group, { kind: "plan", title: "计划", body: msg });
    }
    scrollThreadToBottom();
    return;
  }

  if (event.type === "done" && event.text) {
    const entry = getLastAssistantEntry();
    const text = stripExecutionMemoryBlock(event.text);
    if (entry) entry.content = text;
    updateMessageGroup(group, text);
    persistActiveChat();
    return;
  }

  if (event.type === "final" && event.content) {
    const entry = getLastAssistantEntry();
    const text = stripExecutionMemoryBlock(event.content);
    if (entry) entry.content = text;
    updateMessageGroup(group, text);
    persistActiveChat();
    return;
  }

  if (event.type === "thinking" && String(event.content || "").trim()) {
    appendThinkingStep(group, {
      kind: "thinking",
      title: "Thinking",
      body: event.content,
    });
    return;
  }

  if (event.type === "tool_call" && event.name !== "finish") {
    appendExecutionLog({
      tool: event.name,
      title: event.summary || event.name,
      summary: "",
    });
    appendThinkingStep(group, {
      kind: "tool",
      title: event.summary || event.name,
    });
    return;
  }

  if (event.type === "code_execution_started" || event.type === "code_execution_progress") {
    updateCodeExecutionProgress(group, event);
    return;
  }

  if (event.type === "tool_result") {
    appendExecutionLog({
      tool: event.name,
      title: event.summary || event.name,
      summary: event.content || "",
    });
    if (event.name === "execute_code") {
      finishCodeExecutionProgress(group);
      appendThinkingStep(group, {
        kind: "result",
        title: event.summary || "execute_code 完成",
        body: event.content || "",
      });
    }
    return;
  }
}

function setComposerMode(mode) {
  if (!sendBtn) return;
  sendBtn.dataset.mode = mode;
  const isStop = mode === "stop";
  sendBtn.setAttribute("aria-label", isStop ? "停止" : "发送");
  sendBtn.classList.toggle("chat-send-btn--stop", isStop);
  sendBtn.type = isStop ? "button" : "submit";
}

function resetComposerControls() {
  syncComposerForActiveChat();
  if (!isActiveChatSending()) {
    window.KernelPanel?.stopPolling?.();
    window.KernelPanel?.refresh?.();
  }
}

function releaseComposerAfterStream(chatId) {
  finishChatStream(chatId);
}

function isStreamGenerationLive(chatId, generation) {
  const stream = chatStreams.get(chatId);
  return Boolean(stream && stream.generation === generation);
}

async function handleStop() {
  if (!isActiveChatSending()) return;
  const stream = chatStreams.get(activeChatId);
  const follow = liveFollows.get(activeChatId);
  const runId = stream?.runId || follow?.runId || "";

  if (stream && !stream.isFollower) {
    stream.generation = -1;
    stream.abortController?.abort();
  }
  if (follow) {
    // 先取消后端任务，再断开旁观流
  }
  markRunningExecutionsStopped();
  stopBackgroundRunWatch(activeChatId);
  if (window.cancelAgentRun) {
    await window.cancelAgentRun(runId || null, activeChatId);
  }
  detachLiveEventStream(activeChatId);
  if (stream?.isFollower) {
    chatStreams.delete(activeChatId);
  }
  syncComposerForActiveChat();
}

function updateMessageGroup(group, text) {
  if (!group) return;
  const textEl = group.querySelector(".chat-text");
  if (!textEl) return;
  textEl.classList.remove("chat-text--loading");
  textEl.textContent = stripExecutionMemoryBlock(text);

  const thinkingEl = group.querySelector(".chat-thinking");
  if (thinkingEl && !thinkingEl.querySelector(".chat-thinking__step")) {
    thinkingEl.remove();
  } else if (thinkingEl) {
    thinkingEl.open = false;
  }
  scrollThreadToBottom();
}

function resetComposer() {
  if (!composer) return;
  composer.value = "";
  composer.style.height = "auto";
}

async function handleSend() {
  if (!composer) return;
  if (isActiveChatSending()) {
    await handleStop();
    return;
  }

  const value = composer.value.trim();
  if (!value) return;

  const llm = window.getLlmConfig?.();
  const account = llm?.account;
  const vendor = llm?.vendor;
  const agent = llm?.config?.agent;

  if (account?.authMode === "api_key" && !account?.apiKey) {
    appendMessage(
      "assistant",
      "请先在 Config 页面配置 API Key 并点击「保存配置」，然后再试。\n\n提示：从 localhost 换成服务器 IP 访问时，浏览器配置不共享，需要重新填写并保存。",
    );
    return;
  }

  const serverOk = await window.probeProxyServer?.(true);
  if (!serverOk) {
    appendMessage(
      "assistant",
      `无法连接 UI 后端（${window.llmProxyBase || "serve.py"}）。\n\n请确认：\n1. 服务器已运行：cd ui && python3 serve.py\n2. 浏览器地址为 http://<服务器IP>:8765/index.html\n3. 防火墙已放行 8765 端口\n4. 硬刷新页面（Ctrl+Shift+R）加载最新脚本`,
    );
    return;
  }

  const streamChatId = activeChatId;
  const streamMessages = chatHistory;
  let pending = null;
  let streamGeneration = 0;
  let runId = null;
  let abortController = null;
  let answered = false;
  let historyRecorded = false;
  let assistantEntry = null;
  let streamComposerReleased = false;

  const touchStreamActivity = () => {
    const stream = chatStreams.get(streamChatId);
    if (stream) stream.lastStreamEventAt = Date.now();
  };

  const clearStreamIdleTimer = () => {
    const stream = chatStreams.get(streamChatId);
    if (stream?.idleTimer) {
      window.clearInterval(stream.idleTimer);
      stream.idleTimer = null;
    }
  };

  const startStreamIdleTimer = () => {
    clearStreamIdleTimer();
    const stream = chatStreams.get(streamChatId);
    if (!stream) return;
    stream.idleTimer = window.setInterval(() => {
      void (async () => {
        const current = chatStreams.get(streamChatId);
        if (!current || Date.now() - current.lastStreamEventAt < STREAM_IDLE_MS) return;
        const status = await fetchRunStatus(streamChatId);
        if (isTaskLikelyActive(status, streamChatId)) {
          current.lastStreamEventAt = Date.now();
          return;
        }
        current.abortController?.abort();
        window.cancelAgentRun?.(current.runId, streamChatId);
      })();
    }, 5000);
  };

  const clearStreamStatusPoll = () => {
    const stream = chatStreams.get(streamChatId);
    if (stream?.statusPollTimer) {
      window.clearInterval(stream.statusPollTimer);
      stream.statusPollTimer = null;
    }
  };

  const startStreamStatusPoll = () => {
    clearStreamStatusPoll();
    const stream = chatStreams.get(streamChatId);
    if (!stream) return;
    stream.statusPollTimer = window.setInterval(() => {
      if (!isStreamGenerationLive(streamChatId, streamGeneration)) return;
      void (async () => {
        const status = await fetchRunStatus(streamChatId);
        if (isTaskLikelyActive(status, streamChatId)) {
          syncRunStatusToUI(streamChatId, status || { ok: true, codePanelRunning: true }, {
            pending,
            assistantEntry,
            messages: streamMessages,
          });
          return;
        }
        // Agent/内核已空闲：收尾轮询期间创建的「Agent 运行中」背景卡片，避免聊天结束后仍显示运行中
        if (streamChatId === activeChatId) {
          finishBackgroundExecutionCard();
        }
      })();
    }, STREAM_STATUS_POLL_MS);
  };

  try {
    stopBackgroundRunWatch(streamChatId);
    abandonStaleAssistantPlaceholders(streamMessages);
    persistChatMessages(streamChatId, streamMessages);
    appendMessage("user", value);
    streamMessages.push({ role: "user", content: value });
    assistantEntry = { role: "assistant", content: "", thinkingSteps: [] };
    streamMessages.push(assistantEntry);
    ensureActiveChatRecord();
    persistChatMessages(streamChatId, streamMessages);
    resetComposer();

    pending = appendMessage("assistant", "思考中…", { loading: true });

    streamGeneration = ++nextStreamGeneration;
    runId = createId();
    abortController = new AbortController();
    activeCodeExecutionId = null;

    chatStreams.set(streamChatId, {
      generation: streamGeneration,
      runId,
      abortController,
      messages: streamMessages,
      assistantEntry,
      pending,
      idleTimer: null,
      statusPollTimer: null,
      lastStreamEventAt: Date.now(),
      codeExecutionId: null,
    });

    syncComposerForActiveChat();
    window.KernelPanel?.stopPolling?.();
    touchStreamActivity();
    startStreamIdleTimer();
    startStreamStatusPoll();

    const fullConfig = window.loadLlmConfig?.() || llm?.config;
    const { text: reply, meta } = await window.agentChatStream({
      account,
      vendor,
      agent: fullConfig?.agent || agent,
      messages: messagesForAgentApi(),
      executionContext: buildExecutionContextForApi(),
      runId,
      chatId: streamChatId,
      autoApproveCode,
      signal: abortController.signal,
      onEvent: (event) => {
        if (!isStreamGenerationLive(streamChatId, streamGeneration)) return;
        touchStreamActivity();
        const isVisible = streamChatId === activeChatId;
        if (event.type === "run_start") {
          if (isVisible) {
            const target = resolvePendingGroup(pending);
            const textEl = target?.querySelector(".chat-text");
            if (textEl) {
              textEl.classList.add("chat-text--loading");
              textEl.textContent = "已连接，等待 Agent 响应…";
            }
          } else if (assistantEntry) {
            assistantEntry.content = "已连接，等待 Agent 响应…";
          }
          return;
        }
        if (event.type === "heartbeat") {
          touchStreamActivity();
          if (isVisible) {
            handleAgentStreamEvent(resolvePendingGroup(pending), event);
          } else if (assistantEntry && event.message) {
            assistantEntry.content = event.message;
          }
          return;
        }
        if (isVisible) {
          const target = resolvePendingGroup(pending);
          if (event.type === "code_approval_required") {
            showCodeApproval(target, event, {
              runId,
              autoApprove: autoApproveCode,
            });
            return;
          }
          handleAgentStreamEvent(target, event);
        } else {
          handleAgentStreamEventBackground(streamChatId, streamMessages, assistantEntry, event);
          persistChatMessages(streamChatId, streamMessages);
        }
        if (event.type === "final" && event.content) {
          answered = true;
          if (!historyRecorded) {
            if (assistantEntry) assistantEntry.content = stripExecutionMemoryBlock(event.content);
            historyRecorded = true;
            persistChatMessages(streamChatId, streamMessages);
          }
        }
        if (event.type === "done") {
          answered = true;
        }
        if (event.type === "cancelled" || event.type === "error") {
          if (event.type === "error" && assistantEntry) {
            const message = `调用失败：${event.message || "Agent 执行失败"}`;
            assistantEntry.content = message;
            if (isVisible) {
              const target = resolvePendingGroup(pending);
              updateMessageGroup(target, message);
              ensureThinkingPanel(target);
              appendThinkingStep(target, { kind: "result", title: "错误", body: message });
            } else {
              appendThinkingStepToEntry(assistantEntry, { kind: "result", title: "错误", body: message });
            }
            persistChatMessages(streamChatId, streamMessages);
            answered = true;
          }
          if (event.type === "cancelled" && assistantEntry) {
            markAssistantStopped(assistantEntry);
            if (isVisible) {
              updateMessageGroup(resolvePendingGroup(pending), "（已停止生成）");
            }
            persistChatMessages(streamChatId, streamMessages);
            answered = true;
          }
          if (!streamComposerReleased) {
            streamComposerReleased = true;
            releaseComposerAfterStream(streamChatId);
          }
        }
      },
    });
    if (!isStreamGenerationLive(streamChatId, streamGeneration)) return;
    if (!historyRecorded) {
      const finalText = stripExecutionMemoryBlock(reply);
      if (assistantEntry) assistantEntry.content = finalText;
      else streamMessages.push({ role: "assistant", content: finalText, thinkingSteps: [] });
      if (streamChatId === activeChatId) {
        const displayText = finalText || (
          isPlaceholderAssistantContent(assistantEntry?.content)
            ? "（流式连接已结束，任务可能仍在后台运行，请查看 CODE 面板或刷新页面）"
            : assistantEntry?.content || "（无回复内容）"
        );
        if (!finalText && assistantEntry) {
          assistantEntry.content = displayText;
        }
        updateMessageGroup(resolvePendingGroup(pending), displayText);
      }
    } else if (streamChatId === activeChatId) {
      updateMessageGroup(resolvePendingGroup(pending), stripExecutionMemoryBlock(reply));
    }
    if (meta) {
      updateAgentBackendStatus(meta);
    }
    persistChatMessages(streamChatId, streamMessages);
  } catch (error) {
    const isAbort = error instanceof DOMException && error.name === "AbortError";
    const isCancelled = error instanceof Error && error.name === "AgentCancelledError";
    const superseded = !isStreamGenerationLive(streamChatId, streamGeneration);
    if (superseded) {
      if (!answered && (isAbort || isCancelled) && assistantEntry) {
        if (isPlaceholderAssistantContent(assistantEntry.content)) {
          markAssistantStopped(assistantEntry);
          if (streamChatId === activeChatId) {
            updateMessageGroup(resolvePendingGroup(pending), "（已停止生成）");
          }
          persistChatMessages(streamChatId, streamMessages);
        }
      }
      return;
    }
    if (answered) return;
    if (isAbort || isCancelled) {
      if (assistantEntry) markAssistantStopped(assistantEntry);
      if (streamChatId === activeChatId) {
        updateMessageGroup(resolvePendingGroup(pending), "（已停止生成）");
      }
      persistChatMessages(streamChatId, streamMessages);
      return;
    }
    const message = `调用失败：${error instanceof Error ? error.message : String(error)}`;
    if (streamChatId === activeChatId) {
      const target = resolvePendingGroup(pending);
      updateMessageGroup(target, message);
      ensureThinkingPanel(target);
      appendThinkingStep(target, { kind: "result", title: "错误", body: message });
    } else if (assistantEntry) {
      appendThinkingStepToEntry(assistantEntry, { kind: "result", title: "错误", body: message });
    }
    if (assistantEntry) assistantEntry.content = message;
    persistChatMessages(streamChatId, streamMessages);
  } finally {
    clearStreamIdleTimer();
    clearStreamStatusPoll();
    const status = await fetchRunStatus(streamChatId);
    if (isStaleTaskState(status)) {
      reconcileStaleTaskUi(streamChatId, status, {
        pending,
        assistantEntry,
        persist: true,
      });
      finishChatStream(streamChatId);
      return;
    }
    const stillRunning = isTaskLikelyActive(status, streamChatId);
    if (stillRunning) {
      syncRunStatusToUI(streamChatId, status, {
        pending,
        assistantEntry,
        messages: streamMessages,
      });
      startBackgroundRunWatch(streamChatId, { pending, assistantEntry });
      finishChatStream(streamChatId, { keepBackgroundWatch: true });
      return;
    }
    // 流式已结束且任务不在跑：必须收尾背景监控卡片（轮询期间可能已创建「Agent 运行中」）
    if (streamChatId === activeChatId) {
      finishBackgroundExecutionCard();
    }
    if (assistantEntry && isPlaceholderAssistantContent(assistantEntry.content)) {
      const fallback = "（流式连接已结束，未检测到后台任务）";
      assistantEntry.content = fallback;
      if (streamChatId === activeChatId) {
        updateMessageGroup(resolvePendingGroup(pending), fallback);
      }
      persistChatMessages(streamChatId, streamMessages);
    }
    finishChatStream(streamChatId);
  }
}

function updateComposerStatus() {
  const statusPill = document.querySelector(".agent-chat__composer-footer .status-pill");
  const llm = window.getLlmConfig?.();
  const account = llm?.account;
  const vendor = llm?.vendor;
  if (!statusPill) return;

  const port = window.location.port || (window.location.protocol === "https:" ? "443" : "80");
  const host = window.location.hostname || "—";
  const backendOk = window.__proxyServerReady === true;

  if (!account) {
    const statusText = `${backendOk ? "后端已连接" : "后端未连接"} · ${host}:${port}`;
    statusPill.innerHTML = `
      <span class="sidebar-status__dot" style="background:${backendOk ? "var(--ok)" : "var(--accent)"}"></span>
      <a href="#" class="status-pill__link">${statusText}</a>
    `;
    return;
  }

  const model = account.model || vendor?.defaultModel || "—";
  const hasKey = account.authMode === "local" || Boolean(account.apiKey);
  const skillCount = window.__agentBackendSkills?.length || 0;
  const statusParts = [
    backendOk ? "已连接" : "未连接",
    `${host}:${port}`,
    "sRNAgent",
    skillCount ? `${skillCount} skill(s)` : "",
    model,
    hasKey ? "" : "未配置 Key",
  ].filter(Boolean);
  statusPill.innerHTML = `
    <span class="sidebar-status__dot" style="background:${backendOk && hasKey ? "var(--ok)" : "var(--accent)"}"></span>
    <a href="#" class="status-pill__link">${statusParts.join(" · ")}</a>
  `;
}

function updateAgentBackendStatus(meta) {
  window.__agentBackendSkills = meta?.skills || [];
  window.__agentExecution = meta?.execution || window.__agentExecution || {};
  updateComposerStatus();
}

async function loadAgentBackendStatus() {
  updateComposerStatus();
  const ok = await window.probeProxyServer?.();
  updateComposerStatus();
  if (!ok || !window.fetchAgentStatus) return;
  try {
    const status = await window.fetchAgentStatus();
    window.__agentBackendSkills = status.skills || [];
    window.__agentExecution = status.execution || {};
    updateComposerStatus();
  } catch (_error) {
    updateComposerStatus();
  }
}

function setPage(page) {
  currentPage = page;

  document.querySelectorAll(".nav-item[data-page]").forEach((item) => {
    item.classList.toggle("nav-item--active", item.dataset.page === page);
  });

  document.querySelectorAll(".page-view[data-view]").forEach((view) => {
    view.classList.toggle("page-view--active", view.dataset.view === page);
  });

  const label = document.querySelector(`.nav-item[data-page="${page}"] .nav-item__text`)?.textContent?.trim() || page;
  if (breadcrumbCurrent) breadcrumbCurrent.textContent = label;

  const isAgent = page === "agent";
  const isAnalysis = page === "analysis";

  if (agentSessions) agentSessions.hidden = !isAgent;

  shell.classList.remove("shell--nav-drawer-open");

  if (isAgent) scrollThreadToBottom();
  if (isAgent) updateComposerStatus();
}

function appendAnalysisLog(message) {
  if (!analysisLog) return;
  const stamp = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  const prefix = analysisLog.textContent.includes("Waiting") ? "" : `${analysisLog.textContent}\n`;
  analysisLog.textContent = `${prefix}[${stamp}] ${message}`.trim();
}

function selectAnalysisCategory(category) {
  currentAnalysisCategory = category;
  const label = categoryLabels[category] || category;

  document.querySelectorAll(".analysis-nav__item").forEach((item) => {
    item.classList.toggle("analysis-nav__item--active", item.dataset.category === category);
  });

  if (parameterHint) parameterHint.hidden = true;
  if (parameterForm) parameterForm.hidden = false;
  appendAnalysisLog(`Selected analysis: ${label}`);
}

function bindUploadCard(cardId, inputId, modeLabel) {
  const card = document.getElementById(cardId);
  const input = document.getElementById(inputId);
  if (!card || !input) return;

  const openPicker = () => input.click();

  card.addEventListener("click", openPicker);
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openPicker();
    }
  });

  card.addEventListener("dragover", (event) => {
    event.preventDefault();
    card.classList.add("upload-card--dragover");
  });

  card.addEventListener("dragleave", () => {
    card.classList.remove("upload-card--dragover");
  });

  card.addEventListener("drop", (event) => {
    event.preventDefault();
    card.classList.remove("upload-card--dragover");
    const file = event.dataTransfer?.files?.[0];
    if (file) handleAnalysisFile(file, modeLabel);
  });

  input.addEventListener("change", () => {
    const file = input.files?.[0];
    if (file) handleAnalysisFile(file, modeLabel);
    input.value = "";
  });
}

function handleAnalysisFile(file, modeLabel) {
  appendAnalysisLog(`${modeLabel}: selected ${file.name}`);
  if (analysisLog) {
    appendAnalysisLog(`Ready to load .h5ad dataset (${(file.size / 1024 / 1024).toFixed(2)} MB)`);
  }
}

navToggle?.addEventListener("click", () => {
  shell.classList.toggle("shell--nav-drawer-open");
});

navBackdrop?.addEventListener("click", () => {
  shell.classList.remove("shell--nav-drawer-open");
});

collapseToggle?.addEventListener("click", () => {
  shell.classList.toggle("shell--nav-collapsed");
});

themeToggle?.addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme-mode") === "light" ? "dark" : "light";
  setTheme(next);
});

document.querySelectorAll(".nav-item[data-page]").forEach((item) => {
  item.addEventListener("click", (event) => {
    event.preventDefault();
    setPage(item.dataset.page || "agent");
  });
});

newChatBtn?.addEventListener("click", () => {
  startNewChat();
});

runAnalysisBtn?.addEventListener("click", () => {
  const label = categoryLabels[currentAnalysisCategory] || "Analysis";
  appendAnalysisLog(`Running ${label}... (mock)`);
  window.setTimeout(() => {
    appendAnalysisLog(`${label} completed successfully.`);
  }, 800);
});

composer?.addEventListener("compositionstart", () => {
  isComposing = true;
});

composer?.addEventListener("compositionend", () => {
  isComposing = false;
});

composer?.addEventListener("input", () => {
  imeEnterStroke = false;
  composer.style.height = "auto";
  composer.style.height = `${composer.scrollHeight}px`;
});

composer?.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey) return;

  // 仅看当前按键的 IME 状态，不用模块级 isComposing（避免残留导致无法发送）
  if (event.isComposing || event.keyCode === 229) {
    imeEnterStroke = true;
    return;
  }

  // 同一次物理 Enter：先确认 IME，再弹起的 keydown 不发送
  if (imeEnterStroke) {
    return;
  }

  event.preventDefault();
  void handleSend();
});

composer?.addEventListener("keyup", (event) => {
  if (event.key === "Enter") {
    imeEnterStroke = false;
  }
});

composerForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  if (event.isComposing || imeEnterStroke) return;
  void handleSend();
});

document.querySelector(".agent-chat__composer-footer")?.addEventListener("click", (event) => {
  const link = event.target.closest(".status-pill__link");
  if (!link) return;
  event.preventDefault();
  setPage("config");
});

sendBtn?.addEventListener("click", (event) => {
  event.preventDefault();
  handleSend();
});

bindUploadCard("drop-zone-analysis", "file-input-analysis", "Analysis Mode");
bindUploadCard("drop-zone-preview", "file-input-preview", "Preview Mode");

initTheme();
void initChatSessions();
updateAutoApproveUi();
setPage("agent");
setComposerMode("send");
scrollThreadToBottom();
window.updateComposerStatus = updateComposerStatus;
updateComposerStatus();
loadAgentBackendStatus();
window.getActiveChatId = () => activeChatId;
window.isAgentSending = () => isActiveChatSending();
window.KernelPanel?.refresh?.();

autoApproveToggle?.addEventListener("click", () => {
  setAutoApproveCode(!autoApproveCode);
});

window.addEventListener("llm-config-updated", () => {
  updateComposerStatus();
  loadAgentBackendStatus();
});

window.addEventListener("proxy-server-probed", () => {
  updateComposerStatus();
  loadAgentBackendStatus();
  if (window.__proxyServerReady) {
    chatPersistenceMode = "server";
    try {
      localStorage.removeItem(CHAT_STORE_KEY);
    } catch {
      // ignore storage failures
    }
    void syncChatStoreFromServer().then((synced) => {
      if (synced) applyActiveChatFromStore();
    });
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    void refreshChatStoreFromServerIfIdle();
  }
});
