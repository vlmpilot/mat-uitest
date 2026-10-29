"""HarmonyDriver:鸿蒙驱动代码面(真机校准前的解析与交互逻辑锁定)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from insta_engine import harmony_driver as hd
from insta_engine import harmony_hdc as hdc
from insta_engine.driver_factory import connect_driver
from insta_engine.ui_tree import parse_nodes


@pytest.fixture(autouse=True)
def _assume_hdc(monkeypatch):
    """测试不依赖真机 hdc:统一假定 hdc 可用,命令层另行 fake。"""
    monkeypatch.setattr(hdc, "hdc_available", lambda: True)


def _fake_run(stdout: str = "", rc: int = 0):
    def fake(cmd, **k):
        return type("P", (), {"returncode": rc, "stdout": stdout, "stderr": ""})()
    return fake


_LAYOUT = {"attributes": {"type": "Button", "text": "登录", "id": "login_btn",
                          "bounds": "[100,200][300,260]", "clickable": True},
           "children": [
               {"attributes": {"type": "Text", "text": "账号", "bounds": "10,20-90,50",
                               "clickable": False}, "children": []}]}


# -- bounds/属性宽容解析(待校准点 1) ---------------------------------------------------

def test_parse_bounds_three_dialects():
    assert hd.parse_bounds("[100,200][300,260]") == "[100,200][300,260]"
    assert hd.parse_bounds("10,20-90,50") == "[10,20][90,50]"
    assert hd.parse_bounds("10,20~90,50") == "[10,20][90,50]"
    assert hd.parse_bounds("") == "" and hd.parse_bounds("垃圾") == ""


def test_layout_to_xml_is_uiautomator_shaped():
    """dump 转 XML 后 parse_nodes 可直接消费——全链零改动的关键约定。"""
    xml = hd._layout_to_xml(_LAYOUT, bundle="com.demo")
    nodes = parse_nodes(xml)
    assert len(nodes) == 2
    n0 = nodes[0]
    assert n0.cls == "Button" and n0.text == "登录" and n0.rid == "login_btn"
    assert n0.clickable and n0.package == "com.demo" and n0.center == (200, 230)
    # 别名:content-desc 缺失时 desc 为空串,text 语义不破
    assert nodes[1].center == (50, 35)


# -- dump:两步走(设备落盘 → recv → 转 XML) -------------------------------------------

def test_dump_roundtrip(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake(cmd, **k):
        calls.append(cmd)
        if cmd[:2] == ["file", "recv"]:
            # 模拟 recv 落盘:写到调用方给的 local 路径
            Path(k.get("local_dst") or cmd[3]).write_text(
                json.dumps(_LAYOUT), encoding="utf-8")
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    # recv 用 positional local;简化 fake:按命令第 3 元素落盘
    def fake2(cmd, **k):
        calls.append(cmd)
        if cmd[:2] == ["file", "recv"]:
            Path(cmd[3]).write_text(json.dumps(_LAYOUT), encoding="utf-8")
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(hdc, "_run", fake2)
    drv = hd.HarmonyDriver(serial="fake-target")
    xml = drv.dump()
    assert "<node" in xml and "登录" in xml
    assert any("dumpLayout" in " ".join(c) for c in calls), "dump 走 uitest dumpLayout"


# -- find 轮询与节点行为 ---------------------------------------------------------------

def test_find_polls_until_match(monkeypatch):
    drv = hd.HarmonyDriver(serial="fake-target")
    monkeypatch.setattr(drv, "dump", lambda: hd._layout_to_xml(_LAYOUT))
    monkeypatch.setattr(hdc, "shell", lambda cmd, serial=None, timeout=20: "")
    node = drv.find("text", "登录", timeout=0)
    assert node.exists and node.center() == (200, 230)
    node.click()   # 走 uiInput click(fake _run 默认 rc0)
    assert drv._last_xy == (200, 230)
    miss = drv.find("text", "不存在", timeout=0)
    assert not miss.exists


def test_find_res_id_and_index(monkeypatch):
    drv = hd.HarmonyDriver(serial="fake-target")
    xml = hd._layout_to_xml(_LAYOUT)
    drv.dump = lambda: xml
    assert drv.find("res-id", "login_btn", timeout=0).exists
    with pytest.raises(ValueError, match="xpath"):
        drv.find("xpath", "//node", timeout=0)


# -- factory 路由 ----------------------------------------------------------------------

def test_factory_routes_harmony(tmp_path, monkeypatch):
    monkeypatch.setattr(hdc, "hdc_available", lambda: True)
    monkeypatch.setattr(hdc, "list_targets", lambda: ["fake-target"])
    drv = connect_driver(None, platform="harmony")
    assert drv.platform == "harmony" and drv.serial == "fake-target"


def test_driver_raises_without_hdc(monkeypatch):
    monkeypatch.setattr(hdc, "hdc_available", lambda: False)
    with pytest.raises(RuntimeError, match="hdc 不可用"):
        hd.HarmonyDriver(serial="fake-target")


# -- 前台/按键/降级 ----------------------------------------------------------------------

def test_foreground_app_parses_mission_list(monkeypatch):
    """真机校准(模拟器 API24):`aa dump -l` 的 mission 列表,取第一个 FOREGROUND。"""
    drv = hd.HarmonyDriver(serial="fake-target")
    monkeypatch.setattr(hdc, "shell",
                        lambda cmd, serial=None, timeout=20:
                        "User ID #100\n  current mission lists:{\n"
                        "    Mission ID #31\n      AbilityRecord ID #34\n"
                        "        app name [com.demo.hap]\n"
                        "        main name [com.demo.hap.MainAbility]\n"
                        "        bundle name [com.demo.hap]\n"
                        "        state #FOREGROUND\n"
                        "      AbilityRecord ID #35\n"
                        "        bundle name [com.other.svc]\n"
                        "        state #ACTIVE\n }")
    fg = drv.foreground_app()
    assert fg == {"package": "com.demo.hap", "activity": "com.demo.hap.MainAbility"}


def test_resolve_ability_uses_metadata_shape_verbatim(monkeypatch):
    """🔴 真机校准:ability 名以元数据**原样**为准——单模块工程是短名 EntryAbility,
    拼接前缀(→ com.x.EntryAbility)会报 10104001(真机实测)。"""
    drv = hd.HarmonyDriver(serial="fake-target")
    bm_info = '"name": "EntryAbility",\n    "name": "EntryBackupAbility"'
    seen: list[str] = []

    def fake_shell(cmd, serial=None, timeout=20):
        seen.append(cmd)
        return bm_info if cmd.startswith("bm dump") else "start ability successfully."

    monkeypatch.setattr(hdc, "shell", fake_shell)
    drv.app_start("com.demo.hap")
    assert any("-a EntryAbility" in c for c in seen), "短名原样,不拼前缀"
    drv.app_start("com.demo.hap")
    assert sum(1 for c in seen if c.startswith("bm dump")) == 1, "二次启动走缓存"


def test_resolve_ability_full_name_metadata(monkeypatch):
    """系统设置类 App 的元数据是全名形态 ⇒ 原样用全名。"""
    drv = hd.HarmonyDriver(serial="fake-target")
    bm_info = ('"name": "com.huawei.hmos.settings.ExternalWifiSettingsAbility",\n'
               '    "name": "com.huawei.hmos.settings.MainAbility"')
    monkeypatch.setattr(hdc, "shell",
                        lambda cmd, serial=None, timeout=20:
                        bm_info if cmd.startswith("bm dump") else "ok")
    assert drv._resolve_ability("com.huawei.hmos.settings", None) \
        == "com.huawei.hmos.settings.MainAbility"


def test_resolve_ability_respects_caller_when_metadata_matches(monkeypatch):
    """调用方给的短名与元数据匹配(全名以「.短名」结尾)⇒ 用元数据的全名形态。"""
    drv = hd.HarmonyDriver(serial="fake-target")
    bm_info = '"name": "com.huawei.hmos.settings.MainAbility"'
    monkeypatch.setattr(hdc, "shell",
                        lambda cmd, serial=None, timeout=20:
                        bm_info if cmd.startswith("bm dump") else "ok")
    assert drv._resolve_ability("com.huawei.hmos.settings", "MainAbility") \
        == "com.huawei.hmos.settings.MainAbility"


def test_foreground_app_empty_on_garbage(monkeypatch):
    drv = hd.HarmonyDriver(serial="fake-target")
    monkeypatch.setattr(hdc, "shell", lambda cmd, serial=None, timeout=20: "看不懂")
    assert drv.foreground_app() == {}, "解析失败按取不到处理(离场检测保守口径)"


def test_press_maps_back_home(monkeypatch):
    drv = hd.HarmonyDriver(serial="fake-target")
    monkeypatch.setattr(hdc, "shell", lambda cmd, serial=None, timeout=20: "")
    drv.press("back")
    with pytest.raises(ValueError):
        drv.press("menu")


def test_evidence_degrades_to_empty():
    """鸿蒙首版不采 hilog:runner 暴露 warning,不阻塞主链路(同 iOS 降级先例)。"""
    drv = hd.HarmonyDriver(serial="fake-target")
    drv.start_evidence()
    assert drv.evidence_tail() == ""


def test_node_set_text_uses_focused_coordinate_input(monkeypatch):
    """🔴 Runner 的 INPUT 动作调 element.set_text——鸿蒙节点必须实现(此前缺失
    会让所有输入类用例 AttributeError);带坐标 inputText 是真机校准的可用通道。"""
    drv = hd.HarmonyDriver(serial="fake-target")
    monkeypatch.setattr(drv, "dump", lambda: hd._layout_to_xml(_LAYOUT))
    calls: list[str] = []
    monkeypatch.setattr(hdc, "shell",
                        lambda cmd, serial=None, timeout=20: calls.append(cmd) or "")
    node = drv.find("text", "登录", timeout=0)
    node.set_text("admin")
    assert any("inputText" in c and "admin" in c for c in calls), "走带坐标 inputText"
    node.clear_text()
    assert any('inputText' in c and '" "' not in c.split("inputText")[1] for c in calls[-1:]), "清空=空串 inputText"


def test_harmony_state_devices_uses_emulator_instance_name(tmp_path, monkeypatch):
    """控制台设备卡显示机型名(Mate 90 Pro2)而非 "emulator"——数据源是
    ~/.Huawei/Emulator/deployed/lists.json(最近启动实例);真机回落 param。"""
    import json

    from insta_engine.web_api import device as dev_mod

    lists_json = tmp_path / "lists.json"
    lists_json.write_text(json.dumps([
        {"name": "Mate 90 Pro", "lastStartTime": 100, "showVersion": "HarmonyOS 6.1.1(24)"},
        {"name": "Mate 90 Pro2", "lastStartTime": 200, "showVersion": "HarmonyOS 7.0.0(26.0.0)"},
    ]), encoding="utf-8")
    # 注意补丁的接缝：web_api/device.py 里是 `from .. import harmony_hdc as hdc`，
    # 只替 hd.hdc 打不到它，必须同时替真实模块上的函数。
    fake = mock_hdc_module(list_targets=["127.0.0.1:5555"], param_model="emulator")
    monkeypatch.setattr(hd, "hdc", fake)
    monkeypatch.setattr(hdc, "list_targets", fake.list_targets)
    monkeypatch.setattr(hdc, "shell", fake.shell)
    monkeypatch.setattr(dev_mod.Path, "home", lambda: _FakeHome(lists_json))
    out = dev_mod.harmony_state_devices()
    assert out == [{"serial": "127.0.0.1:5555", "state": "device",
                    "model": "Mate 90 Pro2", "os": "HarmonyOS 7.0.0(26.0.0)",
                    "platform": "harmony"}], "单台在线 ⇒ 最近启动实例的机型名"


class _FakeHome:
    """Path.home() 替身:让 lists.json 落在 fake 目录。"""

    def __init__(self, lists):
        self._lists = lists

    def __truediv__(self, sub):
        return _FakeLeaf(self._lists)


class _FakeLeaf:
    def __init__(self, lists):
        self._lists = lists

    def __truediv__(self, sub):
        return self

    def read_text(self, encoding=None):
        return self._lists.read_text(encoding=encoding)


def mock_hdc_module(list_targets, param_model="emulator"):
    """构造一个假 hdc 模块(hdc_available/list_targets/shell)。"""
    import types
    m = types.ModuleType("fake_hdc")
    m.hdc_available = lambda: True
    m.list_targets = lambda: list_targets
    m.shell = lambda cmd, serial=None, timeout=20: (param_model if "product.model" in cmd
                                                   else ("24" if "apiversion" in cmd else ""))
    return m
