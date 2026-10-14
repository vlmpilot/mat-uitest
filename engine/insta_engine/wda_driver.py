"""iOS 驱动：WebDriverAgent 实现（BaseDriver 协议 · 模拟器 MVP，/）。

依赖：`pip install facebook-wda`；WDA 服务在模拟器/真机上常驻（复现命令）。
连接：模拟器共享宿主网络栈（localhost:8100 直连，无需 iproxy）；真机的 usbmux 转发属 P2。

关键实测依据：
- dump 走 lean 口径 `/source?format=xml&excluded_attributes=visible`（0.15–1.1s）——
  `visible` 的逐元素解析占整树快照 4–5× 延迟；visible 语义由几何判断补（WdaNode.visible）。
- CJK 输入 setValue/typeText 双路径可用；iOS 无华为安全键盘类拦截（T28 回退不适用）。
- NavigationBar 的 title 在 `name` 属性（P3 探索适配 page_key 锚点用）。

已知边界（P1 模拟器 MVP）：
- XPATH 定位策略不支持（WDA 无 XPath 查询；class chain 映射后置）⇒ LocatorNotSupported。
- press("back") 映射为左缘滑动（iOS 无全局返回键，N2）；"home" 映射 pressHome。
- app_start 的 `activity` 参数忽略（iOS 无 Activity 概念，bundleId 即身份）。
- 会话按 bundleId 绑定（WDA 查询是 App 作用域的）；App 崩溃/被切走后树会过期——
  崩溃检测与多 App 场景属 P2/P3。
"""

from __future__ import annotations

from typing import Any

from .driver_base import BaseDriver
from .locator import LocatorNotSupported

_DUMP_LEAN = "/source?format=xml&excluded_attributes=visible"


def _esc(value: str) -> str:
    """WDA predicate 字符串转义（单引号/反斜杠）。"""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# By（Android 选择器 kwargs 语义）→ WDA predicate。label=可见文本，name=identifier——
# iOS 无独立 content-desc，Android 的 description 与 identifier 同源。
_PREDICATES = {
    "resourceId": lambda v: f"name == '{_esc(v)}'",
    "text": lambda v: f"label == '{_esc(v)}'",
    "textContains": lambda v: f"label CONTAINS '{_esc(v)}'",
    "description": lambda v: f"name == '{_esc(v)}'",
    "descriptionContains": lambda v: f"name CONTAINS '{_esc(v)}'",
}


class WdaNode:
    """WDA 节点包装：为引擎提供 Android 侧 UiObject 同形接口（Node 协议见 driver_base）。

    底层是 wda.Selector（惰性查询）；`value`（输入框内容）优先于 `text`（静态文本）
    作为 get_text 的来源——T28 回读校验语义在 iOS 侧靠 value 字段成立。
    """

    def __init__(self, sel: Any, drv: WdaDriver):
        self._sel = sel
        self._drv = drv

    @property
    def exists(self) -> bool:
        try:
            return bool(self._sel.exists)
        except Exception:
            return False

    def wait(self, timeout: float = 0) -> bool:
        try:
            return bool(self._sel.wait(timeout=timeout or 0.01, raise_error=False))
        except Exception:
            return False

    def click(self) -> None:
        # ⚠️ 不用 Selector.click()——实测（2026-10-07，MatDemo 靶场）它点击不生效
        # （点了 NavigationLink 仍停在首页），而 Element.tap() 可靠。原因未深究，
        # 两条路径在 wda 库里行为不一致——以实测为准（T65 同款教训）。
        self._sel.get().tap()

    def center(self) -> tuple[int, int]:
        b = self._sel.get().bounds
        return (int(b.x + b.width / 2), int(b.y + b.height / 2))

    def set_text(self, text: str) -> None:
        self._sel.get().set_text(text)

    def long_click(self, duration: float = 2.0) -> None:
        """元素级长按。2.0s：实测翻转点 ≤2.0s（U8 标定），留余量——同 T65 哲学。"""
        self._sel.get().tap_hold(duration)

    def clear_text(self) -> None:
        self._sel.get().clear_text()

    def get_text(self) -> str | None:
        el = self._sel.get()
        val = getattr(el, "value", None)
        if val:
            return val
        return el.text

    @property
    def text(self) -> str | None:
        return self.get_text()

    @property
    def info(self) -> dict:
        """u2 info 同形：enabled/checked/selected。

        ⚠️ iOS 的 Switch 开合在 **value**（"0"/"1"）而非 selected——15×3 评测 t12
        稳定失败暴露；checked 按 className 分派，其余控件 selected
        照旧。"""
        el = self._sel.get()
        if getattr(el, "className", "") == "XCUIElementTypeSwitch":
            checked = str(getattr(el, "value", "0")) == "1"
        else:
            checked = bool(el.selected)
        return {"enabled": bool(el.enabled), "checked": checked,
                "selected": bool(el.selected)}

    @property
    def visible(self) -> bool:
        """lean dump 丢了 visible 属性（U2 实测是快照延迟元凶）——几何判断补：
        rect 与窗口求交。x/y/width/height 为负或超界即视为不可见。"""
        el = self._sel.get()
        b = el.bounds
        w, h = self._drv.window_size()
        return b.x + b.width > 0 and b.y + b.height > 0 and b.x < w and b.y < h


class WdaDriver(BaseDriver):
    """iOS 驱动（模拟器 MVP）。真机接入（usbmux 转发/签名/udid）属 P2。"""

    platform = "ios"

    def __init__(self, serial: str | None = None, client: Any = None):
        self._c = client
        if self._c is None:
            import wda  # 延迟导入：设备无关测试不需要安装

            self._c = wda.Client()
        self.serial = serial or self._resolve_udid()
        self._session: Any = None
        self._session_bundle: str | None = None

    def _resolve_udid(self) -> str:
        """真实 udid（T76 同款纪律：对外数字必须能回答「测的是哪台设备」）。

        `/wda/device/info` 返回 `{'uuid': ..., 'model': ..., 'simulator': bool, ...}`；
        取不到（旧版 WDA / 会话未就绪 / 无 device_info）回落字面量 `'simulator'` 并保持
        可辨识（评审 I8：此前恒为 `'simulator'`，接入第二台设备后数字无法区分来源）。
        探测失败**绝不影响主流程**（同 `driver.py` 的 `app_version` 纪律）。
        """
        try:
            info = self._c.device_info() or {}
            udid = info.get("uuid")
            if udid:
                return str(udid)
        except Exception:  # noqa: BLE001 —— 探测失败不影响主流程
            pass
        return "simulator"

    # -- 会话绑定（WDA 查询是 App 作用域） --------------------------------------

    def _bind_session(self, bundle: str | None = None) -> Any:
        if self._session is None or (bundle and bundle != self._session_bundle):
            bundle = bundle or (self._c.app_current() or {}).get("bundleId")
            self._session = self._c.session(bundle) if bundle else self._c.session()
            self._session_bundle = bundle
        return self._session

    # -- 屏幕读取 ---------------------------------------------------------------

    def dump(self) -> str:
        return str(self._bind_session().http.get(_DUMP_LEAN)["value"])

    def screenshot(self, path: str) -> None:
        self._c.screenshot().save(path)

    def screenshot_pil(self) -> Any:
        return self._c.screenshot()

    def window_size(self) -> tuple[int, int]:
        size = self._c.window_size()
        try:
            return int(size[0]), int(size[1])
        except TypeError:
            return int(size.width), int(size.height)

    # -- 动作 -------------------------------------------------------------------

    def click(self, x: int, y: int) -> None:
        self._c.click(x, y)

    def long_click(self, x: int, y: int, duration: float = 2.0) -> None:
        self._c.tap_hold(x, y, duration)

    def long_click_node(self, node: Any) -> None:
        # 坐标级 touchAndHold 在模拟器上不稳定（U8 多轮标定仅一次成功）；
        # 元素级 tap_hold 可靠——override 默认的中心坐标路径。
        node.long_click()

    def swipe_ext(self, direction: str) -> None:
        w, h = self.window_size()
        cx, cy = w // 2, h // 2
        d = direction.lower()
        if d == "up":
            self._c.swipe(cx, int(h * 0.75), cx, int(h * 0.2))
        elif d == "down":
            self._c.swipe(cx, int(h * 0.25), cx, int(h * 0.8))
        elif d == "left":
            self._c.swipe(int(w * 0.8), cy, int(w * 0.2), cy)
        elif d == "right":
            self._c.swipe(int(w * 0.2), cy, int(w * 0.8), cy)
        else:
            raise ValueError(f"不支持的滑动方向: {direction}")

    def press(self, key: str) -> None:
        if key == "home":
            self._c.home()
        elif key == "back":
            # iOS 无全局返回键（N2）。**主路径=点导航栏返回钮**（label=上一页
            # 标题，SwiftUI/UIKit 通用）——边缘滑动合成在模拟器上不触发 interactive
            # pop（U8 同族：多档时长实测均失效），只作兜底（真机 P2 复验）。
            try:
                btn = (self._bind_session()(type="XCUIElementTypeNavigationBar")
                       .child(type="XCUIElementTypeButton"))
                if btn.exists:
                    btn.get().tap()
                    return
            except Exception:
                pass
            w, h = self.window_size()
            self._c.swipe(1, h // 2, int(w * 0.9), h // 2, 0.3)
        else:
            raise ValueError(f"iOS 驱动不支持按键 {key!r}（仅 home/back）")

    def send_keys(self, text: str) -> None:
        self._c.send_keys(text)

    def open_recents(self) -> None:
        """App 切换器（F3 操控）：底部上滑并稍作停顿。

        手势合成在模拟器上有 U8 前科（边缘滑动不触发 interactive pop），
        本手势待模拟器实测标定、真机 P2 复验——失败会如实抛 WDA 异常，不静默。
        """
        w, h = self.window_size()
        self._c.swipe(w // 2, int(h * 0.95), w // 2, int(h * 0.45), 0.6)

    def open_settings(self) -> None:
        """系统设置（F3 操控）：bundleId 直启。

        **不走 `app_start`**：它会 `_bind_session` 把 WDA 会话绑到设置 App——
        设置页不是被测 App，绑了会让后续 `find()` 的 App 作用域漂移。
        """
        self._c.app_launch("com.apple.Preferences")

    # -- App 生命周期 -----------------------------------------------------------

    def app_start(self, package: str, activity: str | None = None) -> None:
        self._c.app_launch(package)  # activity 在 iOS 无意义（bundleId 即身份）
        self._bind_session(package)

    def app_stop(self, package: str) -> None:
        self._c.app_terminate(package)

    def foreground_app(self) -> dict:
        info = self._c.app_current() or {}
        bundle = info.get("bundleId")
        # 协议契约：bundleId 必须填进 'package' 键——上层判 App 归属不分叉（driver_base）
        return {"package": bundle, "bundleId": bundle,
                "name": info.get("name"), "pid": info.get("pid")}

    # -- 定位 -------------------------------------------------------------------

    def find(self, by: str, value: str, timeout: float) -> Any:
        pred = _PREDICATES.get(by)
        if pred is None:
            raise LocatorNotSupported(f"iOS 驱动不支持定位属性 {by!r}（支持：{sorted(_PREDICATES)}）")
        sel = self._bind_session()(predicate=pred(value))
        if timeout > 0:
            sel.wait(timeout=timeout, raise_error=False)
        return WdaNode(sel, self)

    def find_xpath(self, expr: str, timeout: float) -> Any:
        raise LocatorNotSupported("iOS 驱动不支持 XPath 定位策略（WDA 无 XPath 查询，class chain 映射后置）")
