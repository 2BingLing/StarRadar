// StarRadar · 主控脚本
// 职责：加载 3 份 JSON（scores=潜力雷达 / trending=正在升温 / picks=我的收藏）
//       → 骨架屏 → 渲染卡片（网格/列表视图）→ tab 切换 → 收藏（localStorage）→ toast
(function () {
  "use strict";

  var TABS = ["potential", "trending", "picks"];
  var DATA_FILES = {
    potential: "data/scores.json",
    trending: "data/trending.json",
    picks: "data/picks.json",
  };
  var TAB_LABEL = { potential: "潜力雷达", trending: "正在升温", picks: "我的收藏" };
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

  var cardsEl = document.querySelector("#cards");
  var toastEl = document.querySelector("#toast");
  var data = { potential: null, trending: null, picks: null };
  var tab = "potential";
  var favs = {};
  var sortBy = "score";

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
  function sortedItems(items) {
    var arr = items.slice();
    arr.sort(function (a, b) {
      var x, y;
      if (sortBy === "stars") { x = repoOf(a).stars; y = repoOf(b).stars; }
      else if (sortBy === "gain") { x = weekGain(a); y = weekGain(b); }
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
      fetchJson(DATA_FILES[key])
        .then(function (d) { data[key] = d; })
        .catch(function () {
          console.warn("[StarRadar] " + key + ".json 加载失败，降级到示例数据");
          data[key] = (window.SAMPLE_DATA && window.SAMPLE_DATA[key]) || [];
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
    if (tab === "potential") {
      return s.explanation && s.explanation.indexOf("处于") !== 0
        ? s.explanation
        : (r.description || s.explanation || "");
    }
    return r.description || s.explanation || "";
  }
  function isExplain(item) {
    var s = scoreOf(item);
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

  function openSearch() {
    searchPanel.classList.add("open");
    searchPanel.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    setTimeout(function () { searchInput.focus(); }, 60);
    renderSearch();
  }
  function closeSearch() {
    searchPanel.classList.remove("open");
    searchPanel.setAttribute("aria-hidden", "true");
    if (!detailEl.classList.contains("open")) document.body.style.overflow = "";
  }

  function renderSearch() {
    var q = searchInput.value;
    var lang = document.querySelector("#fLang").value;
    var stage = document.querySelector("#fStage").value;
    var minScore = Number(document.querySelector("#fMin").value) || 0;
    var tokens = searchTokens(q);

    var items = searchIndex().filter(function (it) {
      var r = repoOf(it);
      var s = scoreOf(it);
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
            "<strong>" + escapeHtml(fullName(it)) + "</strong>" +
            '<small>' + escapeHtml(r.description || (s.explanation || "")) + "</small>" +
          "</div>" +
          '<span class="s-meta">' +
            "<i>" + escapeHtml(r.language || "?") + "</i>" +
            '<b>' + sc + "</b>" +
          "</span>" +
        "</div>";
    });
    searchResultsEl.innerHTML = html;

    // 绑定点击 → 打开全屏详情
    Array.prototype.forEach.call(searchResultsEl.querySelectorAll(".s-item"), function (el) {
      el.addEventListener("click", function () {
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
    var r = repoOf(item);
    var s = scoreOf(item);
    var name = fullName(item);
    var score = (Number(s.score) != null ? Number(s.score) : 0).toFixed(1);
    var stage = stageLabel(item);
    var saved = !!favs[name];

    document.querySelector("#dIcon").textContent = langLetter(item);
    document.querySelector("#dName").textContent = name;
    document.querySelector("#dStage").textContent = stage || "—";
    document.querySelector("#dOwner").textContent = r.owner + " / " + r.name + " · " + (r.language || "未知语言") +
      " · ⭐ " + formatCount(r.stars) + " · 周增 " + trendText(item);
    document.querySelector("#dScore").textContent = score;
    document.querySelector("#dTrend").textContent = trendText(item).replace(" / 周", "");
    document.querySelector("#dDesc").textContent = r.description || "暂无描述";
    document.querySelector("#dExplain").textContent = s.explanation ||
      "该仓库暂未生成 AI 解读，可前往 GitHub 查看详情。";
    document.querySelector("#dDims").innerHTML = dimRows(item);
    document.querySelector("#dRadar").innerHTML = renderRadar(s.breakdown || {}, 340);
    document.querySelector("#dLink").href = r.html_url || "https://github.com/" + (r.owner + "/" + r.name);

    var saveBtn = document.querySelector("#dSave");
    saveBtn.textContent = saved ? "✓ 已在你的雷达" : "☆ 加入我的雷达";
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
    detailItem = null;
  }

  // ===== 卡片渲染 =====
  function renderCard(item, index) {
    var r = repoOf(item);
    var s = scoreOf(item);
    var name = fullName(item);
    var lang = r.language || "";
    var h3 = escapeHtml(lang ? lang + " · " + TAB_LABEL[tab] : TAB_LABEL[tab]);
    var stage = stageLabel(item);
    var chip = stage ? '<span class="stage-chip">' + escapeHtml(stage) + "</span>" : "";
    var desc = descText(item);
    var explain = isExplain(item) && tab === "potential" ? " explain" : "";
    var score = (Number(s.score) != null ? Number(s.score) : 0).toFixed(1);
    var favKey = name;
    var saved = favs[favKey] ? " saved" : "";
    var savedText = favs[favKey] ? "✓" : "☆";
    var radar = tab === "potential" && s.breakdown
      ? '<div class="radar-fig">' + renderRadar(s.breakdown, 118) + "</div>"
      : "";

    return (
      '<article class="card' + (radar ? " has-radar" : "") + '">' +
        '<span class="rank">' + ("0" + (index + 1)).slice(-2) + "</span>" +
        '<div class="repo"><span class="repo-icon">' + langLetter(item) + "</span>" +
          "<div><strong>" + escapeHtml(name) + "</strong>" +
          "<small>" + escapeHtml(r.owner || "") + " / " + escapeHtml(r.name || "") + "</small></div></div>" +
        (radar ? '<div class="radar-fig" title="5 维评分：速度 · 加速度 · 健康 · 新鲜 · 信号">' + renderRadar(s.breakdown, 96) + "</div>" : "") +
        '<div class="card-main">' +
          '<div class="card-info">' +
            "<h3>" + h3 + chip + "</h3>" +
            (desc ? '<p class="score-desc' + explain + '">' + escapeHtml(desc) + "</p>" : '<p class="score-desc"></p>') +
            '<div class="bottom"><div>' +
              '<div class="score">' + score + '<small> / 100</small></div>' +
              '<div class="trend ' + trendClass(item) + '">' + trendText(item) + "</div>" +
            "</div>" +
            (tab === "potential" ? dimBars(item) : "") +
            "</div>" +
          "</div>" +
        "</div>" +
        '<button class="save' + saved + '" title="收藏到我的雷达">' + savedText + "</button>" +
        '<svg class="spark" viewBox="0 0 110 34" fill="none">' + sparkPath(item) + "</svg>" +
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

  function renderCards() {
    var items = data[tab] || [];
    if (!items.length) {
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
    var items = sortedItems(data[tab] || []);
    var html = "";
    for (var i = 0; i < items.length; i++) html += renderCard(items[i], i);
    cardsEl.innerHTML = html;
  }

  // ===== Hero 统计 =====
  function renderHero() {
    var sig = (data.potential || []).length + (data.trending || []).length;
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
    document.querySelector('[data-view="grid"]').classList.add("active");
    document.querySelector('[data-view="list"]').classList.remove("active");
    loadAll(function () {
      renderHero();
      renderCards();
      fillLangFilter();
    });
  }

  function fillLangFilter() {
    var langs = {};
    TABS.forEach(function (k) {
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

  // ===== 事件绑定 =====
  document.querySelectorAll("[data-tab]").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll("[data-tab]").forEach(function (x) { x.classList.remove("active"); });
      b.classList.add("active");
      tab = b.dataset.tab;
      cardsEl.style.opacity = 0;
      setTimeout(function () { renderCards(); cardsEl.style.opacity = 1; }, 150);
    });
  });

  document.querySelectorAll("[data-view]").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll("[data-view]").forEach(function (x) { x.classList.remove("active"); });
      b.classList.add("active");
      cardsEl.classList.toggle("list", b.dataset.view === "list");
    });
  });

  cardsEl.addEventListener("click", function (e) {
    var btn = e.target.closest(".save");
    if (btn) {
      var card0 = e.target.closest(".card");
      if (!card0) return;
      var nm0 = card0.querySelector(".repo strong").textContent;
      if (favs[nm0]) { delete favs[nm0]; btn.classList.remove("saved"); btn.textContent = "☆"; notify("已从你的雷达移除"); }
      else { favs[nm0] = Date.now(); btn.classList.add("saved"); btn.textContent = "✓"; notify("已加入你的雷达"); }
      saveFavs();
      return;
    }
    var card = e.target.closest(".card");
    if (!card || card.classList.contains("sk-card")) return;
    var idx = Array.prototype.indexOf.call(cardsEl.children, card);
    var items = data[tab] || [];
    if (idx >= 0 && idx < items.length) openDetail(items[idx]);
  });

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
    if (favs[name]) { delete favs[name]; btn.classList.remove("saved"); btn.textContent = "☆ 加入我的雷达"; notify("已从你的雷达移除"); }
    else { favs[name] = Date.now(); btn.classList.add("saved"); btn.textContent = "✓ 已在你的雷达"; notify("已加入你的雷达"); }
    saveFavs();
    var c = cardsEl.querySelector(".card .repo strong");
    if (c && c.textContent === name) {
      var sv = c.parentNode.parentNode.parentNode.querySelector(".save");
      if (sv) { sv.classList.toggle("saved", !!favs[name]); sv.textContent = favs[name] ? "✓" : "☆"; }
    }
  });

  // ===== 排序切换 =====
  document.querySelector("#sortBy").addEventListener("change", function () {
    sortBy = this.value;
    renderCards();
    notify("已按" + this.options[this.selectedIndex].text + "排序");
  });

  // ===== 收藏导出 / 导入 =====
  function exportFavs() {
    var names = Object.keys(favs);
    if (!names.length) { notify("还没有收藏任何项目"); return; }
    var payload = { exported_at: new Date().toISOString(), favs: favs };
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "starradar-favs-" + new Date().toISOString().slice(0, 10) + ".json";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 3000);
    notify("收藏已导出（" + names.length + " 项）");
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
        saveFavs();
        renderCards();
        notify("已导入 " + count + " 个收藏（共 " + Object.keys(favs).length + " 项）");
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
  exportBtn.title = "导出收藏 JSON（可分享 / 备份）";
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
  ["#fLang", "#fStage"].forEach(function (sel) {
    document.querySelector(sel).addEventListener("change", renderSearch);
  });
  document.querySelector("#fMin").addEventListener("input", function () {
    document.querySelector("#fMinVal").textContent = this.value;
    renderSearch();
  });
  searchPanel.addEventListener("click", function (e) {
    if (e.target === searchPanel) closeSearch();
  });
  document.querySelector("#profile").addEventListener("click", function () {
    notify("兴趣档案即将上线 · 越用越懂你");
  });
  document.querySelector("#refresh").addEventListener("click", function (e) {
    e.preventDefault();
    init();
    notify("雷达已重新扫描");
  });

  loadFavs();
  init();
  console.log("StarRadar · Observatory 已启动");
})();
