"""无 adb CLI 时的回退通道（adbutils）——命令构造口径。

病灶（2026-10-25 本机实测，本机就没有 adb CLI）：`adb()` 的 CLI 通道
`FileNotFoundError` 后回退 `_adb_py`，而后者把参数**原样**拼给
`adbutils.device.shell()`；带 `shell` 前缀的调用方（`perf.py` 的
启动耗时 / `dumpsys meminfo`）于是执行了 `sh -c "shell dumpsys …"`，
报 `/system/bin/sh: shell: inaccessible or not found`。

危害形如"静默变空"：`adb()` 只在 returncode==0 时返回 stdout，失败返回 None，
调用方普遍 `if not out: return` ⇒ 启动耗时与内存采集**全丢，但没有任何报错**，
设备连接看起来完全正常。
"""

from __future__ import annotations

from insta_engine.adb import _shell_command


def test_shell_prefix_is_stripped():
    assert _shell_command(("shell", "dumpsys", "meminfo", "com.x")) == "dumpsys meminfo com.x"


def test_args_without_shell_prefix_untouched():
    assert _shell_command(("logcat", "-d")) == "logcat -d"


def test_args_are_shell_quoted():
    assert _shell_command(("shell", "input", "text", "hello world")) == "input text 'hello world'"


def test_perf_uses_the_shared_channel():
    """perf 必须复用统一 adb 通道，不能再养第 4 份 subprocess 副本。

    为什么单独立一条：`perf` 的所有字段都是 best-effort（失败返回 None，run.json 里
    就是个空 `{}`），**没有任何断言会因此变红**。这类"静默无数据"只能靠结构断言钉住。
    """
    from insta_engine import adb as adb_mod
    from insta_engine import perf

    assert perf._adb is adb_mod.adb


def test_am_start_w_parses_total_time(monkeypatch):
    from insta_engine import perf

    monkeypatch.setattr(perf, "_adb",
                        lambda serial, *a, **k: "Status: ok\nTotalTime: 1234\nComplete\n")
    assert perf.am_start_w("S", "pkg", "pkg/.Main") == {
        "launch_ms": 1234, "component": "pkg/.Main"}


def test_mem_pss_reads_total_line(monkeypatch):
    from insta_engine import perf

    monkeypatch.setattr(perf, "_adb",
                        lambda serial, *a, **k: "  TOTAL PSS:  1024  TOTAL RSS: 2048\n")
    assert perf.mem_pss_mb("S", "pkg") == 1.0
