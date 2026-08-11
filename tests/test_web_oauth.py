"""OAuth 登录链路测试（本地 server 模式）。

覆盖：
- 默认配置（无 secret）→ probe configured=false / start 400（前端走设备流）
- 配 secret + Host=localhost → redirect_uri 规范化为 127.0.0.1（GitHub loopback 要求）
- OAUTH_REDIRECT_BASE 覆盖 Host 推导（未来服务器部署铺垫）
- CORS：默认全放开；配置白名单后按 Origin 校验

运行：pytest tests/test_web_oauth.py -v
"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

import config
from src.web.server import StarRadarHandler


@pytest.fixture()
def oauth_server():
    """起一个本地 server；修改 settings 后测试结束恢复。"""
    saved = {
        "client_id": config.settings.oauth.client_id,
        "client_secret": config.settings.oauth.client_secret,
        "redirect_base": config.settings.oauth.redirect_base,
        "cors_origins": config.settings.cors_origins,
    }
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StarRadarHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        config.settings.oauth.client_id = saved["client_id"]
        config.settings.oauth.client_secret = saved["client_secret"]
        config.settings.oauth.redirect_base = saved["redirect_base"]
        config.settings.cors_origins = saved["cors_origins"]


def req(port: int, path: str, host: str | None = None) -> tuple[int, dict, str]:
    """http.client 不自动跟随重定向，能拿到原始 302 + Location。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers={"Host": host} if host else {})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    headers = {k.lower(): v for k, v in resp.getheaders()}
    status = resp.status
    conn.close()
    return status, headers, body


def test_default_config_falls_back_to_device_flow(oauth_server):
    """无 secret 时：probe=false（前端走设备流），start 拒绝。"""
    config.settings.oauth.client_secret = None
    s, _, body = req(oauth_server, "/api/oauth/start?probe=1")
    assert s == 200
    assert json.loads(body)["configured"] is False
    s, _, _ = req(oauth_server, "/api/oauth/start")
    assert s == 400


def test_redirect_uri_normalizes_localhost(oauth_server):
    """配 secret 后：Host=localhost 时 redirect_uri 规范化为 127.0.0.1
    （GitHub 要求 loopback 字面量，否则与注册回调不匹配）。"""
    config.settings.oauth.client_secret = "test_secret"
    s, _, body = req(oauth_server, "/api/oauth/start?probe=1", host="localhost:8970")
    assert json.loads(body)["configured"] is True
    s, h, _ = req(oauth_server, "/api/oauth/start", host="localhost:8970")
    assert s == 302
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8970" in h["location"]
    assert "client_id=Ov23lidxHa5chVTqVBXX" in h["location"]


def test_redirect_base_overrides_host(oauth_server):
    """OAUTH_REDIRECT_BASE 优先于 Host 推导（服务器部署铺垫）。"""
    config.settings.oauth.client_secret = "test_secret"
    config.settings.oauth.redirect_base = "https://example.com"
    s, h, _ = req(oauth_server, "/api/oauth/start", host="localhost:8970")
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fapi%2Foauth%2Fcallback" in h["location"]


def test_cors_wildcard_by_default(oauth_server):
    """默认 CORS 全放开（GitHub Pages 跨域上报）。"""
    conn = http.client.HTTPConnection("127.0.0.1", oauth_server, timeout=10)
    conn.request("GET", "/api/health")
    resp = conn.getresponse()
    assert resp.getheader("Access-Control-Allow-Origin") == "*"
    conn.close()


def test_cors_whitelist_restricts_origin(oauth_server):
    """配置 CORS_ORIGINS 后：白名单内 Origin 放行，其他不带 CORS 头。"""
    config.settings.cors_origins = ["https://allowed.example"]
    conn = http.client.HTTPConnection("127.0.0.1", oauth_server, timeout=10)
    conn.request("GET", "/api/health", headers={"Origin": "https://allowed.example"})
    resp = conn.getresponse()
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "https://allowed.example"
    conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", oauth_server, timeout=10)
    conn.request("GET", "/api/health", headers={"Origin": "https://evil.example"})
    resp = conn.getresponse()
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") is None
    conn.close()
