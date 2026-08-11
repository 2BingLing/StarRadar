// StarRadar · GitHub 原生集成（P4）
// 职责：连接 GitHub（PAT / OAuth Device Flow）→ 一键加星 / Fork / 克隆 / 随行笔记
//       → 状态本地恢复（token 仅存 localStorage，不经过任何第三方）
// 依赖：无。app.js 渲染卡片/详情时调用 GH.actionsHTML(item)，并监听 sr:gh-action 记行为日志。
(function () {
  "use strict";

  var TOKEN_KEY = "starradar:gh_token";
  var USER_KEY = "starradar:gh_user";
  var STAR_KEY = "starradar:gh_starred";
  var NOTES_KEY = "starradar:notes";

  // ===== 可配置：OAuth App 注册后填入 client_id，解锁 Device Flow 方式 =====
  var CLIENT_KEY = "starradar:gh_client_id";
  var OAUTH_CLIENT_ID = "";
  var CORS_PROXY = "https://corsproxy.io/?";
  function getClientId() {
    try { return localStorage.getItem(CLIENT_KEY) || ""; } catch (e) { return ""; }
  }
  function saveClientId(id) {
    try {
      if (id) localStorage.setItem(CLIENT_KEY, id);
      else localStorage.removeItem(CLIENT_KEY);
    } catch (e) {}
    OAUTH_CLIENT_ID = id || "";
  }

  var token = "";
  var user = null;      // {login, avatar_url}
  var starred = {};     // full_name -> true
  var notes = {};       // full_name -> {text, ts}
  var listeners = [];
  var ghPanel, ghBody, ghFoot, ghConnBtn, notePanel;
  var noteTarget = "";
  var ghConnInit = false;

  // ===== 工具 =====
  function lsGet(k, d) {
    try { var v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; }
  }
  function lsSet(k, v) {
    try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {}
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function notify(msg) {
    var t = document.querySelector("#toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(window.__tt);
    window.__tt = setTimeout(function () { t.classList.remove("show"); }, 1900);
  }
  function githubLogoSVG() {
    return '<svg viewBox="0 0 24 24" fill="currentColor" width="15" height="15"><path d="M12 2C6.5 2 2 6.6 2 12.2c0 4.5 2.9 8.3 6.8 9.7.5.1.7-.2.7-.5v-1.7c-2.8.6-3.4-1.2-3.4-1.2-.5-1.2-1.1-1.5-1.1-1.5-.9-.6.1-.6.1-.6 1 .1 1.5 1 1.5 1 .9 1.6 2.4 1.1 3 .9.1-.7.4-1.1.6-1.4-2.2-.3-4.6-1.1-4.6-5 0-1.1.4-2 1-2.7-.1-.3-.4-1.3.1-2.7 0 0 .8-.3 2.7 1a9.4 9.4 0 0 1 5 0c1.9-1.3 2.7-1 2.7-1 .5 1.4.2 2.4.1 2.7.6.7 1 1.6 1 2.7 0 3.9-2.4 4.7-4.6 5 .4.3.7.9.7 1.9v2.8c0 .3.2.6.7.5 4-1.4 6.8-5.2 6.8-9.7C22 6.6 17.5 2 12 2z"/></svg>';
  }

  // ===== 状态 =====
  function isConnected() { return !!token; }
  function getUserInfo() { return user; }
  function isStarred(fullName) { return !!starred[fullName]; }
  function getNote(fullName) { return notes[fullName] || null; }
  function hasNote(fullName) { return !!notes[fullName]; }
  function cloneCmd(fullName) { return "git clone https://github.com/" + fullName + ".git"; }
  function onChange(cb) { listeners.push(cb); }
  function emit() {
    listeners.forEach(function (cb) { try { cb(); } catch (e) {} });
    updateAllButtons();
    renderConnBtn();
  }

  // ===== 存储 =====
  function saveToken(t) {
    token = t || "";
    try {
      if (t) localStorage.setItem(TOKEN_KEY, t);
      else localStorage.removeItem(TOKEN_KEY);
    } catch (e) {}
  }
  function saveUser(u) {
    user = u || null;
    try {
      if (u) localStorage.setItem(USER_KEY, JSON.stringify(u));
      else localStorage.removeItem(USER_KEY);
    } catch (e) {}
  }
  function saveStarred() { lsSet(STAR_KEY, starred); }
  function saveNotes() { lsSet(NOTES_KEY, notes); }

  // ===== GitHub API（api.github.com 支持 CORS，token 直连） =====
  function api(path, method, body) {
    return fetch("https://api.github.com" + path, {
      method: method || "GET",
      headers: {
        "Authorization": "token " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) {
      if (r.status === 204) return null;
      return r.json().catch(function () { return null; });
    });
  }

  function verifyToken(pat) {
    return fetch("https://api.github.com/user", {
      headers: { "Authorization": "token " + pat, "Accept": "application/vnd.github+json" },
    }).then(function (r) {
      if (r.status === 401 || r.status === 403) throw new Error("invalid");
      return r.json();
    });
  }

  // 连接成功后拉取我的全部星标（分页，最多 3 页），用于按钮状态恢复
  function loadStarredAll() {
    var out = {};
    var page = 1;
    function next() {
      return api("/user/starred?per_page=100&sort=created&page=" + page).then(function (repos) {
        if (!Array.isArray(repos)) return;
        repos.forEach(function (r) { if (r && r.full_name) out[r.full_name] = true; });
        if (repos.length === 100 && page < 3) { page++; return next(); }
      });
    }
    return next().then(function () { starred = out; saveStarred(); emit(); });
  }

  function starRepo(fullName) {
    return api("/user/starred/" + encodeURIComponent(fullName), "PUT").then(function () {
      starred[fullName] = true;
      saveStarred();
      emit();
      notify("已加星 " + fullName);
      fireAction(fullName, "star");
    });
  }
  function unstarRepo(fullName) {
    return api("/user/starred/" + encodeURIComponent(fullName), "DELETE").then(function () {
      delete starred[fullName];
      saveStarred();
      emit();
      notify("已取消加星");
    });
  }
  function forkRepo(fullName) {
    return api("/repos/" + encodeURIComponent(fullName) + "/forks", "POST").then(function (data) {
      if (data && data.html_url) return data;
      throw new Error("fork-failed");
    });
  }

  // ===== 行为事件（app.js 监听并记入画像日志） =====
  function fireAction(repo, action) {
    try {
      window.dispatchEvent(new CustomEvent("sr:gh-action", { detail: { repo: repo, action: action } }));
    } catch (e) {}
  }

  // ===== 笔记 =====
  function setNote(fullName, text) {
    if (text && text.trim()) notes[fullName] = { text: text.trim(), ts: new Date().toISOString() };
    else delete notes[fullName];
    saveNotes();
    emit();
  }

  // ===== 操作栏 HTML（卡片 / 详情页复用） =====
  function actionsHTML(item) {
    var r = item.repo || {};
    var full = r.full_name || (r.owner + "/" + r.name) || "";
    var gh = r.html_url || ("https://github.com/" + full);
    var s = starred[full] ? " on" : "";
    var n = notes[full] ? " has-note" : "";
    var forkTitle = isConnected() ? "Fork 到你的账号" : "连接 GitHub 后可用";
    return (
      '<div class="opbar" data-full="' + escapeHtml(full) + '">' +
        '<a class="op" href="' + escapeHtml(gh) + '" target="_blank" rel="noopener" title="在 GitHub 打开">' +
          githubLogoSVG() + "</a>" +
        '<button class="op gh-star' + s + '" title="加星 / 取消加星" aria-label="加星">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5-5.9-3.1-5.9 3.1 1.2-6.5L2.5 9.4l6.6-.9z"/></svg></button>' +
        '<button class="op gh-fork" title="' + forkTitle + '" aria-label="Fork">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="6" cy="4" r="2"/><circle cx="18" cy="4" r="2"/><circle cx="18" cy="20" r="2"/><path d="M6 6v2a3 3 0 0 0 3 3h6a3 3 0 0 1 3 3v2"/></svg></button>' +
        '<button class="op gh-clone" title="复制 git clone 命令" aria-label="克隆">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg></button>' +
        '<button class="op gh-note' + n + '" title="随行笔记" aria-label="笔记">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg></button>' +
      "</div>"
    );
  }

  // ===== 全局按钮状态刷新（加星/笔记变化后） =====
  function updateAllButtons() {
    Array.prototype.forEach.call(document.querySelectorAll(".opbar"), function (bar) {
      var full = bar.dataset.full;
      if (!full) return;
      var st = bar.querySelector(".gh-star");
      if (st) st.classList.toggle("on", !!starred[full]);
      var nt = bar.querySelector(".gh-note");
      if (nt) nt.classList.toggle("has-note", !!notes[full]);
    });
  }

  // ===== 顶部连接按钮 =====
  function renderConnBtn() {
    if (!ghConnBtn) return;
    if (isConnected() && user) {
      ghConnBtn.innerHTML = user.avatar_url
        ? '<img src="' + escapeHtml(user.avatar_url) + '" alt="" width="20" height="20">'
        : "<span>" + escapeHtml((user.login || "G").charAt(0).toUpperCase()) + "</span>";
      ghConnBtn.classList.add("connected");
      ghConnBtn.title = user.login + " · 点击管理连接";
    } else {
      ghConnBtn.innerHTML = githubLogoSVG();
      ghConnBtn.classList.remove("connected");
      ghConnBtn.title = "连接 GitHub，解锁一键加星 / Fork";
    }
  }

  // ===== 连接面板 =====
  function openPanel() {
    ghPanel.classList.add("open");
    ghPanel.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    renderPanel();
  }
  function closePanel() {
    ghPanel.classList.remove("open");
    ghPanel.setAttribute("aria-hidden", "true");
    if (!notePanel.classList.contains("open")) document.body.style.overflow = "";
  }

  function renderPanel() {
    if (isConnected() && user) {
      ghTitle.textContent = "已连接 GitHub";
      ghBody.innerHTML =
        '<div class="gh-user">' +
          (user.avatar_url ? '<img src="' + escapeHtml(user.avatar_url) + '" alt="" width="44" height="44">' :
            '<span class="gh-avatar-letter">' + escapeHtml((user.login || "G").charAt(0).toUpperCase()) + "</span>") +
          "<div><strong>" + escapeHtml(user.login) + "</strong>" +
          "<small>加星 / Fork 将以你的身份操作</small></div></div>";
      ghFoot.innerHTML = '<button class="gh-btn danger" id="ghDisconnect">断开连接</button>';
      var dc = document.querySelector("#ghDisconnect");
      if (dc) dc.addEventListener("click", function () {
        saveToken(""); saveUser(null); starred = {}; saveStarred();
        closePanel();
        notify("已断开 GitHub 连接");
        emit();
      });
      return;
    }
    ghTitle.textContent = "连接 GitHub";
    var clientId = getClientId();
    ghBody.innerHTML =
      '<button class="gh-btn primary gh-big" id="ghDevice">通过 GitHub 登录</button>' +
      "<div id='ghDeviceBody'></div>" +
      (clientId ? "" :
        '<details class="gh-cid">' +
          '<summary>首次使用？查看两种登录方式配置</summary>' +
          '<p class="gh-tip"><b>方式一 · 跳转登录（推荐，零输入）</b><br>' +
            "在项目根目录 <code>.env</code> 中添加：<br>" +
            "<code>GH_OAUTH_CLIENT_ID=你的Client_ID</code><br>" +
            "<code>GH_OAUTH_CLIENT_SECRET=你的Client_Secret</code><br>" +
            "创建 OAuth App 时回调 URL 填 <code>http://127.0.0.1:8970/api/oauth/callback</code>（本地 server 模式，点登录直接跳 GitHub 授权页自动回跳）。</p>" +
          '<p class="gh-tip"><b>方式二 · 设备流</b>（GitHub Pages 静态页）：只需 Client ID，填入下方：</p>' +
          '<div class="gh-cid-row"><input id="ghClientId" placeholder="Client ID（如 Iv1.xxxx）" autocomplete="off">' +
          '<button class="gh-btn ghost" id="ghSaveCid">保存</button></div>' +
        "</details>") +
      '<div class="gh-divider">或使用 Token</div>' +
      '<label class="gh-field">Personal Access Token' +
        '<input id="ghPat" type="password" placeholder="ghp_…" autocomplete="off">' +
      "</label>" +
      '<p class="gh-tip">生成方式：GitHub → Settings → Developer settings → <b>Personal access tokens</b> → 勾选 <b>public_repo</b> 和 <b>read:user</b>。Token 仅保存在本机浏览器。</p>';
    ghFoot.innerHTML = '<button class="gh-btn primary" id="ghConnect">验证并连接</button>';
    var saveCid = document.querySelector("#ghSaveCid");
    if (saveCid) saveCid.addEventListener("click", function () {
      var val = document.querySelector("#ghClientId").value.trim();
      if (!val) { notify("请粘贴 Client ID"); return; }
      saveClientId(val);
      notify("Client ID 已保存");
      renderPanel();
    });
    var dev = document.querySelector("#ghDevice");
    if (dev) dev.addEventListener("click", startOAuthOrDevice);
    document.querySelector("#ghConnect").addEventListener("click", function () {
      var pat = document.querySelector("#ghPat").value.trim();
      if (!pat) { notify("请先粘贴 Token"); return; }
      this.disabled = true;
      this.textContent = "验证中…";
      verifyToken(pat).then(function (u) {
        if (!u || !u.login) throw new Error("invalid");
        saveToken(pat);
        onGhLogin(u);
      }).catch(function () {
        notify("Token 无效，请检查后重试");
        var btn = document.querySelector("#ghConnect");
        if (btn) { btn.disabled = false; btn.textContent = "验证并连接"; }
      });
    });
    if (OAUTH_CLIENT_ID) {
      var dev = document.querySelector("#ghDevice");
      if (dev) dev.addEventListener("click", startDeviceFlow);
    }
  }

  // ===== GitHub OAuth 端点不支持 CORS → 优先同源代理（本地 server），降级公共 CORS 代理 =====
  function ghProxyFetch(url, opts) {
    return fetch(url, opts).then(function (r) {
      if (!r.ok && r.status === 404) throw { code: "no-backend" };  // GitHub Pages 无后端
      return r;
    }).catch(function (e) {
      if (e && e.code === "no-backend") {
        return fetch(CORS_PROXY + encodeURIComponent(url), opts);
      }
      throw e;
    });
  }

  function onGhLogin(u) {
    saveUser({ login: u.login, avatar_url: u.avatar_url || "" });
    emit();
    closePanel();
    notify("已连接 " + u.login);
    try {
      window.dispatchEvent(new CustomEvent("sr:gh-login",
        { detail: { login: u.login, avatar_url: u.avatar_url || "" } }));
    } catch (e) {}
    loadStarredAll().then(function () {
      notify("已连接 " + u.login + "，同步 " + Object.keys(starred).length + " 个星标");
    });
  }

  // ===== OAuth Device Flow（一键登录；client_id 公开值，token 只存本机） =====
  function startDeviceFlow() {
    var box = document.querySelector("#ghDeviceBody");
    if (!box) return;
    var clientId = getClientId();
    if (!clientId) {
      box.innerHTML = "<p class='gh-tip'>请先在上方展开「配置 OAuth Client ID」并粘贴你的 Client ID</p>";
      return;
    }
    box.innerHTML = "<p class='gh-tip'>正在获取授权码…</p>";
    ghProxyFetch("https://github.com/login/device/code", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ client_id: clientId, scope: "public_repo read:user" }),
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.device_code) throw new Error("device");
      var uri = d.verification_uri || "https://github.com/login/device";
      box.innerHTML =
        '<div class="gh-device">' +
          '<p>在 GitHub 输入授权码：</p>' +
          '<code>' + escapeHtml(d.user_code) + "</code>" +
          '<a class="gh-btn ghost" href="' + escapeHtml(uri) + '" target="_blank" rel="noopener">前往授权 ↗</a>' +
        "</div>";
      var tries = 0;
      var iv = setInterval(function () {
        ghProxyFetch("https://github.com/login/oauth/access_token", {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ client_id: clientId, device_code: d.device_code,
            grant_type: "urn:ietf:params:oauth:grant-type:device_code" }),
        }).then(function (r) { return r.json(); }).then(function (a) {
          if (a.access_token) {
            clearInterval(iv);
            saveToken(a.access_token);
            verifyToken(a.access_token).then(function (u) { onGhLogin(u); });
            return;
          }
          if (a.error === "authorization_pending") return;
          if (a.error === "slow_down") { tries--; return; }
          if (a.error === "access_denied") {
            clearInterval(iv);
            box.innerHTML = "<p class='gh-tip'>你取消了授权</p>";
          }
        }).catch(function () {});
        if (++tries > 300) clearInterval(iv);
      }, 5000);
    }).catch(function () {
      box.innerHTML = "<p class='gh-tip'>设备流请求失败（网络 / 代理不可用），可改用 Token 方式</p>";
    });
  }

  // ===== 跳转式登录（Authorization Code Flow，本地 server 场景） =====
  // 探测本地服务是否配置了 OAuth 凭据 → 已配置则整页跳 GitHub 授权页（自动回跳）；否则降级设备流
  function startOAuthOrDevice() {
    var box = document.querySelector("#ghDeviceBody");
    if (!box) return;
    box.innerHTML = "<p class='gh-tip'>正在检测登录方式…</p>";
    fetch("/api/oauth/start?probe=1").then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      if (d && d.configured) {
        window.location = "/api/oauth/start";
      } else {
        if (d) notify("本地服务未配置 OAuth 凭据，改用设备流");
        startDeviceFlow();
      }
    }).catch(function () {
      startDeviceFlow();
    });
  }

  // 跳转登录回跳后：/?gh_ticket=… → 一次性换 token → 连接 → 清 URL。返回是否有票。
  function handleOAuthTicket() {
    var m = location.search.match(/[?&]gh_ticket=([^&]+)/);
    if (!m) return false;
    var clean = function () {
      try { history.replaceState(null, "", location.pathname); } catch (e) {}
    };
    fetch("/api/oauth/ticket?t=" + encodeURIComponent(m[1]))
      .then(function (r) { if (!r.ok) throw new Error("ticket"); return r.json(); })
      .then(function (d) {
        if (!d || !d.token) throw new Error("no-token");
        saveToken(d.token);
        return verifyToken(d.token);
      })
      .then(function (u) {
        if (u && u.login) onGhLogin(u);
        else throw new Error("no-user");
      })
      .catch(function () { notify("跳转登录失败，请重试或改用 Token"); })
      .then(clean);
    return true;
  }

  // ===== 操作分发（document 委托，卡片 + 详情共用） =====
  function fullFromBtn(btn) {
    var bar = btn.closest(".opbar");
    return bar ? bar.dataset.full : "";
  }

  function handleOpClick(e) {
    var btn = e.target.closest(".op");
    if (!btn) return;
    e.stopPropagation();
    if (btn.tagName === "A") return; // 链接放行默认跳转
    e.preventDefault();
    var full = fullFromBtn(btn);
    if (!full) return;

    if (btn.classList.contains("gh-star")) {
      if (!isConnected()) { openPanel(); notify("请先连接 GitHub"); return; }
      if (isStarred(full)) {
        unstarRepo(full).catch(function () { notify("操作失败，请检查网络"); });
      } else {
        starRepo(full).catch(function () { notify("加星失败，请检查网络或 Token 权限"); });
      }
      return;
    }
    if (btn.classList.contains("gh-fork")) {
      if (!isConnected()) { openPanel(); notify("请先连接 GitHub"); return; }
      var forkURL = "https://github.com/" + full + "/forks";
      if (!window.confirm("Fork「" + full + "」到你的账号？")) return;
      notify("正在 Fork…");
      forkRepo(full).then(function (data) {
        fireAction(full, "fork");
        notify("Fork 成功");
        window.open(data.html_url, "_blank");
      }).catch(function (err) {
        if (err && err.message === "fork-failed") {
          // 422 → 可能已 fork 过：跳到我的 fork
          notify("你可能已经 Fork 过，正在打开你的副本");
          window.open("https://github.com/" + (user ? user.login : "") + "/" + full.split("/")[1], "_blank");
        } else {
          notify("Fork 失败，请检查网络");
        }
      });
      return;
    }
    if (btn.classList.contains("gh-clone")) {
      var cmd = cloneCmd(full);
      copyText(cmd);
      notify("已复制 " + cmd);
      fireAction(full, "clone");
      return;
    }
    if (btn.classList.contains("gh-note")) {
      openNoteEditor(full);
    }
  }

  function copyText(text) {
    function legacy() {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(ta);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(legacy);
    } else {
      legacy();
    }
  }

  // ===== 笔记编辑器 =====
  function openNoteEditor(fullName) {
    noteTarget = fullName;
    var n = notes[fullName] || { text: "", ts: null };
    document.querySelector("#noteTitle").textContent = "随行笔记 · " + fullName;
    document.querySelector("#noteText").value = n.text || "";
    document.querySelector("#noteDate").textContent = n.ts ? "上次编辑 " + n.ts.slice(0, 10) : "";
    document.querySelector("#noteDel").hidden = !n.text;
    notePanel.classList.add("open");
    notePanel.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    setTimeout(function () { document.querySelector("#noteText").focus(); }, 60);
  }
  function closeNote() {
    notePanel.classList.remove("open");
    notePanel.setAttribute("aria-hidden", "true");
    if (!ghPanel.classList.contains("open")) document.body.style.overflow = "";
    noteTarget = "";
  }

  // ===== 初始化 =====
  function boot() {
    ghPanel = document.querySelector("#ghPanel");
    ghBody = document.querySelector("#ghBody");
    ghFoot = document.querySelector("#ghFoot");
    ghConnBtn = document.querySelector("#ghConn");
    notePanel = document.querySelector("#notePanel");

    var ghTitle = document.querySelector("#ghTitle");
    window.ghTitle = ghTitle;

    token = localStorage.getItem(TOKEN_KEY) || "";
    user = lsGet(USER_KEY, null);
    starred = lsGet(STAR_KEY, {}) || {};
    notes = lsGet(NOTES_KEY, {}) || {};

    renderConnBtn();
    if (!ghConnBtn) return;
    if (!ghConnInit) {
      ghConnInit = true;
      ghConnBtn.addEventListener("click", openPanel);
      document.querySelector("#ghClose").addEventListener("click", closePanel);
      ghPanel.addEventListener("click", function (e) { if (e.target === ghPanel) closePanel(); });
      document.querySelector("#noteClose").addEventListener("click", closeNote);
      document.querySelector("#noteCancel").addEventListener("click", closeNote);
      notePanel.addEventListener("click", function (e) { if (e.target === notePanel) closeNote(); });
      document.querySelector("#noteSave").addEventListener("click", function () {
        setNote(noteTarget, document.querySelector("#noteText").value);
        closeNote();
        notify("笔记已保存");
      });
      document.querySelector("#noteDel").addEventListener("click", function () {
        setNote(noteTarget, "");
        closeNote();
        notify("笔记已删除");
      });
      document.addEventListener("click", handleOpClick, true);
      document.addEventListener("keydown", function (e) {
        if (e.key !== "Escape") return;
        if (notePanel.classList.contains("open")) closeNote();
        else if (ghPanel.classList.contains("open")) closePanel();
      });
    }

    // 跳转登录回跳：有一次性票据 → 先换 token，跳过旧 token 恢复
    if (handleOAuthTicket()) return;

    // 已有 token → 后台验证 + 同步星标
    if (token) {
      verifyToken(token).then(function (u) {
        if (u && u.login) {
          saveUser({ login: u.login, avatar_url: u.avatar_url || "" });
          renderConnBtn();
          loadStarredAll();
        }
      }).catch(function () {
        saveToken(""); saveUser(null); starred = {}; saveStarred();
        renderConnBtn();
      });
    } else {
      renderConnBtn();
    }
  }

  // ===== 对外接口 =====
  window.GH = {
    isConnected: isConnected,
    getUserInfo: getUserInfo,
    isStarred: isStarred,
    getNote: getNote,
    hasNote: hasNote,
    cloneCmd: cloneCmd,
    actionsHTML: actionsHTML,
    onChange: onChange,
    openPanel: openPanel,
    openNoteEditor: openNoteEditor,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
