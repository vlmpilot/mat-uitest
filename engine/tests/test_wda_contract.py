"""WDA 库契约守卫（评审 P0-2 / mat-p0-fix-plan 修复 5）。

为什么必须是测试而不是再次人肉核对：`WdaNode.info` 用 `getattr(el, "className", "")`
取属性——库若改了属性名，`className` 恒空串、`value` 恒 "0" ⇒ `checked` 恒 False，
**零异常、零红灯**。而本项目 24 小时内刚因同一模式发生过 P0 回归
（`_vision_locate` 依赖 `drv.raw`，WdaDriver 移除 raw 后视觉兜底在 iOS 静默失效，
单测不覆盖 vision 路径所以全绿）。「测试全绿但真实路径失效」在此项目
不是假设，是已发生事实。

**零真机依赖**：只 import 库 + 查签名/属性，不打网络、不起模拟器。
未装 `facebook-wda` 的环境（如默认 CI 未装 `[ios]` extra）优雅跳过——
但注意 `importorskip` 使 CI 会以 skip 计通过，所以 ci.yml 的安装面必须含
`./engine[mcp,ios]`，否则本守卫是空转的（评审 F5，已同批修复）。
"""

from __future__ import annotations

import inspect

import pytest

wda = pytest.importorskip("wda")


# WdaDriver 实际调用的 Client 方法面（含修复 2 引入的 device_info）。
_CLIENT_METHODS = (
    "session", "device_info", "app_current", "app_launch", "app_terminate",
    "click", "tap_hold", "swipe", "home", "send_keys", "screenshot", "window_size",
)

# WdaNode 实际读取的 Element 属性/方法面。
_ELEMENT_ATTRS = (
    "className", "bounds", "enabled", "selected", "value", "text",
    "tap", "tap_hold", "set_text", "clear_text",
)


def test_wda_client_contract():
    for m in _CLIENT_METHODS:
        assert hasattr(wda.Client, m), f"wda.Client 缺 {m}: WdaDriver 会 AttributeError"
    # session 首参位置传 bundle_id（WdaDriver._bind_session 用位置/关键字传参）
    assert list(inspect.signature(wda.Client.session).parameters)[:2] == ["self", "bundle_id"]
    # swipe 前四参为 x1/y1/x2/y2（WdaDriver.swipe_ext 位置传参）
    assert list(inspect.signature(wda.Client.swipe).parameters)[:5] == [
        "self", "x1", "y1", "x2", "y2"]


def test_wda_element_contract():
    for attr in _ELEMENT_ATTRS:
        assert hasattr(wda.Element, attr), (
            f"wda.Element 缺 {attr}: WdaNode 的 getattr 默认值会吞掉契约漂移"
            "（checked 恒 False 且无异常无红灯）")


def test_wda_selector_contract():
    """dump/查询路径依赖的选择器面：exists 是 property，wait/get 是方法。"""
    assert isinstance(wda.Selector.exists, property), (
        "Selector.exists 须为 property（WdaNode.exists 直接读 .exists）")
    for m in ("wait", "get", "child"):
        assert hasattr(wda.Selector, m), f"wda.Selector 缺 {m}: WdaNode 查询会失败"
    # wait 默认 raise_error=False（WdaNode.wait 显式传 raise_error=False）
    assert "raise_error" in inspect.signature(wda.Selector.wait).parameters


def test_wda_session_http_contract():
    """dump 走 session.http.get(...)['value'] —— 这条链断了 dump 就静默拿不到树。"""
    assert hasattr(wda.Session, "http"), "wda.Session 缺 http：WdaDriver.dump 会失败"
