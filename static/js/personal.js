// StarRadar · 个人特化雷达（版本 2 · 本地后端）
// 渲染沿用公版卡片结构（五维雷达图/维度条/sparkline/操作栏），数据源 /api/personal/scores
// 登录：跳转式 GitHub 登录（配置了 OAuth 时）或「使用本地 Token」（复用 .env GITHUB_TOKEN）
(function () {
  "use strict";

  var cardsEl = document.querySelector("#pCards");
  var toastEl = document.querySelector("#toast");

  // ===== 工具（与公版 app.js 同构） =====
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
  function notify(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(window.__pt);
    window.__pt = setTimeout(function () { toastEl.classList.remove("show"); }, 2400);
  }
  function langLetter(item) {
    var r = item.repo || {};
    var lang = r.language || "?";
    return escapeHtml(lang.charAt(0).toUpperCase());
  }
  var STAGE_LABELS = {
    early: "早期", mid_early: "中早期", mid_late: "中后期",
    late: "后期", saturated: "已饱和",
  };
  function stageLabel(item) {
    var s = item.score || {};
    return STAGE_LABELS[s.stage] || s.stage || "";
  }
  function weekGain(item) {
    var r = item.repo || {};
    if (r.stars_7d_ago != null && r.stars != null) return r.stars - r.stars_7d_ago;
    var b = (item.score && item.score.breakdown) || {};
    return Math.round((Number(b.vel) || 0) * 7);
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
  // 五维雷达图（与公版 renderRadar 同构）
  var RADAR_DIMS = [
    { key: "vel", label: "速度" },
    { key: "acc", label: "加速度" },
    { key: "health", label: "健康" },
    { key: "fresh", label: "新鲜" },
    { key: "signal", label: "信号" },
  ];
  function radarPoint(cx, cy, r, angleDeg) {
    var rad = (angleDeg - 90) * Math.PI / 180;
    return { x: (cx + r * Math.cos(rad)).toFixed(2), y: (cy + r * Math.sin(rad)).toFixed(2) };
  }
  function renderRadar(breakdown, size) {
    size = size || 96;
    var cx = size / 2, cy = size / 2;
    var R = size * 0.34;
    var angles = [];
    for (var i = 0; i < RADAR_DIMS.length; i++) angles.push(i * 72);
    var grid = "";
    [0.33, 0.66, 1.0].forEach(function (s) {
      var pts = [];
      for (var a = 0; a < angles.length; a++) {
        var p = radarPoint(cx, cy, R * s, angles[a]);
        pts.push(p.x + "," + p.y);
      }
      grid += '<polygon class="rd-grid" points="' + pts.join(" ") + '"/>';
    });
    var axes = "";
    for (var a2 = 0; a2 < angles.length; a2++) {
      var pa = radarPoint(cx, cy, R, angles[a2]);
      axes += '<line class="rd-axis" x1="' + cx + '" y1="' + cy + '" x2="' + pa.x + '" y2="' + pa.y + '"/>';
    }
    var dataPts = [], dots = "";
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
    return '<svg class="rd-radar" viewBox="-10 -6 ' + (size + 22) + ' ' + (size + 14) + '" aria-hidden="true">' +
      grid + axes + '<polygon class="rd-data" points="' + poly + '"/>' + dots + labels + "</svg>";
  }
  function dimBars(item) {
    var b = (item.score && item.score.breakdown) || {};
    var keys = ["vel", "acc", "health", "fresh", "signal"];
    var names = { vel: "速", acc: "加", health: "健", fresh: "鲜", signal: "信" };
    var html = "";
    for (var i = 0; i < keys.length; i++) {
      var v = Math.max(0, Math.min(100, Number(b[keys[i]]) || 0));
      html += '<span class="dim"><span class="dim-name">' + names[keys[i]] + '</span>' +
              '<i><b style="width:' + v + '%"></b></i><span class="dim-val">' + v + "</span></span>";
    }
    return '<div class="dims" title="5 维评分：速度 · 加速度 · 健康 · 新鲜 · 信号">' + html + "</div>";
  }
  function sparkPath(item) {
    var r = item.repo || {};
    var series = r.star_series || [];
    if (series.length >= 3) {
      var vals = series.map(function (p) { return Number(p.s) || 0; });
      var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
      var span = (max - min) || 1;
      var W = 110, H = 34, pts = [];
      for (var i = 0; i < vals.length; i++) {
        var x = 1 + (W - 2) * (i / (vals.length - 1));
        var y = H - 5 - (H - 10) * ((vals[i] - min) / span);
        pts.push(x.toFixed(1) + "," + y.toFixed(1));
      }
      return '<path d="M' + pts.join(" L") + '" stroke="#1677ff" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>';
    }
    var now = Number(r.stars) || 0;
    var ago = r.stars_7d_ago != null ? Number(r.stars_7d_ago) : now;
    if (now <= 0) return '<path d="M1 29C18 29 17 25 28 25s8-13 21-13c10 0 10 8 18 8 12 0 10-16 22-16 7 0 7 7 20 7" stroke="#1677ff" stroke-width="2" stroke-linecap="round"/>';
    return '<path d="M1 ' + (now >= ago ? 30 : 4) + "L109 " + (now >= ago ? 4 : 30) + '" stroke="#1677ff" stroke-width="2" stroke-linecap="round"/>';
  }

  // ===== 卡片（沿用公版结构） =====
  function renderCard(item, index) {
    var r = item.repo || {};
    var s = item.score || {};
    var name = r.full_name || (r.owner + "/" + r.name) || "";
    var lang = r.language || "";
    var topics = r.topics || [];
    var cat = topics[0] || lang || "未知";
    var stage = stageLabel(item);
    var score = Number(s.score || 0).toFixed(1);
    var chips = [];
    for (var t = 0; t < topics.length && chips.length < 4; t++) chips.push("<i>" + escapeHtml(topics[t]) + "</i>");
    if (!chips.length && lang) chips.push("<i>" + escapeHtml(lang) + "</i>");
    var tagsHtml = chips.length ? '<div class="card-tags">' + chips.join("") + "</div>" : "";
    var explain = s.explanation || "";
    var desc = explain && explain.indexOf("处于") !== 0 ? explain : (r.description || explain || "");
    var isExplain = explain && explain.indexOf("处于") !== 0 && r.description;
    var radar = s.breakdown ? renderRadar(s.breakdown, 96) : "";

    return (
      '<article class="card has-radar" data-full="' + escapeHtml(name) + '">' +
        '<span class="rank">' + ("0" + (index + 1)).slice(-2) + "</span>" +
        '<div class="repo"><span class="repo-icon">' + langLetter(item) + "</span>" +
          "<div><strong>" + escapeHtml(name) + "</strong>" +
          "<small>" + escapeHtml(r.owner || "") + " / " + escapeHtml(r.name || "") +
            (lang ? " · " + escapeHtml(lang) : "") + "</small></div></div>" +
        '<div class="radar-fig" title="5 维评分：速度 · 加速度 · 健康 · 新鲜 · 信号">' + radar + "</div>" +
        '<div class="card-main"><div class="card-info">' +
          "<h3>" + escapeHtml(cat) + " · 为我定制" +
            (stage ? '<span class="stage-chip">' + escapeHtml(stage) + "</span>" : "") + "</h3>" +
          tagsHtml +
          '<p class="score-desc' + (isExplain ? " explain" : "") + '">' + escapeHtml(desc) + "</p>" +
          '<div class="bottom"><div>' +
            '<div class="score">' + score + "<small> / 100</small></div>" +
            '<div class="trend ' + trendClass(item) + '">' + trendText(item) + "</div>" +
          "</div>" + dimBars(item) +
        "</div></div></div>" +
        '<svg class="spark" viewBox="0 0 110 34" fill="none">' + sparkPath(item) + "</svg>" +
        (typeof window.GH !== "undefined" ? window.GH.actionsHTML(item) : "") +
      "</article>"
    );
  }

  // ===== 状态 =====
  function setDot(id, ok) {
    var el = document.querySelector(id);
    if (el) el.className = "p-dot " + (ok ? "ok" : "no");
  }
  function refreshStatus() {
    fetch("/api/personal/status", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) throw new Error("no backend");
        setDot("#stLogin", d.logged_in);
        setDot("#stLLM", d.llm_configured);
        setDot("#stData", d.data_exists);
        var hint = document.querySelector("#pHint");
        if (!hint) return;
        var parts = [];
        if (!d.logged_in) parts.push("未登录：点击「通过 GitHub 登录」（跳转授权）或「使用本地 Token」（复用 .env 的 GITHUB_TOKEN，无需创建 OAuth App）。");
        if (!d.llm_configured) parts.push("LLM 未配置：在 .env 填入 LLM_API_KEY，解读才会个性化。");
        if (d.logged_in && !d.data_exists) parts.push("已就绪：运行 <code>python src/main.py --personal</code> 生成你的专属雷达。");
        if (d.logged_in && d.data_exists) parts.push("你的专属雷达已就绪（" + (d.login || "") + "）。运行 <code>python src/main.py --personal</code> 可每日刷新。");
        hint.innerHTML = parts.join("<br>");
      })
      .catch(function () { notify("未检测到本地服务，请先运行 python src/main.py --serve"); });
  }

  // ===== 登录 =====
  function handoffToken() {
    var gh = window.GH && window.GH.getUserInfo && window.GH.getUserInfo();
    var token = null;
    try { token = localStorage.getItem("starradar:gh_token"); } catch (e) {}
    if (!gh || !gh.login || !token) return;
    fetch("/api/gh_token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login: gh.login, token: token }),
    }).then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          notify("登录已交给本地后端：" + d.saved + "，可运行管道生成专属雷达");
          refreshStatus();
        }
      })
      .catch(function () {});
  }
  function localTokenLogin() {
    fetch("/api/gh_token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ use_local: true }),
    }).then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) throw new Error(d && d.error ? d.error : "failed");
        try {
          localStorage.setItem("starradar:gh_token", d.token);
          localStorage.setItem("starradar:gh_user", JSON.stringify({ login: d.login, avatar_url: "" }));
        } catch (e) {}
        notify("已用本地 Token 登录：" + d.login + "，刷新后即可加星 / Fork");
        setTimeout(function () { location.reload(); }, 900);
      })
      .catch(function (e) {
        notify("本地 Token 登录失败：" + (e.message || "请确认 .env 已配置 GITHUB_TOKEN"));
      });
  }

  // ===== 数据加载 =====
  function renderEmpty() {
    cardsEl.innerHTML =
      '<div class="p-empty"><b>还没有你的专属雷达。</b><br>' +
      "完成两步：<br>" +
      "1. 点击「通过 GitHub 登录」或「使用本地 Token」建立登录<br>" +
      "2. 运行 <code>python src/main.py --personal</code> 生成数据<br>" +
      "之后每次打开本页都是为你定制的内容，已看项目自动排除，每天不重样。</div>";
  }
  function loadData() {
    fetch("/api/personal/scores", { cache: "no-store" })
      .then(function (r) {
        if (r.status === 404) return null;
        if (!r.ok) throw new Error("load failed");
        return r.json();
      })
      .then(function (d) {
        if (!d || !Array.isArray(d.items) || !d.items.length) { renderEmpty(); return; }
        cardsEl.innerHTML = "";
        d.items.forEach(function (item, i) {
          cardsEl.insertAdjacentHTML("beforeend", renderCard(item, i));
          var full = item.repo && item.repo.full_name;
          if (full && window.GH && window.GH.actionsHTML) {
            var card = cardsEl.querySelector('[data-full="' + full + '"]');
            if (card && card.querySelector(".opbar")) {
              var ops = card.querySelector(".opbar");
              ops.insertAdjacentHTML("beforeend", window.GH.actionsHTML({ repo: item.repo }));
            }
          }
        });
      })
      .catch(function () { notify("个人数据加载失败（本地服务未启动？）"); });
  }

  // ===== 事件 =====
  var go = document.querySelector("#pGoLogin");
  if (go) go.addEventListener("click", function () { if (window.GH) window.GH.openPanel(); });
  var local = document.querySelector("#pLocalLogin");
  if (local) local.addEventListener("click", localTokenLogin);
  var ref = document.querySelector("#pRefresh");
  if (ref) ref.addEventListener("click", function () { refreshStatus(); loadData(); });
  window.addEventListener("sr:gh-login", function () { setTimeout(handoffToken, 600); refreshStatus(); });

  if (window.GH && window.GH.onChange) {
    window.GH.onChange(function () {
      refreshStatus();
      if (window.GH.isConnected()) handoffToken();
    });
  }

  refreshStatus();
  loadData();
})();
