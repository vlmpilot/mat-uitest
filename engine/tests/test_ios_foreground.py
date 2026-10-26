"""iOS「App 是否在屏幕上」的判定（2026-10-19 真机回归）。

背景：iOS 的通知横幅、控制中心、来电横幅都归 SpringBoard，横幅弹出时
`app_current()` 返回 `com.apple.springboard`，而被测 App 明明还在屏幕上、
控件树照常可读。此前 runner 只看 `foreground_app()` 就判"有系统浮层盖住前台"，
一条开发者通知横幅让 42 条用例全部误判失败（截图里靶场主菜单清清楚楚）。

判据改为两层：`foreground_app()` 是目标 App ⇒ 直接算落地；否则看
`app_on_screen()`（能不能读到 App 的控件树）。这里锁住这两条分支。
"""

from __future__ import annotations

import pytest

from insta_engine.runner import Runner

PKG = "com.u56rw5zm8j.WebDriverAgentRunner"


class _FgDriver:
    """最小 iOS 驱动桩：前台固定返回 springboard，`app_on_screen` 可脚本化。"""

    platform = "ios"
    serial = "00008101-000A1B2C3D4E5F60"
    _session_bundle = PKG

    def __init__(self, on_screen: bool, alert_tapped: str | None = None):
        self._on_screen = on_screen
        self._alert_tapped = alert_tapped
        self.dismiss_calls = 0

    def foreground_app(self) -> dict:
        return {"package": "com.apple.springboard", "bundleId": "com.apple.springboard"}

    def app_on_screen(self, bundle=None) -> bool:
        return self._on_screen

    def dismiss_system_alert(self):
        self.dismiss_calls += 1
        return self._alert_tapped


def test_notification_banner_is_not_a_blocking_overlay(tmp_path):
    """横幅在（前台=springboard）但 App 树可读 ⇒ **不能**判成浮层、不能报错。

    这是本次的真凶：一条通知横幅就能让整批用例全红。
    """
    drv = _FgDriver(on_screen=True)
    Runner(drv, tmp_path)._dismiss_system_alerts()   # 不抛即通过
    assert drv.dismiss_calls == 0, "树可读时不该去点弹窗"


def test_real_overlay_still_reports_actionable_error(tmp_path):
    """App 树读不到、弹窗也点不掉 ⇒ 仍要抛可行动错误（真浮层不能静默）。"""
    drv = _FgDriver(on_screen=False, alert_tapped=None)
    with pytest.raises(RuntimeError) as exc:
        Runner(drv, tmp_path)._dismiss_system_alerts()
    assert "系统浮层" in str(exc.value) or "浮层" in str(exc.value)
    assert drv.dismiss_calls == 1


def test_wait_landed_accepts_readable_tree_despite_springboard(tmp_path):
    """`_wait_landed`：app_current 报 springboard 但树可读 ⇒ 认为已落地。"""
    drv = _FgDriver(on_screen=True)
    assert Runner(drv, tmp_path)._wait_landed(PKG, tries=2) is True


# ---------------- 软键盘（input → tap 前必须收） -----------------------------

from insta_engine.dsl import TestCase  # noqa: E402


class _KbNode:
    exists = True

    def __init__(self, drv):
        self._drv = drv

    def wait(self, timeout: float = 0, raise_error: bool = False) -> bool:
        return True

    def click(self) -> None:
        self._drv.taps += 1

    def set_text(self, text: str) -> None:
        self._drv.typed.append(text)

    def get_text(self) -> str:
        return "张三"

    def center(self):
        return (100, 200)


class _KbDriver(_FgDriver):
    """能记录 tap / 收键盘的 iOS 桩。"""

    def __init__(self):
        super().__init__(on_screen=True)
        self.taps = 0
        self.typed: list[str] = []
        self.keyboard_hidden = 0

    def app_stop(self, package): ...
    def app_start(self, package): ...
    def dump(self) -> str:
        return '<XCUIElementTypeApplication name="MAT 靶场"/>'
    def screenshot(self, path): ...
    def press(self, name): ...
    def dismiss_keyboard(self) -> bool:
        self.keyboard_hidden += 1
        return True
    def find(self, by, value, timeout): return _KbNode(self)
    def find_xpath(self, expr, timeout): return _KbNode(self)


def _kb_case() -> TestCase:
    return TestCase.model_validate({
        "caseId": "kb", "name": "输入后点击",
        "target": {"package": PKG},
        "steps": [
            {"id": "s1", "intent": "填姓名", "action": "input",
             "target": {"strategy": "res-id", "value": "form_name"},
             "params": {"text": "张三"}},
            {"id": "s2", "intent": "点提交", "action": "tap",
             "target": {"strategy": "res-id", "value": "form_submit"}},
        ],
    })


def test_input_then_tap_hides_keyboard_first(tmp_path):
    """input→tap 必须先收键盘。

    回归（2026-10-19 真机）：iOS 输入后键盘把页面顶起，**WDA 报的元素 frame
    不跟着更新** ⇒ `element.tap()` 点到旧位置；表单用例「填姓名→点提交」失败在
    断言「已提交」，截图里「提交」还被键盘压着。
    """
    drv = _KbDriver()
    run = Runner(drv, tmp_path).run(_kb_case())
    assert run.status == "passed"
    assert drv.typed == ["张三"]
    assert drv.keyboard_hidden == 1, "input 之后第一次 tap 必须先收键盘"
    # tap 次数 ≥1：桩的 dump 是静态的，引擎会判"树无变化"而重放一次（真实设备不会）
    assert drv.taps >= 1
