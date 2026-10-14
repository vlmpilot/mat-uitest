"""P0：平台中立驱动接口的结构护栏。

DeviceDriver 必须是 BaseDriver 的子类；抽象方法集合=上层允许依赖的全部驱动能力
（移植面盘点见 driver_base 模块注释——14 处 `.raw` 调用点要迁到这些方法上）；
By 常量必须覆盖 locator 的选择器 kwargs（防中立层与调用层漂移）。
"""

from insta_engine.driver import DeviceDriver
from insta_engine.driver_base import BaseDriver, By


def test_devicedriver_implements_base_protocol():
    assert issubclass(DeviceDriver, BaseDriver)
    assert DeviceDriver.platform == "android"


def test_abstract_surface_is_the_port_surface():
    assert set(BaseDriver.__abstractmethods__) == {
        "dump", "screenshot", "screenshot_pil",
        "click", "long_click", "swipe_ext", "press", "send_keys",
        "app_start", "app_stop", "foreground_app",
        "find", "find_xpath",
    }


def test_by_constants_cover_locator_selector_kwargs():
    from insta_engine.locator import _SELECTOR_KWARGS

    assert set(_SELECTOR_KWARGS.values()) <= {b.value for b in By}
    assert By.DESCRIPTION_CONTAINS.value in ("textContains", "descriptionContains")


def test_shell_is_opt_in_escape_hatch():
    """shell 只在 Android 驱动实现；BaseDriver 默认拒绝（iOS 无对应物）。"""

    class _Minimal(BaseDriver):
        platform = "test"

        # 仅实现必需项以验证 shell 默认抛错；其余 abstractmethod 用 pass 满足
        def dump(self): ...
        def screenshot(self, path): ...
        def screenshot_pil(self): ...
        def click(self, x, y): ...
        def long_click(self, x, y, duration): ...
        def swipe_ext(self, direction): ...
        def press(self, key): ...
        def send_keys(self, text): ...
        def app_start(self, package, activity=None): ...
        def app_stop(self, package): ...
        def foreground_app(self): ...
        def find(self, by, value, timeout): ...
        def find_xpath(self, expr, timeout): ...

    import pytest

    with pytest.raises(NotImplementedError):
        _Minimal().shell("anything")
