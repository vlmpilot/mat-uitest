"""平台中立驱动接口（· feature/ios-support 分支）。

目标：上层引擎（locator/runner/explore/page_index/arrivals/web_server）只依赖本协议，
不直接触碰 uiautomator2 / WDA 平台 API——Android/iOS 双实现后，平台差异只存在于
各驱动模块内部（Android=driver.py，iOS=wda_driver.py，后续）。

移植面盘点（2026-10-07，grep `.raw` 全仓）：上层经 `.raw` 触达 u2 的调用点共
**12 处 / 5 文件**——foreground_app×4（arrivals/explore/page_index/web_server）、
选择器构造×5（locator resolve/heal）、xpath×1（locator）、shell×1（locator 华为
输入回退）、screenshot PIL×2（locator 模板匹配/web_server 投屏）。全部对应下方
中立方法。**P0② 迁 5 处（foreground_app×4 + screenshot_pil×1）+ P0③ 迁 7 处
（locator.py 全部）⇒ 上层 `.raw` 调用已清零，Android 驱动上的 `raw` 属性已随之移除**——
上层从此只依赖本协议；iOS 实现本协议即可接入全部上层（locator/runner/explore）。

`Node` 协议（find/find_xpath 返回的对象）需支持：exists / wait(timeout) / click() /
center() / set_text(text) / clear_text() / get_text() / text / info——Android 侧即
u2 的 UiObject / XPathSelector 原生对象；iOS 侧由 WdaNode 提供同形对象（后续）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any


class By(StrEnum):
    """中立定位属性名。Android 实现按同名直接映射为 u2 选择器 kwargs。"""

    RES_ID = "resourceId"
    TEXT = "text"
    TEXT_CONTAINS = "textContains"
    DESCRIPTION = "description"
    DESCRIPTION_CONTAINS = "descriptionContains"


class BaseDriver(ABC):
    """平台中立驱动接口：上层引擎只 import 本模块，不 import u2/WDA。"""

    platform: str = "unknown"

    #: 设备序列号（Android=serial，iOS=udid）；解析失败保持 None（功能不受影响）
    serial: str | None = None

    # -- 屏幕读取 ---------------------------------------------------------------

    @abstractmethod
    def dump(self) -> str:
        """当前控件树 XML。"""

    @abstractmethod
    def screenshot(self, path: str) -> None:
        """截图保存到路径。"""

    @abstractmethod
    def screenshot_pil(self) -> Any:
        """截图为 PIL Image（RGB）：模板匹配与投屏用。"""

    # -- 动作 -------------------------------------------------------------------

    @abstractmethod
    def click(self, x: int, y: int) -> None: ...

    @abstractmethod
    def long_click(self, x: int, y: int, duration: float) -> None:
        """长按。按压时长与平台阈值的校准由实现负责（T65 教训：文档值不可信）。"""

    def long_click_node(self, node: Any) -> None:
        """节点级长按：默认取中心走坐标长按（Android/T65 语义，行为不变）。

        有更可靠的元素级实现时 override——iOS 的坐标级 touchAndHold 在模拟器上
        不稳定（多轮标定仅一次成功，U8），而元素级 tap_hold 可靠。
        """
        x, y = node.center()
        self.long_click(int(x), int(y))

    def fallback_input(self, text: str) -> bool:
        """输入回读为空时的**二级输入通道**。默认无（返回 False）。

        与 `long_click_node` 同款范式：基类默认实现 = 「无二级通道」，有需要的平台 override。

        - **Android override**：华为安全键盘回退（走 `adb input text`）。
        - **iOS 不 override**：iOS 无华为安全键盘类拦截（U6 实测 `setValue`/`typeText`
          双路径可用），所以「没有二级通道」是事实——调用方据此**如实报「输入未生效」**。

        返回值：True=已通过二级通道输入；False=无二级通道，调用方必须如实报错。
        为什么要有这个协议方法（评审 I1）：此前 `Element.set_text` 直接调 Android 专属的
        `_adb_input_text` → `drv.shell()`，在 iOS 上抛 `NotImplementedError: ios 驱动不支持 shell`
        ——异常文本与真实原因（输入未生效）无关，且会进入 `diagnose` 归因链被误判。
        """
        return False

    @abstractmethod
    def swipe_ext(self, direction: str) -> None: ...

    @abstractmethod
    def press(self, key: str) -> None: ...

    @abstractmethod
    def send_keys(self, text: str) -> None: ...

    # -- 设备级操控(配置台「常见操控」,F3) --------------------------------------
    # 语义固定为「用户手动按了下设备键」:**不抢设备锁**——人的干预恰是任务
    # 卡死时的救场通道,必须永远畅通;后果(可能干扰用例)由调用方警示,
    # 选择权留给人(设计文档 §4.3)。

    def home(self) -> None:
        """回主屏。默认 `press("home")`(双平台该键语义一致)。"""
        self.press("home")

    def back(self) -> None:
        """返回。`press("back")` 已含平台差异(Android 物理键 / iOS 导航栏返回钮)。"""
        self.press("back")

    def open_recents(self) -> None:
        """打开最近任务。默认 `press("recent")`(Android);iOS override 为上滑停顿。"""
        self.press("recent")

    def open_settings(self) -> None:
        """打开系统设置。**无跨平台默认**(启动入口是平台 API)——平台各自 override。"""
        raise NotImplementedError(f"{self.platform} 驱动未实现 open_settings")

    def wake_and_unlock(self) -> None:
        """唤醒并解锁屏幕（2026-10-12 评审 C1 契约方法）。

        锁屏是执行失败与调用挂死的头号环境因素。**默认无操作**（iOS WDA 场景
        P2），Android 平台必须 override（KEYCODE_WAKEUP + dismiss-keyguard）。
        契约存在的意义：上层 `_wake_device` 不再用「`raw` 属性缺失」推断真机/桩
        —— `raw` 随 P0 迁移删除后该判据恒真，唤醒防护实际从未生效。
        """
        return None

    # -- App 生命周期 -----------------------------------------------------------

    @abstractmethod
    def app_start(self, package: str, activity: str | None = None) -> None: ...

    @abstractmethod
    def app_stop(self, package: str) -> None: ...

    @abstractmethod
    def foreground_app(self) -> dict:
        """前台 App 信息。

        Android={'package', 'activity', ...}；iOS 实现必须把 bundleId 填进
        **'package'** 键——上层判 App 归属（离场检测）只读这一个键，平台判断
        代码因此不用分叉。
        """

    def app_info(self, package: str) -> dict:
        """已装 App 信息（versionName 等，供靶场指纹）；平台不支持/未装返回 {}。

        Android 覆盖实现（u2 app_info）；iOS 暂用默认（WDA appInfo 属 P2）。
        """
        return {}

    # -- 定位（选择器构造收进驱动，上层不见平台 API） -----------------------------

    @abstractmethod
    def find(self, by: str, value: str, timeout: float) -> Any:
        """按属性构造选择器并等待出现；返回 Node（调用方以 `.exists` 判命中）。

        `timeout=0` 表示不等待只构造——text 语义等价链末级回退的既有语义。
        """

    @abstractmethod
    def find_xpath(self, expr: str, timeout: float) -> Any: ...

    # -- 平台逃生舱 -------------------------------------------------------------

    def shell(self, cmd: str) -> Any:
        """平台命令通道。Android=adb shell（华为输入回退专用）；iOS 不提供。"""
        raise NotImplementedError(f"{self.platform} 驱动不支持 shell")
