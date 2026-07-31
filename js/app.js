// StarRadar · 状态协调器
// 职责：骨架屏渲染、加载失败提示、概览统计聚合、状态条联动
// 各 card 模块在 init 时调用 window.StarRadar.{renderSkeleton,renderError,reportLoad,reportFail}

(function () {
  "use strict";

  // ===== 全局加载状态 =====
  var KEYS = ["scores", "trending", "picks"];
  var state = {
    scores: { status: "pending", data: null },
    trending: { status: "pending", data: null },
    picks: { status: "pending", data: null },
  };

  // ===== DOM 引用（脚本位于 body 末尾，DOM 已就绪，可同步缓存） =====
  var els = {};
  function cacheEls() {
    els.statusText = document.querySelector(".status-text");
    els.statusTime = document.querySelector(".status-time");
    els.statusPulse = document.querySelector(".status-pulse");
    els.statusRow = document.querySelector(".status-row");
    var vals = document.querySelectorAll(".stat-value");
    els.statTracked = vals[0];
    els.statTrending = vals[1];
    els.statScores = vals[2];
  }
  cacheEls();

  // ===== 骨架屏模板 =====
  function skeletonCard() {
    return (
      '<div class="sk-card">' +
        '<div class="sk sk-circle"></div>' +
        '<div class="sk-body">' +
          '<div class="sk sk-line w55"></div>' +
          '<div class="sk sk-line w85"></div>' +
          '<div class="sk sk-line w40"></div>' +
        '</div>' +
        '<div class="sk-side">' +
          '<div class="sk sk-square"></div>' +
          '<div class="sk sk-line w30"></div>' +
        '</div>' +
      '</div>'
    );
  }

  function skeletonRow() {
    return (
      '<div class="sk-row">' +
        '<div class="sk sk-circle-sm"></div>' +
        '<div class="sk-body">' +
          '<div class="sk sk-line w50"></div>' +
          '<div class="sk sk-line w80"></div>' +
        '</div>' +
        '<div class="sk sk-line w20"></div>' +
      '</div>'
    );
  }

  function renderSkeleton(container, type) {
    if (!container) return;
    var n = type === "trending" ? 8 : (type === "picks" ? 3 : 6);
    var tpl = type === "trending" ? skeletonRow : skeletonCard;
    var html = "";
    for (var i = 0; i < n; i++) html += tpl();
    container.innerHTML = html;
    container.classList.remove("section-empty", "has-scores", "has-trending");
    container.classList.add("has-skeleton");
  }

  // ===== 错误提示 + 重试 =====
  function renderError(container, onRetry) {
    if (!container) return;
    container.innerHTML =
      '<div class="load-error">' +
        '<div class="load-error-icon" aria-hidden="true">' +
          '<svg width="36" height="36" viewBox="0 0 36 36" fill="none">' +
            '<circle cx="18" cy="18" r="15" stroke="currentColor" stroke-width="1.4" opacity="0.45"/>' +
            '<path d="M18 10v8.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>' +
            '<circle cx="18" cy="24.5" r="1.3" fill="currentColor"/>' +
          '</svg>' +
        '</div>' +
        '<p class="load-error-text">数据加载失败</p>' +
        '<button class="retry-btn" type="button">重试</button>' +
      '</div>';
    container.classList.remove("section-empty", "has-scores", "has-trending", "has-skeleton");
    var btn = container.querySelector(".retry-btn");
    if (btn && typeof onRetry === "function") {
      btn.addEventListener("click", onRetry);
    }
  }

  // ===== 状态汇报 =====
  function reportLoad(key, data) {
    if (!state[key]) return;
    state[key].status = "loaded";
    state[key].data = Array.isArray(data) ? data : [];
    updateOverview();
    updateStatus();
  }

  function reportFail(key) {
    if (!state[key]) return;
    state[key].status = "failed";
    state[key].data = [];
    updateOverview();
    updateStatus();
  }

  // ===== 概览统计聚合 =====
  function updateOverview() {
    var scoresData = state.scores.data || [];
    var trendingData = state.trending.data || [];
    var picksData = state.picks.data || [];

    if (els.statScores && scoresData.length) {
      els.statScores.innerHTML = "<span>" + scoresData.length + "</span>";
    }
    if (els.statTrending && trendingData.length) {
      els.statTrending.innerHTML = "<span>" + trendingData.length + "</span>";
    }
    if (els.statTracked && (scoresData.length || trendingData.length || picksData.length)) {
      var seen = {};
      var total = 0;
      var all = scoresData.concat(trendingData).concat(picksData);
      for (var i = 0; i < all.length; i++) {
        var r = all[i].repo || {};
        var fn = r.full_name || r.name || null;
        if (fn && !seen[fn]) { seen[fn] = 1; total++; }
      }
      els.statTracked.innerHTML = "<span>" + total + "</span>";
    }
  }

  // ===== 状态条联动 =====
  function updateStatus() {
    var allDone = KEYS.every(function (k) { return state[k].status !== "pending"; });
    if (!allDone || !els.statusText) return;

    var anySuccess = KEYS.some(function (k) { return state[k].status === "loaded"; });
    if (anySuccess) {
      els.statusText.textContent = "本周报已发布";
      if (els.statusTime) els.statusTime.textContent = formatWeekRange();
      if (els.statusPulse) els.statusPulse.classList.add("pulse-done");
      if (els.statusRow) els.statusRow.classList.add("status-done");
    } else {
      els.statusText.textContent = "数据加载失败";
      if (els.statusTime) els.statusTime.textContent = "请稍后重试";
    }
  }

  // ===== 本周日期范围（周一 ~ 周日，MM.DD — MM.DD） =====
  function formatWeekRange() {
    var now = new Date();
    var day = now.getDay() || 7;
    var monday = new Date(now);
    monday.setDate(now.getDate() - day + 1);
    var sunday = new Date(monday);
    sunday.setDate(monday.getDate() + 6);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return p(monday.getMonth() + 1) + "." + p(monday.getDate()) +
           " — " + p(sunday.getMonth() + 1) + "." + p(sunday.getDate());
  }

  // ===== 暴露 API =====
  window.StarRadar = {
    renderSkeleton: renderSkeleton,
    renderError: renderError,
    reportLoad: reportLoad,
    reportFail: reportFail,
  };

  console.log("StarRadar · 协调器已加载");
})();
