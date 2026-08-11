// StarRadar · 个人特化雷达页面逻辑（版本 2 · 本地后端）
// 职责：状态检测（登录/LLM/数据）→ 拉取 /api/personal/scores → 渲染专属卡片
//       + 登录 token 上交给本地后端（/api/gh_token）供 --personal 管道使用
(function () {
  "use strict";

  var cardsEl = document.querySelector("#pCards");
  var toastEl = document.querySelector("#toast");

  function notify(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(window.__pt);
    window.__pt = setTimeout(function () { toastEl.classList.remove("show"); }, 2200);
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function starNum(n) {
    n = Number(n) || 0;
    if (n >= 1000) {
      var v = (n / 1000).toFixed(1);
      if (v.indexOf(".0", v.length - 2) !== -1) v = v.slice(0, -2);
      return v + "k";
    }
    return String(n);
  }

  function stageLabel(stage) {
    return {
      early: "早期", mid_early: "中早期", mid_late: "中后期",
      late: "后期", saturated: "已饱和",
    }[stage] || (stage || "");
  }

  function setDot(id, ok) {
    var el = document.querySelector(id);
    if (el) { el.className = "p-dot " + (ok ? "ok" : "no"); }
  }

  // ===== 状态 =====
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
        if (!d.logged_in) parts.push("未登录：点击「通过 GitHub 登录」，授权后 token 将交给本地后端。");
        if (!d.llm_configured) parts.push("LLM 未配置：在 .env 填入 LLM_API_KEY（DeepSeek 等），解读才会个性化。");
        if (d.logged_in && !d.data_exists) parts.push("已就绪：运行 python src/main.py --personal 生成你的专属雷达。");
        if (d.logged_in && d.data_exists) parts.push("你的专属雷达已就绪（" + (d.login || "") + "）。");
        hint.textContent = parts.join(" ");
      })
      .catch(function () {
        notify("未检测到本地服务，请先运行 python src/main.py --serve");
      });
  }

  // ===== 登录：token 上交给本地后端 =====
  function handoffToken() {
    var gh = window.GH && window.GH.getUserInfo && window.GH.getUserInfo();
    if (!gh || !gh.login) return;
    var token = null;
    try { token = localStorage.getItem("starradar:gh_token"); } catch (e) {}
    if (!token) return;
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

  // ===== 渲染卡片 =====
  function renderCard(item) {
    var r = item.repo || {};
    var s = item.score || {};
    var full = r.full_name || (r.owner + "/" + r.name) || "";
    var gh = r.html_url || ("https://github.com/" + full);
    var tags = (r.topics || []).slice(0, 4).map(function (t) {
      return '<i>' + escapeHtml(t) + "</i>";
    }).join("");
    var bd = s.breakdown || {};
    var dims = [["速", bd.vel], ["加", bd.acc], ["健", bd.health], ["鲜", bd.fresh], ["信", bd.signal]]
      .filter(function (d) { return d[1] != null; })
      .map(function (d) {
        return '<span class="pd"><b>' + d[0] + "</b><i><em style='width:" + d[1] + "%'></em></i></span>";
      }).join("");
    return (
      '<article class="card">' +
        '<div class="card-head">' +
          '<h3>' + escapeHtml(full) + "</h3>" +
          '<span class="stage-chip">' + escapeHtml(stageLabel(s.stage)) + "</span>" +
        "</div>" +
        '<div class="repo-line">' + escapeHtml(r.owner || "") + " / " + escapeHtml(r.name || "") +
          " · " + escapeHtml(r.language || "—") + "</div>" +
        (tags ? '<div class="card-tags">' + tags + "</div>" : "") +
        '<p class="card-desc">' + escapeHtml(r.description || "") + "</p>" +
        '<p class="card-explain">' + escapeHtml(s.explanation || "") + "</p>" +
        '<div class="p-dims">' + dims + "</div>" +
        '<div class="card-foot">' +
          '<span>⭐ ' + starNum(r.stars) + "</span>" +
          '<span class="p-score">潜力 ' + Number(s.score || 0).toFixed(0) + "</span>" +
          '<span class="sp"></span>' +
          '<span id="pOp-' + escapeHtml(full.replace(/\//g, "_")) + '"></span>' +
          '<a class="op" href="' + escapeHtml(gh) + '" target="_blank" rel="noopener" title="在 GitHub 打开">↗</a>' +
        "</div>" +
      "</article>"
    );
  }

  function loadData() {
    fetch("/api/personal/scores", { cache: "no-store" })
      .then(function (r) {
        if (r.status === 404) return null;
        if (!r.ok) throw new Error("load failed");
        return r.json();
      })
      .then(function (d) {
        if (!d || !Array.isArray(d.items) || !d.items.length) {
          renderEmpty();
          return;
        }
        cardsEl.innerHTML = "";
        d.items.forEach(function (item) {
          cardsEl.insertAdjacentHTML("beforeend", renderCard(item));
          var full = item.repo && item.repo.full_name;
          if (full && window.GH && window.GH.actionsHTML) {
            var anchor = document.querySelector("#pOp-" + full.replace(/\//g, "_"));
            if (anchor) {
              anchor.insertAdjacentHTML("beforeend", window.GH.actionsHTML({ repo: item.repo }));
            }
          }
        });
      })
      .catch(function () {
        notify("个人数据加载失败（本地服务未启动？）");
      });
  }

  function renderEmpty() {
    cardsEl.innerHTML =
      '<div class="p-empty">' +
        "<b>还没有你的专属雷达。</b><br>" +
        "完成两步即可：<br>" +
        "1. 点击上方「通过 GitHub 登录」（跳转授权一次）<br>" +
        "2. 在项目目录运行 <code>python src/main.py --personal</code><br>" +
        '<span class="p-gen">管道会拉取你的加星 / 仓库主题 → 画像驱动搜索 → 为你解读 → 生成数据。<br>' +
        "之后每次打开本页都是为你定制的内容，且已看项目自动排除，每天不重样。</span>" +
      "</div>";
  }

  // ===== 事件 =====
  document.querySelector("#pRefresh").addEventListener("click", function () {
    refreshStatus();
    loadData();
  });
  document.querySelector("#pGoLogin").addEventListener("click", function () {
    if (window.GH) window.GH.openPanel();
  });

  // 登录成功（github.js 的 sr:gh-login）→ token 交给本地后端
  window.addEventListener("sr:gh-login", function () {
    setTimeout(handoffToken, 600);
    refreshStatus();
  });

  // ===== 启动 =====
  if (window.GH && window.GH.onChange) {
    window.GH.onChange(function () {
      refreshStatus();
      if (window.GH.isConnected()) handoffToken();
    });
  }
  refreshStatus();
  loadData();
})();
