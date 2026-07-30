// StarRadar · 热门榜卡片渲染
// 紧凑列表式卡片（无雷达图），与潜力雷达视觉区分
// 数据格式：[{repo: {...}}, ...]

(function () {
  "use strict";

  // ===== 内联 SVG 图标（与 score_card.js 一致） =====
  var ICON = {
    star: '<svg class="meta-icon" width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M8 1.5l1.96 4.02 4.43.64-3.2 3.12.76 4.4L8 11.6l-3.95 2.08.76-4.4-3.2-3.12 4.43-.64L8 1.5z" fill="currentColor"/></svg>',
    fork: '<svg class="meta-icon" width="13" height="13" viewBox="0 0 16 16" fill="none"><circle cx="4" cy="3" r="1.4" stroke="currentColor" stroke-width="1.3"/><circle cx="12" cy="3" r="1.4" stroke="currentColor" stroke-width="1.3"/><circle cx="8" cy="13" r="1.4" stroke="currentColor" stroke-width="1.3"/><path d="M4 4.4v2.6c0 .8.4 1.8 1.8 1.8M12 4.4v2.6c0 .8-.4 1.8-1.8 1.8M8 9.2v2.4" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round"/></svg>',
    link: '<svg class="meta-icon" width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M6.5 9.5L9.5 6.5M6 10L4.5 11.5a2.5 2.5 0 0 1-3.5-3.5L2.5 6.5a2.5 2.5 0 0 1 3.5 0M10 6l1.5-1.5a2.5 2.5 0 0 1 3.5 3.5L13.5 9.5a2.5 2.5 0 0 1-3.5 0" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  // ===== 工具：数字格式化（75200 -> 75.2k） =====
  function formatCount(n) {
    n = Number(n) || 0;
    if (n >= 1000) {
      var v = (n / 1000).toFixed(1);
      if (v.indexOf(".0", v.length - 2) !== -1) v = v.slice(0, -2);
      return v + "k";
    }
    return String(n);
  }

  // ===== 工具：HTML 转义 =====
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ===== 渲染单行热门榜卡片 =====
  function renderTrendingRow(item, index) {
    var repo = item.repo || {};
    var rank = index + 1;
    var delay = (0.3 + index * 0.04).toFixed(2);

    var nameHtml = repo.full_name
      ? '<a class="trend-name" href="' + escapeHtml(repo.html_url || "#") +
        '" target="_blank" rel="noopener">' + escapeHtml(repo.full_name) + '</a>'
      : '<span class="trend-name">' + escapeHtml(repo.name || "未知项目") + '</span>';

    var langHtml = repo.language
      ? '<span class="trend-lang">' + escapeHtml(repo.language) + '</span>'
      : '';

    var descHtml = repo.description
      ? '<p class="trend-desc">' + escapeHtml(repo.description) + '</p>'
      : '<p class="trend-desc trend-desc-empty">（暂无描述）</p>';

    // 主题标签（最多 2 个）
    var topicsHtml = "";
    if (repo.topics && repo.topics.length) {
      var topics = repo.topics.slice(0, 2);
      topicsHtml = '<div class="trend-topics">' +
        topics.map(function (t) {
          return '<span class="topic-tag">' + escapeHtml(t) + '</span>';
        }).join("") + '</div>';
    }

    return (
      '<article class="trend-row rank-' + rank + '" style="--row-delay:' + delay + 's">' +
        '<div class="trend-rank"><span class="trend-rank-num">' + rank + "</span></div>" +
        '<div class="trend-main">' +
          '<div class="trend-head">' + nameHtml + langHtml + "</div>" +
          descHtml +
          topicsHtml +
        "</div>" +
        '<div class="trend-stats">' +
          '<span class="meta-item">' + ICON.star + formatCount(repo.stars) + "</span>" +
          '<span class="meta-item">' + ICON.fork + formatCount(repo.forks) + "</span>" +
        "</div>" +
      "</article>"
    );
  }

  // ===== 渲染热门榜列表 =====
  function renderTrendingBoard(data, container) {
    if (!container) return;
    var items = (Array.isArray(data) ? data : []).slice(0, 20);
    if (!items.length) {
      container.innerHTML = '<p class="score-empty">暂无热门榜数据</p>';
      return;
    }
    var html = "";
    for (var i = 0; i < items.length; i++) {
      html += renderTrendingRow(items[i], i);
    }
    container.innerHTML = html;
    container.classList.remove("section-empty");
    container.classList.add("has-trending");
  }

  // ===== 自动初始化：渲染到"热门榜"栏目 =====
  // 流程：骨架屏 → fetch data/trending.json → 成功渲染+reportLoad；
  //       失败/空 → renderError+reportFail（带重试）
  function init() {
    var container = document.querySelector("#hot .section-body");
    if (!container) return;

    if (window.StarRadar && window.StarRadar.renderSkeleton) {
      window.StarRadar.renderSkeleton(container, "trending");
    }

    if (!window.fetch) {
      handleFail(container);
      return;
    }

    fetch("data/trending.json", { cache: "no-cache" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (Array.isArray(data) && data.length) {
          renderTrendingBoard(data, container);
          if (window.StarRadar) window.StarRadar.reportLoad("trending", data);
        } else {
          handleFail(container);
        }
      })
      .catch(function (err) {
        console.warn("[StarRadar] trending.json 加载失败:", err);
        handleFail(container);
      });
  }

  function handleFail(container) {
    if (window.StarRadar) {
      window.StarRadar.renderError(container, function () { init(); });
      window.StarRadar.reportFail("trending");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // 暴露接口
  window.StarRadarTrending = {
    renderTrendingBoard: renderTrendingBoard,
    renderTrendingRow: renderTrendingRow,
  };
})();
