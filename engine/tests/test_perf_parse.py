"""perf.py 采集解析直测（MAT 评估 2026-10-24 B2：此前零覆盖，全靠真机间接验证）。

解析器是 best-effort 契约：解析得出 ⇒ 指标；解析不出 ⇒ None，**绝不能抛**
（否则影响执行主流程）。全部 monkeypatch `_adb`，不打真设备。
"""

from __future__ import annotations

import pytest

import insta_engine.perf as perf


@pytest.fixture()
def fake_adb(monkeypatch):
    """按子命令返回预设输出的假 adb；未命中返回 None（模拟命令失败）。"""
    outputs: dict[str, str] = {}
    calls: list[tuple] = []

    def _fake(serial, *args, timeout=15):
        calls.append((serial, args))
        key = " ".join(args)
        for prefix, out in outputs.items():
            if key.startswith(prefix):
                return out
        return None

    monkeypatch.setattr(perf, "_adb", _fake)
    return outputs, calls


RESOLVE_OUT = (
    "priority=0 preferred=true\n"
    "com.demo/.MainActivity\n"
)
AM_START_OUT = (
    "Starting: Intent { cmp=com.demo/.MainActivity }\n"
    "Status: ok\n"
    "LaunchState: COLD\n"
    "TotalTime: 812\n"
    "WaitTime: 950\n"
)
MEMINFO_OUT = (
    "Applications Memory Usage (in Kilobytes):\n"
    "Uptime: 123456  Realtime: 234567\n"
    "\n"
    "  PSS:    Total\n"
    "  ------  ------\n"
    "   12345  67890\n"
    "TOTAL   88064   12345   67890   1234   5678   9012   3456   7890   1234     512   34567\n"
)


def test_am_start_w_with_explicit_activity_parses_total_time(fake_adb):
    outputs, calls = fake_adb
    outputs["shell am start"] = AM_START_OUT
    out = perf.am_start_w("SER1", "com.demo", "com.demo/.Main")
    assert out == {"launch_ms": 812, "component": "com.demo/.Main"}
    assert calls[0] == ("SER1", ("shell", "am", "start", "-W", "-n", "com.demo/.Main")), \
        "显式 activity 时不应先 resolve"


def test_am_start_w_resolves_activity_when_absent(fake_adb):
    outputs, calls = fake_adb
    outputs["shell cmd package resolve-activity"] = RESOLVE_OUT
    outputs["shell am start"] = AM_START_OUT
    out = perf.am_start_w(None, "com.demo")
    assert out is not None and out["component"] == "com.demo/.MainActivity"
    assert calls[0][1][1:4] == ("cmd", "package", "resolve-activity")


def test_am_start_w_returns_none_when_total_time_missing(fake_adb):
    outputs, _ = fake_adb
    outputs["shell am start"] = "Status: ok\n"  # 无 TotalTime 行
    assert perf.am_start_w("SER1", "com.demo", "com.demo/.Main") is None


def test_am_start_w_returns_none_when_adb_fails(fake_adb):
    outputs, _ = fake_adb  # 空表：所有命令失败
    assert perf.am_start_w("SER1", "com.demo", "com.demo/.Main") is None
    assert perf.am_start_w("SER1", "com.demo") is None  # resolve 失败同样 None


def test_am_start_w_rejects_component_without_slash(fake_adb):
    outputs, _ = fake_adb
    outputs["shell cmd package resolve-activity"] = "nothing usable here"
    assert perf.am_start_w("SER1", "com.demo") is None


def test_mem_pss_mb_parses_total_kb(fake_adb):
    outputs, _ = fake_adb
    outputs["shell dumpsys meminfo"] = MEMINFO_OUT
    assert perf.mem_pss_mb("SER1", "com.demo") == pytest.approx(88064 / 1024)


def test_mem_pss_mb_returns_none_on_garbage(fake_adb):
    outputs, _ = fake_adb
    outputs["shell dumpsys meminfo"] = "some unexpected locale output\n没有 TOTAL 行"
    assert perf.mem_pss_mb("SER1", "com.demo") is None


def test_mem_pss_mb_returns_none_when_adb_fails(fake_adb):
    outputs, _ = fake_adb
    assert perf.mem_pss_mb(None, "com.demo") is None
