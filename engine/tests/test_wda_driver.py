"""WdaDriver 单测：全走注入的 FakeClient，不打真网络/真模拟器。

覆盖：lean dump 口径（U2 依据）/ foreground_app 的 bundleId→package 契约 /
By→predicate 映射与转义 / timeout=0 不等待语义 / press 的 iOS 映射 /
WdaNode 的 info 同形与 value 优先读回 / visible 几何判断。
"""

from __future__ import annotations

import pytest

from insta_engine.driver_base import BaseDriver
from insta_engine.locator import Element, LocatorNotSupported
from insta_engine.wda_driver import WdaDriver, WdaNode


class _Rect:
    def __init__(self, x, y, width, height):
        self.x, self.y, self.width, self.height = x, y, width, height


class _Element:
    def __init__(self, text=None, value=None, enabled=True, selected=False,
                 bounds=None):
        self.text, self.value = text, value
        self.enabled, self.selected = enabled, selected
        self.bounds = bounds or _Rect(10, 10, 100, 40)
        self.set_texts: list[str] = []
        self.cleared = False
        self.tapped = 0

    def set_text(self, text):
        self.set_texts.append(text)
        self.value = text

    def tap(self):
        self.tapped += 1

    def tap_hold(self, duration):
        self.tap_holds = getattr(self, "tap_holds", []) + [duration]

    def clear_text(self):
        self.cleared = True
        self.value = ""


class _Selector:
    def __init__(self, predicate, element=None, exists=True, registry=None):
        self.predicate = predicate
        self._element = element or _Element()
        self._exists = exists
        self.waits: list[float] = []
        self.clicked = False
        self.registry = registry if registry is not None else []

    @property
    def exists(self):
        return self._exists

    def wait(self, timeout=None, raise_error=False):
        self.waits.append(timeout)
        return self._exists

    def click(self):
        self.clicked = True

    def get(self):
        return self._element

    def child(self, **kwargs):
        child = _Selector(f"{self.predicate}>{kwargs.get('type', '')}",
                          exists=self._exists, registry=self.registry)
        self.registry.append(child)
        return child


class _SessionHTTP:
    def __init__(self, fake):
        self._fake = fake
        self.gets: list[str] = []

    def get(self, url):
        self.gets.append(url)
        return {"value": self._fake.dump_xml}


class _Session:
    def __init__(self, fake, bundle):
        self.bundle = bundle
        self.http = _SessionHTTP(fake)
        self._fake = fake

    def __call__(self, **kwargs):
        pred = kwargs.get("predicate") or f"type={kwargs.get('type', '')}"
        sel = _Selector(pred,
                        registry=self._fake.selectors,
                        exists=self._fake.child_exists)
        self._fake.selectors.append(sel)
        return sel


class FakeClient:
    """最小 WDA 客户端桩：记录调用、按脚本返回。"""

    def __init__(self, dump_xml="<XCUI/>", bundle="com.apple.Preferences",
                 size=(402, 874), selector_exists=True):
        self.dump_xml = dump_xml
        self.bundle = bundle
        self.size = size
        self.child_exists = selector_exists
        self.selectors: list[_Selector] = []
        self.calls: list[tuple] = []
        self.session_bundles: list[str | None] = []
        self.element = _Element(value="输入内容", bounds=_Rect(0, 900, 402, 60))

    def session(self, bundle=None):
        self.session_bundles.append(bundle)
        return _Session(self, bundle)

    def app_current(self):
        return {"bundleId": self.bundle, "name": "Settings", "pid": 1}

    def app_launch(self, bundle, *a, **k):
        self.calls.append(("app_launch", bundle))

    def app_terminate(self, bundle, *a, **k):
        self.calls.append(("app_terminate", bundle))

    def click(self, x, y):
        self.calls.append(("click", x, y))

    def tap_hold(self, x, y, duration):
        self.calls.append(("tap_hold", x, y, duration))

    def swipe(self, x1, y1, x2, y2, duration=0):
        self.calls.append(("swipe", x1, y1, x2, y2, duration))

    def home(self):
        self.calls.append(("home",))

    def send_keys(self, text):
        self.calls.append(("send_keys", text))

    def screenshot(self):
        self.calls.append(("screenshot",))

    def window_size(self):
        return self.size


def make_driver(**kw):
    fake = FakeClient(**kw)
    return WdaDriver(client=fake), fake


def test_implements_base_protocol_and_platform():
    drv, _ = make_driver()
    assert isinstance(drv, BaseDriver) and drv.platform == "ios"


def test_dump_uses_lean_query_and_session_binding():
    drv, fake = make_driver()
    assert drv.dump() == "<XCUI/>"
    s = drv._session
    assert s.http.gets == ["/source?format=xml&excluded_attributes=visible"], (
        "dump 必须走 lean 口径——visible 的逐元素解析占快照 4-5x 延迟（U2）")


def test_foreground_app_maps_bundle_to_package():
    drv, fake = make_driver(bundle="com.apple.Preferences")
    info = drv.foreground_app()
    assert info["package"] == "com.apple.Preferences" == info["bundleId"]


def test_find_builds_predicate_per_by():
    drv, fake = make_driver()
    cases = [
        ("resourceId", "go", "name == 'go'"),
        ("text", "通用", "label == '通用'"),
        ("textContains", "通", "label CONTAINS '通'"),
        ("description", "go", "name == 'go'"),
        ("descriptionContains", "go", "name CONTAINS 'go'"),
    ]
    for by, value, _pred in cases:
        drv.find(by, value, 0)
    assert [s.predicate for s in fake.selectors] == [pred for _, _, pred in cases]


def test_find_escapes_single_quote():
    drv, fake = make_driver()
    drv.find("text", "it's", 0)
    assert fake.selectors[-1].predicate == "label == 'it\\'s'"


def test_find_timeout_zero_skips_wait_positive_waits():
    drv, fake = make_driver()
    drv.find("text", "通用", 0)
    assert fake.selectors[-1].waits == [], "timeout=0 不得等待（text 链末级回退语义）"
    drv.find("text", "通用", 2.5)
    assert fake.selectors[-1].waits == [2.5]


def test_find_unsupported_by_raises():
    drv, _ = make_driver()
    with pytest.raises(LocatorNotSupported):
        drv.find("xpath", "anything", 1)


def test_find_xpath_not_supported():
    drv, _ = make_driver()
    with pytest.raises(LocatorNotSupported):
        drv.find_xpath("//Button", 1)


def test_press_maps_home_and_back():
    drv, fake = make_driver()
    drv.press("home")
    assert ("home",) in fake.calls
    drv.press("back")
    navbar, back_btn = fake.selectors[0], fake.selectors[1]
    assert "NavigationBar" in navbar.predicate, "返回主路径=导航栏返回钮（label=上一页标题）"
    assert "Button" in back_btn.predicate
    assert back_btn._element.tapped == 1
    assert not [c for c in fake.calls if c[0] == "swipe"], "返回钮存在时不得走滑动兜底"
    with pytest.raises(ValueError):
        drv.press("menu")


def test_press_back_falls_back_to_edge_swipe_when_no_navbar():
    """无导航栏返回钮（全屏页/自绘导航）⇒ 左缘滑动兜底（模拟器上不可靠，真机 P2 复验）。"""
    drv, fake = make_driver(selector_exists=False)
    drv.press("back")
    assert [c for c in fake.calls if c[0] == "swipe"], "兜底必须是左缘滑动（起点 x=1）"
    sw = [c for c in fake.calls if c[0] == "swipe"][-1]
    assert sw[1] == 1


def test_app_start_ignores_activity_and_binds_session():
    drv, fake = make_driver()
    drv.app_start("com.example.app", "com.example.app/.MainActivity")
    assert ("app_launch", "com.example.app") in fake.calls
    assert fake.session_bundles[-1] == "com.example.app"


def test_swipe_ext_uses_window_size():
    drv, fake = make_driver(size=(400, 800))
    drv.swipe_ext("up")
    (_, x1, y1, x2, y2, _) = [c for c in fake.calls if c[0] == "swipe"][-1]
    assert x1 == x2 == 200 and y1 > y2, "上滑应从下往上"


def test_wda_node_info_mirrors_selected_to_checked():
    drv, fake = make_driver()
    node = drv.find("text", "开关", 0)
    fake.selectors[-1]._element = _Element(selected=True, enabled=False)
    info = node.info
    assert info == {"enabled": False, "checked": True, "selected": True}, (
        "普通控件：selected 即 checked 同源")


def test_wda_node_switch_checked_reads_value():
    """15×3 评测 t12 发现：Switch 的开合在 value（"0"/"1"）而非 selected。"""
    drv, fake = make_driver()
    node = drv.find("text", "开关", 0)

    class _SwitchEl(_Element):
        className = "XCUIElementTypeSwitch"

    fake.selectors[-1]._element = _SwitchEl(value="1", selected=False)
    assert node.info["checked"] is True, "Switch value=1 ⇒ checked"
    fake.selectors[-1]._element = _SwitchEl(value="0", selected=True)
    assert node.info["checked"] is False, "Switch value=0 ⇒ 未勾选（selected 不参与 Switch）"


def test_long_click_node_uses_element_level_tap_hold():
    """U8 标定：iOS 坐标级 touchAndHold 不稳定，元素级 tap_hold 可靠——
    long_click_node 必须 override 默认的中心坐标路径。"""
    drv, fake = make_driver()
    node = drv.find("text", "行", 0)
    node.long_click()
    assert fake.selectors[-1]._element.tap_holds == [2.0], (
        "节点长按必须走元素级 tap_hold（2.0s），不走坐标级")
    drv.long_click_node(node)
    assert fake.selectors[-1]._element.tap_holds == [2.0, 2.0]


def test_connect_driver_routes_by_platform(monkeypatch):
    """_connect_driver 按 --platform 分派；缺省 android（行为不变）。

    路由已收敛到 `driver_factory.connect_driver` 单源（评审 I4）——工厂在调用时
    从 `driver` / `wda_driver` 模块取类，故 monkeypatch 打在这两个模块上。
    """
    from insta_engine import __main__ as cli
    from insta_engine import driver as drv_mod
    from insta_engine import wda_driver as wd

    class FakeWda:
        def __init__(self, serial=None):
            self.serial = serial

    class FakeAndroid:
        def __init__(self, serial=None):
            self.serial = serial

    monkeypatch.setattr(wd, "WdaDriver", FakeWda)
    monkeypatch.setattr(drv_mod, "DeviceDriver", FakeAndroid)
    assert isinstance(cli._connect_driver(None, "ios"), FakeWda)
    assert isinstance(cli._connect_driver(None, "android"), FakeAndroid)
    assert isinstance(cli._connect_driver(None), FakeAndroid), "缺省必须 android（Android 零影响）"


def test_driver_factory_routes_and_defaults(monkeypatch):
    """共享工厂（评审 I4）：单源平台路由；默认 android；iOS 走 WdaDriver。"""
    from insta_engine import driver as drv_mod
    from insta_engine import driver_factory as fac
    from insta_engine import wda_driver as wd

    calls: list[tuple] = []

    class FakeWda:
        def __init__(self, serial=None):
            calls.append(("ios", serial))

    class FakeAndroid:
        def __init__(self, serial=None):
            calls.append(("android", serial))

    monkeypatch.setattr(wd, "WdaDriver", FakeWda)
    monkeypatch.setattr(drv_mod, "DeviceDriver", FakeAndroid)
    fac.connect_driver("serial-1")
    fac.connect_driver("serial-2", "ios")
    assert calls == [("android", "serial-1"), ("ios", "serial-2")], "默认 android；显式 ios 走 WdaDriver"


def test_explore_and_index_ios_honestly_blocked(monkeypatch, capsys):
    """explore/index 的 iOS 适配在 P3——必须诚实拒绝而非跑一半崩。"""
    import types

    from insta_engine import __main__ as cli

    for cmd in (cli.cmd_explore, cli.cmd_index):
        capsys.readouterr()
        rc = cmd(types.SimpleNamespace(platform="ios", serial=None))
        assert rc == 1
        assert "P3" in capsys.readouterr().out


def test_wda_node_get_text_prefers_value():
    drv, fake = make_driver()
    node = drv.find("text", "输入框", 0)
    fake.selectors[-1]._element = _Element(text="label文本", value="输入内容")
    assert node.get_text() == "输入内容", "输入框读回应取 value（T28 回读语义）"
    fake.selectors[-1]._element = _Element(text="label文本", value=None)
    assert node.get_text() == "label文本"


def test_wda_node_visible_geometric():
    drv, fake = make_driver(size=(402, 874))
    node = WdaNode(_Selector("p", _Element(bounds=_Rect(10, 10, 100, 40))), drv)
    assert node.visible is True
    off = WdaNode(_Selector("p", _Element(bounds=_Rect(-400, 10, 100, 40))), drv)
    assert off.visible is False, "rect 完全出屏 ⇒ 不可见（lean dump 丢了 visible，几何补）"


# -- P0-A 修复1：iOS 输入失败须如实报「输入未生效」（不得抛 shell 的 NotImplementedError）-----


def _stale_element_cls():
    """模拟「输入未生效」：set_text 被调用但不改 value/text（华为类拦截的 iOS 对应物）。"""

    class _StaleElement(_Element):
        def __init__(self):
            super().__init__(text=None, value=None)

        def set_text(self, text):
            self.set_texts.append(text)  # 记录但不改 value ⇒ 回读仍为空

    return _StaleElement


def test_set_text_ios_reports_honest_failure():
    """修复1 反证：iOS 输入回读为空时，异常文本必须与真因一致（评审 I1 / P0-A）。

    旧行为：`Element.set_text` → `_adb_input_text` → `drv.shell()` ⇒ 抛
    `NotImplementedError: ios 驱动不支持 shell`（与"输入未生效"无关，还污染 diagnose 归因）。
    新行为：走协议 `fallback_input` ⇒ iOS 返回 False ⇒ `RuntimeError("输入未生效…")`。
    """
    drv = WdaDriver(client=FakeClient())
    node = drv.find("text", "用户名", 0)
    drv._c.selectors[-1]._element = _stale_element_cls()()
    el = Element(strategy="text", value="用户名", node=node, drv=drv)
    with pytest.raises(RuntimeError, match="输入未生效"):
        el.set_text("hello")


def test_set_text_ios_never_raises_shell_notimplemented():
    """同一路径不得再冒 `NotImplementedError: ios 驱动不支持 shell`（把旧 P0 形态钉死）。"""
    drv = WdaDriver(client=FakeClient())
    node = drv.find("text", "用户名", 0)
    drv._c.selectors[-1]._element = _stale_element_cls()()
    el = Element(strategy="text", value="用户名", node=node, drv=drv)
    try:
        el.set_text("hello")
    except NotImplementedError as exc:  # noqa: BLE001
        pytest.fail(f"iOS 输入失败不得抛 NotImplementedError(shell)：{exc}")
    except RuntimeError:
        pass  # 期望：如实报「输入未生效」


# -- 修复2：serial 取真实 udid（T76 纪律：对外数字须能回答"哪台设备"）-------------------------


def test_serial_resolves_real_udid_from_device_info():
    """构造时经 /wda/device/info 取真实 udid（评审 I8：此前恒为字面量 'simulator'）。"""

    class _FakeWithInfo(FakeClient):
        def device_info(self):
            return {"uuid": "8A3C9B00-1234-5678-9ABC-DEF012345678",
                    "model": "iPhone 15", "simulator": False}

    drv = WdaDriver(client=_FakeWithInfo())
    assert drv.serial == "8A3C9B00-1234-5678-9ABC-DEF012345678"


def test_serial_falls_back_to_simulator_when_udid_absent():
    """取不到 uuid（旧版 WDA / 无 device_info）⇒ 回落 'simulator' 且不抛异常（不影响主流程）。"""
    drv = WdaDriver(client=FakeClient())  # FakeClient 无 device_info
    assert drv.serial == "simulator"


def test_serial_explicit_argument_skips_probe():
    """显式传 serial 时不探测设备（保持既有语义，探测失败也不影响）。"""
    drv = WdaDriver(serial="explicit-udid", client=FakeClient())
    assert drv.serial == "explicit-udid"
