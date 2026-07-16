/**
 * Config page — LLM provider & agent context settings (ClawX-inspired, static mock)
 */
(function initConfigPage() {
  const STORAGE_KEY = "ui-mock-llm-config";

  const providerListEl = document.getElementById("config-provider-list");
  const addVendorSelect = document.getElementById("config-add-vendor");
  const addProviderBtn = document.getElementById("config-add-provider");
  const editorEmptyEl = document.getElementById("config-editor-empty");
  const editorForm = document.getElementById("config-editor");
  const testBtn = document.getElementById("config-test");
  const saveBtn = document.getElementById("config-save");
  const resetBtn = document.getElementById("config-reset");
  const statusEl = document.getElementById("config-status");
  const activeSummaryEl = document.getElementById("config-active-summary");
  const vendorFilterEl = document.getElementById("config-vendor-filter");

  const fields = {
    label: document.getElementById("cfg-label"),
    authMode: document.getElementById("cfg-auth-mode"),
    apiKey: document.getElementById("cfg-api-key"),
    toggleApiKey: document.getElementById("cfg-toggle-api-key"),
    baseUrl: document.getElementById("cfg-base-url"),
    apiProtocol: document.getElementById("cfg-api-protocol"),
    model: document.getElementById("cfg-model"),
    customModel: document.getElementById("cfg-custom-model"),
    enabled: document.getElementById("cfg-enabled"),
    systemPrompt: document.getElementById("cfg-system-prompt"),
    temperature: document.getElementById("cfg-temperature"),
    topP: document.getElementById("cfg-top-p"),
    maxTokens: document.getElementById("cfg-max-tokens"),
    maxHistory: document.getElementById("cfg-max-history"),
    maxTurns: document.getElementById("cfg-max-turns"),
    contextWindow: document.getElementById("cfg-context-window"),
    stream: document.getElementById("cfg-stream"),
    rememberKey: document.getElementById("cfg-remember-key"),
  };

  let config = loadConfig();
  let selectedProviderId = config.defaultProviderId;
  let vendorFilter = "all";
  let apiKeyVisible = false;

  function loadConfig() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return structuredClone(window.DEFAULT_LLM_CONFIG);
      const parsed = JSON.parse(raw);
      return {
        ...structuredClone(window.DEFAULT_LLM_CONFIG),
        ...parsed,
        agent: { ...window.DEFAULT_LLM_CONFIG.agent, ...(parsed.agent || {}) },
        providers: Array.isArray(parsed.providers) && parsed.providers.length
          ? parsed.providers
          : structuredClone(window.DEFAULT_LLM_CONFIG.providers),
      };
    } catch {
      return structuredClone(window.DEFAULT_LLM_CONFIG);
    }
  }

  function saveConfig() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
    window.dispatchEvent(new CustomEvent("llm-config-updated", { detail: config }));
  }

  function getVendor(vendorId) {
    return window.PROVIDER_CATALOG.find((v) => v.id === vendorId);
  }

  function getSelectedProvider() {
    return config.providers.find((p) => p.id === selectedProviderId) || null;
  }

  function maskApiKey(key) {
    if (!key) return "未配置";
    if (key.length <= 8) return "••••••••";
    return `${key.slice(0, 4)}••••${key.slice(-4)}`;
  }

  function setStatus(message, type = "info") {
    if (!statusEl) return;
    statusEl.textContent = message;
    statusEl.dataset.type = type;
  }

  function populateAddVendorSelect() {
    if (!addVendorSelect) return;
    addVendorSelect.innerHTML = window.PROVIDER_CATALOG.map(
      (v) => `<option value="${v.id}">${v.icon} ${v.name}</option>`,
    ).join("");
  }

  function populateVendorFilter() {
    if (!vendorFilterEl) return;
    const categories = [
      { id: "all", label: "全部厂商" },
      { id: "official", label: "官方" },
      { id: "compatible", label: "兼容聚合" },
      { id: "local", label: "本地" },
      { id: "custom", label: "自定义" },
    ];
    vendorFilterEl.innerHTML = categories
      .map(
        (c) =>
          `<button type="button" class="config-chip${vendorFilter === c.id ? " config-chip--active" : ""}" data-filter="${c.id}">${c.label}</button>`,
      )
      .join("");
  }

  function renderProviderList() {
    if (!providerListEl) return;

    const canDelete = config.providers.length > 1;
    const items = config.providers
      .map((account) => {
        const vendor = getVendor(account.vendorId);
        const active = account.id === selectedProviderId;
        const defaultBadge = account.isDefault ? '<span class="config-provider-card__badge">默认</span>' : "";
        const statusClass = account.enabled ? "config-provider-card__dot--ok" : "config-provider-card__dot--off";
        const title = account.label || vendor?.name || account.vendorId;
        const meta = `${account.model || vendor?.defaultModel || "—"} · ${maskApiKey(account.apiKey)}`;
        return `
          <div class="config-provider-card${active ? " config-provider-card--active" : ""}" data-provider-id="${account.id}">
            <button type="button" class="config-provider-card__main" data-select-provider="${account.id}" title="${title}">
              <span class="config-provider-card__icon">${vendor?.icon || "⚙️"}</span>
              <span class="config-provider-card__body">
                <span class="config-provider-card__title">${title}</span>
                <span class="config-provider-card__meta">${meta}</span>
              </span>
            </button>
            ${defaultBadge}
            <button
              type="button"
              class="config-provider-card__delete"
              data-delete-provider="${account.id}"
              title="${canDelete ? "删除此 Provider" : "至少保留一个 Provider"}"
              aria-label="删除 ${title}"
              ${canDelete ? "" : "disabled"}
            >删除</button>
            <span class="config-provider-card__dot ${statusClass}" aria-hidden="true"></span>
          </div>
        `;
      })
      .join("");

    providerListEl.innerHTML = items || '<p class="config-empty">尚未添加 Provider，点击右上角添加。</p>';
    updateActiveSummary();
  }

  function updateActiveSummary() {
    if (!activeSummaryEl) return;
    const account = config.providers.find((p) => p.isDefault) || getSelectedProvider();
    if (!account) {
      activeSummaryEl.textContent = "未选择默认模型";
      return;
    }
    const vendor = getVendor(account.vendorId);
    activeSummaryEl.innerHTML = `
      <strong>${vendor?.icon || ""} ${account.label || vendor?.name}</strong>
      <span>${account.model}</span>
      <span class="config-active-summary__muted">${account.baseUrl || vendor?.defaultBaseUrl || ""}</span>
    `;
  }

  function fillModelOptions(vendor, currentModel) {
    if (!fields.model) return;
    const models = vendor?.models || [];
    fields.model.innerHTML = models
      .map((m) => `<option value="${m}"${m === currentModel ? " selected" : ""}>${m}</option>`)
      .join("");
    if (fields.customModel) {
      const isCustom = currentModel && !models.includes(currentModel);
      fields.customModel.value = isCustom ? currentModel : "";
      fields.customModel.hidden = models.includes(currentModel);
    }
  }

  function renderEditor() {
    const account = getSelectedProvider();
    const hasSelection = Boolean(account);
    const deleteBtn = document.getElementById("config-delete-provider");

    if (editorEmptyEl) editorEmptyEl.hidden = hasSelection;
    if (editorForm) editorForm.hidden = !hasSelection;
    if (deleteBtn) {
      deleteBtn.disabled = config.providers.length <= 1;
      deleteBtn.title = deleteBtn.disabled ? "至少保留一个 Provider" : "删除当前 Provider";
    }
    if (!account) return;

    const vendor = getVendor(account.vendorId);
    if (!vendor) return;

    if (fields.label) fields.label.value = account.label || vendor.name;
    if (fields.authMode) {
      fields.authMode.innerHTML = vendor.authModes
        .map((mode) => {
          const labels = {
            api_key: "API Key",
            oauth_device: "OAuth 设备码",
            oauth_browser: "OAuth 浏览器",
            local: "本地（无需 Key）",
          };
          return `<option value="${mode}"${account.authMode === mode ? " selected" : ""}>${labels[mode] || mode}</option>`;
        })
        .join("");
    }
    if (fields.apiKey) {
      fields.apiKey.type = apiKeyVisible ? "text" : "password";
      fields.apiKey.value = account.apiKey || "";
      fields.apiKey.placeholder = vendor.placeholder || "API Key";
      fields.apiKey.disabled = account.authMode === "local";
    }
    if (fields.baseUrl) fields.baseUrl.value = account.baseUrl || vendor.defaultBaseUrl || "";
    if (fields.apiProtocol) fields.apiProtocol.value = account.apiProtocol || vendor.apiProtocol || "openai-completions";
    fillModelOptions(vendor, account.model || vendor.defaultModel);
    if (fields.enabled) fields.enabled.checked = account.enabled !== false;

    if (fields.systemPrompt) fields.systemPrompt.value = config.agent.systemPrompt || "";
    if (fields.temperature) fields.temperature.value = String(config.agent.temperature ?? 0.3);
    if (fields.topP) fields.topP.value = String(config.agent.topP ?? 1);
    if (fields.maxTokens) fields.maxTokens.value = String(config.agent.maxTokens ?? 4096);
    if (fields.maxHistory) fields.maxHistory.value = String(config.agent.maxHistoryMessages ?? 40);
    if (fields.maxTurns) fields.maxTurns.value = String(config.agent.maxTurns ?? 100);
    if (fields.contextWindow) {
      fields.contextWindow.value = String(config.agent.contextWindow ?? vendor.contextWindow ?? 128000);
    }
    if (fields.stream) fields.stream.checked = config.agent.stream !== false;
    if (fields.rememberKey) fields.rememberKey.checked = config.agent.rememberApiKey !== false;
  }

  function readEditorIntoConfig() {
    const account = getSelectedProvider();
    if (!account) return false;

    const vendor = getVendor(account.vendorId);
    account.label = fields.label?.value.trim() || vendor?.name || account.vendorId;
    account.authMode = fields.authMode?.value || "api_key";
    if (account.authMode !== "local") {
      account.apiKey = fields.apiKey?.value.trim() || "";
    } else {
      account.apiKey = "";
    }
    account.baseUrl = fields.baseUrl?.value.trim() || vendor?.defaultBaseUrl || "";
    account.apiProtocol = fields.apiProtocol?.value || vendor?.apiProtocol || "openai-completions";

    const selectedModel = fields.model?.value || "";
    const customModel = fields.customModel?.value.trim() || "";
    account.model = customModel || selectedModel || vendor?.defaultModel || "";

    account.enabled = fields.enabled?.checked !== false;

    config.agent.systemPrompt = fields.systemPrompt?.value || "";
    config.agent.temperature = Number(fields.temperature?.value || 0.3);
    config.agent.topP = Number(fields.topP?.value || 1);
    config.agent.maxTokens = Number(fields.maxTokens?.value || 4096);
    config.agent.maxHistoryMessages = Number(fields.maxHistory?.value || 40);
    config.agent.maxTurns = Number(fields.maxTurns?.value || 100);
    config.agent.contextWindow = Number(fields.contextWindow?.value || 128000);
    config.agent.stream = fields.stream?.checked !== false;
    config.agent.rememberApiKey = fields.rememberKey?.checked !== false;

    return true;
  }

  function selectProvider(id) {
    selectedProviderId = id;
    renderProviderList();
    renderEditor();
  }

  function addProvider(vendorId) {
    const vendor = getVendor(vendorId);
    if (!vendor) return;

    const id = `${vendorId}-${Date.now()}`;
    const account = {
      id,
      vendorId,
      label: vendor.name,
      authMode: vendor.defaultAuthMode || vendor.authModes[0] || "api_key",
      apiKey: "",
      baseUrl: vendor.defaultBaseUrl || "",
      apiProtocol: vendor.apiProtocol || "openai-completions",
      model: vendor.defaultModel || "",
      enabled: true,
      isDefault: config.providers.length === 0,
    };

    config.providers.push(account);
    if (account.isDefault) config.defaultProviderId = id;
    selectProvider(id);
    setStatus(`已添加 ${vendor.name}`, "success");
  }

  function deleteProvider(id) {
    const idx = config.providers.findIndex((p) => p.id === id);
    if (idx < 0) return;

    if (config.providers.length <= 1) {
      setStatus("至少保留一个 Provider", "error");
      return;
    }

    const account = config.providers[idx];
    const vendor = getVendor(account.vendorId);
    const name = account.label || vendor?.name || account.vendorId || id;
    const tip = account.isDefault
      ? `「${name}」是当前默认模型，删除后将自动切换到列表中的下一个。确定删除？`
      : `确定删除「${name}」？`;
    if (!window.confirm(`${tip}\n删除后会立即保存到本地。`)) return;

    const removed = config.providers.splice(idx, 1)[0];
    if (removed.isDefault && config.providers.length) {
      const nextDefault = config.providers[Math.min(idx, config.providers.length - 1)];
      config.providers.forEach((p) => {
        p.isDefault = p.id === nextDefault.id;
      });
      config.defaultProviderId = nextDefault.id;
    }

    if (selectedProviderId === id) {
      const fallback = config.providers[idx] || config.providers[idx - 1] || config.providers[0];
      selectedProviderId = fallback?.id || "";
    }

    saveConfig();
    renderProviderList();
    renderEditor();
    setStatus(`已删除 ${name}`, "success");
  }

  function setDefaultProvider(id) {
    config.providers.forEach((p) => {
      p.isDefault = p.id === id;
    });
    config.defaultProviderId = id;
    renderProviderList();
    updateActiveSummary();
  }

  function listLlmProviders() {
    return config.providers
      .filter((p) => p.enabled !== false)
      .map((account) => {
        const vendor = getVendor(account.vendorId);
        const model = account.model || vendor?.defaultModel || "";
        const name = account.label || vendor?.name || account.vendorId || account.id;
        return {
          id: account.id,
          name,
          model,
          label: model ? `${name} · ${model}` : name,
          isDefault: Boolean(account.isDefault),
          vendorId: account.vendorId,
        };
      });
  }

  function setDefaultLlmProvider(id) {
    if (!id || !config.providers.some((p) => p.id === id)) return false;
    setDefaultProvider(id);
    selectedProviderId = id;
    renderEditor();
    saveConfig();
    return true;
  }

  function handleSave() {
    if (!readEditorIntoConfig()) return;
    saveConfig();
    renderProviderList();
    setStatus("配置已保存到本地（localStorage）", "success");
  }

  function handleTest() {
    if (!readEditorIntoConfig()) return;
    const account = getSelectedProvider();
    const vendor = getVendor(account?.vendorId);
    if (!account) return;

    if (account.authMode === "api_key" && !account.apiKey) {
      setStatus("请先填写 API Key", "error");
      return;
    }

    if (!window.probeProxyServer) {
      setStatus(`请先打开 http://<服务器IP>:8765/index.html 并确保 serve.py 在运行`, "error");
      return;
    }

    setStatus("正在测试连接…", "info");
    window
      .probeProxyServer(true)
      .then((ok) => {
        if (!ok) {
          setStatus(`无法连接 UI 后端，请确认 serve.py 已启动且防火墙放行 8765`, "error");
          return null;
        }
        return window.llmChatCompletion({
          account,
          vendor,
          agent: config.agent,
          messages: [{ role: "user", content: "Hi, reply with OK only." }],
        });
      })
      .then((text) => {
        if (text == null) return;
        setStatus(`连接成功 · ${vendor?.name} / ${account.model} · ${text.slice(0, 80)}`, "success");
        window.dispatchEvent(new CustomEvent("proxy-server-probed", { detail: { ok: true } }));
        window.updateComposerStatus?.();
      })
      .catch((error) => {
        setStatus(error?.message || "连接失败", "error");
      });
  }

  function handleReset() {
    config = structuredClone(window.DEFAULT_LLM_CONFIG);
    selectedProviderId = config.defaultProviderId;
    saveConfig();
    renderProviderList();
    renderEditor();
    setStatus("已恢复默认配置（MiniMax CN）", "success");
  }

  function getActiveLlmConfig() {
    const account = config.providers.find((p) => p.isDefault && p.enabled)
      || config.providers.find((p) => p.enabled)
      || config.providers[0];
    const vendor = account ? getVendor(account.vendorId) : null;
    return { config, account, vendor };
  }

  // Events
  populateAddVendorSelect();
  populateVendorFilter();
  renderProviderList();
  renderEditor();

  providerListEl?.addEventListener("click", (event) => {
    const deleteBtn = event.target.closest("[data-delete-provider]");
    if (deleteBtn) {
      event.preventDefault();
      event.stopPropagation();
      deleteProvider(deleteBtn.dataset.deleteProvider);
      return;
    }
    const selectBtn = event.target.closest("[data-select-provider]");
    const card = event.target.closest("[data-provider-id]");
    const id = selectBtn?.dataset.selectProvider || card?.dataset.providerId;
    if (!id) return;
    selectProvider(id);
  });

  addProviderBtn?.addEventListener("click", () => {
    const vendorId = addVendorSelect?.value || window.PROVIDER_CATALOG[0]?.id;
    addProvider(vendorId);
  });

  vendorFilterEl?.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-filter]");
    if (!chip) return;
    vendorFilter = chip.dataset.filter;
    populateVendorFilter();
    if (addVendorSelect) {
      const options = window.PROVIDER_CATALOG.filter(
        (v) => vendorFilter === "all" || v.category === vendorFilter,
      );
      addVendorSelect.innerHTML = options
        .map((v) => `<option value="${v.id}">${v.icon} ${v.name}</option>`)
        .join("");
    }
  });

  fields.toggleApiKey?.addEventListener("click", () => {
    apiKeyVisible = !apiKeyVisible;
    if (fields.apiKey) fields.apiKey.type = apiKeyVisible ? "text" : "password";
  });

  fields.model?.addEventListener("change", () => {
    if (!fields.customModel) return;
    const vendor = getVendor(getSelectedProvider()?.vendorId);
    const val = fields.model.value;
    fields.customModel.hidden = vendor?.models?.includes(val);
    if (fields.customModel.hidden) fields.customModel.value = "";
  });

  document.getElementById("config-set-default")?.addEventListener("click", () => {
    if (selectedProviderId) setDefaultProvider(selectedProviderId);
    setStatus("已设为 Agent 默认模型", "success");
  });

  document.getElementById("config-delete-provider")?.addEventListener("click", () => {
    if (selectedProviderId) deleteProvider(selectedProviderId);
  });

  saveBtn?.addEventListener("click", handleSave);
  testBtn?.addEventListener("click", handleTest);
  resetBtn?.addEventListener("click", handleReset);

  window.getLlmConfig = getActiveLlmConfig;
  window.loadLlmConfig = loadConfig;
  window.listLlmProviders = listLlmProviders;
  window.setDefaultLlmProvider = setDefaultLlmProvider;
})();
