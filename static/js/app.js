// StarRadar · 主控脚本
// 职责：加载 JSON（scores=潜力雷达 / trends=每周趋势 / picks=我的收藏）
//       → 骨架屏 → 渲染卡片（网格/列表视图）→ tab 切换 → 周报 → 收藏（localStorage）→ toast
(function () {
  "use strict";

  var STATUS_BADGE = { new: "新进", comeback: "回归", accelerating: "加速", streak: "连增", normal: "" };
  var TABS = ["potential", "trends", "picks"];
  var DATA_FILES = {
    potential: "data/scores.json",
    trends: "data/trends.json",
    picks: "data/picks.json",
  };
  // 个人特化模式（?personal=1）：同一套 UI，数据源切换为本地后端 /api/personal/*
  var IS_PERSONAL = location.search.indexOf("personal=1") !== -1;
  var PERSONAL_MISSING = { potential: false, trends: false };
  var TAB_LABEL = { potential: "潜力雷达", trends: "每周趋势", picks: "我的收藏" };
  var STAGE_LABELS = {
    early: "早期", mid_early: "中早期", mid_late: "中后期",
    late: "后期", saturated: "已饱和",
  };
  var STAGE_CLASS = {
    early: "mid_early", mid_early: "mid_early", mid_late: "mid_late",
    late: "mid_late", saturated: "mid_late",
  };
  var DIM_KEYS = ["vel", "acc", "health", "fresh", "signal"];
  var DIM_NAMES = { vel: "速", acc: "加", health: "健", fresh: "鲜", signal: "信" };
  var FAV_KEY = "starradar:favs";
  var HISTORY_KEY = "starradar:history";
  var SURVEY_KEY = "starradar:survey";
  var SURVEY_SYNC_KEY = "starradar:survey_synced";

  var cardsEl = document.querySelector("#cards");
  var toastEl = document.querySelector("#toast");
  var data = { potential: null, trends: null, picks: null };
  var tab = "potential";
  var favs = {};
  var sortBy = "score";
  var historyLog = [];
  var detailOpenAt = 0;
  var dismissed = {};

  // ===== 行为日志（本地记录，导出时交给后端训练画像） =====
  function loadHistory() {
    try { historyLog = JSON.parse(localStorage.getItem(HISTORY_KEY)) || []; }
    catch (e) { historyLog = []; }
  }
  function saveHistory() {
    try {
      if (historyLog.length > 800) historyLog = historyLog.slice(-800);
      localStorage.setItem(HISTORY_KEY, JSON.stringify(historyLog));
    } catch (e) { /* 隐私模式 */ }
  }
  function logAction(item, action, extra) {
    var r = repoOf(item);
    var e = {
      repo: fullName(item),
      action: action,
      ts: new Date().toISOString(),
      topics: (r.topics || []).slice(0, 8),
      language: r.language || "",
      owner: r.owner || "",
    };
    if (extra) {
      if (extra.duration_s) e.duration_s = extra.duration_s;
      if (extra.days_ago) e.days_ago = extra.days_ago;
    }
    if (r.stars) e.stars = r.stars;
    if (r.forks) e.forks = r.forks;
    historyLog.push(e);
    saveHistory();
    queueReport(e);
    maybeRefreshImage();
  }

  // ===== M4 记忆画像（有 key 时：行为 → LLM 提炼兴趣画像，越用越准） =====
  // 触发：新增行为 ≥20 条 或 距上次提炼 >24h（阈值 + 定时兜底）；LLM 每日限 1 次
  var IMAGE_KEY = "starradar:profile_image";
  var PROFILE_STEP = 20;
  var PROFILE_MAX_AGE = 86400000; // 24h
  function profileImage() {
    try { return JSON.parse(localStorage.getItem(IMAGE_KEY)) || null; } catch (e) { return null; }
  }
  function saveProfileImage(img) {
    try { localStorage.setItem(IMAGE_KEY, JSON.stringify(img)); } catch (e) {}
  }
  function maybeRefreshImage() {
    if (typeof window.LLM === "undefined" || !window.LLM.isConfigured()) return;
    if (historyLog.length < PROFILE_STEP) return;
    var img = profileImage();
    var lastTs = img && img.updated_at ? new Date(img.updated_at).getTime() : 0;
    var newEvents = img ? (historyLog.length - (img.events || 0)) : historyLog.length;
    if (img && newEvents < PROFILE_STEP && Date.now() - lastTs < PROFILE_MAX_AGE) return;
    if (!window.LLM.canCall("profile")) return;
    var samples = historyLog.slice(-80).map(function (e) {
      return e.action + ":" + e.repo + (e.topics && e.topics.length ? "[" + e.topics.join(",") + "]" : "");
    }).join("\n");
    window.LLM.chat([
      { role: "system", content: "你是用户的 GitHub 兴趣分析师。根据用户最近的开源项目浏览/操作记录提炼兴趣画像，输出 JSON。" },
      { role: "user", content: "行为记录（action:repo[topics]）：\n" + samples +
        "\n\n输出 JSON：{\"tags\":[{\"tag\":\"方向名\",\"weight\":0.8}],\"summary\":\"一句话（≤50字）\"}\ntag 用简短中文方向词，weight 是 0-1 的偏好权重。" },
    ], { feature: "profile", temperature: 0.3, max_tokens: 400 })
      .then(function (txt) {
        var j = window.LLM.parseJSON(txt);
        var tags = Array.isArray(j.tags) ? j.tags.filter(function (t) {
          return t && t.tag && Number(t.weight) > 0;
        }).slice(0, 10) : [];
        var cleaned = {
          tags: tags.map(function (t) {
            return { tag: String(t.tag).slice(0, 30), weight: Math.max(0, Math.min(1, Number(t.weight) || 0)) };
          }),
          summary: String(j.summary || "").slice(0, 200),
          updated_at: new Date().toISOString(),
          events: historyLog.length,
        };
        saveProfileImage(cleaned);
        var t = document.querySelector("#toast");
        if (t) { t.textContent = "记忆已更新：你的兴趣画像更准了"; t.classList.add("show"); clearTimeout(window.__tt2); window.__tt2 = setTimeout(function () { t.classList.remove("show"); }, 1900); }
      })
      .catch(function () { /* 失败静默，下轮再试 */ });
  }

  // ===== 行为信号上报（增量队列 + /api/health 门控 + 指数退避） =====
  // 本地 `python src/main.py --serve` 落库；GitHub Pages 无后端时静默回退。
  var REPORT_KEY = "starradar:offline";
  var UID_KEY = "starradar:uid";
  var reportQueue = [];
  var reportTimer = null;
  var reportDelay = 30000;
  function loadReportQueue() {
    try { reportQueue = JSON.parse(localStorage.getItem(REPORT_KEY)) || []; }
    catch (e) { reportQueue = []; }
  }
  function saveReportQueue() {
    try {
      if (reportQueue.length > 2000) reportQueue = reportQueue.slice(-2000);
      localStorage.setItem(REPORT_KEY, JSON.stringify(reportQueue));
    } catch (e) { /* 隐私模式 */ }
  }
  function reportUid() {
    var u = null;
    try { u = localStorage.getItem(UID_KEY); } catch (e) {}
    if (!u) {
      u = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
        : "u-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
      try { localStorage.setItem(UID_KEY, u); } catch (e) {}
    }
    return u;
  }
  function queueReport(e) {
    reportQueue.push(e);
    saveReportQueue();
  }
  function flushReport() {
    if (!reportQueue.length) return;
    fetch("/api/health", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("no backend");
        var batch = reportQueue.splice(0, 100);
        if (!batch.length) return null;
        return fetch("/api/events", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ uid: reportUid(), events: batch }),
        }).then(function (r) {
          if (!r.ok) throw new Error("post failed");
          saveReportQueue();
          reportDelay = 30000;
          return r.json();
        }).catch(function (e) {
          reportQueue = batch.concat(reportQueue);
          saveReportQueue();
          throw e;
        });
      })
      .catch(function () {
        reportDelay = Math.min(reportDelay * 2, 600000);
      })
      .then(function () {
        reportTimer = setTimeout(flushReport, reportDelay);
      });
  }

  // ===== 一键数据同步（把本机问卷 + 行为队列完整送进本地服务库） =====
  // 场景：平时只开 GitHub Pages（行为/问卷只存浏览器）→ 本地 --serve 打开后自动补报，
  //       落库后跑一次管道即生成真正的个性化精选。问卷按内容比对，改动即自动重报。
  function syncSurvey() {
    var raw = null;
    try { raw = localStorage.getItem(SURVEY_KEY); } catch (e) {}
    if (!raw) return Promise.resolve("无问卷");
    var synced = null;
    try { synced = localStorage.getItem(SURVEY_SYNC_KEY); } catch (e) {}
    if (synced === raw) return Promise.resolve("问卷已同步");
    return fetch("/api/survey", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uid: reportUid(), survey: JSON.parse(raw) }),
    }).then(function (r) {
      if (!r.ok) throw new Error("survey failed");
      try { localStorage.setItem(SURVEY_SYNC_KEY, raw); } catch (e) {}
      return "问卷 ✓";
    });
  }
  function forceFlushQueue() {
    if (!reportQueue.length) return Promise.resolve(0);
    var batch = reportQueue.splice(0, 100);
    return fetch("/api/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uid: reportUid(), events: batch }),
    }).then(function (r) {
      if (!r.ok) throw new Error("events failed");
      saveReportQueue();
      var n = batch.length;
      if (reportQueue.length) {
        return forceFlushQueue().then(function (m) { return n + m; });
      }
      return n;
    }).catch(function (e) {
      reportQueue = batch.concat(reportQueue);
      saveReportQueue();
      throw e;
    });
  }
  function syncLocalData(manual) {
    fetch("/api/health", { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("no backend");
      return syncSurvey().then(function (s) {
        return forceFlushQueue().then(function (n) {
          return s + (n > 0 ? " · 行为 " + n + " 条 ✓" : "");
        });
      });
    }).then(function (msg) {
      if (manual) notify("已同步到本地库：" + msg + "。跑一次 python src/main.py 即生成新精选");
    }).catch(function () {
      if (manual) notify("未检测到本地服务，请先运行 python src/main.py --serve");
    });
  }
  function startReportLoop() {
    loadReportQueue();
    setTimeout(flushReport, 5000);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") flushReport();
    });
    window.addEventListener("pagehide", function () {
      if (!reportQueue.length) return;
      try {
        var b = new Blob([JSON.stringify({ uid: reportUid(), events: reportQueue })],
          { type: "application/json" });
        navigator.sendBeacon && navigator.sendBeacon("/api/events", b);
      } catch (e) {}
    });
  }

  // ===== 工具函数 =====
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function formatCount(n) {
    n = Number(n) || 0;
    if (n >= 1000) {
      var v = (n / 1000).toFixed(1);
      if (v.indexOf(".0", v.length - 2) !== -1) v = v.slice(0, -2);
      return v + "k";
    }
    return String(n);
  }
  function starAbbr(n) {
    n = Number(n) || 0;
    if (n >= 100000) return (n / 10000).toFixed(0) + "万";
    if (n >= 1000) return formatCount(n);
    return String(n);
  }
  function trendStar(n) {
    return Number(n || 0).toLocaleString("en-US");
  }
  // 星星图标（实心/空心 SVG，替代 emoji）
  function starSvg(outline, size) {
    size = size || 11;
    var p = "M12 2l2.9 6.6 7.1.6-5.4 4.7 1.6 7-6.2-3.7-6.2 3.7 1.6-7L2 9.2l7.1-.6z";
    return outline
      ? '<svg class="star-ico" width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="' + p + '"/></svg>'
      : '<svg class="star-ico" width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="' + p + '"/></svg>';
  }
  function starNum(n) {
    return trendStar(n) + " " + starSvg(false, 10);
  }
  function weekRange() {
    var now = new Date();
    var day = now.getDay() || 7;
    var mon = new Date(now);
    mon.setDate(now.getDate() - day + 1);
    var sun = new Date(mon);
    sun.setDate(mon.getDate() + 6);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return p(mon.getMonth() + 1) + "." + p(mon.getDate()) + " — " + p(sun.getMonth() + 1) + "." + p(sun.getDate());
  }
  function notify(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(window.__tt);
    window.__tt = setTimeout(function () { toastEl.classList.remove("show"); }, 1900);
  }
  function loadFavs() {
    try { favs = JSON.parse(localStorage.getItem(FAV_KEY)) || {}; }
    catch (e) { favs = {}; }
  }
  function saveFavs() {
    try { localStorage.setItem(FAV_KEY, JSON.stringify(favs)); }
    catch (e) { /* 隐私模式 */ }
  }
  // 收藏项兼容：数字时间戳 / {t: 时间戳, note: 备注}
  function favTimestamp(name) {
    var v = favs[name];
    if (!v) return 0;
    if (typeof v === "number") return v;
    return Number(v.t) || 0;
  }
  function favNote(name) {
    var v = favs[name];
    if (v && typeof v === "object" && v.note) return v.note;
    return "";
  }
  // ===== 个性化（M2）：兴趣匹配 + 行为加权 → 混合排序（仅 potential tab） =====
  // 规则模式：纯前端、零 LLM、每个访问者结果不同
  function interestMatch(item) {
    var labels = surveySelected;
    if (!labels || !labels.length) return 0;
    var r = repoOf(item);
    var topics = r.topics || [];
    var desc = String(r.description || "").toLowerCase();
    var name = fullName(item).toLowerCase();
    var hits = 0;
    for (var i = 0; i < labels.length; i++) {
      var kws = SURVEY_KEYWORDS[labels[i]];
      if (!kws) continue;
      var hit = false;
      for (var k = 0; k < kws.length && !hit; k++) {
        var kw = kws[k];
        for (var t = 0; t < topics.length; t++) {
          if (String(topics[t]).toLowerCase() === kw) { hit = true; break; }
        }
        if (!hit && desc.indexOf(kw) !== -1) hit = true;
        if (!hit && name.indexOf(kw) !== -1) hit = true;
      }
      if (hit) hits++;
    }
    return Math.round(hits / labels.length * 100);
  }
  function behaviorScore(item) {
    var name = fullName(item);
    var s = 0;
    if (favs[name]) s += 12;
    if (dismissed[name]) s -= 10;
    var byRepo = 0;
    var byTopic = {};
    historyLog.forEach(function (e) {
      if (e.repo === name) {
        if (e.action === "star") byRepo += 5;
        else if (e.action === "like") byRepo += 3;
        else if (e.action === "dismiss") byRepo -= 6;
        else if (e.action === "click_deep") byRepo += 2;
        else if (e.action === "click") byRepo += 1;
      }
      (e.topics || []).forEach(function (t) {
        byTopic[t] = (byTopic[t] || 0) + (e.action === "dismiss" ? -1 : 1);
      });
    });
    (repoOf(item).topics || []).forEach(function (t) {
      if (byTopic[t]) s += Math.max(-4, Math.min(4, byTopic[t] / 2));
    });
    s += Math.max(-8, Math.min(8, byRepo));
    return Math.max(0, Math.min(100, 50 + s * 4));
  }
  function personalScore(item) {
    var base = Number(scoreOf(item).score) || 0;
    var blended = interestMatch(item) * 0.7 + behaviorScore(item) * 0.3;
    return base * 0.7 + blended * 0.3;
  }

  function sortedItems(items) {
    var arr = items.slice();
    arr.sort(function (a, b) {
      var x, y;
      if (sortBy === "stars") { x = repoOf(a).stars; y = repoOf(b).stars; }
      else if (sortBy === "gain") { x = weekGain(a); y = weekGain(b); }
      else if (tab === "potential" && IS_PERSONAL) { x = personalScore(a); y = personalScore(b); }
      else { x = Number(scoreOf(a).score) || 0; y = Number(scoreOf(b).score) || 0; }
      return (y - x) || (fullName(a) < fullName(b) ? -1 : 1);
    });
    return arr;
  }

  // ===== 数据加载（并行 fetch，失败降级 SAMPLE_DATA） =====
  function fetchJson(url) {
    return fetch(url, { cache: "no-cache" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      });
  }

  function loadAll(cb) {
    var pending = 3;
    TABS.forEach(function (key) {
      var url = IS_PERSONAL
        ? "/api/personal/" + (key === "trends" ? "trends" : "scores")
        : DATA_FILES[key];
      fetchJson(url)
        .then(function (d) {
          if (key === "trends") {
            data[key] = (d && d.weeks) ? d : { weeks: [] };
          } else if (IS_PERSONAL) {
            data[key] = (d && Array.isArray(d.items)) ? d.items : [];
          } else {
            data[key] = d;
          }
        })
        .catch(function () {
          if (IS_PERSONAL) {
            PERSONAL_MISSING[key] = true;
            data[key] = key === "trends" ? { weeks: [] } : [];
          } else {
            console.warn("[StarRadar] " + key + ".json 加载失败，降级到示例数据");
            data[key] = (window.SAMPLE_DATA && window.SAMPLE_DATA[key]) || [];
          }
        })
        .then(function () {
          pending--;
          if (pending === 0) cb();
        });
    });
  }

  // ===== 数据派生 =====
  function repoOf(item) { return item.repo || {}; }
  function scoreOf(item) { return item.score || {}; }
  function fullName(item) {
    var r = repoOf(item);
    return r.full_name || (r.owner + "/" + r.name) || r.name || "未知项目";
  }
  function langLetter(item) {
    var lang = repoOf(item).language || "?";
    return escapeHtml(lang.charAt(0).toUpperCase());
  }
  function descText(item) {
    var r = repoOf(item);
    var s = scoreOf(item);
    if (tab === "picks" && s.reason) {
      return "🎯 " + s.reason;
    }
    if (tab === "potential") {
      return s.explanation && s.explanation.indexOf("处于") !== 0
        ? s.explanation
        : (r.description || s.explanation || "");
    }
    return r.description || s.explanation || "";
  }
  function isExplain(item) {
    var s = scoreOf(item);
    if (tab === "picks" && s.reason) return true;
    return !!(s.explanation && s.explanation.indexOf("处于") !== 0 && repoOf(item).description);
  }
  function weekGain(item) {
    var r = repoOf(item);
    if (r.stars_7d_ago != null && r.stars != null) return r.stars - r.stars_7d_ago;
    var vel = scoreOf(item).breakdown ? scoreOf(item).breakdown.vel : 0;
    return Math.round(vel * 7);
  }
  function trendText(item) {
    var gain = weekGain(item);
    if (gain > 0) return "↑ +" + formatCount(gain) + " / 周";
    if (gain < 0) return "↓ " + formatCount(Math.abs(gain)) + " / 周";
    return "— 持平";
  }
  function trendClass(item) {
    var gain = weekGain(item);
    return gain > 0 ? "" : (gain < 0 ? "down" : "flat");
  }
  function stageLabel(item) {
    var s = scoreOf(item);
    return STAGE_LABELS[s.stage] || s.stage || "";
  }
  function stageClass(item) {
    var s = scoreOf(item);
    return STAGE_CLASS[s.stage] || "mid_early";
  }
  function dimBars(item) {
    var s = scoreOf(item);
    if (!s.breakdown) return "";
    var html = "";
    for (var i = 0; i < DIM_KEYS.length; i++) {
      var k = DIM_KEYS[i];
      var v = Math.max(0, Math.min(100, Number(s.breakdown[k]) || 0));
      html += '<span class="dim">' +
                '<span class="dim-name">' + DIM_NAMES[k] + "</span>" +
                '<i><b style="width:' + v + '%"></b></i>' +
                '<span class="dim-val">' + v + "</span>" +
              "</span>";
    }
    return '<div class="dims" title="5 维评分：速度 · 加速度 · 健康 · 新鲜 · 信号">' + html + "</div>";
  }
  function sparkPath(item) {
    var r = repoOf(item);
    var series = r.star_series || [];
    if (series.length >= 3) {
      // 真实历史序列 → 平滑曲线（viewBox 110x34，底部留 padding）
      var vals = series.map(function (p) { return Number(p.s) || 0; });
      var min = Math.min.apply(null, vals);
      var max = Math.max.apply(null, vals);
      var span = (max - min) || 1;
      var W = 110, H = 34;
      var pts = [];
      for (var i = 0; i < vals.length; i++) {
        var x = 1 + (W - 2) * (i / (vals.length - 1));
        var y = H - 5 - (H - 10) * ((vals[i] - min) / span);
        pts.push(x.toFixed(1) + "," + y.toFixed(1));
      }
      var d = "M" + pts[0];
      for (var j = 1; j < pts.length; j++) d += " L" + pts[j];
      return '<path d="' + d + '" stroke="#1677ff" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>';
    }
    // 兜底：仅 2 点（7d ago → now）
    var now = Number(r.stars) || 0;
    var ago = r.stars_7d_ago != null ? Number(r.stars_7d_ago) : now;
    if (now <= 0) return '<path d="M1 29C18 29 17 25 28 25s8-13 21-13c10 0 10 8 18 8 12 0 10-16 22-16 7 0 7 7 20 7" stroke="#1677ff" stroke-width="2" stroke-linecap="round"/>';
    var yEnd = now >= ago ? 4 : 30;
    var yStart = now >= ago ? 30 : 4;
    return '<path d="M1 ' + yStart + 'L109 ' + yEnd + '" stroke="#1677ff" stroke-width="2" stroke-linecap="round"/>';
  }

  // ===== 五边形雷达图（5 维：vel/acc/health/fresh/signal） =====
  var RADAR_DIMS = [
    { key: "vel",    label: "速度" },
    { key: "acc",    label: "加速度" },
    { key: "health", label: "健康" },
    { key: "fresh",  label: "新鲜" },
    { key: "signal", label: "信号" },
  ];
  function radarPoint(cx, cy, r, angleDeg) {
    var rad = (angleDeg - 90) * Math.PI / 180;
    return {
      x: (cx + r * Math.cos(rad)).toFixed(2),
      y: (cy + r * Math.sin(rad)).toFixed(2),
    };
  }
  function renderRadar(breakdown, size) {
    size = size || 118;
    var cx = size / 2, cy = size / 2;
    var R = size * 0.34;
    var angles = [];
    for (var i = 0; i < RADAR_DIMS.length; i++) angles.push(i * 72);

    var grid = "";
    var scales = [0.33, 0.66, 1.0];
    for (var s = 0; s < scales.length; s++) {
      var pts = [];
      for (var a = 0; a < angles.length; a++) {
        var p = radarPoint(cx, cy, R * scales[s], angles[a]);
        pts.push(p.x + "," + p.y);
      }
      grid += '<polygon class="rd-grid" points="' + pts.join(" ") + '"/>';
    }

    var axes = "";
    for (var a2 = 0; a2 < angles.length; a2++) {
      var pa = radarPoint(cx, cy, R, angles[a2]);
      axes += '<line class="rd-axis" x1="' + cx + '" y1="' + cy + '" x2="' + pa.x + '" y2="' + pa.y + '"/>';
    }

    var dataPts = [];
    var dots = "";
    for (var d = 0; d < RADAR_DIMS.length; d++) {
      var key = RADAR_DIMS[d].key;
      var v = Math.max(0, Math.min(100, Number(breakdown[key]) || 0));
      dataPts.push(radarPoint(cx, cy, R * (v / 100), angles[d]));
      dots += '<circle class="rd-dot" cx="' + dataPts[d].x + '" cy="' + dataPts[d].y + '" r="2.4"><title>' +
              RADAR_DIMS[d].label + "：" + Math.round(v) + "</title></circle>";
    }
    var poly = dataPts.map(function (p) { return p.x + "," + p.y; }).join(" ");

    var labels = "";
    for (var l = 0; l < RADAR_DIMS.length; l++) {
      var pl = radarPoint(cx, cy, R + size * 0.11, angles[l]);
      labels += '<text class="rd-label" x="' + pl.x + '" y="' + pl.y + '" text-anchor="middle" dominant-baseline="middle">' +
                RADAR_DIMS[l].label + "</text>";
    }

    return (
      '<svg class="rd-radar" viewBox="-10 -6 ' + (size + 22) + ' ' + (size + 14) + '" aria-hidden="true">' +
      grid + axes +
      '<polygon class="rd-data" points="' + poly + '"/>' +
      dots + labels +
      "</svg>"
    );
  }

  // ===== 语义搜索（本地检索：全部数据 + 关键词加权 + 筛选） =====
  var searchPanel = document.querySelector("#searchPanel");
  var searchInput = document.querySelector("#searchInput");
  var searchResultsEl = document.querySelector("#searchResults");
  var searchCache = null;

  function searchIndex() {
    if (searchCache) return searchCache;
    var seen = {};
    var items = [];
    TABS.forEach(function (k) {
      if (!Array.isArray(data[k])) return;
      (data[k] || []).forEach(function (it) {
        var key = fullName(it);
        if (seen[key]) return;
        seen[key] = 1;
        items.push(it);
      });
    });
    searchCache = items;
    return items;
  }

  function searchTokens(q) {
    return q.toLowerCase().split(/[\s,，、]+/).filter(function (t) { return t.length >= 1; });
  }

  // ===== 搜索增强：同义词扩展 / 关键词高亮 / 搜索历史 =====
  var HIST_KEY = "starradar:search_hist";
  function loadHist() { try { return JSON.parse(localStorage.getItem(HIST_KEY)) || []; } catch (e) { return []; } }
  function saveHist(list) { try { localStorage.setItem(HIST_KEY, JSON.stringify(list.slice(0, 8))); } catch (e) {} }
  function addHist(q) {
    var t = q.trim().toLowerCase();
    if (!t) return;
    var list = loadHist().filter(function (x) { return x !== t; });
    list.unshift(t);
    saveHist(list);
  }
  // 同义词扩展：搜索词命中 40 标签关键词时，扩展出该标签与全部关联关键词
  // （搜 "ai agent" 也能命中带 "Agent" 标签的项目）
  function expandTokens(tokens) {
    var out = tokens.slice();
    tokens.forEach(function (t) {
      Object.keys(SURVEY_KEYWORDS).forEach(function (label) {
        var kws = SURVEY_KEYWORDS[label] || [];
        var hit = kws.some(function (k) { return k.length >= 2 && t.indexOf(k) !== -1; });
        if (!hit && t.length >= 2) hit = label.toLowerCase().indexOf(t) !== -1;
        if (!hit) return;
        out.push(label.toLowerCase());
        kws.forEach(function (k) { if (k.length >= 2) out.push(k); });
      });
    });
    return out.filter(function (v, i) { return out.indexOf(v) === i; });
  }
  function highlight(text, tokens) {
    var esc = escapeHtml(text || "");
    tokens.forEach(function (t) {
      if (t.length < 2) return;
      try {
        var re = new RegExp("(" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig");
        esc = esc.replace(re, "<mark>$1</mark>");
      } catch (e) {}
    });
    return esc;
  }
  function renderHist() {
    var box = document.querySelector("#searchHist");
    if (!box) return;
    var list = loadHist();
    if (!list.length || searchInput.value.trim()) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = '<span class="search-hist-label">最近搜索</span>' + list.map(function (q) {
      return '<button class="search-hist-item" data-q="' + escapeHtml(q) + '">' + escapeHtml(q) + "</button>";
    }).join("");
    Array.prototype.forEach.call(box.querySelectorAll(".search-hist-item"), function (b) {
      b.addEventListener("click", function () {
        searchInput.value = b.dataset.q;
        var cl = document.querySelector("#searchClear");
        if (cl) cl.hidden = false;
        box.hidden = true;
        renderSearch();
      });
    });
  }

  function searchScore(item, tokens) {
    var r = repoOf(item);
    var hay = {
      name: fullName(item).toLowerCase(),
      desc: (r.description || "").toLowerCase(),
      topics: (r.topics || []).join(" ").toLowerCase(),
      lang: (r.language || "").toLowerCase(),
      owner: (r.owner || "").toLowerCase(),
    };
    var score = 0;
    var hit = 0;
    tokens.forEach(function (t) {
      if (hay.name.indexOf(t) !== -1) { score += 5; hit++; }
      if (hay.topics.indexOf(t) !== -1) { score += 4; hit++; }
      if (hay.lang.indexOf(t) !== -1) { score += 3; hit++; }
      if (hay.owner.indexOf(t) !== -1) { score += 2; hit++; }
      if (hay.desc.indexOf(t) !== -1) { score += 1.5; hit++; }
    });
    return { score: score, hit: hit };
  }

  function fillTopicFilter() {
    var sel = document.querySelector("#fTopic");
    if (!sel) return;
    var hot = SURVEY_TOPICS[0].items.map(function (t) { return t.split(" ")[0]; });
    sel.innerHTML = '<option value="">全部方向</option>';
    hot.forEach(function (t) {
      var opt = document.createElement("option");
      opt.value = t;
      opt.textContent = t;
      sel.appendChild(opt);
    });
  }

  function openSearch() {
    searchPanel.classList.add("open");
    searchPanel.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    setTimeout(function () { searchInput.focus(); }, 60);
    fillTopicFilter();
    renderSearch();
    renderHist();
  }
  function closeSearch() {
    searchPanel.classList.remove("open");
    searchPanel.setAttribute("aria-hidden", "true");
    if (!detailEl.classList.contains("open")) document.body.style.overflow = "";
  }

  function renderSearch() {
    var q = searchInput.value;
    var topic = document.querySelector("#fTopic").value;
    var lang = document.querySelector("#fLang").value;
    var stage = document.querySelector("#fStage").value;
    var minScore = Number(document.querySelector("#fMin").value) || 0;
    var tokens = expandTokens(searchTokens(q));

    var items = searchIndex().filter(function (it) {
      var r = repoOf(it);
      var s = scoreOf(it);
      if (topic) {
        var hay = (r.topics || []).join(" ").toLowerCase() + " " + (r.description || "").toLowerCase() + " " + fullName(it).toLowerCase();
        var hit = false;
        topic.split(",").forEach(function (t) {
          if (hay.indexOf(t.trim().toLowerCase()) !== -1) hit = true;
        });
        if (!hit) return false;
      }
      if (lang && (r.language || "") !== lang) return false;
      if (stage && s.stage !== stage) return false;
      if (Number(s.score) < minScore) return false;
      return true;
    });

    var ranked = items.map(function (it) {
      var m = searchScore(it, tokens);
      return { it: it, score: m.score, hit: m.hit };
    });
    if (tokens.length) {
      ranked = ranked.filter(function (x) { return x.hit > 0; });
      ranked.sort(function (a, b) {
        return b.score - a.score || Number(scoreOf(b.it).score) - Number(scoreOf(a.it).score);
      });
    } else {
      // 空查询：按潜力分排序（精选视角）
      ranked.sort(function (a, b) { return Number(scoreOf(b.it).score) - Number(scoreOf(a.it).score); });
    }

    document.querySelector("#searchCount").textContent =
      ranked.length ? ranked.length + " 个结果" : "无结果";

    if (!ranked.length) {
      searchResultsEl.innerHTML =
        '<div class="search-empty"><svg width="34" height="34" viewBox="0 0 34 34" fill="none">' +
        '<circle cx="14" cy="14" r="9.5" stroke="currentColor" stroke-width="1.6" opacity="0.5"/>' +
        '<path d="M21.5 21.5 L29 29" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>' +
        "<p>没有匹配的项目，换个关键词试试</p></div>";
      return;
    }

    var html = "";
    ranked.forEach(function (x, i) {
      var it = x.it;
      var r = repoOf(it);
      var s = scoreOf(it);
      var sc = (Number(s.score) != null ? Number(s.score) : 0).toFixed(1);
      html +=
        '<div class="s-item" data-i="' + i + '">' +
          '<span class="repo-icon">' + langLetter(it) + "</span>" +
          '<div class="s-main">' +
            "<strong>" + highlight(fullName(it), tokens) + "</strong>" +
            '<small>' + highlight(r.description || (s.explanation || ""), tokens) + "</small>" +
          "</div>" +
          '<span class="s-meta">' +
            "<i>" + escapeHtml(r.language || "?") + "</i>" +
            '<b>' + sc + "</b>" +
          "</span>" +
        "</div>";
    });
    searchResultsEl.innerHTML = html;

    // 绑定点击 → 打开全屏详情 + 记录搜索历史
    Array.prototype.forEach.call(searchResultsEl.querySelectorAll(".s-item"), function (el) {
      el.addEventListener("click", function () {
        addHist(searchInput.value);
        var item = ranked[Number(el.dataset.i)].it;
        closeSearch();
        openDetail(item);
      });
    });
  }

  // ===== 潜力雷达全屏详情页 =====
  var detailEl = document.querySelector("#detail");
  var detailItem = null;
  var sparkRange = 30;

  function sliceSeries(item, days) {
    var r = repoOf(item);
    var series = (r.star_series || []).slice();
    if (days >= 30) return series;
    return series.slice(-days);
  }

  function dimRows(item) {
    var s = scoreOf(item);
    if (!s.breakdown) return "";
    var html = "";
    for (var i = 0; i < DIM_KEYS.length; i++) {
      var k = DIM_KEYS[i];
      var v = Math.max(0, Math.min(100, Number(s.breakdown[k]) || 0));
      html += '<span class="dim">' +
                '<span class="dim-name">' + DIM_NAMES[k] + "</span>" +
                '<i><b style="width:' + v + '%"></b></i>' +
                '<span class="dim-val">' + v + "</span>" +
              "</span>";
    }
    return html;
  }

  function detailSparkPath(item, W, H, days) {
    var r = repoOf(item);
    var series = sliceSeries(item, days);
    if (series.length >= 3) {
      var vals = series.map(function (p) { return Number(p.s) || 0; });
      var min = Math.min.apply(null, vals);
      var max = Math.max.apply(null, vals);
      var span = (max - min) || 1;
      var pad = 14;
      var pts = [];
      for (var i = 0; i < vals.length; i++) {
        var x = pad + (W - pad * 2) * (i / (vals.length - 1));
        var y = H - pad - (H - pad * 2) * ((vals[i] - min) / span);
        pts.push(x.toFixed(1) + "," + y.toFixed(1));
      }
      var d = "M" + pts[0];
      for (var j = 1; j < pts.length; j++) d += " L" + pts[j];
      var area = d + " L" + (W - pad).toFixed(1) + " " + (H - pad) + " L" + pad + " " + (H - pad) + " Z";
      var minV = Math.round(min), maxV = Math.round(max);
      return {
        line: '<path d="' + d + '" stroke="#1677ff" stroke-width="2.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
        area: '<path d="' + area + '" fill="url(#dsparkGrad)" opacity="0.16"/>',
        range: minV + " → " + maxV,
        mid: Math.round((minV + maxV) / 2),
      };
    }
    var now = Number(r.stars) || 0;
    var ago = r.stars_7d_ago != null ? Number(r.stars_7d_ago) : now;
    var yEnd = now >= ago ? 20 : H - 20;
    var yStart = now >= ago ? H - 20 : 20;
    return {
      line: '<path d="M1 ' + yStart + " L" + (W - 1) + " " + yEnd + '" stroke="#1677ff" stroke-width="2.6" fill="none" stroke-linecap="round"/>',
      area: "",
      range: ago + " → " + now,
      mid: Math.round((ago + now) / 2),
    };
  }

  function openDetail(item) {
    detailItem = item;
    detailOpenAt = Date.now();
    logAction(item, "click");
    var r = repoOf(item);
    var s = scoreOf(item);
    var name = fullName(item);
    var stage = stageLabel(item);
    var saved = !!favs[name];

    document.querySelector("#dIcon").textContent = langLetter(item);
    document.querySelector("#dName").textContent = name;
    document.querySelector("#dStage").textContent = stage || "—";
    document.querySelector("#dOwner").innerHTML =
      escapeHtml(r.owner + " / " + r.name + " · " + (r.language || "未知语言")) +
      " · " + starSvg(false, 10) + " " + escapeHtml(formatCount(r.stars)) +
      " · 周增 " + escapeHtml(trendText(item));
    var dTags = document.querySelector("#dTags");
    if (dTags) {
      var dCat = categoryTag(item);
      var dChips = [];
      if (surveySelected.indexOf(dCat) !== -1) dChips.push("<b>" + escapeHtml(dCat) + "</b>");
      (r.topics || []).slice(0, 4).forEach(function (t) {
        if (t === dCat) return;
        dChips.push("<i>" + escapeHtml(t) + "</i>");
      });
      if (!dChips.length && r.language) dChips.push("<i>" + escapeHtml(r.language) + "</i>");
      dTags.innerHTML = dChips.length ? dChips.join("") : "";
    }
    document.querySelector("#dScore").textContent =
      s && s.score != null && !isNaN(Number(s.score)) ? Number(s.score).toFixed(1) : "—";
    document.querySelector("#dTrend").textContent = trendText(item).replace(" / 周", "");
    document.querySelector("#dDesc").textContent = r.description || "暂无描述";
    document.querySelector("#dExplain").textContent = s.reason && tab === "picks"
      ? "为你精选：" + s.reason
      : (s.explanation || "该仓库暂未生成 AI 解读，可前往 GitHub 查看详情。");
    document.querySelector("#dDims").innerHTML = dimRows(item);
    document.querySelector("#dRadar").innerHTML = s.breakdown
      ? renderRadar(s.breakdown, 340)
      : '<div class="d-no-score">暂无五维评分<br><small>该项目来自趋势榜快照，未进入潜力评分池</small></div>';
    document.querySelector("#dLink").href = r.html_url || "https://github.com/" + (r.owner + "/" + r.name);
    var dOp = document.querySelector("#dOpbar");
    if (dOp) dOp.innerHTML = typeof GH !== "undefined" ? GH.actionsHTML(item) : "";

    var saveBtn = document.querySelector("#dSave");
    saveBtn.innerHTML = saved ? "✓ 已在你的雷达" : starSvg(true, 11) + " 加入我的雷达";
    saveBtn.classList.toggle("saved", saved);

    var spark = document.querySelector("#dSpark");
    var sp = detailSparkPath(item, 720, 120, sparkRange);
    var grad = '<defs><linearGradient id="dsparkGrad" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="#1677ff" stop-opacity="1"/>' +
      '<stop offset="1" stop-color="#1677ff" stop-opacity="0"/>' +
      "</linearGradient></defs>";
    spark.innerHTML = grad + sp.area + sp.line;
    document.querySelector("#dSparkVal").textContent = sp.range;

    detailEl.classList.add("open");
    detailEl.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
  }

  function closeDetail() {
    detailEl.classList.remove("open");
    detailEl.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    if (detailItem && detailOpenAt) {
      var dur = Math.round((Date.now() - detailOpenAt) / 1000);
      if (dur > 30) logAction(detailItem, "click_deep", { duration_s: dur });
      else if (dur < 10 && dur >= 3) logAction(detailItem, "click_short", { duration_s: dur });
    }
    detailItem = null;
    detailOpenAt = 0;
  }

  // ===== 卡片渲染 =====
  function renderCard(item, index) {
    var r = repoOf(item);
    var s = scoreOf(item);
    var name = fullName(item);
    var lang = r.language || "";
    var cat = categoryTag(item);
    var h3 = escapeHtml(cat) + " · " + escapeHtml(TAB_LABEL[tab]);
    var stage = stageLabel(item);
    var chip = stage ? '<span class="stage-chip">' + escapeHtml(stage) + "</span>" : "";
    var desc = descText(item);
    var explain = isExplain(item) && tab === "potential" ? " explain" : "";
    var score = (Number(s.score) != null ? Number(s.score) : 0).toFixed(1);
    var favKey = name;
    var saved = favs[favKey] ? " saved" : "";
    var savedText = favs[favKey] ? "✓" : starSvg(true, 11);
    var radar = tab === "potential" && s.breakdown
      ? '<div class="radar-fig">' + renderRadar(s.breakdown, 118) + "</div>"
      : "";
    var catInSurvey = surveySelected.indexOf(cat) !== -1;
    var rel = tab === "potential" && interestMatch(item) >= 50
      ? ' <em class="rel-chip">与你相关</em>' : "";
    var myReason = tab === "potential" ? reasonFor(name) : null;
    var reasonHtml = myReason ? '<p class="my-reason">🎯 ' + escapeHtml(myReason) + "</p>" : "";
    var topicsList = r.topics || [];
    var tagsHtml = "";
    if (topicsList.length || catInSurvey || lang) {
      var chips = [];
      if (catInSurvey) chips.push("<b>" + escapeHtml(cat) + "</b>");
      for (var t = 0; t < topicsList.length && chips.length < 4; t++) {
        if (topicsList[t] === cat) continue;
        chips.push("<i>" + escapeHtml(topicsList[t]) + "</i>");
      }
      if (!chips.length && lang) chips.push("<i>" + escapeHtml(lang) + "</i>");
      tagsHtml = '<div class="card-tags">' + chips.join("") + "</div>";
    }

    return (
      '<article class="card' + (radar ? " has-radar" : "") + '" data-full="' + escapeHtml(name) + '">' +
        '<span class="rank">' + ("0" + (index + 1)).slice(-2) + "</span>" +
        '<div class="repo"><span class="repo-icon">' + langLetter(item) + "</span>" +
          "<div><strong>" + escapeHtml(name) + "</strong>" + rel +
          "<small>" + escapeHtml(r.owner || "") + " / " + escapeHtml(r.name || "") +
            (lang ? " · " + escapeHtml(lang) : "") + "</small></div></div>" +
        (radar ? '<div class="radar-fig" title="5 维评分：速度 · 加速度 · 健康 · 新鲜 · 信号">' + renderRadar(s.breakdown, 96) + "</div>" : "") +
        '<div class="card-main">' +
          '<div class="card-info">' +
            "<h3>" + h3 + chip + "</h3>" +
            tagsHtml +
            (desc ? '<p class="score-desc' + explain + '">' + escapeHtml(desc) + "</p>" : '<p class="score-desc"></p>') +
            reasonHtml +
            '<div class="bottom"><div>' +
              '<div class="score">' + score + '<small> / 100</small></div>' +
              '<div class="trend ' + trendClass(item) + '">' + trendText(item) + "</div>" +
            "</div>" +
            (tab === "potential" ? dimBars(item) : "") +
            "</div>" +
          "</div>" +
        "</div>" +
        '<button class="save' + saved + '" title="收藏到我的雷达">' + savedText + "</button>" +
        '<button class="fb like" title="推荐更多类似">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<path d="M7 11v9H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h3z"/>' +
          '<path d="M7 11l4.5-8a2 2 0 0 1 2 2.2L12.4 9H20a2 2 0 0 1 2 2.4l-1.7 7A2 2 0 0 1 18.3 20H7"/>' +
          "</svg></button>" +
        '<button class="fb dismiss' + (dismissed[favKey] ? " off" : "") + '" title="不感兴趣">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">' +
          '<circle cx="12" cy="12" r="8.5"/>' +
          '<path d="M6.2 6.2l11.6 11.6"/>' +
          "</svg></button>" +
        '<svg class="spark" viewBox="0 0 110 34" fill="none">' + sparkPath(item) + "</svg>" +
        (typeof GH !== "undefined" ? GH.actionsHTML(item) : "") +
      "</article>"
    );
  }

  function skeletonCards() {
    var html = "";
    for (var i = 0; i < 6; i++) {
      html +=
        '<div class="sk-card"><span class="sk sk-circle"></span>' +
        '<span class="sk sk-line w55"></span><span class="sk sk-line w80"></span>' +
        '<span class="sk sk-line w40"></span></div>';
    }
    return html;
  }

  function itemsForTab() {
    if (tab === "picks") {
      var favNames = Object.keys(favs);
      if (favNames.length) {
        var byName = {};
        searchIndex().forEach(function (it) { byName[fullName(it)] = it; });
        var collected = [];
        // 按收藏时间倒序（最近收藏在前）
        favNames.sort(function (a, b) { return favTimestamp(b) - favTimestamp(a); });
        favNames.forEach(function (nm) { if (byName[nm]) collected.push(byName[nm]); });
        if (collected.length) return collected;
      }
    }
    return data[tab] || [];
  }

  // ===== M5 有 key 个性化增强 =====
  var REASONS_KEY = "starradar:reasons";
  var INSIGHT_KEY = "starradar:report_insight";
  var reasonCache = {};
  function loadReasons() {
    try { reasonCache = JSON.parse(localStorage.getItem(REASONS_KEY)) || {}; } catch (e) { reasonCache = {}; }
  }
  function reasonFor(name) {
    var r = reasonCache[name];
    if (!r || Date.now() - r.ts > 86400000) return null;
    return r.text;
  }
  // 潜力雷达前排 5 个：LLM 生成"为什么推荐给你"（异步填充，缓存 24h）
  function fetchReasons(items) {
    if (typeof window.LLM === "undefined" || !window.LLM.isConfigured()) return;
    if (tab !== "potential") return;
    var img = profileImage();
    var need = [];
    items.slice(0, 5).forEach(function (it) {
      if (!reasonFor(fullName(it))) need.push(it);
    });
    if (!need.length || !window.LLM.canCall("reason")) return;
    var list = need.map(function (it) {
      return "- " + fullName(it) + ": " + String(repoOf(it).description || "").slice(0, 120);
    }).join("\n");
    window.LLM.chat([
      { role: "system", content: "你是 GitHub 项目推荐助手。根据用户兴趣画像为项目写推荐理由（≤40字、口语化、从用户角度）。输出 JSON。画像：" +
        (img && img.tags ? JSON.stringify(img.tags) : "无") },
      { role: "user", content: "项目列表：\n" + list + "\n\n输出 JSON：{\"reasons\":[{\"repo\":\"full_name\",\"reason\":\"...\"}]}" },
    ], { feature: "reason", temperature: 0.6, max_tokens: 400 })
      .then(function (txt) {
        var j = window.LLM.parseJSON(txt);
        (j.reasons || []).forEach(function (r) {
          if (r && r.repo) reasonCache[r.repo] = { text: String(r.reason).slice(0, 80), ts: Date.now() };
        });
        saveReasons();
        if (tab === "potential") renderCards();
      })
      .catch(function () { /* 失败静默，无理由可显示 */ });
  }
  function saveReasons() {
    try { localStorage.setItem(REASONS_KEY, JSON.stringify(reasonCache)); } catch (e) {}
  }

  // 周报"为你解读"：有 key → LLM；无 key → 规则（收藏异动 + 兴趣相关榜单项）
  function renderPersonalInsight(rep) {
    if (!rep) return;
    var zone = cardsEl.querySelector(".t-report");
    if (!zone) return;
    var old = zone.querySelector(".t-insight");
    if (old) old.remove();
    var hasLLM = typeof window.LLM !== "undefined" && window.LLM.isConfigured();
    var img = profileImage();
    if (hasLLM) {
      var cached = null;
      try { cached = JSON.parse(localStorage.getItem(INSIGHT_KEY)) || null; } catch (e) {}
      if (cached && cached.week === rep.week) { zone.insertAdjacentHTML("beforeend", insightCard(cached.title, cached.points, true)); return; }
      if (!window.LLM.canCall("report")) return;
      var topBrief = (rep.hot_top || []).slice(0, 5).map(function (r) {
        return r.repo + " +" + r.delta + " [" + (r.topics || []).slice(0, 3).join(",") + "]";
      }).join("\n");
      var domBrief = (rep.hot_topics || []).slice(0, 8).map(function (d) {
        return d.label + " ×" + d.count + (d.delta > 0 ? " +" + d.delta : "");
      }).join(", ");
      window.LLM.chat([
        { role: "system", content: "你是 StarRadar 每周趋势的个性化解读助手。结合用户兴趣画像与本周总体数据，写「为你解读」。输出 JSON。画像：" +
          (img && img.summary ? img.summary : "无") },
        { role: "user", content: "本周热度 TOP：\n" + topBrief + "\n热门领域：" + domBrief +
          "\n\n输出 JSON：{\"title\":\"本周为你 · 一句话标题\",\"points\":[\"3 条与用户相关且有信息量的要点，每条≤50字\"]}" },
      ], { feature: "report", temperature: 0.6, max_tokens: 500 })
        .then(function (txt) {
          var j = window.LLM.parseJSON(txt);
          var card = insightCard(String(j.title || "本周为你").slice(0, 40),
            (Array.isArray(j.points) ? j.points : []).map(function (p) { return String(p).slice(0, 80); }).slice(0, 4), true);
          zone.insertAdjacentHTML("beforeend", card);
          try {
            localStorage.setItem(INSIGHT_KEY, JSON.stringify({ week: rep.week, title: String(j.title || "").slice(0, 40), points: (Array.isArray(j.points) ? j.points : []).slice(0, 4) }));
          } catch (e) {}
        })
        .catch(function () { /* 失败静默 */ });
      return;
    }
    // 规则版（无 key）：收藏异动 + 兴趣相关榜单项
    var points = [];
    var favNames = Object.keys(favs || {});
    if (favNames.length) {
      var movers = (rep.hot_top || []).filter(function (r) { return favNames.indexOf(r.repo) !== -1; });
      var follows = rep.my_follows || [];
      if (follows.length) points.push("你的收藏「" + follows[0].repo + "」本周增星 " + follows[0].delta + " 颗");
      movers.forEach(function (m) { points.push("你收藏的「" + m.repo + "」冲进热度榜，本周 +" + m.delta); });
      if (!movers.length && !follows.length) points.push("你的收藏本周没有上榜项目，继续观望");
    }
    var rel = (rep.hot_top || []).filter(function (r) {
      var fake = { repo: { topics: r.topics || [], description: r.description || "", language: r.language, full_name: r.repo } };
      return interestMatch(fake) >= 50;
    });
    rel.forEach(function (r) { points.push("「" + r.repo + "」与你的兴趣方向相关，本周 +" + r.delta + " 星"); });
    if (!points.length) points.push("本周暂无与你直接相关的动态，试试完成问卷让雷达更懂你");
    zone.insertAdjacentHTML("beforeend", insightCard("本周为你", points, false));
  }
  function insightCard(title, points, llm) {
    var bullets = points.map(function (p) { return "<li>" + escapeHtml(p) + "</li>"; }).join("");
    var ico = llm
      ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2a7 7 0 0 0-4 12.7c.8.6 1.3 1.5 1.3 2.6h5.4c0-1.1.5-2 1.3-2.6A7 7 0 0 0 12 2z"/><path d="M9.5 21h5"/></svg>'
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3.2"/><path d="M12 2v3.2M12 18.8V22M2 12h3.2M18.8 12H22M4.9 4.9l2.3 2.3M16.8 16.8l2.3 2.3M19.1 4.9l-2.3 2.3M7.2 16.8l-2.3 2.3"/></svg>';
    return '<div class="t-insight' + (llm ? " llm" : "") + '"><div class="t-insight-title">' +
      '<span class="ti-ico">' + ico + "</span><h4>" + escapeHtml(title) +
      "</h4>" + (llm ? '<small>AI 生成</small>' : '<small>规则匹配</small>') +
      "</div><ul>" + bullets + "</ul></div>";
  }

  function renderCards() {
    cardsEl.classList.remove("trends");
    var items = itemsForTab();
    if (!items.length) {
      if (IS_PERSONAL && tab !== "trends") {
        cardsEl.innerHTML =
          '<div class="load-error"><svg width="36" height="36" viewBox="0 0 36 36" fill="none">' +
          '<circle cx="18" cy="18" r="15" stroke="currentColor" stroke-width="1.4" opacity="0.45"/>' +
          '<path d="M18 10v8.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>' +
          '<circle cx="18" cy="24.5" r="1.3" fill="currentColor"/></svg>' +
          "<p>个人雷达还没有数据。</p>" +
          "<p style='font-size:12.5px;line-height:2'>① 点击右上角 GitHub 图标登录（或「使用本地 Token」）<br>" +
          "② 运行 <code>python src/main.py --personal</code> 生成你的专属雷达<br>" +
          "③ 之后每次打开本页都是为你定制的内容，已看项目自动排除</p>" +
          '<button class="retry-btn" type="button" id="pOpenLogin">登录 GitHub</button> ' +
          '<button class="retry-btn" type="button">重新加载</button></div>';
        var lg = cardsEl.querySelector("#pOpenLogin");
        if (lg) lg.addEventListener("click", function () {
          if (typeof window.GH !== "undefined") window.GH.openPanel();
        });
        var rt = cardsEl.querySelectorAll(".retry-btn");
        Array.prototype.forEach.call(rt, function (b) {
          if (b.id !== "pOpenLogin") b.addEventListener("click", function () { init(); });
        });
        return;
      }
      cardsEl.innerHTML =
        '<div class="load-error"><svg width="36" height="36" viewBox="0 0 36 36" fill="none">' +
        '<circle cx="18" cy="18" r="15" stroke="currentColor" stroke-width="1.4" opacity="0.45"/>' +
        '<path d="M18 10v8.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>' +
        '<circle cx="18" cy="24.5" r="1.3" fill="currentColor"/></svg>' +
        '<p>暂无数据</p><button class="retry-btn" type="button">重试</button></div>';
      var btn = cardsEl.querySelector(".retry-btn");
      if (btn) btn.addEventListener("click", function () { init(); });
      return;
    }
    var items = sortedItems(itemsForTab());
    var html = "";
    for (var i = 0; i < items.length; i++) html += renderCard(items[i], i);
    cardsEl.innerHTML = html;
    fetchReasons(items);
  }

  // ===== Hero 统计 =====
  function renderHero() {
    var sig = (data.potential || []).length;
    var deep = 0;
    (data.potential || []).forEach(function (it) {
      if (Number(scoreOf(it).score) >= 70) deep++;
    });
    var picks = (data.picks || []).length;
    document.querySelector("#sigCount").textContent = sig;
    document.querySelector("#mSignals").textContent = sig;
    document.querySelector("#mDeep").textContent = deep;
    document.querySelector("#mMatch").textContent = picks;
    document.querySelector("#sigDate").textContent = weekRange();
    document.querySelector("#radarUpd").textContent = "刚刚";
  }

  // ===== 初始化 =====
  function init() {
    cardsEl.innerHTML = skeletonCards();
    cardsEl.classList.remove("list");
    loadReasons();
    document.querySelector('[data-view="grid"]').classList.add("active");
    document.querySelector('[data-view="list"]').classList.remove("active");
    syncSectionHead();
    if (IS_PERSONAL) applyPersonalUI();
    loadAll(function () {
      renderHero();
      renderCards();
      fillLangFilter();
    });
  }

  // 个人模式 UI：hero 文案 / 顶部导航标识（其余界面与公版完全一致）
  function applyPersonalUI() {
    var eb = document.querySelector(".eyebrow");
    if (eb) eb.innerHTML = "<i></i> Personal signal · <span id='sigDate'>—</span>";
    var h1 = document.querySelector(".hero h1");
    if (h1) h1.innerHTML = "只为你发现<br><em>下一颗明星。</em>";
    var hp = document.querySelector(".hero p");
    if (hp) {
      hp.innerHTML = "基于你的加星、仓库与行为画像，专门搜索并解读——每个人看到的雷达都不一样。" +
        '<strong id="sigCount" style="display:none">0</strong>';
    }
    var brand = document.querySelector(".topbar .brand small");
    if (brand) brand.textContent = "个人特化雷达";
    var plink = document.querySelector("#pLink");
    if (plink) {
      plink.setAttribute("href", "index.html");
      plink.textContent = "返回公有版";
    }
  }

  function fillLangFilter() {
    var langs = {};
    TABS.forEach(function (k) {
      if (!Array.isArray(data[k])) return;
      (data[k] || []).forEach(function (it) {
        var l = repoOf(it).language;
        if (l) langs[l] = 1;
      });
    });
    var sel = document.querySelector("#fLang");
    var cur = sel.value;
    sel.innerHTML = '<option value="">全部语言</option>';
    Object.keys(langs).sort().forEach(function (l) {
      var opt = document.createElement("option");
      opt.value = l;
      opt.textContent = l;
      sel.appendChild(opt);
    });
    sel.value = cur;
  }

  // ===== 每周趋势（周报渲染：指标卡仪表盘） =====
  var trendWeek = null; // 当前展示的周 key（默认最新）

  function trendWeeks() {
    var ws = (data.trends && data.trends.weeks) || [];
    return ws.slice().sort(function (a, b) {
      return (a.week < b.week ? 1 : (a.week > b.week ? -1 : 0));
    });
  }

  function trendReport() {
    var ws = trendWeeks();
    if (!ws.length) return null;
    if (!trendWeek) return ws[0];
    for (var i = 0; i < ws.length; i++) if (ws[i].week === trendWeek) return ws[i];
    return ws[0];
  }

  // 周报榜单项 → 轻量详情卡片（热榜/回归/关注里的项目可能不在潜力池，也有解读/描述数据）
  function trendRowLight(full) {
    var rep = trendReport();
    if (!rep) return null;
    var r = null;
    var lists = ["hot_top", "comebacks", "my_follows", "new_stars"];
    for (var i = 0; i < lists.length && !r; i++) {
      (rep[lists[i]] || []).forEach(function (x) { if (x.repo === full) r = x; });
    }
    if (!r) return null;
    var parts = String(full).split("/");
    return {
      repo: {
        full_name: full,
        owner: parts[0] || "",
        name: parts[1] || full,
        language: r.language || "",
        stars: r.stars,
        stars_7d_ago: (Number(r.stars) || 0) - (Number(r.delta) || 0),
        topics: r.topics || [],
        description: r.description || "",
        html_url: "https://github.com/" + encodeURIComponent(full),
      },
      score: {
        score: 0,
        stage: "",
        explanation: r.explanation || "",
        reason: "",
        breakdown: null,
      },
    };
  }

  function trendLangLetter(lang) {
    return escapeHtml((lang || "?").charAt(0).toUpperCase());
  }

  function trendTopRow(r, i, maxDelta, champion, streak) {
    var pct = maxDelta > 0 ? Math.max(2, Math.round((r.delta || 0) / maxDelta * 100)) : 0;
    var cls = "t-top-row" + (champion ? " t-champion" : "");
    var explain = r.explanation || r.description || "";
    if (explain.length > 64) explain = explain.slice(0, 64) + "…";
    var sk = streak && streak > 1 ? '<span class="t-streak">连增 ' + streak + " 周</span>" : "";
    var stBadge = STATUS_BADGE[r.status] ? '<i class="t-badge ' + r.status + '">' + STATUS_BADGE[r.status] + "</i>" : "";
    var pills = (r.topics || []).slice(0, 3).map(function (t) {
      return "<i>" + escapeHtml(t) + "</i>";
    }).join("");
    var deltaHtml = r.delta == null
      ? '<span class="delta new">新进</span>'
      : '<span class="delta">+' + r.delta + "</span>";
    return '<div class="' + cls + '" data-repo="' + escapeHtml(r.repo) + '">' +
      '<span class="rank">' + (i + 1) + "</span>" +
      '<span class="lang-ico">' + trendLangLetter(r.language) + "</span>" +
      '<span class="info">' +
        '<span class="repo">' + escapeHtml(r.repo) + sk + stBadge + "</span>" +
        '<span class="meta"><span class="meta-tag">' + escapeHtml(r.language || "—") + " · " + starAbbr(r.stars || 0) + " 星</span>" +
          (pills ? '<span class="pills">' + pills + "</span>" : "") + "</span>" +
        (explain ? '<span class="desc">' + escapeHtml(explain) + "</span>" : "") +
      "</span>" +
      '<span class="gain">' + deltaHtml +
      '<span class="t-bar"><i style="width:' + pct + '%"></i></span></span>' +
      "</div>";
  }

  // M3：整体增长条（growth_meta）
  function growthStripHtml(g) {
    if (!g) return "";
    var cls = g.gain_vs_prev >= 0 ? " up" : " down";
    var sign = g.gain_vs_prev >= 0 ? "+" : "";
    return '<div class="t-strip">' +
      '<div class="ts-item"><b>+' + trendStar(g.hot_gain_total) + "</b><span>榜单总增星</span></div>" +
      '<div class="ts-item' + cls + '"><b>' + sign + trendStar(g.gain_vs_prev) + "</b><span>环比上周</span></div>" +
      '<div class="ts-item"><b>+' + trendStar(g.avg_gain) + "</b><span>平均 / 项目</span></div>" +
      '<div class="ts-item"><b>' + g.top_count + "</b><span>上榜项目</span></div>" +
      "</div>";
  }

  // M3：本周逐日热度走势（迷你柱状）
  function timelineHtml(tl) {
    if (!tl || tl.length < 1) return "";
    var max = 1;
    tl.forEach(function (d) { if (Number(d.count) > max) max = Number(d.count); });
    var bars = tl.map(function (d) {
      return '<div class="tl-day"><i style="height:' + Math.max(6, Math.round(d.count / max * 56)) + 'px"></i>' +
        "<span>" + escapeHtml(d.date) + "</span></div>";
    }).join("");
    return '<div class="t-card tl-card"><div class="t-card-title"><h3>本周热度走势</h3>' +
      '<span class="hint">每日快照项目数</span></div><div class="tl-bars">' + bars + "</div></div>";
  }

  // M3：回归榜
  // 主题叙事卡（LLM 归纳，失败时后端降级为话题聚合）
  function themesHtml(themes) {
    if (!themes || !themes.length) return "";
    var items = themes.map(function (t) {
      var repos = (t.repos || []).map(function (r) {
        return '<span class="th-repo" data-repo="' + escapeHtml(r) + '">' + escapeHtml(r) + "</span>";
      }).join("");
      return '<div class="t-theme">' +
        '<div class="th-head"><b>' + escapeHtml(t.title) + "</b>" +
        (t.total_delta ? '<span class="th-delta">+' + t.total_delta + "</span>" : "") + "</div>" +
        (t.summary ? "<p>" + escapeHtml(t.summary) + "</p>" : "") +
        (repos ? '<div class="th-repos">' + repos + "</div>" : "") +
        "</div>";
    }).join("");
    return '<div class="t-card themes-card"><div class="t-card-title"><h3>本周主线 · Themes</h3>' +
      '<span class="hint">LLM 归纳 · 点击项目直达</span></div>' + items + "</div>";
  }

  // 跨周追踪卡（上周上榜项目本周去向）
  function memoryTrackHtml(rep) {
    var mt = rep.memory_track || {};
    var milestones = mt.milestones || [];
    var badge = function (k, n, label) {
      return n > 0 ? '<span class="m-badge ' + k + '">' + label + " " + n + "</span>" : "";
    };
    var stats = badge("still", mt.still_up, "仍增") +
      badge("accel", mt.accelerated, "加速") +
      badge("slow", mt.slowed, "回落") +
      badge("drop", mt.dropped, "跌出");
    if (!stats && !milestones.length) return "";
    var ms = milestones.map(function (r) {
      return '<span class="m-milestone" data-repo="' + escapeHtml(r) + '">' + escapeHtml(r) + " 里程碑</span>";
    }).join("");
    return '<div class="t-card mem-card"><div class="t-card-title"><h3>跨周追踪 · Memory</h3>' +
      '<span class="hint">上周 TOP ' + (mt.prev_count || 0) + " 个项目去向</span></div>" +
      '<div class="m-stats">' + (stats || '<span class="m-badge none">数据不足</span>') + "</div>" +
      (ms ? '<div class="m-milestones">' + ms + "</div>" : "") + "</div>";
  }

  function comebacksHtml(rep) {
    var cb = rep.comebacks || [];
    if (!cb.length) return "";
    var rows = cb.map(function (c) {
      return '<div class="t-top-row" data-repo="' + escapeHtml(c.repo) + '">' +
        '<span class="rank"></span><span class="lang-ico">' + trendLangLetter(c.language) + "</span>" +
        '<span class="info"><span class="repo">' + escapeHtml(c.repo) + "</span>" +
        '<span class="meta"><span class="meta-tag">回归热度榜</span></span></span>' +
        '<span class="gain"><span class="delta">+' + c.delta + "</span></span></div>";
    }).join("");
    return '<div class="t-card"><div class="t-card-title"><h3>回归榜 · Comebacks</h3>' +
      '<span class="hint">上周缺席 · 本周归来</span></div>' + rows + "</div>";
  }

  // M3：热门领域 3 周走势条
  function domainBarsHtml(dt) {
    if (!dt || !dt.length) return "";
    var max = 1;
    dt.forEach(function (d) { (d.series || []).forEach(function (v) { if (Number(v) > max) max = Number(v); }); });
    var rows = dt.map(function (d) {
      var segs = (d.series || []).map(function (v) {
        return '<i style="width:' + Math.max(6, Math.round(Number(v) / max * 100)) + '%"></i>';
      }).join("");
      var arrow = d.trend === "up" ? "↗" : (d.trend === "down" ? "↘" : "→");
      var cls = d.trend === "up" ? " up" : (d.trend === "down" ? " down" : "");
      return '<div class="dt-row"><span class="dt-label">' + escapeHtml(d.label) + "</span>" +
        '<span class="dt-bar">' + segs + "</span>" +
        '<span class="dt-arrow' + cls + '">' + arrow + "</span></div>";
    }).join("");
    return '<div class="t-domains">' + rows + "</div>";
  }

  function renderTrends() {
    var rep = trendReport();
    cardsEl.classList.add("trends");
    if (!rep) {
      cardsEl.innerHTML =
        '<div class="load-error"><p>暂无周报数据（周一 08:00 自动生成，或先跑 <code>python src/main.py --weekly</code>）</p></div>';
      return;
    }
    var ws = trendWeeks();
    var opts = ws.map(function (w) {
      var act = w.week === rep.week ? " active" : "";
      return '<button type="button" class="t-opt' + act + '" data-w="' + escapeHtml(w.week) + '">' +
        escapeHtml(w.week) + "</button>";
    }).join("");
    var top = rep.hot_top || [];
    var ch = top[0];
    var maxDelta = 1;
    top.forEach(function (r) { if (Number(r.delta) > maxDelta) maxDelta = Number(r.delta); });

    // 指标卡
    var mChampion = ch
      ? '<div class="t-metric"><div class="label">本周冠军 · Champion</div>' +
        '<div class="num green">+' + formatCount(ch.delta) + "</div>" +
        '<div class="foot"><span class="star-icon">' + starSvg(true, 12) + "</span><span class=\"repo\">" + escapeHtml(ch.repo) + "</span>" +
        '<span class="star">' + starNum(ch.stars || 0) + "</span></div></div>"
      : '<div class="t-metric"><div class="label">本周冠军 · Champion</div>' +
        '<div class="num">—</div><div class="foot"><span>暂无增星数据</span></div></div>';
    var topTopic = (rep.hot_topics || [])[0];
    var mTopics = '<div class="t-metric"><div class="label">热门领域 · Topics</div>' +
      '<div class="num">' + (rep.hot_topics || []).length + '<span class="unit">个</span></div>' +
      '<div class="foot"><span>' + (topTopic ? escapeHtml(topTopic.label) + " 领跑" : "暂无话题") + "</span>" +
      '<span class="mini"><i style="width:' + (topTopic ? Math.min(100, Math.round(topTopic.count / 3 * 100)) : 0) + '%"></i></span></div></div>';
    var mFollows = (rep.my_follows || []).length
      ? '<div class="t-metric"><div class="label">关注动态 · Follows</div>' +
        '<div class="num">' + rep.my_follows.length + '<span class="unit">条</span></div>' +
        '<div class="foot"><span>' + escapeHtml(rep.my_follows[0].repo) + " 有增星</span></div></div>"
      : '<div class="t-metric"><div class="label">关注动态 · Follows</div>' +
        '<div class="num">0<span class="unit">条</span></div>' +
        '<div class="foot"><span>暂无新动态</span><span class="mini"><i style="width:0%"></i></span></div></div>';
    var mRepos = '<div class="t-metric"><div class="label">上榜项目 · Repos</div>' +
      '<div class="num">' + top.length + '<span class="unit">个</span></div>' +
      '<div class="foot"><span>热度 TOP 全量收录</span>' +
      '<span class="mini"><i style="width:' + Math.min(100, top.length * 10) + '%"></i></span></div></div>';

    // 榜单：默认展示 15 行，其余折叠 + 展开按钮
    var TOP_VISIBLE = 15;
    var rowsHtml = "";
    var champHtml = "";
    var extraHtml = "";
    var streakMap = {};
    (rep.streaks || []).forEach(function (s) { streakMap[s.repo] = Number(s.weeks) || 0; });
    if (ch) {
      champHtml = trendTopRow(ch, 0, maxDelta, true, streakMap[ch.repo]);
      for (var i = 1; i < top.length; i++) {
        var row = trendTopRow(top[i], i, maxDelta, false, streakMap[top[i].repo]);
        if (i < TOP_VISIBLE) rowsHtml += row;
        else extraHtml += row;
      }
    }
    var expandBtn = extraHtml
      ? '<div class="t-expand-wrap"><button type="button" class="t-expand" data-total="' + top.length + '">展开全部 · 共 ' + top.length + " 个</button></div>"
      : "";
    // 话题 chips
    var chips = (rep.hot_topics || []).map(function (t) {
      return '<button class="t-chip" type="button" data-tag="' + escapeHtml(t.tag) + '">' +
        '<span class="count">' + t.count + "</span>" + escapeHtml(t.label) + "</button>";
    }).join("");
    var domains = domainBarsHtml(rep.domain_trends);
    var timelineCard = timelineHtml(rep.timeline);
    var comeCard = comebacksHtml(rep);
    var strip = growthStripHtml(rep.growth_meta);
    var themesCard = themesHtml(rep.themes);
    var memCard = memoryTrackHtml(rep);
    // 空态/关注动态
    var zones = "";
    var newStars = rep.new_stars || [];
    zones += newStars.length
      ? '<div class="t-zone"><div class="t-deco"></div><div class="title"><span class="dot"></span>本周新星</div><p>新上榜 ' + newStars.length + ' 个项目</p></div>'
      : '<div class="t-zone"><div class="t-deco"></div><div class="title"><span class="dot"></span>本周新星</div><p>暂无新上榜项目</p><p>新星榜随 Star 突增自动更新，下周一见分晓。</p></div>';
    var follows = rep.my_follows || [];
    if (follows.length) {
      var fHtml = follows.map(function (f) {
        return '<div class="t-top-row" data-repo="' + escapeHtml(f.repo) + '">' +
          '<span class="rank"></span><span class="lang-ico">' + trendLangLetter(f.language) + "</span>" +
          '<span class="info"><span class="repo">' + escapeHtml(f.repo) + "</span>" +
          '<span class="meta"><span class="meta-tag">' + starAbbr(f.stars || 0) + ' 星</span></span></span>' +
          '<span class="gain"><span class="delta">+' + formatCount(f.delta) + "</span></span></div>";
      }).join("");
      zones += '<div class="t-zone"><div class="title"><span class="dot"></span>我的关注</div>' + fHtml + "</div>";
    } else {
      zones += '<div class="t-zone"><div class="t-deco sm"></div><div class="title"><span class="dot"></span>我的关注</div><p>收藏 / 加星项目后，下周这里出现它们的增星动态。</p></div>';
    }

    cardsEl.innerHTML =
      '<div class="t-report">' +
        '<header class="t-header">' +
          "<div>" +
            '<div class="t-eyebrow">StarRadar · Weekly Signal</div>' +
            '<h2 class="t-title">每周趋势<span class="week">' + escapeHtml(rep.week) + "</span></h2>" +
            '<div class="t-sub">' + escapeHtml(rep.range) + " &nbsp;·&nbsp; 生成于 " +
              escapeHtml((rep.generated_at || "").replace("T", " ").slice(5, 16)) + " UTC</div>" +
          "</div>" +
          '<div class="t-weeks">' + opts + "</div>" +
        "</header>" +
        strip +
        themesCard +
        '<section class="t-metrics">' + mChampion + mRepos + mTopics + mFollows + "</section>" +
        '<section class="t-grid">' +
            '<div class="t-card"><div class="t-card-title"><h3>本周热度榜 · ' + top.length + "</h3>" +
            '<span class="hint">点击项目行 → 潜力雷达查看</span></div>' +
            champHtml + '<div class="t-rows">' + rowsHtml + "</div>" +
            (extraHtml ? '<div class="t-rows t-extra" hidden>' + extraHtml + "</div>" : "") +
            expandBtn + "</div>" +
          '<div style="display:flex; flex-direction:column; gap:18px; min-width:0;">' +
            '<div class="t-card" style="padding-bottom:16px;"><div class="t-card-title"><h3>热门领域</h3>' +
              '<span class="hint">共 ' + (rep.hot_topics || []).length + " 个</span></div>" +
              '<div class="t-chips">' + chips + "</div>" + domains + "</div>" +
            timelineCard +
            memCard +
            '<div class="t-empty">' + zones + "</div>" +
            comeCard +
          "</div>" +
        "</section>" +
        '<p class="t-footnote">数据快照 <b>' + escapeHtml(rep.week) + "</b> · 每周一 08:00 自动更新 · <b>StarRadar</b> 星探 · 潜力雷达</p>" +
      "</div>";
    renderPersonalInsight(rep);
  }

  // ===== 事件绑定 =====
  // 每周趋势：隐藏「为你发现」标题 + 排序/视图工具（周报是仪表盘，无卡片/列表概念）
  function syncSectionHead() {
    var head = document.querySelector(".section-head");
    if (!head) return;
    var isTrends = tab === "trends";
    head.classList.toggle("trends", isTrends);
    var tools = head.querySelector(".tools");
    if (tools) tools.classList.toggle("trends", isTrends);
  }

  function switchTabTo(t) {
    document.querySelectorAll("[data-tab]").forEach(function (x) { x.classList.remove("active"); });
    var btn = document.querySelector('[data-tab="' + t + '"]');
    if (btn) btn.classList.add("active");
    tab = t;
    syncSectionHead();
    cardsEl.style.opacity = 0;
    setTimeout(function () {
      if (tab === "trends") renderTrends(); else renderCards();
      cardsEl.style.opacity = 1;
    }, 150);
  }

  document.querySelectorAll("[data-tab]").forEach(function (b) {
    b.addEventListener("click", function () { switchTabTo(b.dataset.tab); });
  });

  // 周报交互：周选择 / 话题联动筛选 / 项目跳转
  cardsEl.addEventListener("click", function (e) {
    var opt = e.target.closest(".t-opt");
    if (opt) {
      trendWeek = opt.dataset.w;
      renderTrends();
      return;
    }
    var ex = e.target.closest(".t-expand");
    if (ex) {
      var card = ex.closest(".t-card");
      var extra = card ? card.querySelector(".t-extra") : null;
      if (extra) {
        var opening = extra.hidden;
        extra.hidden = !opening;
        ex.textContent = opening ? "收起" : "展开全部 · 共 " + (ex.dataset.total || "") + " 个";
      }
      return;
    }
    var topic = e.target.closest(".t-chip");
    if (topic) {
      // 跳到潜力雷达 + 按该话题搜索（数据驱动，不依赖静态标签列表）
      switchTabTo("potential");
      setTimeout(function () {
        searchInput.value = topic.dataset.tag;
        openSearch();
        renderSearch();
      }, 200);
      return;
    }
    var row = e.target.closest(".t-top-row, .th-repo, .m-milestone");
    if (row) {
      var it = itemByName(row.dataset.repo);
      if (it) openDetail(it);
      else {
        var light = trendRowLight(row.dataset.repo);
        if (light) openDetail(light);
        else { switchTabTo("potential"); setTimeout(function () { searchInput.value = row.dataset.repo; openSearch(); renderSearch(); }, 200); }
      }
    }
  });

  document.querySelectorAll("[data-view]").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll("[data-view]").forEach(function (x) { x.classList.remove("active"); });
      b.classList.add("active");
      cardsEl.classList.toggle("list", b.dataset.view === "list");
    });
  });

  cardsEl.addEventListener("click", function (e) {
    if (e.target.closest(".opbar")) return; // GitHub 操作栏按钮：不打开详情
    var fbLike = e.target.closest(".fb.like");
    if (fbLike) {
      var cardL = e.target.closest(".card");
      if (!cardL) return;
      var nmL = cardL.querySelector(".repo strong").textContent;
      var itemL = itemByName(nmL);
      if (itemL) {
        logAction(itemL, "like");
        notify("已记录 · 将推荐更多类似项目");
        fbLike.classList.add("off");
      }
      return;
    }
    var fbDis = e.target.closest(".fb.dismiss");
    if (fbDis) {
      var cardD = e.target.closest(".card");
      if (!cardD) return;
      var nmD = cardD.querySelector(".repo strong").textContent;
      var itemD = itemByName(nmD);
      if (itemD) {
        logAction(itemD, "dismiss");
        dismissed[nmD] = 1;
        fbDis.classList.add("off");
        notify("已记录 · 将减少类似项目");
      }
      return;
    }
    var btn = e.target.closest(".save");
    if (btn) {
      var card0 = e.target.closest(".card");
      if (!card0) return;
      var nm0 = card0.querySelector(".repo strong").textContent;
      var item0 = itemByName(nm0);
      if (favs[nm0]) { delete favs[nm0]; btn.classList.remove("saved"); btn.innerHTML = starSvg(true, 11); notify("已从你的雷达移除"); }
      else {
        favs[nm0] = Date.now(); btn.classList.add("saved"); btn.textContent = "✓"; notify("已加入你的雷达");
        if (item0) logAction(item0, "star");
      }
      saveFavs();
      if (tab === "picks") renderCards();
      return;
    }
    var card = e.target.closest(".card");
    if (!card || card.classList.contains("sk-card")) return;
    var hit = card.dataset.full ? itemByName(card.dataset.full) : null;
    if (hit) openDetail(hit);
  });

  function itemByName(name) {
    var all = searchIndex();
    for (var i = 0; i < all.length; i++) {
      if (fullName(all[i]) === name) return all[i];
    }
    return null;
  }

  document.querySelectorAll("#dRange button").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll("#dRange button").forEach(function (x) { x.classList.remove("active"); });
      b.classList.add("active");
      sparkRange = Number(b.dataset.r);
      if (detailItem) {
        var sp = detailSparkPath(detailItem, 720, 120, sparkRange);
        var spark = document.querySelector("#dSpark");
        var grad = spark.querySelector("defs").outerHTML;
        spark.innerHTML = grad + sp.area + sp.line;
        document.querySelector("#dSparkVal").textContent = sp.range;
        document.querySelector(".d-spark-title small").textContent = "Stars 近 " + sparkRange + " 天";
      }
    });
  });

  document.querySelector("#detailClose").addEventListener("click", closeDetail);
  detailEl.addEventListener("click", function (e) {
    if (e.target === detailEl) closeDetail();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && detailEl.classList.contains("open")) closeDetail();
  });
  document.querySelector("#dSave").addEventListener("click", function () {
    if (!detailItem) return;
    var name = fullName(detailItem);
    var btn = this;
    if (favs[name]) { delete favs[name]; btn.classList.remove("saved"); btn.innerHTML = starSvg(true, 11) + " 加入我的雷达"; notify("已从你的雷达移除"); }
    else { favs[name] = Date.now(); btn.classList.add("saved"); btn.textContent = "✓ 已在你的雷达"; notify("已加入你的雷达"); logAction(detailItem, "star"); }
    saveFavs();
    var c = cardsEl.querySelector(".card .repo strong");
    if (c && c.textContent === name) {
      var sv = c.parentNode.parentNode.parentNode.querySelector(".save");
      if (sv) { sv.classList.toggle("saved", !!favs[name]); sv.innerHTML = favs[name] ? "✓" : starSvg(true, 11); }
    }
  });

  // ===== 排序切换 =====
  document.querySelector("#sortBy").addEventListener("change", function () {
    sortBy = this.value;
    renderCards();
    notify("已按" + this.options[this.selectedIndex].text + "排序");
  });

  // ===== 行为档案导出（收藏 + 交互日志 + 问卷 → 后端 --import-history） =====
  function exportFavs() {
    var names = Object.keys(favs);
    if (!names.length && !historyLog.length) { notify("还没有任何行为数据"); return; }
    var payload = {
      exported_at: new Date().toISOString(),
      favs: favs,
      history: historyLog,
      survey: null,
    };
    try { payload.survey = JSON.parse(localStorage.getItem(SURVEY_KEY)); } catch (e) {}
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "starradar-profile-" + new Date().toISOString().slice(0, 10) + ".json";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 3000);
    notify("行为档案已导出（" + names.length + " 收藏 / " + historyLog.length + " 条交互）");
  }

  function importFavs(file) {
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var payload = JSON.parse(reader.result);
        var incoming = payload.favs || payload;
        var count = 0;
        Object.keys(incoming).forEach(function (k) {
          if (typeof k !== "string" || !k) return;
          if (!favs[k]) count++;
          favs[k] = typeof incoming[k] === "number" ? incoming[k] : Date.now();
        });
        var hist = payload.history;
        if (Array.isArray(hist) && hist.length) {
          historyLog = historyLog.concat(hist);
          saveHistory();
        }
        if (payload.survey && typeof payload.survey === "object") {
          localStorage.setItem(SURVEY_KEY, JSON.stringify(payload.survey));
        }
        saveFavs();
        renderCards();
        notify("已导入 " + count + " 个收藏 / " + (hist ? hist.length : 0) + " 条交互");
      } catch (err) {
        notify("导入失败：文件格式不正确");
      }
    };
    reader.readAsText(file);
  }

  var importInput = document.createElement("input");
  importInput.type = "file";
  importInput.accept = ".json,application/json";
  importInput.style.display = "none";
  document.body.appendChild(importInput);
  importInput.addEventListener("change", function () {
    if (importInput.files && importInput.files[0]) importFavs(importInput.files[0]);
    importInput.value = "";
  });

  // 导出按钮：挂在工具条 views 左侧
  var exportBtn = document.createElement("button");
  exportBtn.className = "icon export-btn";
  exportBtn.title = "导出行为档案（收藏+交互+问卷 → 后端学习）";
  exportBtn.textContent = "⇪";
  exportBtn.addEventListener("click", exportFavs);
  var viewsEl = document.querySelector(".views");
  viewsEl.parentNode.insertBefore(exportBtn, viewsEl);

  // 导入：双击 logo 触发（或长按导出按钮不行，用按钮组：⇪ 导出，⇓ 导入）
  var importBtn = document.createElement("button");
  importBtn.className = "icon export-btn";
  importBtn.title = "导入收藏 JSON（恢复 / 合并）";
  importBtn.textContent = "⇓";
  importBtn.addEventListener("click", function () { importInput.click(); });
  viewsEl.parentNode.insertBefore(importBtn, viewsEl);

  document.querySelector("#search").addEventListener("click", openSearch);
  searchInput.addEventListener("input", function () {
    document.querySelector("#searchClear").hidden = !searchInput.value;
    renderSearch();
    renderHist();
  });
  searchInput.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeSearch();
  });
  document.querySelector("#searchClear").addEventListener("click", function () {
    searchInput.value = "";
    this.hidden = true;
    renderSearch();
    searchInput.focus();
  });
  ["#fTopic", "#fLang", "#fStage"].forEach(function (sel) {
    document.querySelector(sel).addEventListener("change", renderSearch);
  });
  document.querySelector("#fMin").addEventListener("input", function () {
    document.querySelector("#fMinVal").textContent = this.value;
    renderSearch();
  });
  searchPanel.addEventListener("click", function (e) {
    if (e.target === searchPanel) closeSearch();
  });
  document.querySelector("#refresh").addEventListener("click", function (e) {
    e.preventDefault();
    init();
    notify("雷达已重新扫描");
  });

  // ===== 冷启动问卷（与后端 SURVEY_TOPIC_MAP 保持一致） =====
  var SURVEY_TOPICS = [
    { group: "hot", items: [
      "AI 大模型 / LLM", "AI 智能体 / Agent", "MCP 服务器", "Agent Skills",
      "RAG / 知识库", "Prompt 提示词工程", "Deep Research 深度研究",
      "Vibe Coding", "推理 / Inference", "向量数据库", "API / 工具链",
      "自动化工作流",
    ]},
    { group: "classic", items: [
      "机器学习", "深度学习 / AI 训练", "数据科学", "Python 生态", "C / C++",
      "Java / JVM", "Go 生态", "Rust 生态", "JavaScript / TypeScript",
      "编程语言 / 编译器", "前端框架", "后端 / 云原生", "数据库",
      "DevOps / CI-CD", "移动开发", "桌面应用", "嵌入式 / 物联网",
      "游戏开发", "安全 / 隐私", "测试 / 质量", "可观测性 / 监控",
      "网络 / 爬虫", "协作 / 生产力", "文档 / 知识管理", "设计 / 创意",
      "多媒体 / 音视频", "教育 / 学习资源", "区块链 / Web3",
    ]},
  ];
  var surveyStep = 1;
  var surveyEl = document.querySelector("#surveyPanel");

  // 问卷标签 → 主题关键词映射（与后端 src/profile/interest_model.py SURVEY_TOPIC_MAP 一致）
  var SURVEY_KEYWORDS = {
    "AI 大模型 / LLM": ["llm", "large-language-model", "gpt", "deepseek", "openai", "generative-ai", "transformer"],
    "AI 智能体 / Agent": ["agent", "ai-agent", "ai-agents", "autonomous-agents", "multi-agent", "agents"],
    "MCP 服务器": ["mcp", "model-context-protocol", "mcp-server", "mcp-servers"],
    "Agent Skills": ["skills", "agent-skills", "claude-skills", "skill"],
    "RAG / 知识库": ["rag", "knowledge-base", "retrieval", "retrieval-augmented", "semantic-search"],
    "Prompt 提示词工程": ["prompt", "prompt-engineering", "prompts", "prompt-library"],
    "Deep Research 深度研究": ["deep-research", "research", "autonomous-research", "auto-research"],
    "Vibe Coding": ["vibe-coding", "coding-agent", "ai-coding", "ai-assisted"],
    "推理 / Inference": ["inference", "llm-inference", "serving", "vllm", "llama.cpp"],
    "向量数据库": ["vector-db", "vector-database", "vector-search", "embedding", "embeddings", "hnsw"],
    "API / 工具链": ["api", "api-client", "developer-tools", "cli", "openapi", "sdk", "devtools"],
    "自动化工作流": ["automation", "workflow", "workflows", "automat"],
    "机器学习": ["machine-learning", "ml", "neural-network", "scikit-learn", "xgboost"],
    "深度学习 / AI 训练": ["deep-learning", "pytorch", "tensorflow", "cnn", "transformer", "fine-tuning", "training"],
    "数据科学": ["data-science", "data-analysis", "pandas", "notebook", "data-visualization"],
    "Python 生态": ["python", "pypi", "django", "fastapi", "flask"],
    "C / C++": ["c", "c-plus-plus", "cpp", "cmake", "opengl"],
    "Java / JVM": ["java", "jvm", "spring", "kotlin", "maven", "scala"],
    "Go 生态": ["go", "golang"],
    "Rust 生态": ["rust", "cargo", "wasm"],
    "JavaScript / TypeScript": ["javascript", "typescript", "nodejs", "npm", "bun", "deno", "esm"],
    "编程语言 / 编译器": ["compiler", "interpreter", "programming-language", "parser", "linter", "lang-design"],
    "前端框架": ["frontend", "react", "vue", "svelte", "web", "css", "tailwind"],
    "后端 / 云原生": ["backend", "cloud", "kubernetes", "docker", "serverless", "microservices"],
    "数据库": ["database", "sql", "nosql", "data-stores", "postgres", "redis", "clickhouse"],
    "DevOps / CI-CD": ["devops", "ci", "cd", "github-actions", "terraform", "infrastructure-as-code", "ansible"],
    "移动开发": ["mobile", "android", "ios", "react-native", "flutter", "swift"],
    "桌面应用": ["desktop", "electron", "tauri", "qt", "gui"],
    "嵌入式 / 物联网": ["embedded", "iot", "arduino", "esp32", "raspberry-pi", "firmware"],
    "游戏开发": ["game", "game-engine", "gamedev", "unity", "godot"],
    "安全 / 隐私": ["security", "privacy", "encryption", "cybersecurity", "pentest", "ctf"],
    "测试 / 质量": ["testing", "test", "unit-testing", "e2e", "quality", "code-coverage"],
    "可观测性 / 监控": ["observability", "monitoring", "grafana", "prometheus", "opentelemetry", "logging", "tracing"],
    "网络 / 爬虫": ["networking", "scraper", "crawler", "http", "proxy", "websocket"],
    "协作 / 生产力": ["productivity", "collaboration", "team", "project-management", "notes", "task-manager"],
    "文档 / 知识管理": ["documentation", "knowledge-management", "wiki", "docs", "second-brain"],
    "设计 / 创意": ["design", "ui", "ux", "figma", "creative", "art", "color-scheme"],
    "多媒体 / 音视频": ["multimedia", "audio", "video", "ffmpeg", "image-processing", "codec"],
    "教育 / 学习资源": ["education", "learning", "tutorial", "awesome", "books", "courses", "cs-resources"],
    "区块链 / Web3": ["blockchain", "web3", "crypto"],
  };

  // 卡片类别标签：优先问卷选中标签（topics / 仓库名命中），其次 topics[0]，最后语言
  function loadSurveySelected() {
    try {
      var raw = JSON.parse(localStorage.getItem(SURVEY_KEY));
      return (raw && raw.step1 && Array.isArray(raw.step1.selected)) ? raw.step1.selected : [];
    } catch (e) { return []; }
  }
  var surveySelected = loadSurveySelected();
  function topicHits(topic, label) {
    var kws = SURVEY_KEYWORDS[label];
    if (!kws) return false;
    var t = String(topic).toLowerCase();
    for (var i = 0; i < kws.length; i++) if (t.indexOf(kws[i]) !== -1) return true;
    return false;
  }
  function nameHits(name, label) {
    var kws = SURVEY_KEYWORDS[label];
    if (!kws) return false;
    var n = String(name).toLowerCase();
    for (var i = 0; i < kws.length; i++)
      if (kws[i].length >= 3 && n.indexOf(kws[i]) !== -1) return true;
    return false;
  }
  function categoryTag(item) {
    var r = repoOf(item);
    var topics = r.topics || [];
    var name = fullName(item);
    for (var i = 0; i < surveySelected.length; i++) {
      var label = surveySelected[i];
      for (var j = 0; j < topics.length; j++) {
        if (topicHits(topics[j], label)) return label;
      }
      if (nameHits(name, label)) return label;
    }
    if (topics.length) return String(topics[0]);
    return r.language || "未知";
  }

  function updateProfileAvatar() {
    var el = document.querySelector(".profile span");
    if (!el) return;
    var letter = "?";
    try {
      var raw = JSON.parse(localStorage.getItem(SURVEY_KEY));
      var gh = ((raw && raw.step3 && raw.step3.github_username) || "").trim();
      if (gh) letter = gh.charAt(0).toUpperCase();
    } catch (e) {}
    el.textContent = letter;
  }

  function reportSurvey(survey) {
    try {
      var raw = JSON.stringify(survey);
      fetch("/api/survey", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uid: reportUid(), survey: survey }),
      }).then(function (r) {
        if (r.ok) {
          try { localStorage.setItem(SURVEY_SYNC_KEY, raw); } catch (e) {}
        } else {
          try { localStorage.removeItem(SURVEY_SYNC_KEY); } catch (e) {}
        }
      }).catch(function () {
        try { localStorage.removeItem(SURVEY_SYNC_KEY); } catch (e) {}
      });
    } catch (e) {}
  }

  function saveSurvey() {
    var selected = [];
    Array.prototype.forEach.call(
      surveyEl.querySelectorAll(".survey-tags .st-tag.sel"),
      function (el) { selected.push(el.textContent.trim()); }
    );
    var value = {
      min: Number(document.querySelector("#surveyMin").value) || 0,
      max: Number(document.querySelector("#surveyMax").value) || null,
    };
    var survey = {
      step1: { selected: selected },
      step2: { value: value },
      step3: { github_username: document.querySelector("#surveyGithub").value.trim() },
    };
    localStorage.setItem(SURVEY_KEY, JSON.stringify(survey));
    updateProfileAvatar();
    surveySelected = loadSurveySelected();
    renderCards();
    reportSurvey(survey);
    if (typeof window.LLM !== "undefined" && window.LLM.saveFromForm) window.LLM.saveFromForm();
    return survey;
  }

  function openSurvey(force) {
    if (!force && localStorage.getItem(SURVEY_KEY)) return;
    surveyStep = 1;
    if (typeof window.LLM !== "undefined" && window.LLM.syncForm) window.LLM.syncForm();
    surveyEl.classList.add("open");
    surveyEl.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    renderSurveyStep();
  }
  function closeSurvey() {
    surveyEl.classList.remove("open");
    surveyEl.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
  }
  var SURVEY_STEPS = 4;
  function renderSurveyStep() {
    Array.prototype.forEach.call(surveyEl.querySelectorAll(".survey-step"), function (s) {
      s.hidden = Number(s.dataset.step) !== surveyStep;
    });
    document.querySelector("#surveyCount").textContent = surveyStep + " / " + SURVEY_STEPS;
    document.querySelector("#surveyNext").hidden = surveyStep === SURVEY_STEPS;
    document.querySelector("#surveyDone").hidden = surveyStep !== SURVEY_STEPS;
    document.querySelector("#surveyBar").style.width = (surveyStep / SURVEY_STEPS * 100) + "%";
    if (surveyStep === SURVEY_STEPS) renderSuggestions();
    if (surveyStep === SURVEY_STEPS && typeof window.LLM !== "undefined" && window.LLM.syncForm) window.LLM.syncForm();
  }

  // ===== 调研 AI 区：问卷完成页生成个性化建议（有 key → LLM；无 key → 规则模板） =====
  var SUGGEST_KEY = "starradar:suggests";
  function loadSurveyDoc() {
    try { return JSON.parse(localStorage.getItem(SURVEY_KEY)) || {}; } catch (e) { return {}; }
  }
  function ruleSuggestions() {
    var doc = loadSurveyDoc();
    var sel = (doc.step1 && doc.step1.selected) || [];
    var val = (doc.step2 && doc.step2.value) || {};
    var items = [];
    if (sel.length) {
      items.push({
        title: "兴趣聚焦",
        text: "雷达已按「" + sel.slice(0, 3).join("、") + "」加权排序，同方向项目将优先出现在为你发现。",
      });
    } else {
      items.push({ title: "兴趣聚焦", text: "先收藏 / 点开几个项目，雷达会从你的行为中学习偏好。" });
    }
    var min = Number(val.min) || 0, max = Number(val.max) || 0;
    var minTxt = formatCount(min) + " 星", maxTxt = formatCount(max) + " 星";
    items.push({
      title: "体量区间",
      text: max > 0
        ? "已过滤 " + minTxt + " 以下与 " + maxTxt + " 以上的项目，可在「我的雷达」随时调整。"
        : "已过滤 " + minTxt + " 以下的小众项目，可在「我的雷达」随时调整。",
    });
    items.push({ title: "下周一见", text: "每周一 08:00 自动生成周报：热度榜 / 新星发现 / 关注动态，并附你的专属解读。" });
    return items;
  }
  function renderSuggestions() {
    var box = document.querySelector("#suggestBox");
    if (!box || box.dataset.done) return;
    box.dataset.done = "1";
    var cached = null;
    try { cached = JSON.parse(localStorage.getItem(SUGGEST_KEY)); } catch (e) {}
    if (cached && Array.isArray(cached.items) && cached.items.length) {
      // 清洗历史脏缓存（旧版建议文本混入过未转义的 SVG 源码）
      var dirty = cached.items.some(function (it) {
        return it.text && it.text.indexOf("<svg") !== -1;
      });
      if (!dirty) {
        box.innerHTML = suggestionHTML(cached.items);
        return;
      }
      try { localStorage.removeItem(SUGGEST_KEY); } catch (e) {}
    }
    var hasLLM = typeof window.LLM !== "undefined" && window.LLM.isConfigured() && window.LLM.canCall("survey");
    if (!hasLLM) {
      var items = ruleSuggestions();
      box.innerHTML = suggestionHTML(items);
      try { localStorage.setItem(SUGGEST_KEY, JSON.stringify({ items: items })); } catch (e) {}
      return;
    }
    box.innerHTML = '<div class="suggest-loading">AI 正在分析你的兴趣画像…</div>';
    window.LLM.chat([
      { role: "system", content: "你是 StarRadar 的个性化雷达顾问。基于用户刚填写的冷启动问卷，给 3 条可执行的个性化建议（兴趣方向、使用技巧、预期收获各 1 条），语气亲切、每条 ≤40 字。输出 JSON。" },
      { role: "user", content: "我的选择：" + JSON.stringify(loadSurveyDoc()) },
    ], { feature: "survey", temperature: 0.7, max_tokens: 400 })
      .then(function (txt) {
        var j = window.LLM.parseJSON(txt);
        var arr = Array.isArray(j.items) ? j.items : [];
        var items = arr.slice(0, 3).map(function (it) {
          return { title: String(it.title || "").slice(0, 12), text: String(it.text || it.body || "").slice(0, 60) };
        }).filter(function (it) { return it.text; });
        if (!items.length) items = ruleSuggestions();
        box.innerHTML = suggestionHTML(items);
        try { localStorage.setItem(SUGGEST_KEY, JSON.stringify({ items: items })); } catch (e) {}
      })
      .catch(function () {
        var items = ruleSuggestions();
        box.innerHTML = suggestionHTML(items);
        try { localStorage.setItem(SUGGEST_KEY, JSON.stringify({ items: items })); } catch (e) {}
      });
  }
  function suggestionHTML(items) {
    return '<div class="suggest-list">' + items.map(function (it) {
      return '<div class="suggest-item"><b>' + escapeHtml(it.title) + "</b><span>" + escapeHtml(it.text) + "</span></div>";
    }).join("") + '</div><p class="suggest-note">建议只存本机 · 行为越多越准 · 可随时重做问卷刷新</p>';
  }

  // 领域标签渲染（首次打开时）
  var tagsBoxes = surveyEl.querySelectorAll(".survey-tags");
  if (!tagsBoxes.length || !tagsBoxes[0].children.length) {
    SURVEY_TOPICS.forEach(function (g) {
      var box = surveyEl.querySelector('.survey-tags[data-group="' + g.group + '"]');
      if (!box) return;
      g.items.forEach(function (t) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "st-tag";
        b.textContent = t;
        b.addEventListener("click", function () {
          b.classList.toggle("sel");
        });
        box.appendChild(b);
      });
    });
  }

  function skipSurvey() {
    // 首次跳过 → 存空档案，避免下次自动重弹
    if (!localStorage.getItem(SURVEY_KEY)) {
      var empty = {
        step1: { selected: [] },
        step2: { value: { min: 500, max: null } },
        step3: { github_username: "" },
      };
      localStorage.setItem(SURVEY_KEY, JSON.stringify(empty));
      reportSurvey(empty);
    }
    closeSurvey();
    updateProfileAvatar();
    surveySelected = loadSurveySelected();
    renderCards();
  }

  // GitHub 登录成功 → 自动填入问卷用户名（免手动输入）
  window.addEventListener("sr:gh-login", function (e) {
    var login = e && e.detail && e.detail.login;
    if (!login) return;
    var hadSurvey = !!localStorage.getItem(SURVEY_KEY);
    try {
      var raw = JSON.parse(localStorage.getItem(SURVEY_KEY) || "{}");
      raw.step3 = raw.step3 || {};
      if (raw.step3.github_username !== login) {
        raw.step3.github_username = login;
        localStorage.setItem(SURVEY_KEY, JSON.stringify(raw));
        if (hadSurvey) reportSurvey(raw);
        surveySelected = loadSurveySelected();
        renderCards();
      }
    } catch (err) {}
    var input = document.querySelector("#surveyGithub");
    if (input) input.value = login;
    updateProfileAvatar();
  });

  document.querySelector("#surveyNext").addEventListener("click", function () {
    if (surveyStep === 1 && !surveyEl.querySelector(".survey-tags .st-tag.sel")) {
      notify("至少选一个领域，或点跳过");
      return;
    }
    surveyStep++;
    if (surveyStep === SURVEY_STEPS) saveSurvey();
    renderSurveyStep();
  });
  document.querySelector("#surveyDone").addEventListener("click", function () {
    saveSurvey();
    closeSurvey();
    var aiOn = typeof window.LLM !== "undefined" && window.LLM.isConfigured();
    // 个人版：问卷修改在下次管道生成时生效（管道每次读最新问卷）
    notify(IS_PERSONAL ? "问卷已保存 · 改动将于下次生成时生效"
      : (aiOn ? "兴趣档案已建立 · AI 个性化已开启" : "兴趣档案已建立"));
  });
  document.querySelector("#surveySkip").addEventListener("click", skipSurvey);
  document.querySelector("#surveyClose").addEventListener("click", skipSurvey);
  surveyEl.addEventListener("click", function (e) {
    if (e.target === surveyEl) skipSurvey();
  });

  // 我的雷达按钮：仅个人版显示（公版回归客观，无问卷）
  var profileBtn = document.querySelector("#profile");
  if (!IS_PERSONAL) {
    if (profileBtn) profileBtn.style.display = "none";
  } else if (profileBtn) {
    profileBtn.addEventListener("click", function () {
      openSurvey(true);
      notify("可随时调整你的兴趣方向");
    });
  }

  loadFavs();
  loadHistory();
  updateProfileAvatar();
  startReportLoop();
  // 本地服务在跑时自动补报（问卷 + 积压行为）；静默失败，不打扰
  setTimeout(function () { syncLocalData(false); }, 8000);
  var syncBtn = document.querySelector("#syncBtn");
  if (syncBtn) syncBtn.addEventListener("click", function () { syncLocalData(true); });
  init();
  // 冷启动问卷：仅个人版自动弹出（公版打开即用，无问卷）
  if (IS_PERSONAL) setTimeout(function () { openSurvey(false); }, 900);
  console.log("StarRadar · Observatory 已启动" + (IS_PERSONAL ? "（个人版）" : "（公版）"));

  // ===== GitHub 原生集成（P4）：操作 → 行为日志 =====
  window.addEventListener("sr:gh-action", function (ev) {
    var d = ev.detail || {};
    if (!d.repo || !d.action) return;
    var it = itemByName(d.repo);
    if (it) logAction(it, d.action);
  });
})();


