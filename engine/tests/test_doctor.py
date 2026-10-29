"""doctor 检查函数库单测(F2):结构与纪律,不断言环境相关的具体值。

doctor 是「检查逻辑与展示分离」的产物(CLI 与 /api/doctor 同一份数据);
这里守住的是结构契约:关键 4 项的次序(CLI「前 4 项」结论依据)、iOS 组
不计入退出码、wda-server 的降级态、以及 status 三层语义的完备性。
"""

from __future__ import annotations

import sys
from pathlib import Path

from insta_engine import doctor


def test_android_checks_structure():
    checks = doctor.android_checks()
    ids = [c.id for c in checks]
    # 关键 4 项的次序是 CLI「前 4 项」结论与退出码的依据,不能动
    assert ids[:4] == ["python", "adb", "devices", "u2"]
    assert all(c.critical for c in checks[:4])
    assert all(c.status in ("ok", "warn", "fail") for c in checks)
    # 提示级检查(可选能力)缺失必须是 warn,不得升格为 fail
    # （scrcpy 已下架,发起人 2026-10-12:可选能力易被误读为缺失告警,后续需要再恢复）
    for cid in ("notify", "vl", "gen"):
        c = next(c for c in checks if c.id == cid)
        assert not c.critical
        if not c.ok:
            assert c.status == "warn", f"{cid} 缺失应为 warn"


def test_ios_checks_structure(monkeypatch):
    # usbmux 已隐藏（发起人 2026-10-12）：iOS 真机链路未支持前不展示该检查项
    from insta_engine import ios_device

    monkeypatch.setattr(doctor, "_wda_installed", lambda: False)
    checks = doctor.ios_checks()
    assert {c.id for c in checks} == {"wda-lib", "simctl", "simulators", "wda-server"}
    # iOS 全组是可选能力:不影响「能否开测」结论(与 CLI 退出码口径一致)
    assert all(not c.critical for c in checks)

    # 装了 [ios] extra 才多出真机两行（2026-10-17 P2）——纯 Android 用户看不到，
    # 避免"永远 warn 的噪音"（与 usbmux 检查被隐藏同一理由）。
    monkeypatch.setattr(doctor, "_wda_installed", lambda: True)
    monkeypatch.setattr(ios_device, "list_devices", lambda: [])
    monkeypatch.setattr(ios_device, "team_id", lambda: None)
    monkeypatch.setattr(ios_device, "xcode_account_count", lambda: 0)
    ids = {c.id for c in doctor.ios_checks()}
    assert ids == {"wda-lib", "simctl", "simulators", "wda-server",
                   "ios-device", "ios-team", "xcode-account"}


def test_ios_checks_structure_on_non_macos(monkeypatch):
    """非 macOS 也返回同一套结构（2026-10-25 D-1：契约改成「渲染由调用方决定」）。

    原实现非 macOS 直接 `return []` ⇒ 把平台判断写进了**检查**函数，而 CLI 又自己判了一次
    平台。于是直接调它的测试在 Linux/Windows CI 上 10 条全 KeyError（`ci` 连红 7 天的
    来源之一）。这里钉住新契约：
    - 结构不随平台变（否则调用方又要自己判平台）；
    - 不可执行的修复动作一律不给按钮（非 macOS 上「装 Xcode/开模拟器/启 WDA」都是空话）；
    - 必须说清「为什么不可用」——留白无法区分「没检查」和「不支持」。
    """
    from insta_engine import ios_device

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(doctor, "_wda_installed", lambda: True)
    monkeypatch.setattr(doctor, "_wda_status_ok", lambda: False)
    monkeypatch.setattr(ios_device, "list_devices", lambda *a, **k: [])
    monkeypatch.setattr(ios_device, "team_id", lambda: None)
    monkeypatch.setattr(ios_device, "xcode_account_count", lambda: 0)

    rows = {c.id: c for c in doctor.ios_checks()}
    assert {"wda-lib", "simctl", "simulators", "wda-server"} <= set(rows)
    assert all(c.fix is None for c in rows.values())
    assert "macOS" in rows["simctl"].as_dict()["detail"]


def test_run_merges_groups():
    assert len(doctor.run("all")) == (len(doctor.android_checks())
                                      + len(doctor.ios_checks())
                                      + len(doctor.harmony_checks()))
    assert len(doctor.run("android")) == len(doctor.android_checks())
    assert len(doctor.run("ios")) == len(doctor.ios_checks())
    assert len(doctor.run("harmony")) == len(doctor.harmony_checks())


def test_wda_server_downgrades_without_lib(monkeypatch):
    """wda-lib 未装 ⇒ wda-server 呈 warn(诚实降级,不是一片红)——设计表 3-2 注。"""
    monkeypatch.setattr(doctor, "_wda_installed", lambda: False)
    monkeypatch.setattr(doctor, "_wda_status_ok", lambda: False)
    checks = {c.id: c for c in doctor.ios_checks()}
    assert checks["wda-lib"].status == "warn"
    assert checks["wda-server"].status == "warn"


def test_wda_server_fails_when_lib_present(monkeypatch):
    """装了库但服务没起 = 想用 iOS 而当前不可用 ⇒ fail(显式覆盖 status)。"""
    monkeypatch.setattr(doctor, "_wda_installed", lambda: True)
    monkeypatch.setattr(doctor, "_wda_status_ok", lambda: False)
    checks = {c.id: c for c in doctor.ios_checks()}
    assert checks["wda-server"].status == "fail"


def test_check_status_inference():
    ok = doctor.Check(id="x", label="x", ok=True, critical=True)
    crit_fail = doctor.Check(id="y", label="y", ok=False, critical=True)
    opt_fail = doctor.Check(id="z", label="z", ok=False)
    assert (ok.status, crit_fail.status, opt_fail.status) == ("ok", "fail", "warn")
    # as_dict 携带全部字段(配置台渲染需要 status/detail/hint)
    d = opt_fail.as_dict()
    assert d["id"] == "z" and d["status"] == "warn" and "hint" in d and "detail" in d


# ---- 控制台 iOS 卡片：真机三项 + 两个修复动作（2026-10-20） --------------------

def _with_device(monkeypatch, transport="wired"):
    from insta_engine import ios_device

    dev = {"udid": "00008150-000528183C98C01C", "name": "iPhone", "model": "iPhone 17 Pro",
           "os_version": "26.5.2", "transport": transport, "tunnel": "", "paired": True,
           "developer_mode": True}
    monkeypatch.setattr(doctor, "_wda_installed", lambda: True)
    monkeypatch.setattr(ios_device, "list_devices", lambda *a, **k: [dev])
    monkeypatch.setattr(ios_device, "team_id", lambda: "U56RW5ZM8J")
    monkeypatch.setattr(ios_device, "xcode_account_count", lambda: 1)
    return dev


def test_ios_checks_include_cert_and_signed_runner(monkeypatch):
    """控制台此前看不到证书/已签名产物——真机最常卡住的两项必须在卡片里。"""
    from insta_engine import ios_setup

    _with_device(monkeypatch)
    monkeypatch.setattr(ios_setup, "cert_check", lambda: doctor.Check(
        id="cert-expiry", label="证书有效期: 剩 3 天", ok=True, status="warn",
        fix={"action": "renew-cert"}))
    monkeypatch.setattr(ios_setup, "lock_check", lambda udid: doctor.Check(
        id="unlocked", label="设备解锁: 已解锁", ok=True))
    monkeypatch.setattr(ios_setup, "signed_runner_exists", lambda: True)

    ids = {c.id for c in doctor.ios_checks()}
    assert {"cert-expiry", "unlocked", "signed-runner"} <= ids


def test_ios_checks_skip_device_extras_without_device(monkeypatch):
    """没有真机时不能加这三项（对纯模拟器用户是噪音，且 devices[0] 会越界）。"""
    from insta_engine import ios_device

    monkeypatch.setattr(doctor, "_wda_installed", lambda: True)
    monkeypatch.setattr(ios_device, "list_devices", lambda *a, **k: [])
    monkeypatch.setattr(ios_device, "team_id", lambda: "T")
    monkeypatch.setattr(ios_device, "xcode_account_count", lambda: 1)
    ids = {c.id for c in doctor.ios_checks()}
    assert "cert-expiry" not in ids and "signed-runner" not in ids


def test_fix_whitelist_has_device_actions():
    assert doctor.FIX_ACTIONS["start-wda-device"]["kind"] == "wda-device"
    assert doctor.FIX_ACTIONS["renew-cert"]["kind"] == "renew"
    # 登录账号必须在 Xcode GUI 里做——能自动的只有"把 Xcode 打开"
    assert doctor.FIX_ACTIONS["open-xcode"]["cmd"] == ["open", "-a", "Xcode"]


def test_run_fix_start_wda_device_dispatches(monkeypatch):
    """真机启动必须走 start_on_device（start-wda 只认模拟器，点它没用）。"""
    from insta_engine import ios_wda

    _with_device(monkeypatch)
    called = {}

    def fake_start(udid, **kw):
        called["udid"] = udid
        return True, "ok"

    monkeypatch.setattr(ios_wda, "start_on_device", fake_start)
    code, body = doctor.run_fix("start-wda-device", Path("."), "127.0.0.1")
    assert code == 200 and called["udid"] == "00008150-000528183C98C01C"
    assert body["ok"] is True


def test_run_fix_renew_cert_dispatches(monkeypatch):
    from insta_engine import ios_wda

    _with_device(monkeypatch)
    called = {}
    def fake_renew(udid, **kw):
        called["udid"] = udid
        return False, "没变化"

    monkeypatch.setattr(ios_wda, "renew_device_profile", fake_renew)
    code, body = doctor.run_fix("renew-cert", Path("."), "127.0.0.1")
    assert code == 400 and "没变化" in body["error"]


def test_account_blocking_offers_open_xcode(monkeypatch):
    """没账号**且**没已签名产物时，账号是真阻塞 ⇒ 给"打开 Xcode"入口。

    显式钉住 darwin（2026-10-25）：这个按钮是 **macOS 专属**的——非 macOS 根本没有
    Xcode 可打开，实现里按平台收起了按钮（见 ios_checks 的 not supported 分支）。
    不钉平台的话，这条测试会在 Linux CI 上因「按钮不该出现」而红。
    """
    from insta_engine import ios_device, ios_setup

    monkeypatch.setattr(sys, "platform", "darwin")

    dev = {"udid": "00008150-000528183C98C01C", "name": "iPhone", "model": "iPhone 17 Pro",
           "os_version": "26.5.2", "transport": "wired", "tunnel": "", "paired": True,
           "developer_mode": True}
    monkeypatch.setattr(doctor, "_wda_installed", lambda: True)
    monkeypatch.setattr(ios_device, "list_devices", lambda *a, **k: [dev])
    monkeypatch.setattr(ios_device, "team_id", lambda: "T")
    monkeypatch.setattr(ios_device, "xcode_account_count", lambda: 0)
    monkeypatch.setattr(ios_setup, "signed_runner_exists", lambda: False)

    row = {c.id: c for c in doctor.ios_checks()}["xcode-account"]
    assert row.fix == {"action": "open-xcode"}
    assert row.as_dict()["fix"]["label"] == "打开 Xcode"


def test_account_not_blocking_when_signed_runner(monkeypatch):
    """有已签名产物时账号可跳过 ⇒ **不给**按钮（给了就是误导）。"""
    from insta_engine import ios_device, ios_setup

    dev = {"udid": "00008150-000528183C98C01C", "name": "iPhone", "model": "iPhone 17 Pro",
           "os_version": "26.5.2", "transport": "wired", "tunnel": "", "paired": True,
           "developer_mode": True}
    monkeypatch.setattr(doctor, "_wda_installed", lambda: True)
    monkeypatch.setattr(ios_device, "list_devices", lambda *a, **k: [dev])
    monkeypatch.setattr(ios_device, "team_id", lambda: "T")
    monkeypatch.setattr(ios_device, "xcode_account_count", lambda: 0)
    monkeypatch.setattr(ios_setup, "signed_runner_exists", lambda: True)

    row = {c.id: c for c in doctor.ios_checks()}["xcode-account"]
    assert row.fix is None and row.ok is True
