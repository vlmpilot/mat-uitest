"""长按（T65）：**u2 的默认 0.5s 正好等于 Android 的 500ms 长按阈值，会被判成普通点击。**

## 真实症状（2026-10-06 实测）

靶场里长按列表项 ⇒ **长按监听器根本没触发**，反而按普通点击**跳进了详情页**。
（是在做对抗集配对臂时暴露的：那条长按探针在"带缺陷版"和"无缺陷版"上**都失败**，
说明它测的不是缺陷——真因是引擎的长按本身没生效。）

## 根因

`uiautomator2` 的 `long_click(x, y, duration=0.5)` 底层发的是 `click(x, y, 500)`——
**500ms 正好是 `ViewConfiguration.getLongPressTimeout()`**，事件分发抖动一多就落到阈值之下。
更糟的是它的 XPath 分支 `XPathSelector.long_click()` **连 duration 参数都没有**
（源码注释写着 "400ms between down and up"，比阈值还低）。

## 修法

① 驱动层 `LONG_PRESS_SECONDS = 1.0`（阈值的两倍）；
② `Element.long_click()` 的**节点路径也走驱动**（取节点中心），不再调 `node.long_click()`。

⚠️ 这两条**必须同时成立**：只改①不改②的话，节点路径根本走不到驱动，改了也没用。
"""

from __future__ import annotations

import pytest

from insta_engine.driver import LONG_PRESS_SECONDS, DeviceDriver
from insta_engine.driver_base import BaseDriver
from insta_engine.locator import Element


def test_long_press_duration_is_above_measured_flip_point():
    """必须**高于实测翻转点**（1.2s），不能只看 Android 文档里的 500ms。

    实测（华为 ELE-AL00 / Android 10）：0.5s ❌、1.0s ❌、**1.2s ✅**。
    ⇒ 翻转点在 1.0–1.2s 之间，**不是** 500ms——u2 的 `jsonrpc.click(x,y,ms)`
    实际按压比请求的短。取 2.0s 留 67% 余量。
    """
    assert LONG_PRESS_SECONDS >= 1.5, (
        f"长按时长 {LONG_PRESS_SECONDS}s 太接近实测翻转点（1.2s），"
        "换个机型/系统负载就可能翻车")
    assert LONG_PRESS_SECONDS > 0.5, "0.5s 连 Android 的 500ms 阈值都压线"


def test_driver_long_click_passes_explicit_duration():
    """驱动必须把 duration **显式**传给 u2（不能吃它的 0.5s 默认值）。"""
    seen: dict = {}

    class _FakeU2:
        def long_click(self, x, y, duration=0.5):     # noqa: ARG002 - 假对象：只记录
            seen["duration"] = duration
            seen["xy"] = (x, y)

    drv = object.__new__(DeviceDriver)                # 不连真机
    drv._d = _FakeU2()
    drv.long_click(100, 200)

    assert seen["xy"] == (100, 200)
    assert seen["duration"] == LONG_PRESS_SECONDS, "不能落到 u2 的 0.5s 默认值上"
    assert seen["duration"] > 0.5, "0.5s 正好是阈值，会被判成点击"


class _Node:
    """u2 节点的最小替身：只提供 center()。"""

    def center(self):
        return (321, 654)

    def long_click(self, *a, **k):                    # noqa: ARG002
        raise AssertionError("节点路径**不该**调 node.long_click()——它用 u2 的 0.5s（T65）")


class _RecordingDriver:
    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    def long_click(self, x, y):
        self.calls.append((x, y))

    # P1-iOS（U8）：Element.long_click 的节点路径改经驱动的 long_click_node
    # 分派——本 fake 用 BaseDriver 的默认实现（取中心走 long_click），语义不变。
    long_click_node = BaseDriver.long_click_node


def test_element_long_click_on_node_goes_through_driver():
    """节点型 Element 必须**取中心点交给驱动**，而不是 `node.long_click()`。"""
    drv = _RecordingDriver()
    el = Element(strategy="res-id", value="pkg:id/x", node=_Node(), drv=drv)
    el.long_click()
    assert drv.calls == [(321, 654)], "应按节点中心走驱动"


def test_element_long_click_on_point_goes_through_driver():
    """点型 Element（vision / 模板匹配命中）本来就走驱动，这里钉住别退化。"""
    drv = _RecordingDriver()
    Element(strategy="vision-llm", value="(1,2)", point=(11, 22), drv=drv).long_click()
    assert drv.calls == [(11, 22)]


def test_element_without_driver_falls_back_but_warns_in_docstring():
    """**反例**：没有驱动时退回老行为（不抛异常）——老驱动/假驱动不该因此炸掉。"""
    called: dict = {}

    class _N:
        def long_click(self, *a, **k):                # noqa: ARG002
            called["hit"] = True

    Element(strategy="text", value="x", node=_N()).long_click()
    assert called.get("hit") is True


def test_driver_long_click_signature_defaults_to_long_press_seconds():
    """护栏：**驱动层**的 `long_click` 默认值必须是 `LONG_PRESS_SECONDS`。

    只看驱动层——`Element.long_click()` 不带 duration 是对的（它委托给驱动）。
    这条防的是"以后有人把驱动默认值改回 0.5s"。
    """
    import inspect

    sig = inspect.signature(DeviceDriver.long_click)
    default = sig.parameters["duration"].default
    assert default == LONG_PRESS_SECONDS, f"驱动默认时长被改成了 {default}"
    assert default > 0.5, "0.5s 正好是 Android 长按阈值，会被判成普通点击"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
