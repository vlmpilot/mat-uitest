"""Android 性能采集 v0（本地子集）：启动耗时（am start -W）与内存（dumpsys meminfo）。

全部 best-effort：无 adb/设备离线/解析失败返回 None，不影响执行主流程。
"""

from __future__ import annotations

import re
import subprocess


def _adb(serial: str | None, *args: str, timeout: int = 15) -> str | None:
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")  # Windows 默认 GBK 会炸中文日志
        return proc.stdout if proc.returncode == 0 else None
    except Exception:
        return None


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
