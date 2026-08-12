"""StarRadar 本地 Web 服务：静态托管 + 行为信号接收（纯标准库）。

运行：
    python src/main.py --serve                # 端口 8970
    python src/main.py --serve --port 8080

API：
    GET  /api/health     → {"ok": true}                  （前端健康门控）
    GET  /api/stats      → 近 30 天行为汇总               （验证用）
    POST /api/events     → 批量行为上报 {"uid","events"} → {"accepted","duplicates"}
    POST /api/gh/device  → 转发 GitHub Device Flow 授权码请求（同上 token 轮询）

行为上报字段（来自前端 logAction）：
    repo, action, ts(ISO), topics[], language, owner, duration_s, stars, forks
CORS 全放开，GitHub Pages 静态站可跨域上报。
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlencode
from urllib.request import Request, urlopen

from config import STATIC_DIR, settings
from src.profile.feedback_collector import (
    has_interaction,
    load_latest_survey,
    log_project,
    record_interaction,
    save_survey,
    summarize_history,
)

logger = logging.getLogger(__name__)

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
}

_ACTION_WHITELIST = {
    "click", "click_deep", "click_short", "like", "dismiss",
    "star", "unstar", "fork", "clone", "note", "block",
}
_MAX_EVENTS_PER_BATCH = 200

# ===== OAuth 跳转登录（Authorization Code Flow，本地 server 模式） =====
_OAUTH_STATE_TTL = 300      # state 5 分钟过期
_OAUTH_TICKET_TTL = 60      # 一次性票据 60 秒过期
_GH_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GH_TOKEN_URL = "https://github.com/login/oauth/access_token"
_oauth_states: dict = {}    # state -> (ts, redirect_uri)
_oauth_tickets: dict = {}   # ticket -> (ts, access_token)
_oauth_lock = threading.Lock()


def _oauth_configured() -> bool:
    return bool(settings.oauth.client_id and settings.oauth.client_secret)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ===== 个人版自动生成（后台调度） =====
# 目标：用户只需开着 --serve，数据每天自动更新（启动补跑 + 每日 06:00 定时），
# 也可通过 POST /api/personal/refresh 手动触发。生成在子进程异步执行，不阻塞服务。
_PERSONAL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "personal"
_personal_proc: subprocess.Popen | None = None
_personal_lock = threading.Lock()


def _personal_scores_gen_date() -> str:
    """个人雷达数据最后生成日期（YYYY-MM-DD），无数据返回空串。"""
    f = _PERSONAL_DIR / "scores.json"
    if not f.is_file():
        return ""
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return str(data.get("generated_at") or "")[:10]
    except (json.JSONDecodeError, OSError):
        return ""


def _spawn_personal() -> None:
    """子进程异步跑个人管道（不阻塞服务）；日志写 data/profile/personal_run.log。"""
    global _personal_proc
    with _personal_lock:
        if _personal_proc and _personal_proc.poll() is None:
            return  # 已在跑，防并发
        from config import PROFILE_DIR

        logf = open(PROFILE_DIR / "personal_run.log", "a", encoding="utf-8")
        root = Path(__file__).resolve().parent.parent.parent
        _personal_proc = subprocess.Popen(
            [sys.executable, "src/main.py", "--personal"],
            cwd=root,
            stdout=logf,
            stderr=subprocess.STDOUT,
        )


def _maybe_run_personal(force: bool = False) -> bool:
    """触发个人管道（条件：已登录 + [force 或 今天未生成]）。返回是否触发。"""
    from config import PROFILE_DIR

    if not (PROFILE_DIR / "gh_token.json").is_file():
        return False  # 未登录不触发
    if not force and _personal_scores_gen_date() == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
        return False  # 今天已生成
    _spawn_personal()
    return True


def _personal_scheduler() -> None:
    """后台线程：每小时检查，当日 06:00 后若今天还没生成 → 自动跑个人管道。"""
    while True:
        try:
            hour = datetime.now().hour
            if hour >= 6:
                _maybe_run_personal(force=False)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3600)


def _start_personal_scheduler() -> None:
    threading.Thread(target=_personal_scheduler, daemon=True).start()
    try:
        _maybe_run_personal(force=False)  # 启动即检查补跑（子进程异步，不阻塞）
    except Exception:  # noqa: BLE001
        pass


class StarRadarHandler(BaseHTTPRequestHandler):
    server_version = "StarRadar/0.1"

    # ===== 基础 =====

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        logger.info("[%s] %s", self.address_string(), fmt % args)

    def _cors(self) -> None:
        """CORS 响应头：配置了 CORS_ORIGINS 白名单则按 Origin 校验，否则全放开。"""
        origins = settings.cors_origins
        if origins:
            origin = self.headers.get("Origin") or ""
            if origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _bad(self, msg: str) -> None:
        self._json(400, {"ok": False, "error": msg})

    # ===== 方法分发 =====

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        raw = unquote(self.path)
        path = raw.split("?", 1)[0]
        query = parse_qs(raw.split("?", 1)[1]) if "?" in raw else {}
        if path == "/api/health":
            self._json(200, {"ok": True, "ts": _iso_now()})
        elif path == "/api/stats":
            self._json(200, summarize_history(30))
        elif path == "/api/oauth/start":
            self._oauth_start(query)
        elif path == "/api/oauth/callback":
            self._oauth_callback(query)
        elif path == "/api/oauth/ticket":
            self._oauth_ticket(query)
        elif path == "/api/personal/status":
            self._personal_status()
        elif path == "/api/personal/scores":
            self._personal_scores()
        elif path == "/api/personal/trends":
            self._personal_trends()
        else:
            self._serve_static(path)

    def do_POST(self) -> None:
        path = unquote(self.path.split("?", 1)[0])
        if path == "/api/survey":
            self._receive_survey()
            return
        if path == "/api/gh/device":
            self._gh_proxy(
                "https://github.com/login/device/code",
                need=("client_id",),
                optional=("scope",),
            )
            return
        if path == "/api/gh/token":
            self._gh_proxy(
                "https://github.com/login/oauth/access_token",
                need=("client_id", "device_code", "grant_type"),
                optional=(),
            )
            return
        if path == "/api/gh_token":
            self._save_gh_token()
            return
        if path == "/api/llm_key":
            self._save_llm_key()
            return
        if path == "/api/personal/refresh":
            started = _maybe_run_personal(force=True)
            self._json(200, {"ok": True, "started": started})
            return
        if path != "/api/events":
            self._bad("not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._bad("bad content-length")
            return
        if length <= 0 or length > 1 << 20:  # 1MB 上限
            self._bad("payload too large")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._bad("invalid json")
            return
        events = payload.get("events")
        if not isinstance(events, list) or not events:
            self._bad("empty events")
            return
        events = events[:_MAX_EVENTS_PER_BATCH]
        accepted = duplicates = rejected = 0
        for ev in events:
            if not isinstance(ev, dict):
                rejected += 1
                continue
            repo = str(ev.get("repo") or "").strip()
            action = str(ev.get("action") or "").strip()
            if not repo or action not in _ACTION_WHITELIST:
                rejected += 1
                continue
            ts_raw = str(ev.get("ts") or "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(timezone.utc)
            ts_iso = ts.astimezone(timezone.utc).isoformat(timespec="seconds")
            if has_interaction(repo, action, ts_iso):
                duplicates += 1
                continue
            topics = ev.get("topics")
            if not isinstance(topics, list):
                topics = [t for t in str(topics or "").split(",") if t]
            record_interaction(
                repo,
                action,
                duration_s=int(ev.get("duration_s") or 0),
                topics=[str(t) for t in topics[:8]],
                language=str(ev.get("language") or "") or None,
                stars=int(ev.get("stars") or 0) or None,
                timestamp=ts,
            )
            if ev.get("stars") or ev.get("forks"):
                log_project(
                    repo,
                    description=None,
                    topics=[str(t) for t in topics[:8]],
                    language=str(ev.get("language") or "") or None,
                    stars=int(ev.get("stars") or 0) or None,
                    forks=int(ev.get("forks") or 0) or None,
                )
            accepted += 1
        self._json(200, {"ok": True, "accepted": accepted,
                         "duplicates": duplicates, "rejected": rejected})

    def _gh_proxy(self, url: str, *, need: tuple, optional: tuple) -> None:
        """转发 GitHub OAuth 端点（固定目标地址，不透传任意 URL，防 SSRF）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._bad("bad content-length")
            return
        if length <= 0 or length > 1 << 16:
            self._bad("payload too large")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._bad("invalid json")
            return
        if not isinstance(payload, dict):
            self._bad("payload must be object")
            return
        body = {}
        for key in need:
            val = payload.get(key)
            if not isinstance(val, str) or not val:
                self._bad(f"missing field: {key}")
                return
            body[key] = val.strip()
        for key in optional:
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                body[key] = val.strip()
        try:
            req = Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "StarRadar",
                },
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                data = resp.read(1 << 16)
                logger.info("gh proxy %s → %s: %s", url, resp.status, data[:160])
        except Exception as exc:  # noqa: BLE001
            logger.warning("gh proxy %s failed: %s", url, exc)
            self._json(502, {"ok": False, "error": "upstream failed"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self) -> None:
        path = unquote(self.path.split("?", 1)[0])
        if path == "/api/gh_token":
            from config import PROFILE_DIR
            f = PROFILE_DIR / "gh_token.json"
            try:
                if f.is_file():
                    f.unlink()
            except OSError:
                pass
            self._json(200, {"ok": True, "logged_out": True})
            return
        if path == "/api/llm_key":
            from config import PROFILE_DIR
            f = PROFILE_DIR / "llm_config.json"
            try:
                if f.is_file():
                    f.unlink()
            except OSError:
                pass
            self._json(200, {"ok": True, "cleared": True})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def _receive_survey(self) -> None:
        """POST /api/survey：接收问卷档案（前端 saveSurvey/skipSurvey 上报）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._bad("bad content-length")
            return
        if length <= 0 or length > 1 << 20:
            self._bad("payload too large")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._bad("invalid json")
            return
        survey = payload.get("survey")
        uid = str(payload.get("uid") or "anon").strip()[:64]
        if not isinstance(survey, dict):
            self._bad("survey must be object")
            return
        save_survey(uid, survey)
        self._json(200, {"ok": True, "saved": uid})

    # ===== OAuth 跳转登录 =====

    def _redirect(self, url: str) -> None:
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _build_redirect_uri(self) -> str:
        """跳转回调地址：OAUTH_REDIRECT_BASE 配置优先（服务器部署铺垫）；
        否则按 Host 推导，并把 localhost 规范化为 127.0.0.1（GitHub 推荐的 loopback 字面量，
        避免用户用 localhost 打开时与注册的 127.0.0.1 回调不匹配）。"""
        base = settings.oauth.redirect_base
        if base:
            return base.rstrip("/") + "/api/oauth/callback"
        host = self.headers.get("Host") or "127.0.0.1:8970"
        host = host.replace("localhost", "127.0.0.1")
        return f"http://{host}/api/oauth/callback"

    def _oauth_start(self, query: dict) -> None:
        """GET /api/oauth/start → 302 跳 GitHub 授权页；?probe=1 探测是否已配置。"""
        if query.get("probe", [""])[0] == "1":
            self._json(200, {"ok": True, "configured": _oauth_configured()})
            return
        if not _oauth_configured():
            self._json(400, {"ok": False, "error": "oauth not configured"})
            return
        redirect_uri = self._build_redirect_uri()
        state = secrets.token_urlsafe(16)
        with _oauth_lock:
            _oauth_states[state] = (time.time(), redirect_uri)
        params = urlencode({
            "client_id": settings.oauth.client_id,
            "redirect_uri": redirect_uri,
            "scope": "public_repo read:user",
            "state": state,
        })
        self._redirect(_GH_AUTHORIZE_URL + "?" + params)

    def _oauth_callback(self, query: dict) -> None:
        """GET /api/oauth/callback?code&state → 换 token → 302 回首页带一次性票据。"""
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if not code or not state:
            self._json(400, {"ok": False, "error": "missing code/state"})
            return
        with _oauth_lock:
            item = _oauth_states.pop(state, None)
        if not item:
            self._json(400, {"ok": False, "error": "bad state"})
            return
        ts, redirect_uri = item
        if time.time() - ts > _OAUTH_STATE_TTL:
            self._json(400, {"ok": False, "error": "state expired"})
            return
        try:
            req = Request(
                _GH_TOKEN_URL,
                data=urlencode({
                    "client_id": settings.oauth.client_id,
                    "client_secret": settings.oauth.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                }).encode("utf-8"),
                headers={"Accept": "application/json", "User-Agent": "StarRadar"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read(1 << 16).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("oauth token exchange failed: %s", exc)
            self._json(502, {"ok": False, "error": "token exchange failed"})
            return
        token = data.get("access_token")
        if not token:
            self._json(400, {
                "ok": False,
                "error": data.get("error_description") or data.get("error") or "no token",
            })
            return
        ticket = secrets.token_urlsafe(16)
        with _oauth_lock:
            _oauth_tickets[ticket] = (time.time(), token)
        self._redirect("/?gh_ticket=" + ticket)

    def _oauth_ticket(self, query: dict) -> None:
        """GET /api/oauth/ticket?t=… → 一次性领取 token（前端转存 localStorage）。"""
        t = (query.get("t") or [""])[0]
        if not t:
            self._json(400, {"ok": False, "error": "missing ticket"})
            return
        with _oauth_lock:
            item = _oauth_tickets.pop(t, None)
        if not item:
            self._json(404, {"ok": False, "error": "bad or expired ticket"})
            return
        ts, token = item
        if time.time() - ts > _OAUTH_TICKET_TTL:
            self._json(404, {"ok": False, "error": "ticket expired"})
            return
        self._json(200, {"ok": True, "token": token})

    # ===== 个人特化版（本地后端，数据不公开） =====

    def _save_gh_token(self) -> None:
        """POST /api/gh_token：建立本地后端登录态。
        两种模式：
        - {use_local: true}        用 .env 的 GITHUB_TOKEN 建立登录（无需 OAuth App），
                                  返回 login + token 给浏览器（本机场景）
        - {login, token}           浏览器 GitHub 登录成功后上交给后端
        存 data/profile/gh_token.json（已被 .gitignore 忽略，绝不上传）。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._bad("bad content-length")
            return
        if length <= 0 or length > 1 << 16:
            self._bad("payload too large")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._bad("invalid json")
            return

        from config import PROFILE_DIR

        if payload.get("use_local"):
            token = settings.github.token
            if not token:
                self._bad("GITHUB_TOKEN 未配置（.env）")
                return
            try:
                import urllib.request
                req = urllib.request.Request(
                    "https://api.github.com/user",
                    headers={"Authorization": "Bearer " + token, "User-Agent": "StarRadar"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    login = json.loads(resp.read().decode("utf-8")).get("login", "")
            except Exception as exc:  # noqa: BLE001
                logger.warning("local token 验证失败: %s", exc)
                self._json(502, {"ok": False, "error": "token 验证失败"})
                return
            if not login:
                self._json(502, {"ok": False, "error": "token 无效"})
                return
            try:
                (PROFILE_DIR / "gh_token.json").write_text(
                    json.dumps({"login": login, "token": token}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                self._json(500, {"ok": False, "error": "write failed"})
                return
            self._json(200, {"ok": True, "saved": login, "login": login, "token": token})
            return

        token = str(payload.get("token") or "").strip()
        login = str(payload.get("login") or "").strip()[:64]
        if not token or not login:
            self._bad("missing token/login")
            return
        try:
            (PROFILE_DIR / "gh_token.json").write_text(
                json.dumps({"login": login, "token": token}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            self._json(500, {"ok": False, "error": "write failed"})
            return
        self._json(200, {"ok": True, "saved": login})

    def _save_llm_key(self) -> None:
        """POST /api/llm_key：浏览器配置的 LLM key 上交本地后端（供 --personal 管道调用）。
        存 data/profile/llm_config.json（.gitignore 忽略，绝不上传）。
        body: {base_url?, key, model?} —— 与前端 localStorage starradar:llm_config 同构。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._bad("bad content-length")
            return
        if length <= 0 or length > 1 << 16:
            self._bad("payload too large")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._bad("invalid json")
            return
        if not isinstance(payload, dict):
            self._bad("payload must be object")
            return
        key = str(payload.get("key") or "").strip()
        if not key:
            self._bad("missing key")
            return
        from config import PROFILE_DIR

        cfg = {
            "key": key,
            "base_url": str(payload.get("base_url") or "").strip() or None,
            "model": str(payload.get("model") or "").strip() or None,
        }
        try:
            (PROFILE_DIR / "llm_config.json").write_text(
                json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            self._json(500, {"ok": False, "error": "write failed"})
            return
        self._json(200, {"ok": True, "saved": True})

    def _personal_status(self) -> None:
        """GET /api/personal/status：个人版状态（登录 / LLM / 数据是否就绪）。"""
        from config import PROFILE_DIR
        gh = PROFILE_DIR / "gh_token.json"
        scores = Path(__file__).resolve().parent.parent.parent / "data" / "personal" / "scores.json"
        logged = gh.is_file()
        login = ""
        try:
            if logged:
                login = json.loads(gh.read_text(encoding="utf-8")).get("login", "")
        except (json.JSONDecodeError, OSError):
            pass
        self._json(200, {
            "ok": True,
            "logged_in": logged,
            "login": login,
            "llm_configured": bool(settings.llm.api_key),
            "data_exists": scores.is_file(),
        })

    def _personal_scores(self) -> None:
        """GET /api/personal/scores：返回个人版雷达数据（仅本机可访问）。"""
        scores = Path(__file__).resolve().parent.parent.parent / "data" / "personal" / "scores.json"
        if not scores.is_file():
            self._json(404, {"ok": False, "error": "personal data not generated"})
            return
        try:
            data = scores.read_text(encoding="utf-8")
        except OSError:
            self._json(500, {"ok": False, "error": "read failed"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data.encode("utf-8"))))
        self._cors()
        self.end_headers()
        self.wfile.write(data.encode("utf-8"))

    def _personal_trends(self) -> None:
        """GET /api/personal/trends：个人版每周趋势（未生成时返回空周报）。"""
        trends = Path(__file__).resolve().parent.parent.parent / "data" / "personal" / "trends.json"
        if not trends.is_file():
            self._json(200, {"weeks": []})
            return
        try:
            data = trends.read_text(encoding="utf-8")
        except OSError:
            self._json(200, {"weeks": []})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data.encode("utf-8"))))
        self._cors()
        self.end_headers()
        self.wfile.write(data.encode("utf-8"))

    # ===== 静态文件 =====

    def _serve_static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        try:
            file = (STATIC_DIR / path.lstrip("/")).resolve()
            file.relative_to(STATIC_DIR.resolve())
        except (ValueError, OSError):
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not file.is_file():
            self._json(404, {"ok": False, "error": "not found"})
            return
        mime = MIME_TYPES.get(file.suffix.lower(), "application/octet-stream")
        try:
            body = file.read_bytes()
        except OSError:
            self._json(500, {"ok": False, "error": "read failed"})
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)


def _bind_server(host: str, port: int) -> ThreadingHTTPServer:
    """绑定端口并启动 HTTP server。

    Windows 下必须显式 SO_EXCLUSIVEADDRUSE 独占绑定——否则多个 --serve 实例
    会同时监听同一端口、请求随机分发（设备流/行为上报随机 502），这是真实踩过的坑。
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows only
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        print(f"  ✗ 端口 {port} 已被占用——可能已有 StarRadar 在运行。")
        print("    请先关闭旧实例（或换端口：python src/main.py --serve --port 8971）")
        print("    若旧进程残留：任务管理器结束 python.exe 后重试")
        raise SystemExit(1) from exc
    httpd = ThreadingHTTPServer((host, port), StarRadarHandler, bind_and_activate=False)
    httpd.socket.close()  # 丢弃内部未绑定 socket，替换为已独占绑定的
    httpd.socket = sock
    httpd.server_activate()
    return httpd


def _tls_self_check() -> None:
    """启动时 HTTPS 自检：当前 Python 环境无法访问 HTTPS 时提前警告。

    真实案例：conda 环境 Python 3.10 + OpenSSL 3.6 组合 TLS 全坏（ASN1:
    NOT_ENOUGH_DATA），所有 HTTPS 请求失败——server 转发 GitHub 全部 502，
    新登录（设备流/跳转）静默不可用，且极难排查。

    注意：浏览器能访问 GitHub 不代表 Python 环境正常（两者 TLS 实现独立）；
    已登录浏览/加星/Fork 是浏览器直连 api.github.com，不走本服务，不受影响。
    连测 2 次均失败才警告（防偶发网络抖动误报）。
    """
    from urllib.request import Request, urlopen

    def probe() -> bool:
        try:
            with urlopen(
                Request("https://api.github.com", headers={"User-Agent": "StarRadar"}),
                timeout=5,
            ) as resp:
                resp.read(64)
                return True
        except HTTPError:
            # 收到 HTTP 响应（403 限流等）→ TLS 握手已成功，环境正常
            return True
        except Exception:  # noqa: BLE001
            return False

    if probe():
        return
    if probe():  # 第二次确认（首次可能偶发抖动）
        return
    print("  ⚠️ HTTPS 自检失败——当前 Python 环境无法访问 HTTPS（已确认两次）：")
    print("    SSLError [ASN1: NOT_ENOUGH_DATA] 一类错误 = Python 与 OpenSSL 版本不兼容")
    print("    影响：仅「新登录」（设备流/跳转的授权码获取与 token 交换）会失败；")
    print("          已登录的浏览 / 加星 / Fork 走浏览器直连，不受影响。")
    print("    自验：python -c \"import urllib.request; print(urllib.request.urlopen")
    print("          ('https://api.github.com').status)\"  → 报错即环境问题")
    print("    解决：conda deactivate 后用系统 Python 运行，或升级本环境：")
    print("          conda install python=3.12")


def serve(*, port: int = 8970, host: str = "127.0.0.1") -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    _tls_self_check()
    _start_personal_scheduler()  # 个人版自动生成（启动补跑 + 每日 06:00）
    httpd = _bind_server(host, port)
    print(f"StarRadar 服务已启动 → http://{host}:{port}/")
    print(f"  静态站点：{STATIC_DIR}")
    print(f"  行为信号：POST /api/events（memory.db 落库）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        httpd.server_close()


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    parser = argparse.ArgumentParser(description="StarRadar Web 服务")
    parser.add_argument("--port", type=int, default=8970)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    serve(port=args.port, host=args.host)
