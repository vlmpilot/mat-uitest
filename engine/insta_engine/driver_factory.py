"""共享驱动工厂：平台路由的**单源**（评审 I4 修复）。

为什么单独成模块：此前存在**两份平台路由**——CLI 侧 `__main__._connect_driver` 认得
`--platform`，而 `web_server.get_driver` 硬编码 `DeviceDriver` ⇒ `mat console` / MCP
路径上的 iOS **完全不可达**；两份实现必然漂移，且违背 `driver_base.py` 的架构承诺
（"上层引擎只依赖本协议"，点名了 web_server）。

设计取舍：
- **延迟导入各驱动**：未装 `facebook-wda` 的环境跑 Android 完全不受影响。
- **失败抛原始异常**（不吞、不打印）：排查提示由调用方渲染——CLI 打印可行动指引，
  server 侧进 job error 事件（既有契约：`api_run` 在 job 内建驱动，失败优雅落 error）。
"""

from __future__ import annotations

from .driver_base import BaseDriver


def connect_driver(serial: str | None, platform: str = "android") -> BaseDriver:
    """按平台构造驱动；`platform` 缺省 `"android"`（既有行为不变）。"""
    if platform == "ios":
        from .wda_driver import WdaDriver  # 延迟导入：无 facebook-wda 也能跑 Android

        return WdaDriver(serial)
    if platform == "harmony":
        from .harmony_driver import HarmonyDriver  # 延迟导入：无 hdc 也能跑 Android/iOS

        return HarmonyDriver(serial)
    from .driver import DeviceDriver

    return DeviceDriver(serial)
