"""`mat devices` 输出回归（2026-10-25）——iOS 真机那行不能重复连接方式。

病灶：第三列已经写 `USB`，而描述行来自 `ios_device.describe()` 的默认行为
（带 transport）⇒ 实际打印 `00008120-…  iPhone 17 · iOS 26.5.2 · USB⇥USB`。

为什么之前没人发现：这是**纯文本输出的内部重复**，不影响任何功能，也没有断言
盯着它（`grep -rn "cmd_devices" engine/tests` 为空）。同类教训在台账里出现过
多次——"两处表达同一件事"的漂移只能靠一条断言钉住，靠人眼看输出会漏。
"""

from __future__ import annotations

import insta_engine.__main__ as cli
from insta_engine import ios_device


def test_ios_wired_line_does_not_repeat_transport(monkeypatch, capsys):
    monkeypatch.setattr(cli, "list_adb_devices", lambda: [])
    monkeypatch.setattr(ios_device, "list_devices", lambda: [
        {"udid": "00008120-0001", "model": "iPhone 17",
         "os_version": "26.5.2", "transport": "wired"},
    ])

    assert cli.cmd_devices(None) == 0
    out = capsys.readouterr().out
    assert "USB⇥USB" not in out, f"连接方式被打印两次: {out!r}"
    assert out.count("USB") == 1, f"USB 只应出现一次（第三列）: {out!r}"
    assert "iPhone 17" in out and "iOS 26.5.2" in out


def test_ios_wifi_line_keeps_its_own_hint(monkeypatch, capsys):
    """Wi-Fi 那行同理：描述不再重复 transport，第三列给「暂不支持」的原因。"""
    monkeypatch.setattr(cli, "list_adb_devices", lambda: [])
    monkeypatch.setattr(ios_device, "list_devices", lambda: [
        {"udid": "00008120-0002", "model": "iPhone 17",
         "os_version": "26.5.2", "transport": "localNetwork"},
    ])

    assert cli.cmd_devices(None) == 0
    out = capsys.readouterr().out
    assert "无线调试" not in out.split("\t")[1], f"描述行不该再带 transport: {out!r}"
    assert "暂不支持" in out
