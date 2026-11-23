"""出网守卫：除 `egress.py` 与登记过的本机探测外，源码里不允许直接发请求。

为什么用 AST 而不是正则：正则会把注释、字符串、`getattr(requests, name)` 一起算进去，
既漏报又误报。AST 只看真实的调用表达式。

豁免按「文件 + 条数」登记，不整文件放行：新增一处出网点会改变计数、测试失败，
逼着改动者显式说明理由。这是 2026-11-23 的教训——`_server_usage` 就是在盘点之外
长出来的一处，离线承诺因此破了半年。
"""

from __future__ import annotations

import ast
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "insta_engine"

#: 直接发请求的写法（属性调用）
_NET_ATTRS = {"get", "post", "put", "delete", "patch", "head", "options",
              "request", "urlopen", "create_connection", "socket"}
_NET_BASES = {"requests", "urllib.request", "socket", "http.client",
              "session", "self.session", "self._session"}
#: 直接发请求的写法（裸函数名）
_NET_NAMES = {"urlopen", "create_connection"}

#: 登记豁免：文件 → (允许的条数, 理由)
EXEMPT: dict[str, tuple[int, str]] = {
    "doctor.py": (2, "本机/私有端点 TCP 探活 + WDA localhost:8100 的 /status"),
    "ios_wda.py": (1, "WDA /status 轮询（固定 localhost:8100）"),
    "web_api/runtime.py": (1, "UDP connect 8.8.8.8 只查路由表，不发包"),
}


def _network_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in _NET_ATTRS:
            if ast.unparse(fn.value) in _NET_BASES:
                lines.append(node.lineno)
        elif isinstance(fn, ast.Name) and fn.id in _NET_NAMES:
            lines.append(node.lineno)
    return lines


def test_no_direct_network_calls_outside_egress():
    offenders: dict[str, list[int]] = {}
    for path in sorted(ENGINE.rglob("*.py")):
        rel = str(path.relative_to(ENGINE))
        if rel == "egress.py":
            continue
        hits = _network_lines(path)
        if not hits:
            continue
        allowed = EXEMPT.get(rel, (0, ""))[0]
        if len(hits) > allowed:
            offenders[rel] = hits
    assert not offenders, (
        "以下文件在 egress 之外直接发请求（新增请走 egress，确需本机探测请登记豁免）：\n"
        + "\n".join(f"  {name}: 第 {lines} 行" for name, lines in offenders.items()))


def test_key_call_sites_use_egress():
    """关键调用点必须真的接进统一出口——守卫只查「没有直接发请求」，不查「用了 egress」。"""
    for name in ("ai_client.py", "api_client.py", "cloudapi.py", "notify.py",
                 "__main__.py", "web_api/version_check.py"):
        source = (ENGINE / name).read_text(encoding="utf-8")
        assert "egress." in source, f"{name} 未接入统一出口"


def test_exemptions_are_still_accurate():
    """豁免条数要如实：重构减少出网点时同步下修，别留一个越来越宽的口子。"""
    for rel, (count, reason) in EXEMPT.items():
        assert reason, f"{rel} 的豁免必须写明理由"
        hits = _network_lines(ENGINE / rel)
        assert len(hits) == count, (
            f"{rel} 的出网点从 {count} 变成 {len(hits)}（第 {hits} 行）——"
            "请确认新调用是否该走 egress，再更新本文件的登记")
