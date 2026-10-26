"""open_simulator_app:确定性路径找 Simulator.app(2026-10-26 用户实测 open -a 失败)。"""

from __future__ import annotations

from pathlib import Path

from insta_engine import ios_setup


def _mk_xcode(tmp_path: Path, with_simulator: bool) -> Path:
    """伪造 Xcode 开发者目录；with_simulator 决定有没有 Simulator.app。"""
    dev = tmp_path / "Xcode.app" / "Contents" / "Developer"
    dev.mkdir(parents=True)
    if with_simulator:
        (dev / "Applications" / "Simulator.app").mkdir(parents=True)
    return dev


def _fake_run(dev: Path, open_a_rc: int = 1):
    """xcode-select 返回 dev;open 的返回码按调用形态给(open -a 兜底默认失败)。"""
    def fake(cmd, **k):
        cmd_list = cmd if isinstance(cmd, list) else [cmd]
        if cmd_list[:1] == ["xcode-select"]:
            rc, out = 0, f"{dev}\n"
        elif cmd_list[:2] == ["open", "-a"]:
            rc, out = open_a_rc, ""
        else:
            rc, out = 0, ""
        return type("P", (), {"returncode": rc, "stdout": out})()
    return fake


def test_opens_simulator_via_xcode_select_path(tmp_path, monkeypatch):
    """xcode-select 指向的 Xcode 里找得到就 open 该路径——不再依赖 LaunchServices。"""
    dev = _mk_xcode(tmp_path, with_simulator=True)
    opened: list[list[str]] = []
    real_run = ios_setup.subprocess.run

    def spy(cmd, **k):
        if cmd[:1] == ["open"]:
            opened.append(cmd)
        return real_run(cmd, **k)

    monkeypatch.setattr(ios_setup.subprocess, "run", spy)
    monkeypatch.setattr(ios_setup.subprocess, "run",
                        lambda cmd, **k: opened.append(cmd) or _fake_run(dev)(cmd, **k))
    ok, detail = ios_setup.open_simulator_app()
    assert ok and detail == ""
    assert any("Simulator.app" in " ".join(c) for c in opened), "必须 open 确定性路径"


def test_missing_components_gives_component_hint(tmp_path, monkeypatch):
    """Xcode 在但没装模拟器组件(用户机器实况)——指引下载 iOS platform,不是笼统报错。"""
    dev = _mk_xcode(tmp_path, with_simulator=False)  # 目录在、Simulator.app 缺
    monkeypatch.setattr(ios_setup.subprocess, "run", _fake_run(dev, open_a_rc=1))
    ok, detail = ios_setup.open_simulator_app()
    assert not ok
    assert "iOS platform" in detail and "USB 真机" in detail


def test_no_xcode_gives_install_hint(tmp_path, monkeypatch):
    """连 Xcode 都没有(xcode-select 失败、标准候选被替换为不存在路径)——指引安装 Xcode。

    patch 的 fallback 路径要两层子目录:has_xcode 判的是 parent.parent(开发者目录)。"""
    monkeypatch.setattr(
        ios_setup, "FALLBACK_SIMULATOR_PATHS",
        [tmp_path / "uninstalled" / "x" / "Simulator.app"])
    monkeypatch.setattr(ios_setup.subprocess, "run", _fake_run(tmp_path / "nope", open_a_rc=1))
    ok, detail = ios_setup.open_simulator_app()
    assert not ok
    assert "安装完整 Xcode" in detail


def test_open_a_fallback_still_works(tmp_path, monkeypatch):
    """LaunchServices 注册过的机器:候选全无时回退 open -a 成功。"""
    dev = _mk_xcode(tmp_path, with_simulator=False)
    monkeypatch.setattr(ios_setup.subprocess, "run", _fake_run(dev, open_a_rc=0))
    ok, detail = ios_setup.open_simulator_app()
    assert ok and detail == ""


def test_boots_first_available_device_when_no_gui(tmp_path, monkeypatch):
    """精简 Xcode(无 Simulator.app)但 runtime/设备可用 ⇒ 直接 simctl boot(用户机器实况)。"""
    dev = _mk_xcode(tmp_path, with_simulator=False)
    devices_out = ("-- iOS 18.6 --\n"
                   "    iPhone 16 Pro (A6E12D70-6B72-464A-8288-1828E26F2F57) (Shutdown)\n"
                   "    iPhone 16e (0D5D6DFD-2B8F-4082-8DDC-FA88C9A13D35) (Shutdown)\n")
    booted = []

    def fake(cmd, **k):
        if cmd[:3] == ["xcrun", "simctl", "list"]:
            return type("P", (), {"returncode": 0, "stdout": devices_out})()
        if cmd[:3] == ["xcrun", "simctl", "boot"]:
            booted.append(cmd[3])
            return type("P", (), {"returncode": 0, "stdout": ""})()
        return _fake_run(dev, open_a_rc=1)(cmd, **k)

    monkeypatch.setattr(ios_setup.subprocess, "run", fake)
    ok, detail = ios_setup.open_simulator_app()
    assert ok and booted == ["A6E12D70-6B72-464A-8288-1828E26F2F57"], "boot 第一台可用设备"
    assert "iPhone 16 Pro" in detail and "WDA" in detail


def test_already_booted_reports_ok(tmp_path, monkeypatch):
    """已有 Booted 设备:不重复 boot,直接成功。"""
    dev = _mk_xcode(tmp_path, with_simulator=False)
    devices_out = "    iPhone 16 Pro (A6E12D70-6B72-464A-8288-1828E26F2F57) (Booted)\n"
    booted: list = []

    def fake(cmd, **k):
        if cmd[:3] == ["xcrun", "simctl", "list"]:
            return type("P", (), {"returncode": 0, "stdout": devices_out})()
        if cmd[:3] == ["xcrun", "simctl", "boot"]:
            booted.append(cmd[3])
            return type("P", (), {"returncode": 0, "stdout": ""})()
        return _fake_run(dev, open_a_rc=1)(cmd, **k)

    monkeypatch.setattr(ios_setup.subprocess, "run", fake)
    ok, detail = ios_setup.open_simulator_app()
    assert ok and not booted, "已有启动中的设备不许再 boot"
    assert "已有启动中的模拟器" in detail


def test_boot_first_false_skips_boot(tmp_path, monkeypatch):
    """boot_first=False(已 boot 指定设备的调用方):GUI 打不开就如实失败,不多开机器。"""
    dev = _mk_xcode(tmp_path, with_simulator=False)
    devices_out = "    iPhone 16 Pro (A6E12D70-6B72-464A-8288-1828E26F2F57) (Shutdown)\n"
    booted: list = []

    def fake(cmd, **k):
        if cmd[:3] == ["xcrun", "simctl", "list"]:
            return type("P", (), {"returncode": 0, "stdout": devices_out})()
        if cmd[:3] == ["xcrun", "simctl", "boot"]:
            booted.append(cmd[3])
            return type("P", (), {"returncode": 0, "stdout": ""})()
        return _fake_run(dev, open_a_rc=1)(cmd, **k)

    monkeypatch.setattr(ios_setup.subprocess, "run", fake)
    ok, detail = ios_setup.open_simulator_app(boot_first=False)
    assert not ok and not booted
    assert "iOS platform" in detail, "落回缺组件指引"
