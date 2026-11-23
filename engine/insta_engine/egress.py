"""统一出口：引擎里发往其它进程/主机的 HTTP 请求一律经过这里（2026-11-23）。

## 为什么要有它

离线承诺此前靠每个调用点自己记得调 `require_online`。全量盘点后发现三处缺口：

1. **漏调用**：`mat status` 的 `_server_usage` 完全没调，离线模式下仍会带着
   `GEN_API_KEY` 访问官方代理（已发布版本 0.7.16 实测复现，本次修复）；
2. **重定向**：只校验第一个 URL，被访问方回一个 302 就能把请求（api 步骤还包括
   请求体）带去任意主机；
3. **环境代理**：`requests` 默认读 `HTTP(S)_PROXY` / `ALL_PROXY`，离线模式下
   发往本机/内网的请求会先到代理主机。

## 兼容约束（改这个文件前必读）

1. 每个函数底层调用**同名**的 `requests` 函数，并在**调用时**按模块属性查找。
   测试里有 61 处直接 monkeypatch `requests.post` / `requests.request`，这样一处
   都不用改。
2. **非离线模式参数原样透传**（不加 `allow_redirects` / `proxies`）。测试里有固定
   签名的假函数，多传一个关键字就 TypeError；线上行为也与迁移前完全一致。
3. 离线模式才收紧：发请求前校验主机；关闭自动重定向，本模块逐跳校验后再跟；
   不使用环境代理。

出网守卫见 `tests/test_egress_guard.py`：除本文件与登记过的本机探测外，源码里
不允许直接发请求。
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
from collections import Counter
from collections.abc import Iterator
from urllib.parse import urljoin, urlsplit

import requests

from .offline import host_of, is_offline, require_online

MAX_REDIRECTS = 5
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

#: 请求级 `None`：`merge_environment_settings` 对环境代理用 `setdefault`，覆盖不了
#: 已存在的键；随后 `merge_setting` 删掉值为 `None` 的键 ⇒ 不走代理。`"all"` 挡
#: `ALL_PROXY`。行为由 `tests/test_egress.py` 用真实端口锁定。
_NO_PROXY = {"http": None, "https": None, "all": None}

#: 跨 origin 跳转时要剥掉的请求头（比 requests 只在换主机时剥 Authorization 更严）。
_STRIP_ON_CROSS_ORIGIN = ("authorization", "cookie", "proxy-authorization")

_TOTAL: Counter = Counter()
_TOTAL_LOCK = threading.Lock()
_SCOPE: contextvars.ContextVar[Counter | None] = contextvars.ContextVar(
    "mat_egress_scope", default=None)


def check(url: str, purpose: str) -> None:
    """只校验不发请求：给需要在重试循环之外快速失败的调用方。"""
    require_online(purpose, endpoint=url)


def get(url: str, *, purpose: str, **kwargs):
    return _send("get", "GET", url, purpose, kwargs)


def post(url: str, *, purpose: str, **kwargs):
    return _send("post", "POST", url, purpose, kwargs)


def request(method: str, url: str, *, purpose: str, **kwargs):
    return _send("request", str(method).upper(), url, purpose, kwargs)


@contextlib.contextmanager
def scope() -> Iterator[Counter]:
    """统计一段代码里实际访问过的 (主机, 用途)。Runner 用它把结果写进 run.json。

    用 contextvar 而不是全局计数：控制台会在多个线程里并发跑任务，全局计数会串。
    """
    rec: Counter = Counter()
    token = _SCOPE.set(rec)
    try:
        yield rec
    finally:
        _SCOPE.reset(token)


def summarize(rec: Counter) -> list[dict]:
    return [{"host": h, "purpose": p, "count": n} for (h, p), n in sorted(rec.items())]


def _record(url: str, purpose: str) -> None:
    key = (host_of(url) or "?", purpose)
    with _TOTAL_LOCK:
        _TOTAL[key] += 1
    rec = _SCOPE.get()
    if rec is not None:
        rec[key] += 1


def _call(name: str, method: str, url: str, kwargs: dict):
    fn = getattr(requests, name)  # 调用时查找：monkeypatch 才能生效
    return fn(method, url, **kwargs) if name == "request" else fn(url, **kwargs)


def _origin(url: str) -> tuple:
    p = urlsplit(url)
    return (p.scheme.lower(), (p.hostname or "").lower(), p.port)


def _location(resp) -> str | None:
    if getattr(resp, "status_code", None) not in _REDIRECT_CODES:
        return None
    headers = getattr(resp, "headers", None) or {}
    return headers.get("location") or headers.get("Location")


def _next_method(status: int, method: str) -> str:
    """与 `requests.Session.rebuild_method` 同语义。"""
    if status in (302, 303) and method != "HEAD":
        return "GET"
    if status == 301 and method == "POST":
        return "GET"
    return method


def _send(name: str, method: str, url: str, purpose: str, kwargs: dict):
    require_online(purpose, endpoint=url)
    if not is_offline():
        # 非离线：参数原样透传，行为与迁移前完全一致（见模块 docstring 约束 2）
        _record(url, purpose)
        return _call(name, method, url, kwargs)

    kw = dict(kwargs)
    kw["allow_redirects"] = False
    kw["proxies"] = dict(_NO_PROXY)
    current_name, current_method, current_url = name, method, url
    for _ in range(MAX_REDIRECTS + 1):
        _record(current_url, purpose)
        resp = _call(current_name, current_method, current_url, kw)
        location = _location(resp)
        if not location:
            return resp
        target = urljoin(current_url, location)
        require_online(f"{purpose}（重定向到 {host_of(target) or target}）", endpoint=target)
        new_method = _next_method(resp.status_code, current_method)
        if new_method != current_method:
            # 改成 GET 时去掉请求体：否则会把原请求体带到新地址
            for body_key in ("json", "data", "files"):
                kw.pop(body_key, None)
        if _origin(target) != _origin(current_url):
            kw["headers"] = {k: v for k, v in (kw.get("headers") or {}).items()
                             if str(k).lower() not in _STRIP_ON_CROSS_ORIGIN}
        with contextlib.suppress(Exception):
            resp.close()
        current_name, current_method, current_url = "request", new_method, target
    raise requests.TooManyRedirects(
        f"{purpose}：重定向超过 {MAX_REDIRECTS} 次（最后一跳 {current_url}）")
