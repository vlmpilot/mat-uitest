"""`logcat_tail` **必须按优先级过滤**，否则崩溃证据会被噪声挤出窗口（T76）。

## 为什么必须有这条

实测（2026-10-07，崩溃类缺陷探针）：**探针检出了崩溃，却被分类成 `assert-fail`**。

追下去：崩溃**就在** logcat 缓冲区里（第 **3083** 行），而缓冲区末尾共 **7177** 行
⇒ 距末尾约 **4100 行**；而 `runner` 只取末 **300** 行。
那 300 行里全是噪声——`AccessibilityNodeInfoDumper: Skipping invisible child`
（**就是 dump 自己产生的**，每页几百行）、`TrafficMonitor`、音频日志…
⇒ `classify_failure` 的 `"fatal exception" in log` 永远为假。

⚠️ **修法不是"把窗口开大"**（噪声无上界），而是**按优先级过滤**：
`*:E` 之后同一份缓冲区只剩 ~970 行 ⇒ 300 行窗口能覆盖约 10 分钟的错误。

这条测试**不碰设备**：它只断言"传下去的 argv 里有没有过滤标记"——
因为这次的 bug 恰恰是"调用链看起来完全正常，只是窗口里没有崩溃"。
"""

from __future__ import annotations

import pytest

from insta_engine import adb as adb_mod
from insta_engine.adb import LOGCAT_PRIORITY, logcat_tail

#: ⚠️ conftest 的 autouse fixture 会把 `insta_engine.adb.logcat_clear` **打桩**（防真机）。
#: 这条测试要验的恰恰是**它传下去的 argv** ⇒ 在**导入期**抓住真实实现
#: （那时 fixture 还没跑），而不是调用被桩掉的模块属性。
_REAL_LOG_CLEAR = adb_mod.logcat_clear


@pytest.fixture
def captured(monkeypatch):
    """截住 `adb()`，记录 argv。"""
    seen: list[tuple] = []

    def fake(serial, *args, timeout=15):  # noqa: ARG001
        seen.append(args)
        return "stub"

    monkeypatch.setattr(adb_mod, "adb", fake)
    return seen


def test_default_filters_to_error_priority(captured):
    """**核心**：默认必须带上 `*:E`——否则噪声会把崩溃挤出窗口。"""
    logcat_tail("SERIAL")
    assert captured, "必须真的调了 adb"
    args = captured[0]
    assert f"*:{LOGCAT_PRIORITY}" in args, (
        f"默认必须过滤到 Error 以上，实际 argv={args}；"
        "不过滤时末 300 行会被 dump 噪声占满 ⇒ app-crash 分类失效")


def test_priority_is_a_single_argv_not_shell_glob(captured):
    """`*:E` 必须是**单个 argv**——经 shell 会被 glob 展开成文件名。"""
    logcat_tail("SERIAL")
    args = captured[0]
    assert any(a.startswith("*:") for a in args)
    assert not any(a == "*" for a in args), "`*` 单独成 argv 会被 shell 展开"


def test_priority_none_keeps_raw_tail(captured):
    """**反例的另一半**：显式传 `None` 时不该加过滤（需要 W/I 上下文时用）。"""
    logcat_tail("SERIAL", priority=None)
    args = captured[0]
    assert not any(a.startswith("*:") for a in args), f"不该有过滤标记，实际 {args}"
    assert "-d" in args and "-t" in args, "仍应是末段读取"


def test_lines_is_passed_through(captured):
    logcat_tail("SERIAL", 500)
    args = captured[0]
    assert "500" in args


def test_default_window_is_large_enough(captured):
    """**核心之二**：默认窗口必须足够大。

    ⚠️ `-t` 的语义是"**先取末 N 行原始日志、再按优先级过滤**"（实测：
    `-t 200 *:E` 只剩 15 行、拿不到崩溃；`-t 5000 *:E` 有 663 行、拿到了）
    ⇒ **过滤救不了小窗口**。实测那次崩溃距末尾约 4100 行。
    """
    from insta_engine.adb import LOGCAT_TAIL_LINES

    logcat_tail("SERIAL")
    args = captured[0]
    idx = args.index("-t")
    assert int(args[idx + 1]) == LOGCAT_TAIL_LINES
    assert LOGCAT_TAIL_LINES >= 5000, (
        f"窗口 {LOGCAT_TAIL_LINES} 太小：实测崩溃距末尾约 4100 行，会被噪声挤出窗口")


def test_logcat_clear_calls_the_device(captured):
    """`logcat_clear` 必须真的调 `logcat -c`——它限定崩溃证据的**时效**。

    ⚠️ 用导入期抓住的 `_REAL_LOG_CLEAR`（见文件头），否则会调到 conftest 的桩。
    """
    assert _REAL_LOG_CLEAR("SERIAL") is True
    assert captured[0] == ("logcat", "-c"), f"实际 argv={captured[0]}"


def test_cli_adb_missing_falls_back_to_adbutils(monkeypatch):
    """本机没有 CLI `adb` 时必须回退 adbutils，否则失败步日志静默变空。

    2026-10-25 真机评测：这台机器 adb 不在 PATH（设备经 adbutils 在线），
    `logcat_tail()` 恒空 ⇒ 崩溃证据丢失、`app-crash` 分类失效；
    而驱动本身连得上，说明缺的只是**采集通道**。
    """
    import sys

    seen: list[str] = []

    def no_cli(*args, **kwargs):  # noqa: ARG001
        raise FileNotFoundError("adb")

    class _Dev:
        @staticmethod
        def shell(cmd: str, timeout: float = 10) -> str:  # noqa: ARG004
            seen.append(cmd)
            return "FATAL EXCEPTION: demo"

    class _AdbFacade:
        @staticmethod
        def device(serial: str | None = None) -> _Dev:
            assert serial == "SERIAL", "必须定向到同一台设备（多设备下裸 logcat 会失败）"
            return _Dev()

    class _FakeAdbutils:
        adb = _AdbFacade()

    monkeypatch.setattr(adb_mod.subprocess, "run", no_cli)
    monkeypatch.setitem(sys.modules, "adbutils", _FakeAdbutils())
    assert logcat_tail("SERIAL") == "FATAL EXCEPTION: demo"
    assert seen and seen[0].split()[-1] == "'*:E'", "过滤标记必须被 shell 转义，否则被 glob 展开"
