"""变量替换不得丢 `index`（2026-10-28 真机实测）。

## 症状

用例写 `{"strategy":"res-id","value":"…/channel_bar_item_container","index":5}`
（意图：点频道栏第 6 个 tab「国际」）。运行期 `tap` **passed**——元素找得到、也点下去了，
但界面纹丝不动：`selected` 蓝线还在第一个 tab「7x24」上。只有断言
`selected 国际` 抓到了，3/3 全挂在断言上。

## 根因

`Runner._subst` 每步都要对 target 做一次变量替换，它重建 `Locator` 时只搬了
`strategy / value / fallback`，**`index` 没搬** ⇒ `loc.index is None`
⇒ `locator._find` 走"不传 instance"分支 ⇒ u2 命中第一个实例。

`Locator` 有 `index` 字段、`_find` 会透传、`DeviceDriver.find` 会映射成 u2 的
`instance`——**链路上只有这一处断点**，所以症状是"不报错、点错地方"。

与 `preconditions` 在 `dsl.TestCase` 无处安放是同一类**静默丢字段**。
新增字段时要先问：**这条重建路径上有没有人搬它。**
"""

from __future__ import annotations

from insta_engine.dsl import Locator, LocatorStrategy
from insta_engine.runner import Runner


class _Shim:
    """只借 `_subst` 一个方法——它只在 fallback 递归时用 `self`。"""

    _subst = Runner._subst


def test_subst_keeps_index() -> None:
    loc = Locator(strategy=LocatorStrategy.RES_ID,
                  value="com.tencent.news:id/channel_bar_item_container", index=5)
    out = _Shim()._subst(loc, {})
    assert out.index == 5, "变量替换丢了 index ⇒ 运行期会点第一个实例"
    assert out.value == loc.value


def test_subst_keeps_index_on_fallback_chain() -> None:
    """整条 fallback 链上的 index 都要留（否则"精确匹配第 5 个、兜底退回第 1 个"）。"""
    loc = Locator(strategy=LocatorStrategy.RES_ID, value="a:id/x", index=2,
                  fallback=Locator(strategy=LocatorStrategy.TEXT, value="国际", index=2))
    out = _Shim()._subst(loc, {})
    assert out.index == 2
    assert out.fallback is not None and out.fallback.index == 2


def test_subst_still_substitutes_vars() -> None:
    """修 index 不能把变量替换弄坏（原职责）。"""
    loc = Locator(strategy=LocatorStrategy.TEXT, value="${kw}", index=1)
    out = _Shim()._subst(loc, {"kw": "咖啡"})
    assert out.value == "咖啡"
    assert out.index == 1


def test_subst_keeps_none_index_none() -> None:
    """没写 index 的用例不能被塞一个 0 进去。"""
    loc = Locator(strategy=LocatorStrategy.TEXT, value="消息")
    assert _Shim()._subst(loc, {}).index is None
