"""Android 性能采集 v0（本地子集）：启动耗时（am start -W）与内存（dumpsys meminfo）。

全部 best-effort：无 adb/设备离线/解析失败返回 None，不影响执行主流程。
"""

from __future__ import annotations

import re

#: adb 通道**只有一个实现**（`adb.py`，含"CLI 缺失时回退 adbutils"）。这里曾自己复制
#: 一份 `subprocess.run(["adb", ...])`：在没有 adb CLI 的机器上（本机就是，2026-10-25
#: 真机实测）逐条 `FileNotFoundError ⇒ None`，于是**启动耗时与内存永远采不到**，而
#: `perf` 是 best-effort 字段，run.json 里就是个空 `{}`——没有任何报错、没有任何红。
#: 这正是 adb.py 抬头写的"收敛于此以免口径漂移"要防的第 4 份副本。
from .adb import adb as _adb


def am_start_w(serial: str | None, package: str, activity: str | None = None) -> dict | None:
    """用 `am start -W` 启动并解析 TotalTime（替代 u2.app_start 以获取启动耗时）。

    失败返回 None（调用方回退 u2.app_start，仅损失指标不损失功能）。
    """
    component = activity
    if not component:
        out = _adb(serial, "shell", "cmd", "package", "resolve-activity", "--brief", package)
        if out:
            lines = [line.strip() for line in out.splitlines() if line.strip()]
            if len(lines) >= 2 and "/" in lines[-1]:
                component = lines[-1]
    if not component or "/" not in component:
        return None
    out = _adb(serial, "shell", "am", "start", "-W", "-n", component)
    if out is None:
        return None
    match = re.search(r"TotalTime:\s*(\d+)", out)
    if not match:
        return None
    return {"launch_ms": int(match.group(1)), "component": component}


def mem_pss_mb(serial: str | None, package: str) -> float | None:
    """dumpsys meminfo 的 TOTAL PSS（MB）；解析失败返回 None。"""
    out = _adb(serial, "shell", "dumpsys", "meminfo", package)
    if not out:
        return None
    for line in out.splitlines():
        if line.strip().startswith("TOTAL"):
            numbers = re.findall(r"\d+", line)
            if numbers:
                return int(numbers[0]) / 1024
    return None
