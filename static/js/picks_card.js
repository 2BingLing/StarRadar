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
      if (window.StarRadar) window.StarRadar.reportFail("picks");
      return;
    }

    if (window.StarRadar && window.StarRadar.renderSkeleton) {
      window.StarRadar.renderSkeleton(container, "picks");
    }

    if (!window.fetch) {
      handleFail(container);
      return;
    }

    fetch("data/picks.json", { cache: "no-cache" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (Array.isArray(data) && data.length) {
          window.StarRadarScores.renderScoreBoard(data, container);
          if (window.StarRadar) window.StarRadar.reportLoad("picks", data);
        } else {
          handleFail(container);
        }
      })
      .catch(function (err) {
        console.warn("[StarRadar] picks.json 加载失败:", err);
        handleFail(container);
      });
  }

  function handleFail(container) {
    if (window.StarRadar) {
      window.StarRadar.renderError(container, function () { init(); });
      window.StarRadar.reportFail("picks");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
