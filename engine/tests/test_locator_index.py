"""定位的「第 N 个匹配」(`index`)：DSL → 驱动 → 证据 全链贯通。

**为什么要有这个能力**（2026-10-25 同代重测）：`gen-191453-4` 的意图是
「点击任意新闻卡片」，用例却写死 `text=德约中网七冠王`；执行时首页早已换成别的头条
⇒ 3 轮全挂，且**不是引擎定位能力不足**（元素在屏、可点，只是那条文案没了）。
内容型 App 的正确表达是 `res-id=<卡片容器> + index=0`：用位置，不用内容。

本文件守四件事：
1. `index` 真的传到驱动（Android=u2 `instance`，iOS=wda `Selector.index`）；
2. **没写 index 时调用形状完全不变**（仓里十几个三参测试桩不能被一个新特性打挂）；
3. 序列化/消毒不会把 `index` 静默丢掉（`clean_locator` 是**重建**字典，最典型的丢字段点）；
4. 失败信息里带 `#N`——否则「第 0 个卡片没找到」和「文案没找到」在现场长得一样。
"""

from __future__ import annotations

import json

import pytest

from insta_engine.dsl import TestCase as DslCase
from insta_engine.dsl import dump_dsl
from insta_engine.locator import LocateFailed, Locator, LocatorStrategy, resolve


class _Node:
    """最小节点：`.exists`（旧判定）与 `.info`（位置定位的判定，见 `Element.ok`）同源。"""

    def __init__(self, exists: bool = True):
        self.exists = exists
        self.info = {"bounds": "[0,0][10,10]"} if exists else {}


class _Drv:
    """记录每次 `find` 的实参形状（含是否带 index 关键字）。"""

    platform = "android"

    def __init__(self, hit: bool = True, hit_at: int = 0):
        self.hit = hit
        self.hit_at = hit_at          # 第几次 find 调用算命中（测 text 链逐级透传用）
        self.calls: list[tuple[str, str, dict]] = []
        self.xpaths: list[str] = []

    def find(self, by: str, value: str, timeout: float, **kw):  # noqa: ARG002
        self.calls.append((by, value, kw))
        return _Node(self.hit and len(self.calls) - 1 == self.hit_at)

    def find_xpath(self, expr: str, timeout: float):  # noqa: ARG002
        self.xpaths.append(expr)
        return _Node(False)


class _LegacyDrv:
    """三参签名的历史实现（等价于仓里那些 `find(self, by, value, timeout)` 桩）。"""

    platform = "android"

    def find(self, by: str, value: str, timeout: float):  # noqa: ARG002
        return _Node(True)

    def find_xpath(self, expr: str, timeout: float):  # noqa: ARG002
        return _Node(False)


# -- 1. 传参形状 ---------------------------------------------------------------


def test_index_forwarded_as_keyword():
    drv = _Drv()
    element = resolve(drv, Locator(strategy=LocatorStrategy.RES_ID,
                                   value="com.x:id/card", index=2), timeout_ms=1000)
    assert element.ok
    assert element.index == 2, "命中元素要记下用的第几个，否则证据里看不出是位置定位"
    assert drv.calls == [("resourceId", "com.x:id/card", {"index": 2})]


def test_absent_index_keeps_legacy_call_shape():
    """不写 index ⇒ **一个多余的关键字都不传**（老驱动/老桩原样可用）。"""
    drv = _Drv()
    resolve(drv, Locator(strategy=LocatorStrategy.RES_ID, value="com.x:id/card"),
            timeout_ms=1000)
    assert drv.calls == [("resourceId", "com.x:id/card", {})]


def test_legacy_three_arg_driver_still_works():
    """三参实现 + 无 index ⇒ 不该 TypeError（这正是 `locator._find` 存在的理由）。"""
    assert resolve(_LegacyDrv(), Locator(strategy=LocatorStrategy.TEXT, value="搜索"),
                   timeout_ms=1000).ok


def test_text_chain_propagates_index():
    """text 语义链每一级都带同一个 index（否则位置语义会中途退回"第一个"）。"""
    drv = _Drv(hit_at=1)          # text 未命中、description 命中
    element = resolve(drv, Locator(strategy=LocatorStrategy.TEXT, value="要闻", index=1),
                      timeout_ms=1000)
    assert element.strategy == "text→description"
    assert [kw for _, _, kw in drv.calls] == [{"index": 1}, {"index": 1}]


def test_xpath_index_wraps_whole_expression():
    """XPath 1.0 是 1 基：`(expr)[N+1]`；括号不能省（`//a/b[2]` 是"每个 a 的第 2 个 b"）。"""
    drv = _Drv(hit=False)
    with pytest.raises(LocateFailed):
        resolve(drv, Locator(strategy=LocatorStrategy.XPATH,
                             value='//*[@text="卡片"]', index=2), timeout_ms=600)
    assert drv.xpaths == ['(//*[@text="卡片"])[3]']


def test_failure_message_carries_index():
    """失败证据要能区分「第 3 个没找到」与「文案没找到」。"""
    with pytest.raises(LocateFailed) as exc:
        resolve(_Drv(hit=False), Locator(strategy=LocatorStrategy.RES_ID,
                                         value="com.x:id/card", index=3), timeout_ms=600)
    assert "com.x:id/card#3" in str(exc.value), str(exc.value)


def test_out_of_range_index_is_not_ok_even_when_exists_lies():
    """**真机陷阱**：u2 的 `exist` RPC 不看 `instance`——越界也返回 True。

    2026-10-25 实测：`c_list_view_container` 只有 6 个，`instance=9` 的 `exist` 仍为 True，
    而 `objInfo` 如实抛 `UiObjectNotFoundError`。若按 `.exists` 判命中，
    「第 9 个卡片」会被记成定位成功，然后在 `click()` 里抛一个与定位无关的异常。
    """
    from insta_engine.locator import Element

    class _LyingNode:
        exists = True                      # u2 的 exist RPC：越界也说"有"

        @property
        def info(self):
            raise RuntimeError("UiObjectNotFoundError")

    assert not Element(strategy="res-id", value="com.x:id/card",
                       node=_LyingNode(), index=9).ok



# -- 2. 模型校验（fail fast，不写静默空操作） -----------------------------------


def test_index_accepts_zero():
    assert Locator(strategy=LocatorStrategy.RES_ID, value="x", index=0).index == 0


def test_negative_index_rejected():
    with pytest.raises(ValueError, match="index"):
        Locator(strategy=LocatorStrategy.RES_ID, value="x", index=-1)


@pytest.mark.parametrize("strategy", ["point", "image-template"])
def test_index_rejected_where_not_enumerable(strategy):
    """`point`/`image-template` 定位的是单个目标 ⇒ 写 index 不生效，解析期就拒绝。"""
    with pytest.raises(ValueError, match="index"):
        Locator(strategy=strategy, value="1,2" if strategy == "point" else "tpl.png", index=0)


# -- 3. 序列化 / 消毒不丢字段 ---------------------------------------------------


def _case(target: dict) -> DslCase:
    return DslCase.model_validate({
        "caseId": "c", "name": "卡片", "target": {"package": "com.x"},
        "steps": [{"id": "s1", "action": "tap", "intent": "点击任意新闻卡片",
                   "target": target}],
    })


def test_dump_dsl_omits_unset_index():
    """未写 index 不得产生 `"index": null` —— 否则全仓定位块都被加一行噪声。"""
    doc = json.loads(dump_dsl(_case({"strategy": "res-id", "value": "com.x:id/card"})))
    assert "index" not in doc["steps"][0]["target"]


def test_dump_dsl_keeps_zero_index():
    doc = json.loads(dump_dsl(_case({"strategy": "res-id", "value": "com.x:id/card",
                                     "index": 0})))
    assert doc["steps"][0]["target"]["index"] == 0


def test_clean_locator_preserves_index():
    """`clean_locator` 是**重建**字典 —— 不显式搬运的字段会被静默丢掉（同 preconditions 教训）。"""
    from insta_engine.case_quality import clean_locator

    cleaned = clean_locator({"strategy": "res-id", "value": "com.x:id/card", "index": 1})
    assert cleaned == {"strategy": "res-id", "value": "com.x:id/card", "index": 1}


def test_clean_locator_drops_bad_index():
    from insta_engine.case_quality import clean_locator

    assert clean_locator({"strategy": "res-id", "value": "x", "index": -1}) == {
        "strategy": "res-id", "value": "x"}
    assert clean_locator({"strategy": "res-id", "value": "x", "index": "0"}) == {
        "strategy": "res-id", "value": "x"}


# -- 4. 清空：XPathSelector 没有 clear_text（真机实测） -----------------------------


def test_clear_text_falls_back_to_empty_set_text():
    """u2 的 XPathSelector 只有 `set_text`，没有 `clear_text`（2026-10-25 真机实测）。

    内容型 App 的搜索框把**占位/热词写在 text 里**（tx「网友捕捉绝美"火箭云"！」、
    xhs「搜索, 」），`set_text("世界杯")` 会拼成「火箭世界杯」——搜出来的东西与意图无关，
    而引擎的回读校验认为"有内容"、用例侧完全看不出来。所以 `clear` 必须能用。
    """
    from insta_engine.locator import Element

    class _XPathLike:
        """只有 set_text 的节点（等价 u2 XPathSelector）。"""

        def __init__(self):
            self.text = "火箭"

        def set_text(self, value):
            self.text = value

    node = _XPathLike()
    Element(strategy="xpath", value="//x", node=node).clear_text()
    assert node.text == ""


def test_clear_text_falls_back_when_clear_raises():
    """`clear_text` **存在但调不通**也要降级 —— 这是第二级兜底。

    u2 的 `UiObject.clear_text` 内部走 ADB 广播（`am broadcast -a ADB_KEYBOARD_CLEAR_TEXT`），
    输入框不是 `ExtractEditText` 时抛 `AdbBroadcastError`。**「有这个方法」不等于「调得通」**，
    只判 `callable()` 会漏掉这一整类（`gen-184020-3` 的第 3 步就是这么红的）。
    """
    from insta_engine.locator import Element

    class _BroadcastFails:
        """有 clear_text，但一调就抛（等价 u2 的 ADB 广播失败）。"""

        def __init__(self):
            self.text = "火箭"
            self.clear_called = 0

        def clear_text(self):
            self.clear_called += 1
            raise RuntimeError("broadcast ADB_KEYBOARD_CLEAR_TEXT failed: not ExtractEditText")

        def set_text(self, value):
            self.text = value

    node = _BroadcastFails()
    Element(strategy="res-id", value="com.x:id/et", node=node).clear_text()
    assert node.clear_called == 1, "应当先真的尝试 clear_text，不能一上来就覆写"
    assert node.text == "", "clear_text 抛错后必须降级 set_text('')"


def test_clear_text_raises_on_point_locator():
    """坐标定位没有可清空的节点 —— 必须报错而不是静默空操作。"""
    from insta_engine.locator import Element

    with pytest.raises(RuntimeError, match="坐标定位"):
        Element(strategy="point", value="100,200", point=(100, 200)).clear_text()
