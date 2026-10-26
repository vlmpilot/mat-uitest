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


def _list_adb_devices_py() -> list[dict[str, str]]:
    """CLI `adb` 不可用时的兑底：走 uiautomator2 自带的 adbutils（纯 Python ADB 客户端）。

    实测（2026-10-19）：adb 二进制不在 PATH，但 adb server 由别的工具拉起、真机在线——
    驱动（uiautomator2→adbutils）连得上，CLI 列表却是空 ⇒ 控制台显示「无设备」、
    /api/device/key 的在线校验误判 400，而设备明明能跑。两套传输必须收敛成
    **同一个口径**：CLI 缺失/失败时以驱动同款传输为准。
    """
    try:
        import adbutils
        raw = adbutils.adb.device_list()
    except Exception:  # noqa: BLE001 —— adb server 不在/起不来/依赖缺失：当没有设备
        return []
    devices: list[dict[str, str]] = []
    for d in raw:
        info: dict[str, str] = {"serial": d.serial, "state": "device"}
        for prop, key in (("ro.product.model", "model"),
                          ("ro.build.version.release", "os")):
            try:
                value = (d.getprop(prop) or "").strip()
            except Exception:  # noqa: BLE001 —— 单属性失败不拖垮列表
                value = ""
            if value:
                info[key] = value
        devices.append(info)
    return devices


def list_adb_devices() -> list[dict[str, str]]:
    """返回 [{'serial': ..., 'state': 'device', 'model': ..., 'os': '14'}, ...]；仅列在线设备。

    adb 缺失/异常时回退 adbutils（与驱动同传输，见 _list_adb_devices_py）；
    仍取不到才算空（容错对齐 perf._adb，避免配置台无限骨架屏）。
    """
    try:
        out = subprocess.run(
            ["adb", "devices", "-l"], capture_output=True, text=True, timeout=10, check=True,
            encoding="utf-8", errors="replace"
        ).stdout
    except (OSError, subprocess.SubprocessError):
        # CLI 缺失 ≠ 没有设备：控制台/接口校验必须与驱动看得到的世界一致
        return _list_adb_devices_py()
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
