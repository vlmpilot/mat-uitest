"""证据链契约（P2.2）：syslog 采集器 + 驱动契约 + 分类关键词。

不打真机：syslog 用 `/bin/sh -c printf` 造日志行，驱动用桩。
守的是**判据**：没有证据源时返回空而不是编造、Android 委托单一实现、iOS 只在真机采。
"""

import time

from insta_engine import ios_syslog
from insta_engine.driver import DeviceDriver
from insta_engine.driver_base import BaseDriver
from insta_engine.runner import _classify_builtin
from insta_engine.wda_driver import WdaDriver


class _Stub(BaseDriver):
    """最小驱动桩：只实现抽象方法，用于验证契约默认值。"""

    platform = "stub"

    def dump(self): return ""
    def screenshot(self, path): return None
    def screenshot_pil(self): return None
    def click(self, x, y): return None
    def long_click(self, x, y, duration): return None
    def swipe_ext(self, direction): return None
    def press(self, key): return None
    def send_keys(self, text): return None
    def app_start(self, package, activity=None): return None
    def app_stop(self, package): return None
    def foreground_app(self): return {}
    def find(self, by, value, timeout): return None
    def find_xpath(self, expr, timeout): return None


def test_contract_defaults_are_safe_noops():
    """默认实现必须是"无证据源"而不是抛异常——否则任何桩驱动都跑不了。"""
    drv = _Stub()
    assert drv.start_evidence() is None
    assert drv.evidence_tail() == ""
    assert drv.device_meta() == {}


def test_syslog_buffer_collects_and_clears(monkeypatch):
    monkeypatch.setattr(ios_syslog.SyslogBuffer, "available", staticmethod(lambda: True))
    buf = ios_syslog.SyslogBuffer(
        "u1", cmd=["/bin/sh", "-c", "printf 'line-a\\nline-b\\nline-c\\n'"], max_restarts=0)
    assert buf.start() is True

    deadline = time.monotonic() + 5
    while len(buf) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert buf.tail(2) == "line-b\nline-c", f"实际缓冲：{buf.tail()!r}"

    buf.clear()
    assert buf.tail() == "", "clear 必须清空（对齐 logcat -c 的时效语义）"
    buf.stop()


def test_syslog_unavailable_degrades_quietly(monkeypatch):
    """没装 pymobiledevice3 ⇒ start 返回 False、tail 空——不抛、不编造。"""
    monkeypatch.setattr(ios_syslog.SyslogBuffer, "available", staticmethod(lambda: False))
    buf = ios_syslog.SyslogBuffer("u1")
    assert buf.start() is False
    assert buf.tail() == ""


def test_get_buffer_replaces_old_instance(monkeypatch):
    """同一台设备只保留一个采集器（console 长跑会反复建驱动，防进程泄漏）。"""
    monkeypatch.setattr(ios_syslog.SyslogBuffer, "available", staticmethod(lambda: False))
    a = ios_syslog.get_buffer("u1")
    b = ios_syslog.get_buffer("u1")
    assert a is not b and a._stopped is True, "旧实例必须被停掉"
    ios_syslog.reset()


def test_android_driver_delegates_to_single_impl(monkeypatch):
    """Android 必须委托 adb.py（P1-2 的单一实现纪律），不能在驱动里重写一份。"""
    from insta_engine import adb

    calls: list[tuple] = []
    monkeypatch.setattr(adb, "logcat_clear", lambda serial: calls.append(("clear", serial)) or True)
    monkeypatch.setattr(adb, "logcat_tail", lambda serial: calls.append(("tail", serial)) or "LOG")

    class _D:
        serial = "emulator-5554"  # 撞里程碑正则的测试数据改真实设备号形

        shell = staticmethod(lambda cmd: "Pixel 8")

    drv = DeviceDriver.__new__(DeviceDriver)   # 不走 __init__（不连设备）
    drv.__dict__.update(_D.__dict__)
    drv.start_evidence()
    assert drv.evidence_tail() == "LOG"
    assert calls == [("clear", "emulator-5554"), ("tail", "emulator-5554")]


def test_wda_simulator_does_not_collect(monkeypatch):
    """模拟器不采 syslog（宿主 log stream 不是设备日志）——证据为空是**预期**。"""
    called: list[str] = []
    monkeypatch.setattr(ios_syslog, "get_buffer", lambda udid: called.append(udid))
    drv = WdaDriver(serial="simulator", client=object())
    drv.start_evidence()
    assert called == [] and drv.evidence_tail() == ""


def test_crash_classifier_knows_ios_signals():
    """iOS syslog 的崩溃信号必须能判成 app-crash；泛化的 "fatal error" 不能误判。"""
    assert _classify_builtin("", "…EXC_CRASH (SIGABRT)…") == "app-crash"
    assert _classify_builtin("", "Terminating due to uncaught exception 'NSInvalidArgumentException'") \
        == "app-crash"
    assert _classify_builtin("", "fatal error: something benign") != "app-crash", \
        "iOS 日志里 fatal error 太泛，不该单独作为崩溃证据"


def test_suite_report_shows_device_identity(tmp_path):
    """套件报告要能回答"测的是哪台、哪个系统"（P2.2 溯源）。"""
    import json

    from insta_engine.report import render_index_html
    from insta_engine.runner import RunResult

    run_dir = tmp_path / "artifacts" / "c1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "device_serial": "00008150-000528183C98C01C",
        "platform": "ios",
        "device": {"model": "iPhone 17 Pro", "os_version": "26.5.2"},
    }, ensure_ascii=False), encoding="utf-8")

    rr = RunResult(case_id="c1", case_name="冒烟", status="passed", duration_ms=1000)
    out = render_index_html("suite", [("c1", "c1/report.html", rr)], tmp_path / "artifacts")
    html = out.read_text(encoding="utf-8")
    assert "iPhone 17 Pro" in html, "设备型号必须进报告"
    assert "iOS 26.5.2" in html, "系统版本必须进报告（平台名按惯例大小写）"


def test_locked_error_detection():
    """设备锁屏是环境级故障：套件要能识别并中止，而不是把剩下每条都跑成红。"""
    from types import SimpleNamespace

    from insta_engine.__main__ import _is_device_locked_error

    locked = SimpleNamespace(steps=[SimpleNamespace(error="RuntimeError: 设备锁屏：请解锁 iPhone…")])
    assert _is_device_locked_error(locked) is True
    normal = SimpleNamespace(steps=[SimpleNamespace(error="LocateFailed: 定位失败")])
    assert _is_device_locked_error(normal) is False
    assert _is_device_locked_error(SimpleNamespace(steps=[])) is False
