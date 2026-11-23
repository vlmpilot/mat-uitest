"""统一出口 egress 的行为锁定（2026-11-23）。

用真实本机 HTTP 服务，不用 mock：重定向逐跳校验、跨 origin 剥头、请求体丢弃
这些行为只有真跑才能锁住。另外单独锁一条「非离线模式参数原样透传」——测试库里
有 61 处固定签名的假函数，多传一个关键字就会 TypeError。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from insta_engine import egress
from insta_engine.offline import ENV_PRIVATE_HOSTS, OfflineError


class _Server:
    """记录收到的每个请求；路由按 path 配置（默认 200 `{"ok":true}`）。"""

    def __init__(self):
        self.seen: list[dict] = []
        self.routes: dict[str, tuple[int, dict, bytes]] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: N802 —— 静默测试输出
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.seen.append({"method": self.command, "path": self.path,
                                   "headers": dict(self.headers), "body": body})
                status, headers, payload = outer.routes.get(
                    self.path.split("?")[0], (200, {}, b'{"ok":true}'))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _handle
            do_POST = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv():
    s = _Server()
    yield s
    s.close()


@pytest.fixture
def srv2():
    s = _Server()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _offline_private_loopback(monkeypatch):
    """离线 + 空 MAT_PRIVATE_HOSTS：本机回环仍是私有端点（放行），公网被拒。"""
    monkeypatch.setenv("MAT_OFFLINE", "1")
    monkeypatch.delenv(ENV_PRIVATE_HOSTS, raising=False)


def test_online_passes_kwargs_through_unchanged(monkeypatch):
    """非离线：不能多传 allow_redirects / proxies（固定签名的假函数会 TypeError）。"""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, headers, json, timeout))
        return "resp"

    monkeypatch.delenv("MAT_OFFLINE", raising=False)
    monkeypatch.setattr(requests, "post", fake_post)
    out = egress.post("https://api.example.com/v1", purpose="t",
                      headers={"A": "1"}, json={"x": 1}, timeout=2)
    assert out == "resp"
    assert calls == [("https://api.example.com/v1", {"A": "1"}, {"x": 1}, 2)]


def test_offline_blocks_public_before_sending(monkeypatch, srv):
    with pytest.raises(OfflineError, match="离线模式"):
        egress.get("https://api.example.com/v1", purpose="t")
    assert srv.seen == [], "被拒绝的请求不应落到本机服务"


def test_offline_allows_private_endpoint(srv):
    resp = egress.get(f"{srv.url}/ok", purpose="t")
    assert resp.status_code == 200
    assert [s["path"] for s in srv.seen] == ["/ok"]


def test_offline_sets_no_proxy_and_disables_auto_redirect(monkeypatch):
    """锁住传给 requests 的 kwargs：离线必须禁自动重定向、禁环境代理。"""
    seen = {}

    class _Resp:
        status_code = 200
        headers: dict = {}

        def close(self):
            pass

    def fake_get(url, **kw):
        seen.update(kw)
        return _Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    egress.get("http://127.0.0.1:9/x", purpose="t", timeout=1)
    assert seen["allow_redirects"] is False
    assert seen["proxies"] == {"http": None, "https": None, "all": None}


def test_offline_follows_redirect_to_private(srv):
    srv.routes["/redir"] = (302, {"Location": "/ok"}, b"")
    resp = egress.get(f"{srv.url}/redir", purpose="t")
    assert resp.status_code == 200
    assert [s["path"] for s in srv.seen] == ["/redir", "/ok"]


def test_offline_blocks_redirect_to_public(srv):
    srv.routes["/redir"] = (302, {"Location": "https://api.example.com/x"}, b"")
    with pytest.raises(OfflineError, match="重定向"):
        egress.get(f"{srv.url}/redir", purpose="t")
    assert [s["path"] for s in srv.seen] == ["/redir"], "只应发出第一跳"


def test_offline_cross_origin_redirect_drops_body_and_auth(srv, srv2):
    """302 后改 GET、丢请求体；跨 origin 时剥掉 Authorization/Cookie，保留其它头。"""
    srv.routes["/redir"] = (302, {"Location": f"{srv2.url}/ok"}, b"")
    egress.post(f"{srv.url}/redir", purpose="t", json={"secret": 1},
                headers={"Authorization": "Bearer x", "Cookie": "a=1", "X-Keep": "1"})
    second = srv2.seen[0]
    assert second["method"] == "GET"
    assert second["body"] == b""
    assert "Authorization" not in second["headers"]
    assert "Cookie" not in second["headers"]
    assert second["headers"].get("X-Keep") == "1"


def test_offline_same_origin_redirect_keeps_auth(srv):
    srv.routes["/redir"] = (302, {"Location": "/ok"}, b"")
    egress.get(f"{srv.url}/redir", purpose="t", headers={"Authorization": "Bearer x"})
    assert srv.seen[1]["headers"].get("Authorization") == "Bearer x"


def test_too_many_redirects(srv):
    srv.routes["/loop"] = (302, {"Location": "/loop"}, b"")
    with pytest.raises(requests.TooManyRedirects):
        egress.get(f"{srv.url}/loop", purpose="t")
    assert len(srv.seen) == egress.MAX_REDIRECTS + 1
