"""iOS 驱动：WebDriverAgent 实现（BaseDriver 协议 · 模拟器 + 真机）。

依赖：`pip install facebook-wda`；WDA 服务在模拟器/真机上常驻（复现命令见 `mat ios-setup`）。
连接：**模拟器**共享宿主网络栈（localhost:8100 直连，无需 iproxy）；
**真机**走 usbmux（`wda.USBClient(udid)` → `/var/run/usbmuxd` socket，纯 Python，无 iproxy）。
选路依据是 serial 的形态（真机 udid 是 `8+16` hex，模拟器是 36 位 UUID），见 `_is_device_udid`。

关键实测依据：
- dump 走 lean 口径 `/source?format=xml&excluded_attributes=visible`（0.15–1.1s）——
  `visible` 的逐元素解析占整树快照 4–5× 延迟；visible 语义由几何判断补（WdaNode.visible）。
- CJK 输入 setValue/typeText 双路径可用；iOS 无华为安全键盘类拦截（T28 回退不适用）。
- NavigationBar 的 title 在 `name` 属性（P3 探索适配 page_key 锚点用）。

已知边界（模拟器 + USB 真机，2026-10-18 真机打通）：
- XPATH 定位策略不支持（WDA 无 XPath 查询；class chain 映射后置）⇒ LocatorNotSupported。
- press("back") 映射为左缘滑动（iOS 无全局返回键，N2）；"home" 映射 pressHome。
- app_start 的 `activity` 参数忽略（iOS 无 Activity 概念，bundleId 即身份）。
- 会话按 bundleId 绑定（WDA 查询是 App 作用域的）；App 崩溃/被切走后树会过期——
  崩溃检测以 "session 失效 + app_current 消失" 为准（syslog 只作证据）；
  多 App 场景与页面图谱适配属 P3。
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any

from .driver_base import BaseDriver
from .locator import LocatorNotSupported

_DUMP_LEAN = "/source?format=xml&excluded_attributes=visible"

#: 真机 udid 形态：`00008150-000528183C98C01C`（8+16 hex）。
#: 模拟器是 36 位 UUID（8-4-4-4-12，四段）⇒ 只能靠**段数/长度**区分。
#: 混用的后果：真机被当模拟器去连 localhost:8100（永远连不上，且报错看不懂）。
_DEVICE_UDID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}$")


def _is_device_udid(serial: str | None) -> bool:
    return bool(serial and _DEVICE_UDID_RE.match(str(serial).strip()))


def _esc(value: str) -> str:
    """WDA predicate 字符串转义（单引号/反斜杠）。"""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# By（Android 选择器 kwargs 语义）→ WDA predicate。label=可见文本，name=identifier——
# iOS 无独立 content-desc，Android 的 description 与 identifier 同源。
_PREDICATES = {
    "resourceId": lambda v: f"name == '{_esc(v)}'",
    # ⚠️ TextField/SecureField 的占位文案在 `placeholderValue`，**不在 label 上**
    # （2026-10-18 真机实测：断言「姓名」在表单页定位不到，而控件明明可见）。
    # 所以 text/textContains 必须同时匹配 label 与 placeholderValue。
    "text": lambda v: f"label == '{_esc(v)}' OR placeholderValue == '{_esc(v)}'",
    "textContains": lambda v: f"label CONTAINS '{_esc(v)}' OR placeholderValue CONTAINS '{_esc(v)}'",
    "description": lambda v: f"name == '{_esc(v)}'",
    "descriptionContains": lambda v: f"name CONTAINS '{_esc(v)}'",
}

#: 系统弹窗按钮的**放行优先**顺序（2026-10-19 发起人定：默认允许授权）。
#: 为什么放行而不是拒绝：自动化跑的是被测 App 的真实行为，默认拒绝网络权限会让
#: App 白屏/断网，把**环境问题伪装成产品缺陷**；拒绝只在弹窗没有"允许"类按钮时兜底
#: （弹窗不消失比拒绝更糟——它会一直盖住前台）。
#: 顺序按"最宽松"排：蜂窝弹窗优先「无线局域网与蜂窝网络」（Wi-Fi+蜂窝），
#: 退而求其次「仅限无线局域网」（仍允许 Wi-Fi）。
_ALLOW_LABELS = (
    "无线局域网与蜂窝网络",
    "允许",
    "使用App时允许",
    "允许一次",
    "仅限无线局域网",
    "Allow",
    "好",
    "OK",
    "继续",
    "同意",
)
#: 没有任何"允许"类按钮时的兜底（点掉比留着强）
_FALLBACK_LABELS = ("以后再说", "暂不", "不允许", "Don't Allow", "取消", "Cancel")


def _any_label_predicate(labels: tuple[str, ...]) -> str:
    """一次查询命中任意按钮（省往返：逐个 label 查 10 次要 1~2s）。"""
    return " OR ".join(f"label == '{_esc(x)}'" for x in labels)


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
        el = self._sel.get()
        if getattr(el, "className", "") == "XCUIElementTypeSwitch":
            # Switch 的**行元素**（name=业务标识）bounds 覆盖整行，tap 打的是行中心 ⇒
            # **不触发开关**（2026-10-17 真机实测：元素 tap / 坐标 (201,456) 均无效，
            # 点控件本体 (340,456) 才翻转）。这与真机调试记录 t12 的"合成点击失效"同源，
            # 根因是**点错位置**而不是合成事件不被接受。
            try:
                inner = self._sel.child(type="XCUIElementTypeSwitch")
                if inner.exists:
                    inner.get().tap()
                    return
            except Exception:  # noqa: BLE001 —— 拿不到子控件就退回整元素 tap
                pass
        el.tap()

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
    """iOS 驱动：模拟器（localhost）与真机（usbmux）同一实现，按 serial 形态选路。"""

    platform = "ios"

    def __init__(self, serial: str | None = None, client: Any = None):
        self._c = client
        if self._c is None:
            import wda  # 延迟导入：设备无关测试不需要安装

            # 真机 udid ⇒ usbmux 隧道；None/"simulator"/模拟器 udid ⇒ localhost（原行为不变）
            self._c = (wda.USBClient(udid=serial) if _is_device_udid(serial)
                       else wda.Client())
        self.serial = serial or self._resolve_udid()
        self._session: Any = None
        self._session_bundle: str | None = None
        self._syslog: Any = None
        self._keepalive: Any = None
        #: 批次级保活持有（CLI 批跑/控制台任务）：True 时 Runner 的 stop_keepalive 不生效。
        #: 为什么需要：用例**之间**的空隙也在耗 idle 时间，逐用例停保活会让手机在
        #: 切 case 时锁屏（真机锁屏后 WDA 无法代解锁 ⇒ 后续全红）。
        self._keepalive_hold = False

    # -- 防锁屏保活（2026-10-18 实测） -------------------------------------------

    def start_keepalive(self, interval: float = 20.0) -> None:
        """真机保活：**交替按音量键**，重置系统空闲计时。

        为什么是音量键：它不改前台 App、不改网络状态、不产生触摸事件（不会误点控件），
        实测连续 90s 保持未锁屏。交替上/下让音量不漂移。

        只对真机生效（模拟器不会自动锁屏）；`MAT_IOS_KEEPALIVE=0` 可关闭。
        """
        if not _is_device_udid(self.serial) or os.environ.get("MAT_IOS_KEEPALIVE") == "0":
            return
        if getattr(self, "_keepalive", None) is not None:
            return
        stop = threading.Event()
        state = {"up": True}

        def _beat() -> None:
            while not stop.wait(interval):
                # ⚠️ **单拍失败不能让线程退出**（2026-10-19 真机实测）：冷启动
                # （app_stop→app_start）会让绑定 App 的会话失效，`press` 抛一次
                # 异常；旧实现直接 `return` ⇒ 保活静默死亡 ⇒ 约 1 分钟后手机锁屏，
                # 表现为"跑到第 5 条开始全红"。这里换键重试，下拍继续。
                for key in ("volumeUp" if state["up"] else "volumeDown",
                            "volumeDown" if state["up"] else "volumeUp"):
                    try:
                        self._c.press(key)
                        state["up"] = not state["up"]
                        break
                    except Exception:  # noqa: BLE001 —— 会话失效/瞬断：下一拍再试
                        continue

        thread = threading.Thread(target=_beat, daemon=True)
        thread.start()
        self._keepalive = (stop, thread)

    def stop_keepalive(self) -> None:
        # 批次级持有期间不真正停止（见 `hold_keepalive`）
        if getattr(self, "_keepalive_hold", False):
            return
        ka = getattr(self, "_keepalive", None)
        if not ka:
            return
        stop, thread = ka
        stop.set()
        thread.join(timeout=2)
        self._keepalive = None

    def hold_keepalive(self) -> None:
        """批次级保活：**整个进程**期间不停止（批跑时用例间隙也要续屏）。

        CLI 批处理在连接设备后调用一次即可（进程退出时守护线程自然结束）。
        用户仍可用 `MAT_IOS_KEEPALIVE=0` 关掉保活。
        """
        self._keepalive_hold = True
        self.start_keepalive()

    def release_keepalive(self) -> None:
        self._keepalive_hold = False
        self.stop_keepalive()

    # -- 证据链（P2.2 契约） -----------------------------------------------------

    def start_evidence(self) -> None:
        """真机：起 syslog 采集并清空缓冲（对齐 `logcat -c` 的"只留本轮"语义）。

        **模拟器不采**：宿主 `log stream` 不是设备日志，且暂不做（明确排除项）。
        未装 pymobiledevice3 时静默降级——`evidence_tail()` 会返回空，runner 会把它
        暴露成 warning（"没有证据源"≠"没有日志"）。
        """
        if not _is_device_udid(self.serial):
            return
        from .ios_syslog import get_buffer

        self._syslog = get_buffer(self.serial)
        self._syslog.clear()
        self._syslog.start()

    def evidence_tail(self) -> str:
        buf = self._syslog
        if buf is None:
            return ""
        from .ios_syslog import EVIDENCE_TAIL_LINES

        return buf.tail(EVIDENCE_TAIL_LINES)

    def device_meta(self) -> dict:
        """型号 + iOS 版本：`device_info` 只有 model/name，版本在 `/status.os.version`。

        ⚠️ **真机上 `device_info` 只给泛化的 "iPhone"**（2026-10-18 实测，`name` 也是 "iPhone"），
        拿不到 "iPhone 17 Pro" 这种营销名 ⇒ 真机改用 devicectl 的 `marketingName`
        （`ios_device.list_devices()`），拿不到再退回 WDA 的值。
        """
        info = self._with_retry(lambda: self._c.device_info()) or {}
        status = self._with_retry(lambda: self._c.status()) or {}
        os_block = status.get("os") or {}
        meta = {"model": info.get("name") or info.get("model") or "",
                "os_version": os_block.get("version") or "",
                "platform": "ios"}
        if _is_device_udid(self.serial):
            try:
                from .ios_device import list_devices

                for dev in list_devices():
                    if dev["udid"] == self.serial:
                        meta["model"] = dev.get("model") or meta["model"]
                        meta["os_version"] = dev.get("os_version") or meta["os_version"]
                        break
            except Exception:  # noqa: BLE001 —— 溯源信息缺失不影响执行
                pass
        return meta

    def wake_and_unlock(self) -> None:
        """唤醒并解锁（真机锁屏是头号环境失败源，2026-10-17 实测）。

        **只在真机做**：模拟器上 `unlock()` 的 home+上滑会把 App 切到切换器（副作用），
        而模拟器本来也不会锁屏。失败不抛——BaseDriver 契约要求 best-effort，
        `Runner._wake_device` 已包 try/except。
        """
        if _is_device_udid(self.serial):
            self._c.unlock()

    # -- usbmux 瞬断重试（2026-10-17 真机实测） ---------------------------------
    #: 瞬断特征串：usbmux 拿不到设备对象 / 连接被对端关掉。**不重试**业务错误
    #: （定位失败、锁屏、App 崩溃）——那些重试也没用，只会把 1 次失败拖成 3 次。
    _TRANSIENT = ("make_http_connection", "Remote end closed connection",
                  "Connection reset by peer", "Broken pipe", "Connection aborted")

    def _reconnect(self) -> None:
        """重建客户端（真机）。会话随之失效——WDA 侧会话是绑 App 的，必须重建。"""
        if not _is_device_udid(self.serial):
            return
        import wda  # 延迟导入：与 __init__ 同理由

        self._c = wda.USBClient(udid=self.serial)
        self._session = None
        # ⚠️ **不清 `_session_bundle`**：它是"目标 App"而不是"当前会话状态"，
        # 重连后要靠它把会话绑回被测 App（清掉就会去问 app_current，冷启动窗口期
        # 可能绑定 springboard ⇒ 深树 ⇒ call depth exceed 10）

    @classmethod
    def _is_transient(cls, exc: Exception) -> bool:
        msg = str(exc)
        return any(k in msg for k in cls._TRANSIENT)

    def _with_retry(self, fn: Any) -> Any:
        """执行一次；**仅瞬断**时重连再试一次（实测切飞行模式/拔插会触发）。

        只重试一次：usbmux 真断了的话，第二次也会失败，继续重试只是拖慢失败。
        """
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 —— 只拦瞬断，其余原样抛
            if not self._is_transient(exc):
                raise self._explain(exc) from exc
            self._reconnect()
            try:
                return fn()
            except Exception as again:  # noqa: BLE001
                raise self._explain(again) from again

    @staticmethod
    def _explain(exc: Exception) -> Exception:
        """把底层库的"看不懂"错误换成**可行动**的提示（原样返回其它异常）。

        实测（2026-10-20）：设备走 Wi-Fi / WDA 没起时，usbmux 客户端是 `None`，
        报出来是 `'NoneType' object has no attribute 'make_http_connection'`——
        用户完全看不出"该插数据线"还是"该起 WDA"。
        """
        text = f"{type(exc).__name__}: {exc}"
        if "make_http_connection" in text:
            return RuntimeError(
                "连不上真机 WDA（usbmux 没有设备对象）。常见两种原因：\n"
                "  ① 设备经 **Wi-Fi** 连接——V1 仅支持 USB，请插数据线；\n"
                "  ② WDA 服务没在跑——运行 `mat ios-setup --start` 自动拉起"
                "（有已签名产物时不需要账号）。\n"
                "  先跑 `mat ios-setup` 逐项体检。")
        return exc

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

    # -- 系统弹窗（2026-10-19 真机定论：**不能**建 springboard 会话） -------------

    @staticmethod
    def _tap_any(session: Any, labels: tuple[str, ...]) -> str | None:
        """在给定会话里**一次查询**命中任意按钮并点击；返回点了哪个（None=没有）。"""
        try:
            sel = session(predicate=_any_label_predicate(labels))
            if not sel.exists:
                return None
            el = sel.get()
            hit = getattr(el, "label", None) or getattr(el, "name", None) or "?"
            el.tap()
            return str(hit)
        except Exception:  # noqa: BLE001 —— 探测语义：查不到就是没有
            return None

    def dismiss_system_alert(self) -> str | None:
        """清掉盖住前台的系统弹窗（**只走 App 会话**），返回点了哪个按钮。

        关键实测（2026-10-19，iPhone 17 Pro / iOS 26.5.2）：
        - 系统弹窗（「允许"MAT 靶场"使用无线数据？」）**在 App 会话里就是可见的**：
          `/alert/text` 能拿到全文，`/wda/alert/buttons` 能列出三个按钮；
          `/alert/accept {"name": ...}` 能按名字点中。
        - ⇒ **不需要**建 springboard 会话。此前为了"看到"它去建该会话，
          WDA 会激活/重启 SpringBoard ⇒「黑屏+转圈（respring）→ 锁屏」，
          是自伤（已删除该路径）。

        策略 = **放行优先**（`_ALLOW_LABELS`），没有"允许"类按钮才点拒绝
        （`_FALLBACK_LABELS`）——弹窗不消失会盖死前台，比拒绝更糟。
        """
        session = self._bind_session()
        try:
            alert = session.alert
            if alert.exists:
                labels = [str(x) for x in (alert.buttons() or [])]
                for want in _ALLOW_LABELS + _FALLBACK_LABELS:
                    if want in labels:
                        alert.click(want)   # POST /alert/accept {"name": ...}
                        return want
                # 列不出名字（老版 WDA）时退回 accept 默认按钮
                alert.accept()
                return "accept"
        except Exception:  # noqa: BLE001 —— 没有弹窗/接口不支持：走下面的普通控件路径
            pass
        # App 自己弹的授权框（非系统 Alert）：按文案点
        return self._tap_any(session, _ALLOW_LABELS)

    # -- 会话绑定（WDA 查询是 App 作用域） --------------------------------------

    def _bind_session(self, bundle: str | None = None) -> Any:
        # 已有可用会话且没要求换 App ⇒ 直接复用
        if self._session is not None and (not bundle or bundle == self._session_bundle):
            return self._session
        # ⚠️ 优先用**上次绑定的 App**（`_session_bundle` 在重连后仍保留）：
        # usbmux 瞬断会 `_reconnect()` 把会话置空，此时若去问 `app_current()`，
        # 冷启动/弹窗窗口期会拿到 `com.apple.springboard`——绑定系统 UI 后每次查询
        # 都要遍历 SpringBoard 的深树，表现为 `call depth exceed 10`（2026-10-19 真机）。
        target = bundle or self._session_bundle
        if target is None:
            target = self._with_retry(lambda: self._c.app_current() or {}).get("bundleId")
            if target == "com.apple.springboard":
                target = None  # 系统 UI 没有可用的 App 会话，宁可不绑 bundle
        try:
            self._session = self._with_retry(
                lambda: self._c.session(target) if target else self._c.session())
        except Exception as exc:  # noqa: BLE001 —— 只翻译"锁屏"这一类，其余原样抛
            # 真机锁屏时 WDA 建会话必失败：`Unable to launch … because the device was
            # not, or could not be, unlocked.` 这是**环境问题**，用户必须知道怎么修；
            # 原样抛会让报告里出现一大段 FBSOpenApplicationServiceErrorDomain。
            if "unlocked" in str(exc).lower() or "Locked" in str(exc):
                raise RuntimeError(
                    "设备锁屏：请解锁 iPhone，并把「设置 → 显示与亮度 → 自动锁定」设为"
                    "「永不」（有锁屏密码时 WDA 无法代你解锁——这是 iOS 的限制）"
                ) from exc
            raise
        self._session_bundle = target
        return self._session

    # -- 屏幕读取 ---------------------------------------------------------------

    def dump(self) -> str:
        try:
            return str(self._with_retry(
                lambda: self._bind_session().http.get(_DUMP_LEAN)["value"]))
        except Exception as exc:  # noqa: BLE001 —— 只翻译"拿不到树"这一类，其余原样抛
            # 锁屏时 WDA 的报错是 `Cannot get 'xml' source of the current application`
            # （不是 session 创建失败）——2026-10-17 真机实测。两种原因都指向环境，
            # 给一条能自己排查的提示，别让报告里只剩 WDAUnknownError。
            if "Cannot get 'xml' source" in str(exc):
                raise RuntimeError(
                    "拿不到控件树：设备可能已锁屏（解锁，并把「设置 → 显示与亮度 → 自动锁定」"
                    "设为「永不」），或前台 App 已退出/崩溃"
                ) from exc
            raise

    def screenshot(self, path: str) -> None:
        self._with_retry(lambda: self._c.screenshot().save(path))

    def screenshot_pil(self) -> Any:
        return self._with_retry(lambda: self._c.screenshot())

    def window_size(self) -> tuple[int, int]:
        size = self._with_retry(lambda: self._c.window_size())
        try:
            return int(size[0]), int(size[1])
        except TypeError:
            return int(size.width), int(size.height)

    # -- 动作 -------------------------------------------------------------------

    def click(self, x: int, y: int) -> None:
        self._with_retry(lambda: self._c.click(x, y))

    def long_click(self, x: int, y: int, duration: float = 2.0) -> None:
        self._with_retry(lambda: self._c.tap_hold(x, y, duration))

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
        self._with_retry(lambda: self._c.send_keys(text))

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
        # activity 在 iOS 无意义（bundleId 即身份）
        self._with_retry(lambda: self._c.app_launch(package))
        # **兜底**：真机上 WDA 的 app_launch 在"被测 App 与 test host 同 id/同前缀"时会
        # **静默失败**（不报错，前台仍是 springboard）——2026-10-18 实测。
        # 用 devicectl 再试一次；只在"前台确实不是目标包"时才付这个成本。
        if _is_device_udid(self.serial):
            try:
                if self.foreground_app().get("package") != package:
                    from .ios_device import launch_app

                    launch_app(self.serial, package)
            except Exception:  # noqa: BLE001 —— 兜底失败不影响主流程（后续步骤会如实报）
                pass
        self._bind_session(package)

    def app_stop(self, package: str) -> None:
        self._with_retry(lambda: self._c.app_terminate(package))

    def foreground_app(self) -> dict:
        info = self._with_retry(lambda: self._c.app_current()) or {}
        bundle = info.get("bundleId")
        # 协议契约：bundleId 必须填进 'package' 键——上层判 App 归属不分叉（driver_base）
        return {"package": bundle, "bundleId": bundle,
                "name": info.get("name"), "pid": info.get("pid")}

    def app_on_screen(self, bundle: str | None = None) -> bool:
        """目标 App 的界面是否真的在屏幕上——**比 `foreground_app()` 可靠**。

        真机实测（2026-10-19）：iOS 的**通知横幅**、控制中心、来电横幅都会让
        `app_current()` 返回 `com.apple.springboard`，但 App 明明还在屏幕上、
        控件树照常可读。当时每步开头都按 `foreground_app()` 判"有没有系统浮层"，
        于是一条普通的开发者通知横幅就把 42 条用例全判成「有系统浮层盖住前台」，
        截图里 MAT 靶场主菜单清清楚楚地摆在那儿。

        判据 = **绑定目标 App 的会话能 dump 出树**。App 真退到后台时 WDA 的 App
        会话会失效（dump 抛错），这样才和"横幅挡住但不影响操作"区分开。
        """
        target = bundle or self._session_bundle
        try:
            self._bind_session(target)
        except Exception:  # noqa: BLE001 —— 建不了会话 = App 不在了
            return False
        try:
            xml = self.dump()
        except Exception:  # noqa: BLE001 —— 拿不到树 = 不在前台
            return False
        return bool(xml) and "XCUIElementTypeApplication" in xml

    # -- 软键盘（2026-10-19 真机定论：WDA 没有收键盘的端点） ---------------------

    def dismiss_keyboard(self) -> bool:
        """收起软键盘（若在编辑态）。返回是否成功发出。

        WDA v16.15.3 **没有** `/wda/keyboard/dismiss`（实测 unknown command），
        wda 库的 `keyboard_dismiss()` 也被作者 `raise` 禁用了。真机实测唯一可靠的
        做法是**给当前输入框发送回车**：单行 TextField 会结束编辑 ⇒ 键盘收起
        （form_name 的 y 从 116 回到 249，页面恢复原位）。
        多行 TextEditor 会插入换行，所以只在"输入后要点击别处"时才调用（见 Runner）。
        """
        try:
            self._c.send_keys("\n")
            return True
        except Exception:  # noqa: BLE001 —— 收不起来也不该影响执行
            return False

    # -- 定位 -------------------------------------------------------------------

    def find(self, by: str, value: str, timeout: float, index: int | None = None) -> Any:
        pred = _PREDICATES.get(by)
        if pred is None:
            raise LocatorNotSupported(f"iOS 驱动不支持定位属性 {by!r}（支持：{sorted(_PREDICATES)}）")
        # wda 的 `Selector` 原生支持 `index`（0 基，`find_elements()[index]`），
        # 与 Android 侧 `instance` 同义 ⇒ 直接透传，无需另造包装。
        sel = self._bind_session()(predicate=pred(value), index=index or 0)
        if timeout > 0:
            sel.wait(timeout=timeout, raise_error=False)
        return WdaNode(sel, self)

    def find_xpath(self, expr: str, timeout: float) -> Any:
        raise LocatorNotSupported("iOS 驱动不支持 XPath 定位策略（WDA 无 XPath 查询，class chain 映射后置）")
