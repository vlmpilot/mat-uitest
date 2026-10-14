"""系统弹窗主动点掉（T77）：`back` 关不掉权限框，实测会让探索**空转到终止**。

## 真实症状（2026-10-06，4 个真实 App 同预算探索）

| App | 步数 | 页面 | 结果 |
|---|---|---|---|
| App A | 15 | 13 | **系统弹窗 ⇒ 连续 8 次留在不了 App ⇒ 提前终止** |
| App B | 15 | 6 | 同上 + 黑屏 ×2 |
| App C | 42 | 28 | 同上 |
| App D | 52 | 59 | （无弹窗，唯一没被截断的） |

后果：**污染远大于要测的差异**——App A 那一轮覆盖率只有 **7%**（历史累积图谱是 **33%**）。

## 修法

`dismiss_system_dialog()`：检测到前台是系统 UI 时，**在弹窗自己的控件树里找一个按钮点掉**，
点不到才退回原来的 `back` / `app_start`。

⚠️ **只在 `is_system_dialog(前台包名)` 为真时调用**——那时整棵树都属于弹窗，
否则会点到被测 App 自己的「取消」。

⚠️ **默认策略是「允许」**（2026-10-06 起）：默认拒绝会把"需要权限才能进的功能页"**整个探丢**
⇒ 量出来的是"策略有多保守"而不是"探索有多强"。
代价是探索会改设备的授权状态 ⇒ 覆盖率不再等于"开箱即用"。要回到最小干预就设 `MAT_DIALOG_DENY=1`。
"""

from __future__ import annotations

import pytest

from insta_engine.explore import (
    DIALOG_ALLOW_LABELS,
    DIALOG_DENY_LABELS,
    _dialog_allow,
    dismiss_system_dialog,
    is_system_dialog,
    pick_dialog_button,
)
from insta_engine.ui_tree import Node


def _btn(label: str, clickable: bool = True, bounds: str = "[100,200][300,260]") -> Node:
    return Node(cls="android.widget.Button", text=label, desc="", rid="",
                clickable=clickable, package="com.android.permissioncontroller", bounds=bounds)


def test_allow_is_the_default_policy(monkeypatch):
    """默认「**允许**」——否则"需要权限才能进的功能页"会被自己的策略探丢。"""
    monkeypatch.delenv("MAT_DIALOG_DENY", raising=False)
    assert _dialog_allow() is True

    nodes = [_btn("禁止"), _btn("仅使用期间允许")]
    picked = pick_dialog_button(nodes, allow=_dialog_allow())
    assert picked is not None and picked.label == "仅使用期间允许"


@pytest.mark.parametrize("val,expected", [("1", False), ("true", False), ("YES", False),
                                          ("0", True), ("", True)])
def test_deny_env_switches_back_to_minimal_intervention(monkeypatch, val: str, expected: bool):
    """`MAT_DIALOG_DENY=1` 回到「最小干预」——那时该点「禁止」而不是「允许」。"""
    monkeypatch.setenv("MAT_DIALOG_DENY", val)
    assert _dialog_allow() is expected


def test_deny_policy_still_picks_the_deny_button():
    """**反例**：显式用拒绝策略时，必须点「禁止」而不是「允许」。"""
    nodes = [_btn("禁止"), _btn("仅使用期间允许")]
    picked = pick_dialog_button(nodes, allow=False)
    assert picked is not None and picked.label == "禁止"


def test_allow_policy_prefers_the_exact_label():
    """允许策略下，**精确匹配优先**：「允许本次使用」不该被更短的「允许」抢走。"""
    nodes = [_btn("始终允许"), _btn("允许本次使用")]
    picked = pick_dialog_button(nodes, allow=True)
    assert picked is not None and picked.label == "允许本次使用"


def test_short_label_must_not_swallow_its_negation():
    """**关键反例**：`允许` 是 `不允许` 的子串。

    若让两字标签也走"包含"匹配，allow 策略会**点中「不允许」**——方向完全相反。
    ⇒ 短标签只精确匹配 ⇒ 这里应该**挑不到**，返回 None（宁可不动，也不要点反）。
    """
    nodes = [_btn("不允许")]
    assert pick_dialog_button(nodes, allow=True) is None
    assert pick_dialog_button(nodes, allow=False) is not None      # deny 侧精确命中 ✓


def test_long_label_still_matches_by_containment():
    """长标签仍可被包住（真实弹窗文案常带前缀/后缀）。"""
    nodes = [_btn("我知道了，不再提示")]
    picked = pick_dialog_button(nodes, allow=True)
    assert picked is not None and picked.label == "我知道了，不再提示"


def test_non_clickable_and_boundsless_are_skipped():
    """不可点的、拿不到坐标的一律不选——点了也是空操作。"""
    nodes = [_btn("禁止", clickable=False), _btn("拒绝", bounds="")]
    assert pick_dialog_button(nodes, allow=False) is None


def test_no_matching_button_returns_none():
    assert pick_dialog_button([_btn("下一步"), _btn("跳过")], allow=False) is None


def test_both_label_sets_are_non_empty_and_disjoint():
    """护栏：两组文案不该有交集——有交集就说明策略区分失效了。"""
    assert DIALOG_DENY_LABELS and DIALOG_ALLOW_LABELS
    assert not (set(DIALOG_DENY_LABELS) & set(DIALOG_ALLOW_LABELS))


@pytest.mark.parametrize("pkg,expected", [
    ("com.android.permissioncontroller", True),
    ("com.android.packageinstaller", True),
    ("com.tencent.news", False),
    ("com.android.settings", False),      # **不能算**：它本身就是合法被测目标（spike 的 t0）
])
def test_is_system_dialog_keeps_settings_out(pkg: str, expected: bool):
    assert is_system_dialog(pkg) is expected


class _FakeDrv:
    def __init__(self, xml: str):
        self._xml = xml
        self.clicks: list[tuple[int, int]] = []

    def dump(self) -> str:
        return self._xml

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))


def _xml(label: str) -> str:
    return ('<?xml version="1.0"?><hierarchy>'
            f'<node class="android.widget.Button" text="{label}" clickable="true" '
            'package="com.android.permissioncontroller" bounds="[100,200][300,260]" />'
            '</hierarchy>')


def test_dismiss_clicks_the_button_center():
    drv = _FakeDrv(_xml("禁止"))
    assert dismiss_system_dialog(drv, allow=False) == "禁止"
    assert drv.clicks == [(200, 230)], "应点按钮中心"


def test_dismiss_returns_none_and_does_not_click_when_nothing_matches():
    drv = _FakeDrv(_xml("下一步"))
    assert dismiss_system_dialog(drv, allow=False) is None
    assert drv.clicks == []


def test_dump_failure_is_swallowed():
    """采证类工具：dump 失败不该中断探索（返回 None，由调用方退回 back）。"""

    class _Boom:
        def dump(self):
            raise RuntimeError("device offline")

        def click(self, x, y):    # pragma: no cover - 不该被调用
            raise AssertionError("不该点")

    assert dismiss_system_dialog(_Boom(), allow=False) is None
