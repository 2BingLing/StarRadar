// StarRadar · 潜力评分卡片渲染
// 纯 SVG 五边形雷达图 + 卡片动态生成（不依赖外部库）
// 数据格式：compute_potential_scores() 返回的 [{repo, score}, ...]

(function () {
  "use strict";

  // ===== 维度配置（顺序决定雷达图顶点位置，从顶部顺时针） =====
  var RADAR_DIMS = [
    { key: "vel",    label: "速度" },
    { key: "acc",    label: "加速度" },
    { key: "health", label: "健康" },
    { key: "fresh",  label: "新鲜" },
    { key: "signal", label: "信号" },
  ];

  // ===== 增长阶段中文标签 =====
  var STAGE_LABELS = {
    early:     "早期",
    mid_early: "中早期",
    mid_late:  "中后期",
    late:      "后期",
    saturated: "已饱和",
  };

  // ===== 工具：数字格式化（75200 -> 75.2k） =====
  function formatCount(n) {
    n = Number(n) || 0;
    if (n >= 1000) {
      var v = (n / 1000).toFixed(1);
      // 去掉末尾 .0
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

  // ===== 雷达图顶点坐标（angleDeg: 0° 指向正上方，顺时针递增） =====
  function radarPoint(cx, cy, r, angleDeg) {
    var rad = (angleDeg - 90) * Math.PI / 180;
    return {
      x: (cx + r * Math.cos(rad)).toFixed(2),
      y: (cy + r * Math.sin(rad)).toFixed(2),
    };
  }

  // ===== 渲染纯 SVG 五边形雷达图 =====
  // breakdown: {vel, acc, health, fresh, signal} 均为 0-100
  // size: viewBox 尺寸（实际显示尺寸由 CSS 控制）
  function renderRadar(breakdown, size) {
    size = size || 132;
    var cx = size / 2;
    var cy = size / 2;
    var R = size * 0.34;            // 数据最大半径
    var labelR = R + size * 0.11;   // 标签半径
    var angles = [];
    for (var i = 0; i < RADAR_DIMS.length; i++) angles.push(i * 72);

    // 3 层同心网格五边形
    var gridLayers = "";
    var scales = [0.33, 0.66, 1.0];
    for (var s = 0; s < scales.length; s++) {
      var pts = [];
      for (var a = 0; a < angles.length; a++) {
        var p = radarPoint(cx, cy, R * scales[s], angles[a]);
        pts.push(p.x + "," + p.y);
      }
      gridLayers += '<polygon class="radar-grid" points="' + pts.join(" ") + '"/>';
    }

    // 5 条轴线（从中心到顶点）
    var axes = "";
    for (var a2 = 0; a2 < angles.length; a2++) {
      var pa = radarPoint(cx, cy, R, angles[a2]);
      axes += '<line class="radar-axis" x1="' + cx + '" y1="' + cy +
              '" x2="' + pa.x + '" y2="' + pa.y + '"/>';
    }

    // 数据多边形顶点
    var dataPoints = [];
    for (var d = 0; d < RADAR_DIMS.length; d++) {
      var key = RADAR_DIMS[d].key;
      var score = Math.max(0, Math.min(100, Number(breakdown[key]) || 0));
      var r = R * (score / 100);
      dataPoints.push(radarPoint(cx, cy, r, angles[d]));
    }
    var dataPolygon = dataPoints.map(function (p) { return p.x + "," + p.y; }).join(" ");

    // 数据顶点圆点
    var dataDots = "";
    for (var dd = 0; dd < dataPoints.length; dd++) {
      dataDots += '<circle class="radar-dot" cx="' + dataPoints[dd].x +
                  '" cy="' + dataPoints[dd].y + '" r="2.6"/>';
    }

    // 维度标签
    var labels = "";
    for (var l = 0; l < RADAR_DIMS.length; l++) {
      var pl = radarPoint(cx, cy, labelR, angles[l]);
      labels += '<text class="radar-label" x="' + pl.x + '" y="' + pl.y +
                '" text-anchor="middle" dominant-baseline="middle">' +
                RADAR_DIMS[l].label + '</text>';
    }

    return (
      '<svg class="score-radar" viewBox="-10 -4 ' + (size + 20) + ' ' + (size + 8) +
      '" aria-hidden="true">' +
      gridLayers +
      axes +
      '<polygon class="radar-data" points="' + dataPolygon + '"/>' +
      dataDots +
      labels +
      '</svg>'
    );
  }

  // ===== 渲染单张卡片 =====
  function renderCard(item, index) {
    var repo = item.repo || {};
    var score = item.score || {};
    var rank = index + 1;
    var stageLabel = STAGE_LABELS[score.stage] || score.stage || "未知";
    var stageClass = "stage-" + (score.stage || "unknown");
    var breakdown = score.breakdown || {};
    var delay = (0.45 + index * 0.08).toFixed(2);

    var nameHtml = repo.full_name
      ? '<a class="score-name" href="' + escapeHtml(repo.html_url || "#") +
        '" target="_blank" rel="noopener">' + escapeHtml(repo.full_name) + '</a>'
      : '<span class="score-name">' + escapeHtml(repo.name || "未知项目") + '</span>';

    var langHtml = repo.language
      ? '<span class="score-lang">' + escapeHtml(repo.language) + '</span>'
      : '';

    var confHtml = (score.confidence != null)
      ? '<span class="meta-conf" title="数据置信度">置信 ' +
        Math.round(score.confidence * 100) + '%</span>'
      : '';

    var explainHtml = score.explanation
      ? '<p class="score-explain">' + escapeHtml(score.explanation) + '</p>'
      : '';

    return (
      '<article class="score-card rank-' + rank + '" style="--card-delay:' + delay + 's">' +
        '<div class="score-rank">' +
          '<span class="rank-badge">' + rank + '</span>' +
        '</div>' +
        '<div class="score-head">' + nameHtml + langHtml + '</div>' +
        '<div class="score-meta">' +
          '<span class="meta-stars" title="Stars">★ ' + formatCount(repo.stars) + '</span>' +
          '<span class="meta-forks" title="Forks">⑂ ' + formatCount(repo.forks) + '</span>' +
          '<span class="meta-stage ' + stageClass + '">' + stageLabel + '</span>' +
          confHtml +
        '</div>' +
        explainHtml +
        '<div class="score-side">' +
          '<div class="score-num">' +
            '<span class="score-num-value">' + (Number(score.score) || 0).toFixed(1) + '</span>' +
            '<span class="score-num-label">潜力分</span>' +
          '</div>' +
          renderRadar(breakdown, 132) +
        '</div>' +
      '</article>'
    );
  }

  // ===== 渲染榜单 =====
  function renderScoreBoard(data, container) {
    if (!container) return;
    var items = (Array.isArray(data) ? data : []).slice(0, 5);
    if (!items.length) {
      container.innerHTML = '<p class="score-empty">暂无潜力评分数据</p>';
      return;
    }
    var html = "";
    for (var i = 0; i < items.length; i++) {
      html += renderCard(items[i], i);
    }
    container.innerHTML = html;
    container.classList.remove("section-empty");
    container.classList.add("has-scores");
  }

  // ===== 自动初始化：渲染到"潜力雷达"栏目 =====
  function init() {
    var container = document.querySelector(".section-accent .section-body");
    if (!container) return;
    var data = window.SAMPLE_SCORES || [];
    renderScoreBoard(data, container);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // 暴露接口（便于后续接入真实数据）
  window.StarRadarScores = {
    renderScoreBoard: renderScoreBoard,
    renderRadar: renderRadar,
    renderCard: renderCard,
    STAGE_LABELS: STAGE_LABELS,
  };
})();
