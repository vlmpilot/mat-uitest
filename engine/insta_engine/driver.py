"""Android 驱动：uiautomator2 实现（BaseDriver 协议，见 driver_base）。

uiautomator2 在构造时延迟导入：无设备的单测/CI 环境可安全 import 本模块。
iOS/WDA 由同分支的 wda_driver.py 提供同协议实现。
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from .device import list_adb_devices as _list_adb_devices
from .driver_base import BaseDriver


def _shell_text(out: Any) -> str:
    """驱动 `shell()` 的返回统一转文本。

    Android 走 uiautomator2⇒adbutils 时返回 `ShellResponse`（**不是 str**），iOS 走 WDA
    时返回 str。此前只有 `app_version` 做了兼容，`app_apk_sha256` 与 `device_meta`
    直接按 str 用 ⇒ TypeError 被 `except` 吞掉、产物里的指纹/型号恒为空
    （2026-10-25 真机评测：对外数字必须能回答"测的是哪个构建"）。
    """
    if isinstance(out, str):
        return out
    text = getattr(out, "output", "")
    return text if isinstance(text, str) else str(text or "")


def app_version(drv, package: str) -> str:
    """被测 App 的版本号；取不到返回空串（**绝不因为版本探测失败影响主流程**）。

    **为什么它是共享函数而不是各写一份**（T76）：对外数字必须能回答
    "**这次测的是哪个被测 App 构建**"——`explore` 的图谱一直记 `app_version`，
    而 `runner` 的 `run.json` **没记** ⇒ 靶场源码改过之后（T64 +4 缺陷、T76 +1 崩溃），
    **查不出旧数字是在哪个版本上测的**（`` 那条"在当前 revision 上可复现"
    只追踪了引擎，**漏了靶场**）。
    ⇒ 单源放在这里（低层模块，无环），`explore` 与 `runner` 共用。

    两条路径：先 `app_info()`（平台中立，P0-iOS 起 BaseDriver 契约），失败再
    `dumpsys package`（Android 专属兜底；iOS 端 shell 不支持 ⇒ 如实空串，P2 补 WDA 路径）。
    """
    if not package:
        return ""
    try:
        info = drv.app_info(package) or {}
        name = (info or {}).get("versionName") or ""
        if name:
            return str(name)
    except Exception:  # noqa: BLE001 —— 探测失败就回落下一路径
        pass
    try:
        out = drv.shell(f"dumpsys package {package}")
        text = _shell_text(out)
        match = re.search(r"versionName=([^\s]+)", text)
        return match.group(1) if match else ""
    except Exception:  # noqa: BLE001 —— 两条都失败 ⇒ 空串，不影响主流程
        return ""


def app_apk_sha256(drv, package: str, prefix: int = 12) -> str:
    """安装包指纹（sha256 前 N 位，小写）；取不到返回空串。

    **为什么 `app_version` 不够**（T76 实测）：所有靶场变体共用 `versionName=1.0`
    ⇒ 光看版本号**分辨不出 `baseline` 与 `v09-defect-free`**。
    而"对外数字测的是哪个构建"必须能回答，所以再记一个**安装包指纹**——
    它不依赖构建流程是否规范（手搓的 APK 也能分辨）。

    取不到时返回空串，**绝不影响执行**；`sha256sum` 不可用时回落 `md5sum`。
    （Android 专属路径：pm path + sha256sum 走驱动 shell；iOS 无对应物 ⇒ 空串。）
    """
    if not package:
        return ""
    try:
        match = re.search(r"package:(/\S+\.apk)", _shell_text(drv.shell(f"pm path {package}")))
        if not match:
            return ""
        apk = match.group(1)
    except Exception:  # noqa: BLE001 —— 取不到路径就放弃指纹
        return ""
    for tool in ("sha256sum", "md5sum"):
        try:
            found = re.search(r"\b([0-9a-fA-F]{16,64})\b",
                              _shell_text(drv.shell(f"{tool} {apk}")))
            if found:
                return found.group(1)[:prefix].lower()
        except Exception:  # noqa: BLE001 —— 换下一个工具
            continue
    return ""

#: 长按的按压时长（秒）。
#:
#: ⚠️ **不能用 uiautomator2 的默认 0.5s**。实测（2026-10-06，T65，华为 ELE-AL00 / Android 10，
#: 靶场 `com.insta.demo` 的列表项长按）：
#:
#: | 请求时长 | 实际结果 |
#: |---|---|
#: | 0.5s（u2 默认） | ❌ 被判成**普通点击** ⇒ 跳进详情页，长按监听器根本没触发 |
#: | 1.0s | ❌ 同上 |
#: | **1.2s** | ✅ 长按监听器触发、菜单出现 |
#: | 1.5 / 1.8 / 2.0s | ✅ 同上 |
#:
#: ⇒ **翻转点在 1.0s 与 1.2s 之间**，不是 Android 文档里的 500ms——
#: 因为 u2 的 `long_click` 底层是 `jsonrpc.click(x, y, ms)`，**实际按压比请求的短**
#: （跨进程调用 + 事件分发都有开销）。另外它的 XPath 分支 `XPathSelector.long_click()`
#: **连 duration 参数都没有**（源码注释写 "400ms between down and up"，比阈值还低）。
#:
#: ⇒ 取 **2.0s**：比实测翻转点（1.2s）高 67%，留足余量；且是整数、好记。
LONG_PRESS_SECONDS = 2.0


#: 传输层断连的异常名（假红治理）：u2 走 HTTP 到设备 agent，
#: `RemoteDisconnected`/`ConnectionError` 族出现时**业务没坏、会话坏了**——
#: 重建会话重试一次即可救回，不当失败报（实测 gen-184020-3 连跑 7 次 4 次
#: 假红全是这一类）。名字匹配而非精确类型：u2/requests 的异常跨版本改名，
#: 名字集合足够窄（只认连接族），不会吞业务错。
_TRANSPORT_ERROR_NAMES = {
    "RemoteDisconnected", "ConnectionError", "ConnectionResetError",
    "ConnectionAbortedError", "ReadTimeout", "GatewayError", "ProtocolError",
}


def is_transport_error(exc: BaseException) -> bool:
    """是否传输层断连（决定「重建会话重试一次」要不要触发）。"""
    if isinstance(exc, ConnectionError):
        return True
    return type(exc).__name__ in _TRANSPORT_ERROR_NAMES


class DeviceDriver(BaseDriver):
    """最小驱动接口：dump / screenshot / click / swipe / press / app_start。"""

    platform = "android"

    def __init__(self, serial: str | None = None):
        import uiautomator2 as u2  # 延迟导入：设备无关测试不需要安装

        self._d = u2.connect(serial)
        # 未显式指定 serial 时从 u2/adb 解析真实 serial——性能采集（am start -W）与
        # run.json 的设备记录都依赖它；解析失败保持 None（功能不受影响）
        self.serial = serial or getattr(self._d, "serial", None)
        if not self.serial:
            try:
                devices = [d for d in _list_adb_devices() if d["state"] == "device"]
                if len(devices) == 1:
                    self.serial = devices[0]["serial"]
            except Exception:
                pass

    # -- BaseDriver 中立方法（上层唯一允许依赖的驱动面） --------------------------

    def foreground_app(self) -> dict:
        return self._d.app_current() or {}

    def screenshot_pil(self) -> Any:
        return self._d.screenshot()

    def find(self, by: str, value: str, timeout: float, index: int | None = None) -> Any:
        kwargs: dict[str, Any] = {by: value}
        if index is not None:
            # u2 的 `instance` 直接映射 Android `UiSelector.instance(int)`（0 基，与 DSL 同义）。
            # 只透传这一个键：`index` 在 u2 里是**控件树层级**的 index 属性，语义不同，别混用。
            kwargs["instance"] = index
        node = self._d(**kwargs)
        if timeout > 0:
            # u2 的 `wait()` 超时**抛异常**（不是返回 False）。若不吞，`locator` 的
            # text→description→contains→hint 语义链会在**第一个策略**就中断
            # （2026-10-25 真机实测：链只剩 `text=… 未命中`，等同链不存在）。
            # iOS 侧早已用 `raise_error=False`，这里对齐成"未命中"语义。
            try:
                node.wait(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass
        return node

    def find_xpath(self, expr: str, timeout: float) -> Any:
        selector = self._d.xpath(expr)
        try:
            selector.wait(timeout=timeout)
        except Exception:  # noqa: BLE001 —— 未命中同理：交给调用方读 `.exists`
            pass
        return selector

    def shell(self, cmd: str) -> Any:
        return self._d.shell(cmd)

    def app_info(self, package: str) -> dict:
        try:
            return self._d.app_info(package) or {}
        except Exception:
            return {}

    def dump(self) -> str:
        return self._d.dump_hierarchy()

    def screenshot(self, path: str) -> None:
        self._d.screenshot().save(path)

    def click(self, x: int, y: int) -> None:
        self._d.click(x, y)

    def long_click(self, x: int, y: int, duration: float = LONG_PRESS_SECONDS) -> None:
        """长按。`duration` 默认见 `LONG_PRESS_SECONDS` 的说明（不能用 u2 的 0.5s）。"""
        self._d.long_click(x, y, duration=duration)

    def fallback_input(self, text: str) -> bool:
        """华为安全键盘回退二级通道：`adb input text`。

        `shell` 异常如实上抛（与旧 `Element._adb_input_text` 行为一致，零行为变化）。
        """
        escaped = shlex.quote(text.replace(" ", "%s"))
        self.shell(f"input text {escaped}")
        return True

    #: 按键清空的上限：`count+2` 会再夹到这个值（防一条用例清出 200 个键）。
    MAX_CLEAR_KEYS = 64

    def clear_by_keyevents(self, count: int) -> bool:
        """按键清空输入框（Android）：`MOVE_END` + `count+2` 个 `DEL`，一次 shell 下发。

        **为什么需要第三条通道**：`clearTextField` / `setText("")` 走的都是无障碍
        `ACTION_SET_TEXT`，App 若自绘占位或挂 TextWatcher 就会**静默不生效**
        （2026-10-27 真机：xhs 搜索框 `clear_text()` 不抛异常、文本一点没变）。
        按键事件走的是**焦点输入**通道，与无障碍动作是两条独立的路。

        `count+2` 是余量：清空到占位前缀后多按两下是空操作（实测 xhs 停在「搜索, 」），
        而少按一下就会留一个字符 —— 后者更难发现。

        返回 False 表示"本平台没有这个通道"（iOS 的 WDA 只有元素级 clear），
        由 `Element.clear_text` 决定是记 `note` 还是不记；**不在这里抛异常**。
        """
        if getattr(self, "platform", "android") != "android" or count <= 0:
            return False
        presses = min(count + 2, self.MAX_CLEAR_KEYS)
        keys = " ".join(["KEYCODE_MOVE_END"] + ["KEYCODE_DEL"] * presses)
        self.shell(f"input keyevent {keys}")
        return True

    def open_settings(self) -> None:
        """系统设置（F3 操控）：显式 Intent——固定串，无用户输入拼接，不经 shell 元字符。"""
        self.shell("am start -a android.settings.SETTINGS")

    def wake_and_unlock(self) -> None:
        """唤醒并解锁（C1 契约，2026-10-12 评审）：点亮屏幕 + 清非密码锁。

        `wm dismiss-keyguard` 只能清滑动/无锁屏形态（真机跑测最常见）；有密码的
        锁无法解除，仍需人工解锁一次。shell 失败如实上抛，调用方吞掉（best-effort）。
        """
        self.shell("input keyevent KEYCODE_WAKEUP")
        self.shell("wm dismiss-keyguard")

    def swipe_ext(self, direction: str) -> None:
        self._d.swipe_ext(direction, scale=0.8)

    # -- 证据链（P2.2 契约：Android 委托 adb，单一实现仍在 adb.py） ----------------

    def start_evidence(self) -> None:
        from .adb import logcat_clear

        logcat_clear(self.serial)

    def evidence_tail(self) -> str:
        from .adb import logcat_tail

        return logcat_tail(self.serial)

    def device_meta(self) -> dict:
        """型号 + 系统版本（写进产物溯源；取不到就是空串，不猜）。"""
        def prop(name: str) -> str:
            try:
                return _shell_text(self.shell(f"getprop {name}")).strip()
            except Exception:  # noqa: BLE001 —— 溯源信息缺失不影响执行
                return ""

        return {"model": prop("ro.product.model"),
                "os_version": prop("ro.build.version.release")}

    def press(self, key: str) -> None:
        self._d.press(key)

    def send_keys(self, text: str) -> None:
        """向当前焦点输入框输入文本（u2 快速输入通道，支持中文）。"""
        self._d.send_keys(text)

    def app_start(self, package: str, activity: str | None = None) -> None:
        self._d.app_start(package, activity)

    def reconnect(self) -> None:
        """重建 u2 会话（P0-1 假红治理）：传输断连后由 runner 的重试路径调用。"""
        import uiautomator2 as u2

        self._d = u2.connect(self.serial)

    def app_stop(self, package: str) -> None:
        self._d.app_stop(package)


class NullDriver:
    """无设备占位驱动（P1-1 评审 P2-1）：纯 `Action.API` 用例不需要真机/模拟器。

    Runner 对 api 步骤在 `_run_step_once` 开头提前返回，全程不触碰设备接口；
    本类只负责把 `_persist` 的溯源探测（app_version / app_apk_sha256 /
    device_meta）安全落到**空值**——溯源缺失如实为空，不伪装成有设备。
    混入任何 UI 步骤的用例不适用（cmd_run 会按需连真设备）。
    """

    platform = "null"
    serial = None

    def app_info(self, package: str) -> dict:
        return {}

    def shell(self, cmd: str) -> str:
        return ""

    def device_meta(self) -> dict:
        return {}

    def evidence_tail(self) -> str:
        return ""

    def foreground_app(self) -> dict:
        return {}
