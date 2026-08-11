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
    model: "gpt-4o-mini",
  };
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
  var llmBtn = null;
  var llmPanel = null;

  function refreshBtn() {
    if (!llmBtn) return;
    llmBtn.classList.toggle("on", isConfigured());
  }
  function openLlmPanel() {
    if (!llmPanel) return;
    llmPanel.classList.add("open");
    llmPanel.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    syncForm();
  }
  function closeLlmPanel() {
    if (!llmPanel) return;
    llmPanel.classList.remove("open");
    llmPanel.setAttribute("aria-hidden", "true");
    try {
      var others = ["#ghPanel", "#notePanel", "#forkPanel"];
      var locked = others.some(function (sel) {
        var el = document.querySelector(sel);
        return el && el.classList.contains("open");
      });
      if (!locked) document.body.style.overflow = "";
    } catch (e) {}
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
    llmBtn = document.querySelector("#llmBtn");
    llmPanel = document.querySelector("#llmPanel");
    if (llmBtn) llmBtn.hidden = !IS_PERSONAL;  // 仅个人版显示入口
    if (testBtn) testBtn.addEventListener("click", testConnection);
    if (clearBtn) clearBtn.addEventListener("click", function () {
      saveConfig(null);
      if (baseEl) baseEl.value = DEFAULTS.base_url;
      if (keyEl) keyEl.value = "";
      if (modelEl) modelEl.value = DEFAULTS.model;
      setStatus("已清除，回到规则个性化模式", true);
      if (clearBtn) clearBtn.hidden = true;
      refreshBtn();
    });
    if (llmBtn && llmPanel) {
      llmBtn.addEventListener("click", openLlmPanel);
      var closeBtn = document.querySelector("#llmClose");
      if (closeBtn) closeBtn.addEventListener("click", closeLlmPanel);
      llmPanel.addEventListener("click", function (e) { if (e.target === llmPanel) closeLlmPanel(); });
      var saveBtn = document.querySelector("#llmSave");
      if (saveBtn) saveBtn.addEventListener("click", function () {
        var ok = saveFromForm();
        setStatus(ok ? "已保存并同步到本地后端 · 每日管道将使用该 Key"
          : "未填 Key，已清除（规则模式）", ok);
        refreshBtn();
        if (ok) setTimeout(closeLlmPanel, 400);  // 保存成功后自动关闭
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && llmPanel.classList.contains("open")) closeLlmPanel();
      });
      refreshBtn();
    }
    syncForm();
  }

  // ===== 对外接口 =====
  window.LLM = {
    isConfigured: isConfigured,
    getConfig: getConfig,
    saveConfig: saveConfig,
    clearConfig: clearConfig,
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
