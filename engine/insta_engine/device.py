"""设备发现：解析 `adb devices -l`。

iOS（usbmuxd/WDA）为后续版本范围（Android 优先）。
"""

from __future__ import annotations

import subprocess

_OS_CACHE: dict[str, str] = {}


def _android_version(serial: str) -> str:
    """Android 系统版本（getprop）。按 serial 缓存——/api/state 5s 轮询不能每次跑子进程，
    版本号除非 OTA 升级不会变，缓存失效场景（换机重插同 serial）可忽略。"""
    if serial not in _OS_CACHE:
        try:
            out = subprocess.run(
                ["adb", "-s", serial, "shell", "getprop", "ro.build.version.release"],
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace"
            ).stdout
            if (out or "").strip():
                _OS_CACHE[serial] = out.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return _OS_CACHE.get(serial, "")


def list_adb_devices() -> list[dict[str, str]]:
    """返回 [{'serial': ..., 'state': 'device', 'model': ..., 'os': '14'}, ...]；仅列在线设备。

    adb 缺失/异常时返回 []（容错对齐 perf._adb，避免配置台无限骨架屏）。
    """
    try:
        out = subprocess.run(
            ["adb", "devices", "-l"], capture_output=True, text=True, timeout=10, check=True,
            encoding="utf-8", errors="replace"
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    devices: list[dict[str, str]] = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            info = {"serial": parts[0], "state": parts[1]}
            for item in parts[2:]:
                if ":" in item:
                    key, value = item.split(":", 1)
                    info[key] = value
            info["os"] = _android_version(parts[0])
            devices.append(info)
    return devices
