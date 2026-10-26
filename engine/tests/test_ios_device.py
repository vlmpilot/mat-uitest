"""iOS 真机薄包装（ios_device）与 doctor 真机行。

全部用**合成数据**（不含任何真实 udid/serial）：这里测的是**解析与判据**，
不是"这台机器上有没有手机"。
"""

from unittest import mock

import pytest

from insta_engine import doctor, ios_device


def _dev_payload():
    """仿 `devicectl list devices --json-output` 的真实结构（字段名照抄实测）。"""
    return {"result": {"devices": [
        {
            "identifier": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",   # coredevice id，不是 udid
            "hardwareProperties": {"udid": "00008030-000A1B2C3D4E5F60",
                                   "marketingName": "iPhone 15", "deviceType": "iPhone"},
            "deviceProperties": {"name": "测试机", "osVersionNumber": "17.4",
                                 "developerModeStatus": "enabled"},
            "connectionProperties": {"transportType": "wired", "tunnelState": "disconnected",
                                     "pairingState": "paired"},
        },
        {
            "identifier": "11111111-2222-3333-4444-555555555555",
            "hardwareProperties": {"udid": "00008110-000B2C3D4E5F6071",
                                   "marketingName": "iPad Air"},
            "deviceProperties": {"name": "平板", "osVersionNumber": "17.2",
                                 "developerModeStatus": "disabled"},
            "connectionProperties": {"transportType": "localNetwork", "tunnelState": "connected",
                                     "pairingState": "paired"},
        },
        {"identifier": "no-udid"},                                    # 无 udid ⇒ 跳过
    ]}}


def test_parse_devices_uses_hardware_udid_not_identifier():
    devs = ios_device.parse_devices(_dev_payload())
    assert len(devs) == 2, "缺 udid 的条目应跳过"
    assert devs[0]["udid"] == "00008030-000A1B2C3D4E5F60", "必须取 hardwareProperties.udid"
    assert devs[0]["model"] == "iPhone 15" and devs[0]["os_version"] == "17.4"
    assert devs[0]["transport"] == "wired" and devs[0]["developer_mode"] is True
    # tunnelState 只记录、不参与判定（空闲态也可能是 disconnected）
    assert devs[0]["tunnel"] == "disconnected"
    assert devs[1]["transport"] == "localNetwork" and devs[1]["developer_mode"] is False


def test_pick_device_requires_serial_when_multiple():
    devs = ios_device.parse_devices(_dev_payload())
    dev, why = ios_device.pick_device(None, devs)
    assert dev is None and "--serial" in why, "多台设备不猜，要求显式指定"

    dev, why = ios_device.pick_device("平板", devs)
    assert dev and dev["udid"] == "00008110-000B2C3D4E5F6071", "应按名字匹配"

    dev, why = ios_device.pick_device("00008030-000A1B2C3D4E5F60", devs)
    assert dev and why == "", "应按 udid 匹配"

    dev, why = ios_device.pick_device("nope", devs)
    assert dev is None and "找不到设备" in why


def test_pick_device_empty_gives_actionable_hint():
    dev, why = ios_device.pick_device(None, [])
    assert dev is None and "数据线" in why and "解锁" in why


def test_parse_team_id_from_cert_subject():
    subject = ("subject=UID=ABC123, CN=Apple Development: dev@example.com (PPQC44YHLR), "
               "OU=U56RW5ZM8J, O=Example, C=US")
    assert ios_device.parse_team_id(subject) == "U56RW5ZM8J"
    assert ios_device.parse_team_id("CN=Apple Development: x") is None
    assert ios_device.parse_team_id("") is None


def test_team_id_reads_keychain_and_openssl(monkeypatch):
    cert = mock.Mock(returncode=0, stdout="-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(ios_device, "_run", lambda cmd, timeout=20: cert)
    monkeypatch.setattr(ios_device.subprocess, "run",
                        lambda *a, **k: mock.Mock(
                            stdout="subject=CN=Apple Development: a (X), OU=TEAM123456, O=O"))
    assert ios_device.team_id() == "TEAM123456"


def test_team_id_returns_none_without_certificate(monkeypatch):
    monkeypatch.setattr(ios_device, "_run",
                        lambda cmd, timeout=20: mock.Mock(returncode=1, stdout=""))
    assert ios_device.team_id() is None


def test_install_app_success_and_failure(monkeypatch):
    seen: list = []
    monkeypatch.setattr(ios_device, "_run",
                        lambda cmd, timeout=20: seen.append(cmd) or mock.Mock(returncode=0))
    ok, why = ios_device.install_app("u1", "/tmp/MatDemo.app")
    assert ok and why == ""
    assert seen[0][:6] == ["xcrun", "devicectl", "device", "install", "app", "--device"]
    assert seen[0][-1] == "/tmp/MatDemo.app"

    monkeypatch.setattr(ios_device, "_run", lambda cmd, timeout=20: mock.Mock(
        returncode=1, stdout="", stderr="ERROR: The application is missing\nmore detail\n"))
    ok, why = ios_device.install_app("u1", "/tmp/bad.app")
    assert not ok and why == "more detail", "失败原因取末行（不整段回显）"


def test_uninstall_app_command_shape(monkeypatch):
    seen: list = []
    monkeypatch.setattr(ios_device, "_run",
                        lambda cmd, timeout=20: seen.append(cmd) or mock.Mock(returncode=0))
    ok, _ = ios_device.uninstall_app("u1", "com.example.app")
    assert ok and seen[0][-2:] == ["u1", "com.example.app"]


def test_describe_line():
    dev = {"udid": "u", "model": "iPhone 15", "os_version": "17.4", "transport": "wired"}
    assert ios_device.describe(dev) == "iPhone 15 · iOS 17.4 · USB"


def test_xcode_account_count_reads_disk_not_daemon(tmp_path):
    """账号判据必须读**磁盘** plist（cfprefsd 有、磁盘没有 ⇒ xcodebuild 报 No accounts）。"""
    import plistlib

    p = tmp_path / "com.apple.dt.Xcode.plist"
    plistlib.dump({"DVTDeveloperAccountManagerAppleIDLists":
                   {"IDE.Identifiers.Prod": [{"identifier": "X"}]}}, p.open("wb"))
    assert ios_device.xcode_account_count(p) == 1

    plistlib.dump({"DVTDeveloperAccountManagerAppleIDLists": {"IDE.Identifiers.Prod": []}},
                  p.open("wb"))
    assert ios_device.xcode_account_count(p) == 0, "空数组 = 未登录"

    # 文件不存在 / 不是 plist ⇒ 0（不抛）
    assert ios_device.xcode_account_count(tmp_path / "missing.plist") == 0
    (tmp_path / "bad.plist").write_text("not a plist", encoding="utf-8")
    assert ios_device.xcode_account_count(tmp_path / "bad.plist") == 0


def test_doctor_shows_device_rows_only_when_wda_installed(monkeypatch):
    """真机两行只在装了 [ios] extra 时出现（纯 Android 用户不该看到永远 warn 的行）。"""
    devs = ios_device.parse_devices(_dev_payload())
    monkeypatch.setattr(ios_device, "list_devices", lambda: devs)
    monkeypatch.setattr(ios_device, "team_id", lambda: "TEAM123456")
    monkeypatch.setattr(ios_device, "xcode_account_count", lambda: 1)
    monkeypatch.setattr(doctor, "_booted_simulators", lambda: [])
    monkeypatch.setattr(doctor, "_wda_status_ok", lambda: False)

    monkeypatch.setattr(doctor, "_wda_installed", lambda: False)
    ids = {c.id for c in doctor.run("ios")}
    assert "ios-device" not in ids and "ios-team" not in ids and "xcode-account" not in ids

    monkeypatch.setattr(doctor, "_wda_installed", lambda: True)
    rows = {c.id: c.as_dict() for c in doctor.run("ios")}
    assert rows["ios-device"]["status"] == "ok" and "iPhone 15" in rows["ios-device"]["label"]
    assert rows["ios-team"]["status"] == "ok" and "TEAM123456" in rows["ios-team"]["label"]
    assert rows["xcode-account"]["status"] == "ok" and "1 个" in rows["xcode-account"]["label"]


def test_doctor_warns_when_device_is_wifi_only(monkeypatch):
    """Wi-Fi 连接要 warn（真机执行要 USB），而不是当成可用。"""
    devs = ios_device.parse_devices(_dev_payload())
    wifi_only = [d for d in devs if d["transport"] == "localNetwork"]
    monkeypatch.setattr(ios_device, "list_devices", lambda: wifi_only)
    monkeypatch.setattr(ios_device, "team_id", lambda: None)
    monkeypatch.setattr(ios_device, "xcode_account_count", lambda: 0)
    monkeypatch.setattr(doctor, "_wda_installed", lambda: True)
    monkeypatch.setattr(doctor, "_booted_simulators", lambda: [])
    monkeypatch.setattr(doctor, "_wda_status_ok", lambda: False)
    # 必须打桩：有已签名产物时账号只是"可跳过"（ok），不打桩就**依赖宿主机状态**——
    # 本机有产物 ⇒ 这条在本地红、在干净 CI 绿（2026-10-20 实测到）。
    from insta_engine import ios_setup

    monkeypatch.setattr(ios_setup, "signed_runner_exists", lambda: False)
    rows = {c.id: c.as_dict() for c in doctor.run("ios")}
    assert rows["ios-device"]["status"] == "warn" and "USB" in rows["ios-device"]["hint"]
    assert rows["ios-team"]["status"] == "warn"
    assert rows["xcode-account"]["status"] == "warn" and "退出 Xcode" in rows["xcode-account"]["hint"]


def test_cli_hides_ios_group_off_macos(monkeypatch, capsys):
    """Windows/Linux 不展示 iOS：**渲染层**决定，不是检查函数返回空（2026-10-25 D-1）。

    原实现把平台判断写进 `ios_checks()`（非 macOS `return []`），于是「这一组有没有内容」
    成了平台相关的隐式契约 —— 直接调它的测试在 Linux/Windows CI 上 10 条全 KeyError。
    现在契约是「检查恒有结构（见 test_doctor.test_ios_checks_structure_on_non_macos），
    渲染由 cmd_doctor 决定」，这条测试钉的就是渲染这一侧。
    """
    from insta_engine import __main__ as cli

    monkeypatch.setattr(doctor.sys, "platform", "win32")
    monkeypatch.setattr(doctor, "android_checks", lambda: [])
    monkeypatch.setattr(doctor, "ios_checks",
                        lambda: [doctor.Check(id="ios-sentinel", label="不该出现的行", ok=True)])
    assert cli.cmd_doctor(None) == 0
    out = capsys.readouterr().out
    assert "不该出现的行" not in out
    assert "iOS" not in out
    # 结论行照旧（渲染层少一组不能影响「能否开测」的结论）
    assert "结论" in out


def test_cli_shows_ios_group_on_macos(monkeypatch, capsys):
    """对照组：同样是这一份渲染代码，macOS 上必须把 iOS 组打出来。

    没有这条对照，上面那条「不出现」的断言可能因为渲染整体失效而假绿。
    """
    from insta_engine import __main__ as cli

    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(doctor, "android_checks", lambda: [])
    monkeypatch.setattr(doctor, "ios_checks",
                        lambda: [doctor.Check(id="ios-sentinel", label="iOS 组的行", ok=True)])
    assert cli.cmd_doctor(None) == 0
    out = capsys.readouterr().out
    assert "iOS 组的行" in out


def test_wda_status_ok_falls_back_to_usb_device(monkeypatch):
    """真机 WDA 不在 localhost：localhost 探不到时必须再走 usbmux（2026-10-22 实测）。"""
    pytest.importorskip("wda")
    from insta_engine import ios_wda

    def _refused(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(doctor.urllib.request, "urlopen", _refused)
    monkeypatch.setattr(doctor, "_usb_device_udid", lambda: "TEST-UDID")
    monkeypatch.setattr(ios_wda, "device_wda_ready", lambda udid: True)
    assert doctor._wda_status_ok() is True

    monkeypatch.setattr(ios_wda, "device_wda_ready", lambda udid: False)
    assert doctor._wda_status_ok() is False


def test_renew_fix_requires_usb_device(monkeypatch, tmp_path):
    """续期=重新签名：Wi-Fi 设备试了也白试，执行前就明确拒绝并提示 USB。"""
    wifi = dict(_wired(), transport="localNetwork")
    monkeypatch.setattr(ios_device, "list_devices", lambda: [wifi])
    code, body = doctor.run_fix("renew-cert", tmp_path, host="127.0.0.1")
    assert code == 400 and "USB" in body["error"]


# ---------------- resolve_ios_serial（R9/R10：serial 统一解析） ----------------

def _wired(udid="00008101-000A1B2C3D4E5F60", name="测试机", model="iPhone 15"):
    return {"udid": udid, "name": name, "model": model, "os_version": "17.4",
            "transport": "wired", "tunnel": "", "paired": True, "developer_mode": True}


def _wifi(udid="00008110-000B2C3D4E5F6071", name="平板", model="iPad Air"):
    return dict(_wired(udid, name, model), transport="localNetwork")


def test_resolve_none_picks_unique_wired_device():
    """缺省 serial 有唯一 USB 真机 ⇒ 自动选它（用户插了线就不该被丢去连 localhost）。"""
    got = ios_device.resolve_ios_serial(None, devices=[_wired()])
    assert got == "00008101-000A1B2C3D4E5F60"


def test_resolve_none_without_wired_stays_simulator():
    """只有 Wi-Fi 设备 ⇒ 不当真机（USBClient 连不上），保持 None=模拟器原行为。"""
    assert ios_device.resolve_ios_serial(None, devices=[_wifi()]) is None
    assert ios_device.resolve_ios_serial(None, devices=[]) is None


def test_resolve_none_with_multiple_wired_refuses_to_guess():
    devs = [_wired(), _wired(udid="00008101-000A1B2C3D4E5F61", name="第二台")]
    try:
        ios_device.resolve_ios_serial(None, devices=devs)
    except RuntimeError as exc:
        assert "多台 USB 真机" in str(exc) and "--serial" in str(exc)
    else:
        raise AssertionError("多台 USB 真机必须报错要求指定，不能猜")


def test_resolve_name_maps_to_udid():
    """设备名/营销名 ⇒ udid（WdaDriver 只认 udid 形态，名字会被当模拟器）。"""
    devs = [_wired()]
    assert ios_device.resolve_ios_serial("测试机", devices=devs) == devs[0]["udid"]
    assert ios_device.resolve_ios_serial("iPhone 15", devices=devs) == devs[0]["udid"]


def test_resolve_named_wifi_device_fails_fast():
    """显式指定 Wi-Fi 设备 ⇒ 早报「插数据线」，而不是让 usbmux 连接超时。"""
    try:
        ios_device.resolve_ios_serial("平板", devices=[_wifi()])
    except RuntimeError as exc:
        assert "USB" in str(exc) and "Wi-Fi" in str(exc)
    else:
        raise AssertionError("Wi-Fi 设备必须早失败")


def test_resolve_unknown_identifier_lists_known_devices():
    try:
        ios_device.resolve_ios_serial("not-a-device", devices=[_wired()])
    except RuntimeError as exc:
        assert "不认识的 iOS 设备标识" in str(exc) and "00008101-000A1B2C3D4E5F60" in str(exc)
    else:
        raise AssertionError("未知标识必须报错，不能猜")


def test_resolve_passthrough_for_udid_shapes():
    """真机 udid / 模拟器 udid 原样透传（含 'simulator'）。"""
    dev_udid = "00008101-000A1B2C3D4E5F60"
    sim_udid = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    assert ios_device.resolve_ios_serial(dev_udid, devices=[]) == dev_udid
    assert ios_device.resolve_ios_serial(sim_udid, devices=[]) == sim_udid
    assert ios_device.resolve_ios_serial("simulator", devices=[]) == "simulator"


def test_resolve_serial_rejects_wifi_device_with_actionable_error():
    """显式 udid 也要校验 USB——否则一路走到 usbmux，报出看不懂的 NoneType 错误。"""
    devs = [{"udid": "00008150-000528183C98C01C", "name": "iPhone", "model": "iPhone 17 Pro",
             "os_version": "26.5.2", "transport": "localNetwork", "tunnel": "", "paired": True,
             "developer_mode": True}]
    with pytest.raises(RuntimeError, match="Wi-Fi"):
        ios_device.resolve_ios_serial("00008150-000528183C98C01C", devices=devs)


def test_resolve_serial_accepts_wired_device():
    devs = [{"udid": "00008150-000528183C98C01C", "name": "iPhone", "model": "iPhone 17 Pro",
             "os_version": "26.5.2", "transport": "wired", "tunnel": "", "paired": True,
             "developer_mode": True}]
    assert ios_device.resolve_ios_serial("00008150-000528183C98C01C", devices=devs) \
        == "00008150-000528183C98C01C"
