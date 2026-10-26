"""TEXT 定位链的 `hint` 兜底（2026-10-25 真机评测）。

真机实测：InstaDemo 登录页的输入框 `text="alice"`（默认值）、提示文案只在 `hint="账号"`
⇒ 旧的四级链（text / description 的精确与包含）全部未命中，用例首跑即失败；
tx-news 搜索框（`hint="搜索感兴趣的内容"`）同理。

**为什么必须单独覆盖**：这类输入框是常见形态（记住上次账号、示例值、占位提示），
而失败表现与"控件不存在"完全一样，人很容易误判成用例写错。
"""

from __future__ import annotations

import pytest

from insta_engine import locator as locator_mod
from insta_engine.locator import (
    Element,
    LocateFailed,
    Locator,
    LocatorStrategy,
    _xpath_literal,
    resolve,
)


class _Node:
    def __init__(self, exists: bool = False):
        self.exists = exists


class _Drv:
    """最小驱动：text/description 全未命中，只在 hint（XPath）上命中。"""

    platform = "android"

    def __init__(self, hint_hit: bool = True):
        self.hint_hit = hint_hit
        self.xpaths: list[str] = []

    @staticmethod
    def find(by: str, value: str, timeout: float):  # noqa: ARG004
        return _Node(False)

    def find_xpath(self, expr: str, timeout: float):  # noqa: ARG002
        self.xpaths.append(expr)
        return _Node(self.hint_hit)


def test_hint_is_tried_after_text_and_description():
    drv = _Drv()
    element = resolve(
        drv, Locator(strategy=LocatorStrategy.TEXT, value="账号"), timeout_ms=1200)
    assert element.ok, "hint 命中时不应报定位失败"
    assert element.strategy == "text→hint"
    assert drv.xpaths == ['//*[@hint="账号"]']


def test_hint_miss_keeps_original_failure_semantics():
    drv = _Drv(hint_hit=False)
    with pytest.raises(LocateFailed):
        resolve(drv, Locator(strategy=LocatorStrategy.TEXT, value="账号"), timeout_ms=800)
    assert drv.xpaths, "即使未命中，也必须真的尝试过 hint（否则等于没修）"


def test_xpath_literal_handles_both_quote_kinds():
    """XPath 1.0 没有转义 ⇒ 两种引号都出现时只能 concat 拆字面量。"""
    assert _xpath_literal("账号") == '"账号"'
    assert _xpath_literal("a\"b'c") == 'concat("a", \'"\', "b", "\'", "c")'


# -- D6：hint 命中后输入，节点随提示消失而失效 ----------------------------------


class _Stale:
    """已失效节点：`get_text()` 抛「未找到」（u2 的真实行为），但输入本身已生效。"""

    exists = False

    def __init__(self):
        self.clicks = 0
        self.set_texts: list[str] = []

    def click(self):
        self.clicks += 1

    def set_text(self, text: str):
        self.set_texts.append(text)

    @staticmethod
    def get_text():
        raise RuntimeError("UiObjectNotFoundError: method=wait")


class _Typed:
    """输入成功但字段回读为空（厂商安全键盘拦截的形态）。"""

    exists = True

    def __init__(self, values: list[str]):
        self.values = values

    def get_text(self):
        return self.values.pop(0) if self.values else ""


def test_stale_node_read_back_is_not_empty(monkeypatch):
    """**D6 核心**：hint 命中后输入新值 ⇒ 提示消失、选择器不再匹配。

    u2 的 `get_text()` 这时**抛** `UiObjectNotFoundError`；这是正常时序，不是失败。
    读不到时**不得**当成"回读为空"（否则会重复输入，把已填好的字段覆盖一次）。
    """
    monkeypatch.setattr(locator_mod.time, "sleep", lambda _s: None)
    element = Element(strategy="text→hint", value="账号", node=_Stale())
    assert element._read_back_empty() is False


def test_set_text_on_stale_hint_node_still_succeeds(monkeypatch):
    """端到端：回读异常不得把**已生效的输入**升级成失败（真机实测的失败形态）。"""
    monkeypatch.setattr(locator_mod.time, "sleep", lambda _s: None)

    class _Drv:
        platform = "android"
        calls: list[str] = []

        def fallback_input(self, text: str) -> bool:  # noqa: ARG002
            self.calls.append(text)
            return True

    node = _Stale()
    drv = _Drv()
    Element(strategy="text→hint", value="账号", node=node, drv=drv).set_text("alice")
    assert node.set_texts == ["alice"], "输入本身必须发生"
    assert drv.calls == [], "回读异常不得触发二级输入（会重复输入）"


def test_read_back_empty_is_true_for_genuinely_empty_field(monkeypatch):
    """反例：字段确实为空（安全键盘拦截）时仍要走二级通道（iOS 空输入框也返回 None）。"""
    monkeypatch.setattr(locator_mod.time, "sleep", lambda _s: None)
    element = Element(strategy="text", value="账号", node=_Typed(["", ""]))
    assert element._read_back_empty() is True


