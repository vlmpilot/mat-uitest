"""BaseDriver 协议结构契约（评审 P0-2 / mat-p0-fix-plan 修复 6）。

外部库契约已由 `test_wda_contract.py` 守（修复 5）；本文件守**内部协议**：
若未来给 `BaseDriver` 新增抽象方法却只更新了一个实现，另一个平台要到**运行时**
才 `TypeError`，静态期零提示。这里把「双实现必须满足抽象方法」「Node 必须同形」
「协议方法的 override 矩阵」前移到**提交时刻**。

零真机依赖：只 import 类、不实例化（`DeviceDriver.__init__` / `WdaDriver.__init__` 才需要 u2/wda）。
"""

from __future__ import annotations

from insta_engine.driver import DeviceDriver
from insta_engine.driver_base import BaseDriver
from insta_engine.wda_driver import WdaDriver, WdaNode


def test_all_abstract_methods_implemented():
    abstracts = {m for m in dir(BaseDriver)
                 if getattr(getattr(BaseDriver, m), "__isabstractmethod__", False)}
    assert abstracts, "BaseDriver 应有抽象方法；空协议说明契约已失效"
    for cls in (DeviceDriver, WdaDriver):
        missing = getattr(cls, "__abstractmethods__", set())
        assert not missing, f"{cls.__name__} 未实现抽象方法: {sorted(missing)}"


def test_wda_node_protocol_shape():
    """WdaNode 必须与 u2 节点同形（Node 协议，见 driver_base 模块 docstring）。"""
    for attr in ("exists", "wait", "click", "center", "set_text",
                 "clear_text", "get_text", "text", "info"):
        assert hasattr(WdaNode, attr), f"WdaNode 缺 {attr}: Node 协议断裂，上层会 AttributeError"


def test_fallback_input_override_matrix():
    """`fallback_input` 的 override 矩阵（修复 1）：Android 有二级通道，iOS 如实"无"。

    - 基类默认 = 无二级通道（返回 False）。
    - Android(`DeviceDriver`) override：华为安全键盘回退（走 adb input text）。
    - iOS(`WdaDriver`) **不** override：无华为类拦截 ⇒ 如实失败，异常语义正确。
    """
    assert BaseDriver.fallback_input is not DeviceDriver.fallback_input, (
        "Android 必须 override fallback_input（华为回退）")
    assert WdaDriver.fallback_input is BaseDriver.fallback_input, (
        "iOS 不得 override fallback_input——无二级通道是事实，需如实报「输入未生效」")
    # 基类默认实现返回值语义
    assert BaseDriver.fallback_input(object(), "x") is False


def test_control_method_override_matrix():
    """F3 操控方法 override 矩阵：open_settings 双平台必须 override（无跨平台默认）。

    home/back 走 press() 默认委托——双平台 press 差异已由各自实现收敛；
    open_recents 仅 iOS override（Android 的 press("recent") 即正确语义）。
    """
    # 基类有默认（具体方法，不是抽象）——上层可无条件调用
    for m in ("home", "back", "open_recents", "open_settings"):
        assert callable(getattr(BaseDriver, m, None)), f"BaseDriver.{m} 缺失"
    # open_settings：无跨平台默认，双平台各自 override
    assert DeviceDriver.open_settings is not BaseDriver.open_settings
    assert WdaDriver.open_settings is not BaseDriver.open_settings
    # 基类默认必须如实报未实现（而不是静默或 AttributeError）
    import pytest

    class _Stub:
        platform = "stub"

    with pytest.raises(NotImplementedError):
        BaseDriver.open_settings(_Stub())
    # open_recents：iOS 必须有自己的手势实现
    assert WdaDriver.open_recents is not BaseDriver.open_recents
    # home/back：默认委托 press 即正确，无需 override（谁 override 了反而要写理由）
    assert DeviceDriver.home is BaseDriver.home
    assert WdaDriver.home is BaseDriver.home
    assert DeviceDriver.back is BaseDriver.back
    assert WdaDriver.back is BaseDriver.back


def test_android_control_implementations():
    """Android 操控实现：open_settings 走显式 Intent（固定串）。"""
    drv = object.__new__(DeviceDriver)
    calls: list[str] = []
    drv.shell = lambda cmd: calls.append(cmd)
    drv.open_settings()
    assert calls == ["am start -a android.settings.SETTINGS"]


def test_ios_control_implementations():
    """iOS 操控实现：recents=上滑停顿手势、settings=bundleId 直启（不绑会话）。"""
    drv = object.__new__(WdaDriver)
    calls: list[tuple] = []

    class _FakeClient:
        @staticmethod
        def swipe(x1, y1, x2, y2, duration): calls.append(("swipe", x1, y1, x2, y2, duration))
        @staticmethod
        def app_launch(bundle): calls.append(("launch", bundle))

    drv._c = _FakeClient()
    drv.window_size = lambda: (390, 844)
    drv.open_recents()
    assert calls[0] == ("swipe", 195, 801, 195, 379, 0.6)
    drv.open_settings()
    # bundleId 直启、不走 app_start ⇒ 不会 _bind_session 把会话漂到设置 App
    assert calls[1] == ("launch", "com.apple.Preferences")


def test_fingerprint_and_meta_accept_shell_response():
    """驱动 `shell()` 返回 `ShellResponse` 时，指纹/型号不能被静默吞成空串。

    2026-10-25 真机评测：Android 走 uiautomator2⇒adbutils 时 `shell()` 不是 str，
    `app_apk_sha256` / `device_meta` 按 str 用 ⇒ TypeError 被 `except` 吞掉，
    `run.json` 的 `app_apk_sha256` 与 `device.model` 恒为空 ⇒ 数字无法对上构建。
    """
    from insta_engine.driver import app_apk_sha256, app_version

    class _Out:
        def __init__(self, output: str):
            self.output = output

    class _Drv:
        platform = "android"

        @staticmethod
        def app_info(package: str) -> dict:  # noqa: ARG004
            return {}

        @staticmethod
        def shell(cmd: str):
            if cmd.startswith("pm path"):
                return _Out("package:/data/app/base.apk\n")
            if cmd.startswith("sha256sum"):
                return _Out("ab" * 32 + "  /data/app/base.apk\n")
            if cmd.startswith("dumpsys package"):
                return _Out("versionName=1.0\n")
            if cmd.startswith("getprop ro.product.model"):
                return _Out("ELE-AL00\n")
            if cmd.startswith("getprop ro.build.version.release"):
                return _Out("10\n")
            return _Out("")

    drv = _Drv()
    assert app_apk_sha256(drv, "com.insta.demo") == "abababababab"
    assert app_version(drv, "com.insta.demo") == "1.0"
    assert DeviceDriver.device_meta(drv) == {"model": "ELE-AL00", "os_version": "10"}
