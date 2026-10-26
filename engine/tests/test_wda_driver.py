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
        self.posts: list[tuple] = []

    def get(self, url):
        self.gets.append(url)
        return {"value": self._fake.dump_xml}

    def post(self, url, data=None):
        # `/wda/keyboard/dismiss` 之类（2026-10-19：click 前会按需收键盘）
        self.posts.append((url, data))
        return {"value": None}


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
        ("text", "通用", "label == '通用' OR placeholderValue == '通用'"),
        ("textContains", "通", "label CONTAINS '通' OR placeholderValue CONTAINS '通'"),
        ("description", "go", "name == 'go'"),
        ("descriptionContains", "go", "name CONTAINS 'go'"),
    ]
    for by, value, _pred in cases:
        drv.find(by, value, 0)
    assert [s.predicate for s in fake.selectors] == [pred for _, _, pred in cases]


def test_find_escapes_single_quote():
    drv, fake = make_driver()
    drv.find("text", "it's", 0)
    assert fake.selectors[-1].predicate == "label == 'it\\'s' OR placeholderValue == 'it\\'s'"


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


def test_is_device_udid_distinguishes_simulator():
    """真机 udid 是 8+16 hex；模拟器是 36 位四段 UUID——混用会让真机去连 localhost。"""
    from insta_engine.wda_driver import _is_device_udid

    assert _is_device_udid("00008150-000528183C98C01C"), "真机 udid 应识别"
    assert not _is_device_udid("7544DE5E-8925-4E7E-9D51-D6AA4778F4A3"), "模拟器 UUID 不是真机"
    assert not _is_device_udid(None) and not _is_device_udid("simulator")


def test_locked_device_error_is_translated_to_actionable_message():
    """真机锁屏时 WDA 建会话必失败——要翻成"解锁 + 自动锁定设为永不"，不是原始 FBS 报错。

    2026-10-17 实测：`Unable to launch (null) because the device was not, or could not be,
    unlocked.` 原样抛到报告里没人看得懂，而它是**环境问题**、有明确解法。
    """

    class _Locked(FakeClient):
        def session(self, bundle=None):
            raise RuntimeError("Unable to launch (null) because the device was not, or could not "
                               "be, unlocked. Error Domain=FBSOpenApplicationErrorDomain Code=7")

    drv = WdaDriver(serial="00008150-000528183C98C01C", client=_Locked())
    with pytest.raises(RuntimeError) as ei:
        drv.dump()
    msg = str(ei.value)
    assert "解锁" in msg and "自动锁定" in msg, msg
    assert "FBSOpenApplicationErrorDomain" not in msg, "原始域名不该透给用户"


def test_other_session_errors_pass_through_unchanged():
    """只翻译锁屏这一类；其它错误原样抛（不要把所有异常都包成 RuntimeError）。"""

    class _Boom(FakeClient):
        def session(self, bundle=None):
            raise RuntimeError("some other failure")

    drv = WdaDriver(serial="00008150-000528183C98C01C", client=_Boom())
    with pytest.raises(RuntimeError, match="some other failure"):
        drv.dump()


def test_dump_xml_source_error_gets_actionable_hint():
    """锁屏时 WDA 报 `Cannot get 'xml' source`（不是 session 失败）——同样要翻成人话。"""

    class _Http:
        def get(self, path):
            raise RuntimeError("Cannot get 'xml' source of the current application")

    class _Sess:
        http = _Http()

    class _Client(FakeClient):
        def session(self, bundle=None):
            return _Sess()

    drv = WdaDriver(serial="00008150-000528183C98C01C", client=_Client())
    with pytest.raises(RuntimeError) as ei:
        drv.dump()
    assert "锁屏" in str(ei.value) and "自动锁定" in str(ei.value)


def _switch_node(inner_exists: bool, tapped: list[str]):
    """构造一个 Switch 节点：行元素 + （可选）内层控件，记录谁被 tap。"""
    class _Inner:
        @property
        def exists(self):
            return inner_exists

        def get(self):
            class _E:
                def tap(self):
                    tapped.append("inner")
            return _E()

    class _Row:
        className = "XCUIElementTypeSwitch"

        def tap(self):
            tapped.append("row")

    class _Sel:
        def get(self):
            return _Row()

        def child(self, type=None):  # noqa: A002 —— 对齐 wda.Selector 的签名
            assert type == "XCUIElementTypeSwitch", "只应找内层开关控件"
            return _Inner()

    return WdaNode(_Sel(), drv=None)


def test_switch_click_taps_inner_control_not_row():
    """Switch 的**行元素**覆盖整行，tap 打行中心不触发开关（2026-10-17 真机+模拟器实测）。

    这就是真机调试记录 t12「Switch 合成点击失效」的根因：不是合成事件不被接受，
    而是**点错了位置**。修复 = 优先点内层 `XCUIElementTypeSwitch` 控件。
    """
    tapped: list[str] = []
    _switch_node(inner_exists=True, tapped=tapped).click()
    assert tapped == ["inner"], f"应点内层控件，实际 {tapped}"


def test_switch_click_falls_back_to_row_when_no_inner():
    """拿不到内层控件时退回整元素 tap（不能因为找不到子控件就报错）。"""
    tapped: list[str] = []
    _switch_node(inner_exists=False, tapped=tapped).click()
    assert tapped == ["row"]


def test_usbmux_transient_error_retries_once(monkeypatch):
    """usbmux 瞬断（切网络/拔插）重连后重试一次——2026-10-17 真机实测会遇到。"""

    class _Flaky(FakeClient):
        def __init__(self):
            super().__init__()
            self.n = 0

        def app_current(self):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("'NoneType' object has no attribute 'make_http_connection'")
            return {"bundleId": "com.x", "name": "X", "pid": 1}

    drv = WdaDriver(serial="00008150-000528183C98C01C", client=_Flaky())
    reconnects: list[int] = []
    monkeypatch.setattr(drv, "_reconnect", lambda: reconnects.append(1))
    assert drv.foreground_app()["package"] == "com.x"
    assert reconnects == [1], "瞬断应重连一次并重试"


def test_business_error_is_not_retried(monkeypatch):
    """业务错误（定位失败/锁屏）不重试——重试只会把 1 次失败拖成 2 次。"""

    class _Bad(FakeClient):
        def app_current(self):
            raise RuntimeError("element not found")

    drv = WdaDriver(serial="00008150-000528183C98C01C", client=_Bad())
    reconnects: list[int] = []
    monkeypatch.setattr(drv, "_reconnect", lambda: reconnects.append(1))
    with pytest.raises(RuntimeError, match="element not found"):
        drv.foreground_app()
    assert reconnects == [], "业务错误不该触发重连"


# ---------------- 系统弹窗（2026-10-19：只在 App 会话里点，不碰 springboard） ----

class _AlertClient(FakeClient):
    """可控制"App 会话里有没有弹窗按钮"的桩。"""

    def __init__(self, app_exists: bool = False):
        super().__init__(selector_exists=app_exists)
        self.app_exists = app_exists

    def session(self, bundle=None):
        self.child_exists = self.app_exists
        return super().session(bundle)


def test_dismiss_system_alert_taps_app_session_button():
    """挂在 App 会话上的弹窗：一次 predicate 命中就点掉。"""
    client = _AlertClient(app_exists=True)
    drv = WdaDriver(client=client)
    assert drv.dismiss_system_alert() == "?"
    assert client.selectors[-1]._element.tapped == 1


def test_dismiss_system_alert_never_creates_springboard_session():
    """**绝不建 springboard 会话**（2026-10-19 真机定论）。

    现象：建该会话会激活/重启 SpringBoard ——「黑屏 + 转圈（respring）→ 锁屏」。
    宁可如实报错（由 runner 提示用户手动点一次），也不能自伤把设备锁了。
    """
    client = _AlertClient(app_exists=False)
    drv = WdaDriver(client=client)
    assert drv.dismiss_system_alert() is None
    assert "com.apple.springboard" not in client.session_bundles


# ---------------- 系统弹窗（2026-10-19：/alert/accept 走 App 会话） ---------------

class _FakeAlert:
    def __init__(self, labels):
        self._labels = list(labels)
        self.clicked: str | None = None
        self.exists = True

    def buttons(self):
        return self._labels

    def click(self, name):
        self.clicked = name
        return name

    def accept(self):
        self.clicked = "accept"


class _AlertSession(_Session):
    def __init__(self, fake, bundle, alert):
        super().__init__(fake, bundle)
        self.alert = alert


class _AlertAwareClient(FakeClient):
    def __init__(self, labels):
        super().__init__()
        self.alert = _FakeAlert(labels)

    def session(self, bundle=None):
        self.session_bundles.append(bundle)
        return _AlertSession(self, bundle, self.alert)


def test_system_alert_allow_first_via_alert_api():
    """数据授权弹窗：按名字点**最宽松**的「无线局域网与蜂窝网络」。

    实测（2026-10-19）：系统弹窗在 App 会话里可见，`/alert/accept {"name"}` 能点中；
    完全不需要（也不能）建 springboard 会话。
    """
    client = _AlertAwareClient(["无线局域网与蜂窝网络", "仅限无线局域网", "不允许"])
    drv = WdaDriver(client=client)
    assert drv.dismiss_system_alert() == "无线局域网与蜂窝网络"
    assert client.alert.clicked == "无线局域网与蜂窝网络"


def test_system_alert_falls_back_to_deny_when_no_allow():
    """没有"允许"类按钮时才点拒绝（弹窗不消失比拒绝更糟）。"""
    client = _AlertAwareClient(["不允许", "取消"])
    drv = WdaDriver(client=client)
    assert drv.dismiss_system_alert() == "不允许"
    assert client.alert.clicked == "不允许"


# ---------------- App 是否在屏幕上（2026-10-19：通知横幅会骗过 app_current） ----

def test_app_on_screen_true_when_tree_readable():
    """通知横幅在时 `app_current()` 报 springboard，但 App 树可读 ⇒ 仍在屏幕上。

    回归（2026-10-19 真机）：截图里 MAT 靶场主菜单清清楚楚，`app_current()`
    却返回 com.apple.springboard（通知横幅归 SpringBoard）⇒ 每步都误判
    "有系统浮层盖住前台"，42 条用例全红。
    """
    client = FakeClient(dump_xml='<XCUIElementTypeApplication name="MAT 靶场"/>',
                        bundle="com.apple.springboard")
    drv = WdaDriver("00008101-000A1B2C3D4E5F60", client=client)
    assert drv.app_on_screen("com.u56rw5zm8j.WebDriverAgentRunner") is True


def test_app_on_screen_false_when_tree_unreadable():
    """App 真退到后台：拿不到树 ⇒ 才算不在屏幕上。"""
    client = FakeClient(dump_xml="<XCUI/>")   # 没有 Application 根节点
    drv = WdaDriver("00008101-000A1B2C3D4E5F60", client=client)
    assert drv.app_on_screen("com.u56rw5zm8j.WebDriverAgentRunner") is False


# ---------------- 软键盘（2026-10-19：WDA 没有收键盘端点，只能发回车） ------------

class _KeysClient(FakeClient):
    """记录 `send_keys` 的桩。"""

    def __init__(self):
        super().__init__()
        self.sent: list[str] = []

    def send_keys(self, value):
        self.sent.append(value)


def test_dismiss_keyboard_sends_return():
    """WDA v16.15.3 没有 `/wda/keyboard/dismiss`；实测有效的是给输入框发回车。

    单行 TextField 收到回车 ⇒ 结束编辑 ⇒ 键盘收起（真机：form_name 的 y 从 116
    回到 249）。多行 TextEditor 会插入换行，所以只由 Runner 在 input→tap 时调用。
    """
    client = _KeysClient()
    assert WdaDriver(client=client).dismiss_keyboard() is True
    assert client.sent == ["\n"]


def test_unreachable_wda_error_is_translated_to_actionable_message():
    """`'NoneType' object has no attribute 'make_http_connection'` 必须翻译成人话。

    实测（2026-10-20）：设备走 Wi-Fi 时就是这个错误，用户看不出该插线还是该起 WDA。
    """
    from insta_engine.wda_driver import WdaDriver

    raw = AttributeError("'NoneType' object has no attribute 'make_http_connection'")
    out = WdaDriver._explain(raw)
    assert isinstance(out, RuntimeError)
    assert "Wi-Fi" in str(out) and "ios-setup" in str(out)


def test_other_errors_pass_through_unchanged():
    from insta_engine.wda_driver import WdaDriver

    err = ValueError("别的东西")
    assert WdaDriver._explain(err) is err
