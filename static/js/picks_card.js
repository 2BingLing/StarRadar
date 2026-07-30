// StarRadar · 为你精选卡片渲染
// 复用 score_card.js 的 renderCard（数据格式与 scores.json 一致）
// 数据格式：[{repo: {...}, score: {...}}, ...]

(function () {
  "use strict";

  function init() {
    var container = document.querySelector("#foryou .section-body");
    if (!container) return;

    if (!window.StarRadarScores || !window.StarRadarScores.renderScoreBoard) {
      console.warn("[StarRadar] score_card.js 未加载，picks 无法渲染");
      return;
    }

    if (window.fetch) {
      fetch("data/picks.json", { cache: "no-cache" })
        .then(function (resp) {
          if (!resp.ok) throw new Error("HTTP " + resp.status);
          return resp.json();
        })
        .then(function (data) {
          if (Array.isArray(data) && data.length) {
            window.StarRadarScores.renderScoreBoard(data, container);
          }
        })
        .catch(function (err) {
          console.warn("[StarRadar] picks.json 加载失败:", err);
        });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
