// StarRadar · LLM 直连模块（M1）
// 职责：浏览器直连 OpenAI 兼容端点（用户自填 key，仅存 localStorage，不经过任何第三方）
//       提供 chat() + 每日限额节流 + JSON 解析，供个性化推荐 / 记忆画像 / 周报解读使用
// 依赖：无。app.js 等模块调用 window.LLM.*，失败时由调用方降级到规则模式。
(function () {
  "use strict";

  var CFG_KEY = "starradar:llm_config";
  var USAGE_KEY = "starradar:llm_usage";

  // 公版回归客观（无问卷 / 无个性化入口）→ LLM 仅个人版启用；
  // 个人版配置 key 后：卡片推荐理由 / 记忆画像 / 周报解读 / 搜索建议全部生效。
  var IS_PERSONAL = location.search.indexOf("personal=1") !== -1;
  var DISABLED = !IS_PERSONAL;

  var DEFAULTS = {
    base_url: "https://api.openai.com/v1",
    model: "gpt-5-mini",
  };
  // 常见 OpenAI 兼容服务商【预设】——点击自动填入地址与模型，可自行修改；
  // 模型名以各服务商最新文档为准（2026-08：deepseek-chat 已停用，官方为 deepseek-v4-flash）
  var PRESETS = [
    { name: "OpenAI", base_url: "https://api.openai.com/v1", model: "gpt-5-mini" },
    { name: "DeepSeek", base_url: "https://api.deepseek.com/v1", model: "deepseek-v4-flash" },
    { name: "智谱 GLM", base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-5.2" },
    { name: "通义千问", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen3-flash" },
  ];
  // 每功能每日限额（用户自己的 key，不能乱烧）
  var LIMITS = { profile: 1, report: 1, survey: 1, reason: 20, ask: 30 };

  var cfg = null;
  var baseEl, keyEl, modelEl, statusEl, clearBtn, testBtn;
  var booted = false;

  function lsGet(k, d) {
    try { var v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; }
  }
  function lsSet(k, v) {
    try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {}
  }

  // ===== 配置 =====
  function loadCfg() {
    if (cfg) return cfg;
    cfg = lsGet(CFG_KEY, null);
    return cfg;
  }
  function isConfigured() {
    if (DISABLED) return false;
    var c = loadCfg();
    return !!(c && c.key);
  }
  function getConfig() { return loadCfg(); }
  // key 同步本地后端（供 --personal 每日管道调用 LLM）：存 data/profile/llm_config.json。
  // 无本地 server（GitHub Pages）时静默失败，不影响浏览器内功能。
  function syncKeyToBackend(c) {
    if (c && c.key) {
      fetch("/api/llm_key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_url: c.base_url || "",
          key: c.key,
          model: c.model || "",
        }),
      }).catch(function () {});
    } else {
      fetch("/api/llm_key", { method: "DELETE" }).catch(function () {});
    }
  }
  function saveConfig(c) {
    cfg = c || null;
    if (c && c.key) lsSet(CFG_KEY, c);
    else { try { localStorage.removeItem(CFG_KEY); } catch (e) {} }
    syncKeyToBackend(c);  // 保存/清除均同步后端
  }
  function clearConfig() { saveConfig(null); }

  // ===== 节流（每日限额，localStorage 记录） =====
  function today() { return new Date().toISOString().slice(0, 10); }
  function usageAll() {
    var u = lsGet(USAGE_KEY, {});
    if (u._day !== today()) u = { _day: today() };
    return u;
  }
  function usageLeft(feature) {
    var limit = LIMITS[feature] != null ? LIMITS[feature] : 20;
    return limit - (usageAll()[feature] || 0);
  }
  function canCall(feature) {
    return usageLeft(feature) > 0;
  }
  function markCall(feature) {
    var u = usageAll();
    u[feature] = (u[feature] || 0) + 1;
    lsSet(USAGE_KEY, u);
  }

  // ===== chat：OpenAI 兼容 /chat/completions =====
  function chat(messages, opts) {
    opts = opts || {};
    var c = loadCfg();
    if (!c || !c.key) return Promise.reject(new Error("no-key"));
    var feature = opts.feature || "chat";
    if (!canCall(feature)) return Promise.reject(new Error("rate-limit"));
    var base = String(c.base_url || DEFAULTS.base_url).replace(/\/+$/, "");
    var url = base + "/chat/completions";
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + c.key,
      },
      body: JSON.stringify({
        model: c.model || DEFAULTS.model,
        messages: messages,
        temperature: opts.temperature != null ? opts.temperature : 0.7,
        max_tokens: opts.max_tokens || 700,
      }),
    }).then(function (r) {
      if (r.status === 401 || r.status === 403) throw new Error("auth-failed");
      if (!r.ok) throw new Error("http-" + r.status);
      return r.json();
    }).then(function (j) {
      markCall(feature);
      var text = j && j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content;
      if (!text) throw new Error("empty");
      return String(text).trim();
    });
  }

  // 容错解析 LLM 返回的 JSON（剥 markdown 围栏 / 截取首个对象）
  function parseJSON(text) {
    var t = String(text == null ? "" : text).trim();
    var m = t.match(/```(?:json)?\s*([\s\S]*?)\s*```/);
    if (m) t = m[1].trim();
    try { return JSON.parse(t); } catch (e) {
      var s = t.indexOf("{");
      var en = t.lastIndexOf("}");
      if (s >= 0 && en > s) {
        try { return JSON.parse(t.slice(s, en + 1)); } catch (e2) {}
      }
      throw e;
    }
  }

  // ===== 问卷内嵌表单（P9：并入"我的雷达"问卷 STEP 4） =====
  function setStatus(msg, ok) {
    if (!statusEl) return;
    statusEl.textContent = msg;
    statusEl.style.color = ok ? "#28a86b" : "#ff4d4f";
  }
  function syncForm() {
    var c = loadCfg() || {};
    if (baseEl) baseEl.value = c.base_url || DEFAULTS.base_url;
    if (keyEl) keyEl.value = c.key || "";
    if (modelEl) modelEl.value = c.model || DEFAULTS.model;
    if (clearBtn) clearBtn.hidden = !isConfigured();
    setStatus(isConfigured() ? "AI 个性化已开启 · 推荐 / 记忆 / 周报解读生效" : "未开启（规则模式：问卷兴趣匹配 + 行为加权）", isConfigured());
  }
  function readForm() {
    return {
      base_url: baseEl ? String(baseEl.value || "").trim() || DEFAULTS.base_url : DEFAULTS.base_url,
      key: keyEl ? String(keyEl.value || "").trim() : "",
      model: modelEl ? String(modelEl.value || "").trim() || DEFAULTS.model : DEFAULTS.model,
    };
  }
  function saveFromForm() {
    var c = readForm();
    if (!c.key) { saveConfig(null); return false; }
    saveConfig(c);
    syncForm();
    return true;
  }
  // 服务商【预设】快捷填充：点击填入 base_url + model，Key 留空由用户粘贴（预设可自行修改）
  // box/inputs 参数用于问卷内表单（默认 llmPanel 表单）
  function renderPresets(box, baseInput, modelInput) {
    if (!box) box = document.querySelector("#llmPresets");
    if (!box) return;
    baseInput = baseInput || baseEl;
    modelInput = modelInput || modelEl;
    box.innerHTML = '<span class="llm-presets-label">快捷预设</span>' + PRESETS.map(function (p) {
      return '<button class="llm-preset" type="button" data-base="' + p.base_url + '" data-model="' + p.model + '">' +
        p.name + "</button>";
    }).join("");
    Array.prototype.forEach.call(box.querySelectorAll(".llm-preset"), function (b) {
      b.addEventListener("click", function () {
        if (baseInput) baseInput.value = b.dataset.base;
        if (modelInput) modelInput.value = b.dataset.model;
        setStatus("已填入「" + b.textContent + "」预设（可改），请粘贴你的 API Key", true);
        var keyInput = document.querySelector("#llmKey") || document.querySelector("#qLLMKey");
        if (keyInput) keyInput.focus();
      });
    });
  }
  // 测试连通 → 通过才保存（含同步后端）；返回 Promise，失败抛中文原因
  function saveWithValues(c) {
    var cfgBackup = cfg;
    cfg = c || {};
    if (!cfg.key) { cfg = cfgBackup; return Promise.reject(new Error("请先填入 API Key")); }
    return chat([{ role: "user", content: "ping" }], { max_tokens: 8, feature: "test" })
      .then(function () {
        saveConfig(c);
        refreshBtn();
        return true;
      })
      .catch(function (err) {
        if (err.message === "auth-failed") throw new Error("Key 无效或权限不足");
        if (err.message === "rate-limit") throw new Error("今日测试调用已达上限");
        throw new Error("连接失败：" + (err.message || "请检查 Base URL / 网络"));
      })
      .then(function (v) { cfg = cfgBackup; return v; }, function (e) { cfg = cfgBackup; throw e; });
  }
  // 保存前先测试连通：通过才保存（含同步后端）；失败停留并给出原因
  function saveWithTest() {
    var c = readForm();
    if (!c.key) { setStatus("请先填入 API Key", false); if (keyEl) keyEl.focus(); return; }
    var cfgBackup = cfg;
    cfg = c;  // 用表单值测试（未保存）
    setStatus("正在测试连接…", true);
    chat([{ role: "user", content: "ping" }], { max_tokens: 8, feature: "test" })
      .then(function () {
        cfg = cfgBackup;
        saveConfig(c);  // 测试通过才落库（自动同步 /api/llm_key）
        setStatus("连接成功 · 已保存并同步到本地后端", true);
      })
      .catch(function (err) {
        cfg = cfgBackup;
        setStatus(err.message === "auth-failed" ? "鉴权失败：Key 无效或权限不足"
          : (err.message === "rate-limit" ? "今日测试调用已达上限"
          : "连接失败：" + (err.message || "请检查 Base URL / 网络 / CORS")), false);
      });
  }
  function testConnection() {
    var c = readForm();
    if (!c.key) { setStatus("请先填入 API Key", false); return; }
    testBtn.disabled = true;
    setStatus("测试中…", true);
    var cfgBackup = cfg;
    cfg = c;
    chat([{ role: "user", content: "ping" }], { max_tokens: 8, feature: "test" })
      .then(function () {
        setStatus("连接成功，保存后生效", true);
      })
      .catch(function (err) {
        setStatus(err.message === "auth-failed" ? "鉴权失败：Key 无效"
          : (err.message === "rate-limit" ? "今日调用已达上限"
          : "连接失败：" + (err.message || "网络/CORS 问题")), false);
      })
      .then(function () { cfg = cfgBackup; testBtn.disabled = false; });
  }
  function refreshBtn() {
    // ⚡ 顶部入口已移除（LLM 并入问卷）；保留函数兼容 saveWithValues 调用
    var b = document.querySelector("#llmBtn");
    if (b) b.classList.toggle("on", isConfigured());
  }

  function boot() {
    if (booted) return;
    booted = true;
    baseEl = document.querySelector("#llmBase");
    keyEl = document.querySelector("#llmKey");
    modelEl = document.querySelector("#llmModel");
    statusEl = document.querySelector("#llmStatus");
    clearBtn = document.querySelector("#llmClear");
    testBtn = document.querySelector("#llmTest");
    // 顶部 ⚡ 面板已移除（LLM 配置并入问卷 STEP 3）；表单元素若存在仍可绑定（旧页面兜底）
    if (testBtn) testBtn.addEventListener("click", testConnection);
    if (clearBtn) clearBtn.addEventListener("click", function () {
      saveConfig(null);
      if (baseEl) baseEl.value = DEFAULTS.base_url;
      if (keyEl) keyEl.value = "";
      if (modelEl) modelEl.value = DEFAULTS.model;
      setStatus("已清除，回到规则个性化模式", true);
      if (clearBtn) clearBtn.hidden = true;
    });
    syncForm();
  }

  // 问卷 STEP 3 的 LLM 预设渲染（目标为问卷内表单）
  function renderSurveyPresets() {
    renderPresets(document.querySelector("#qLLMPresets"), document.querySelector("#qLLMBase"), document.querySelector("#qLLMModel"));
  }

  // ===== 对外接口 =====
  window.LLM = {
    isConfigured: isConfigured,
    getConfig: getConfig,
    saveConfig: saveConfig,
    clearConfig: clearConfig,
    saveWithValues: saveWithValues,
    renderSurveyPresets: renderSurveyPresets,
    chat: chat,
    parseJSON: parseJSON,
    canCall: canCall,
    usageLeft: usageLeft,
    syncForm: syncForm,
    saveFromForm: saveFromForm,
    refreshState: syncForm,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
