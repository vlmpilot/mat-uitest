"""定位策略链：按 DSL fallback 链逐级降级（成本递增）。

spike 实现 res-id / text / description / textContains / xpath / point / image-template；
**vision-llm 不在本链内**——它由 runner 的自愈兜底触发（按次计费、默认关闭），在本模块显式拒绝，
避免写了却不生效的静默空操作。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from .driver_base import BaseDriver
from .dsl import VISION_LLM_MISUSE_HINT, Locator, LocatorStrategy


class LocatorNotSupported(RuntimeError):
    """策略未实现或属闭源边界。"""


class LocateFailed(RuntimeError):
    """策略链全部未命中。"""


@dataclass
class Element:
    """统一元素抽象：屏蔽 uiautomator2 选择器与坐标点两种形态。"""

    strategy: str
    value: str
    node: Any = None  # uiautomator2 UiObject / XPathSelector
    point: tuple[int, int] | None = None
    drv: BaseDriver | None = None
    #: 命中所用的「第 N 个匹配」（`None` = 未指定）。只用于**证据**：失败/成功记录里
    #: 能区分「第 0 个卡片没找到」与「文案没找到」，否则位置型定位的失败现场无从复核。
    index: int | None = None
    #: 动作的**可复核结论**（如「清空后回读仍为 X」）。`None` = 无可报。
    #: 由 runner 在动作后拼进 `result.warning` —— 不拼就等于没测（与 C17 同类：
    #: 采到了但没落盘，等于没采）。
    note: str | None = None

    @property
    def ok(self) -> bool:
        if self.point is not None:
            return True
        if self.node is None:
            return False
        if self.index is not None:
            # ⚠️ 位置定位**不能用 `.exists` 判定**：u2 的 `exist` RPC 不看 `instance`
            # （2026-10-25 真机实测：`c_list_view_container` 只有 6 个，`instance=9`
            # 的 `exist` 仍返回 True，而 `objInfo` 如实抛 UiObjectNotFoundError）。
            # 用 `.exists` 会让"第 9 个卡片"这种必然失败的定位记成命中，然后在
            # `click()` 里抛一个与定位无关的异常 ⇒ 失败归因全错。
            # `info` 对两种驱动都是"取到了才有"的语义（iOS 的 WdaNode.info 走 `sel.get()`）。
            try:
                return bool(self.node.info)
            except Exception:  # noqa: BLE001 —— 越界/未找到 = 未命中
                return False
        try:
            return bool(self.node.exists)
        except Exception:  # noqa: BLE001 —— u2 的 `exists` 在部分版本**会抛「未找到」**
            # （2026-10-25 真机实测：`Selector [text='账号']` 的 `.exists` 直接抛
            # UiObjectNotFoundError ⇒ 整个失败步被记成 unknown 而不是 locate-fail）
            return False

    def click(self) -> None:
        if self.point is not None:
            self.drv.click(*self.point)
        else:
            self.node.click()

    def long_click(self) -> None:
        """长按。**统一交驱动发**，不用 `node.long_click()`（Android 侧教训）。

        为什么（T65，2026-10-06 实测）：`node.long_click()` 走的是 u2 的默认 0.5s，
        正好等于 Android 的 500ms 长按阈值 ⇒ 被判成**普通点击**；而 XPath 分支
        `XPathSelector.long_click()` **连 duration 参数都没有**（注释写 400ms，更低）。
        ⇒ 节点路径经驱动的 `long_click_node` 分派：Android=取中心坐标长按
        （LONG_PRESS_SECONDS=2.0），iOS=元素级 tap_hold（坐标级不稳定，U8）。
        """
        if self.point is not None:
            self.drv.long_click(*self.point)
        elif self.drv is not None:
            self.drv.long_click_node(self.node)
        else:  # 兜底：没有驱动时退回 u2（老行为，可能被判成点击）
            self.node.long_click()

    def set_text(self, text: str) -> None:
        if self.point is not None:
            # vision-llm 兜底命中的输入框：点击聚焦 + 向焦点输入（支持 CJK）
            self.drv.click(*self.point)
            self.drv.send_keys(text)
            return
        before, before_known = self._read_text()
        self.node.click()
        self.node.set_text(text)
        if self._input_landed(text):
            return
        # 厂商安全键盘会拦截无障碍文本注入（华为实测：set_text 静默失败、字段全空、
        # 后续登录必报错）。回读校验未通过则走驱动的**二级输入通道**
        # `fallback_input`：Android=华为回退（adb input text，只对 ASCII 可靠，CJK 无效
        # 但失败会如实暴露）；iOS 无二级通道 ⇒ 如实报「输入未生效」（评审 I1）。
        self.node.click()
        time.sleep(0.3)
        if self.drv is None or not self.drv.fallback_input(text):
            platform = self.drv.platform if self.drv is not None else "unknown"
            raise RuntimeError(
                f"输入未生效（回读为空，策略 {self.strategy}）：{platform} 无二级输入通道")
        # 二级通道之后**必须再验一次**：`fallback_input` 返回 True 只说明命令发出去了
        # （`adb input text` 的退出码），不说明字符真进了输入框。
        if self._input_landed(text):
            return
        self._judge_unlanded_input(text, before, before_known)

    def _judge_unlanded_input(self, text: str, before: str | None, before_known: bool) -> None:
        """两级通道后仍不含目标文本：区分「确定没生效」与「无法判定」。

        **只有"回读与动作前逐字相同"才判死**。这条判据是唯一无歧义的：字段一点没变，
        说明动作确实没落到它身上。其余情形（回读变了但不含目标）可能只是**格式转换**
        （手机号「138 0013 8000」、金额「1,000」、日期）⇒ 记 `note` 交给人，不制造假红。
        """
        readback, known = self._read_text()
        if not known or not readback or not readback.strip():
            return  # 读不到/读成空：与旧行为一致，不判死（厂商键盘下"可能成功"）
        if self._is_password():
            return  # 密码框回读是掩码或空，判不了
        if before_known and readback == before:
            raise RuntimeError(
                f"输入未生效（两级通道后回读仍为动作前的 {before!r}，不含 {text!r}，"
                f"策略 {self.strategy}）")
        self.note = (f"输入后回读为 {readback!r}，不含目标 {text!r}——可能是格式转换或掩码，"
                     f"请人工确认这一步真的输入了")

    def _is_password(self) -> bool:
        info = getattr(self.node, "info", None)
        return isinstance(info, dict) and bool(info.get("password"))

    def _read_text(self) -> tuple[str | None, bool]:
        """回读文本：`(text, known)`。**`known=False` 表示读不到**，不是空。

        ⚠️ 读不到不能当"空"：定位命中 **hint** 后输入新值，提示文案消失 ⇒ 同一选择器
        不再匹配，u2 的 `get_text()` 直接抛 `UiObjectNotFoundError`。这是**正常时序**
        （输入本来就会改掉 hint），当成"空"会让 `set_text` 再走一遍二级通道，
        文本被打两遍（InstaDemo 登录用例实测）。
        """
        try:
            return self.get_text(), True
        except Exception:  # noqa: BLE001 —— 节点已失效，视作"未知"
            return None, False

    def _input_landed(self, text: str) -> bool:
        """输入是否真进了框：回读**包含**目标文本才算。

        ⚠️ 这里原来判的是「回读是否为空」——**判据本身是错的**。内容型 App 的搜索框
        把占位/前缀写在 `text` 里（xhs「搜索, 」、tx「网友捕捉绝美"火箭云"！」），
        回读**永远非空** ⇒ 判据恒成立，"输入没进去"与"输入成功"长得一模一样。
        2026-10-27 真机复现（`/tmp/probe_clear.py`）：框里是「搜索, 卤牛肉腱子的做法」，
        `set_text("咖啡")` 后回读仍是「搜索, 卤牛肉腱子的做法」，步骤照样 passed，
        断言再断在结果页排序栏 ⇒ 假绿（实测两条假绿的共同根因）。

        判「包含」而不是「相等」：xhs 的回读是「搜索, 咖啡」，前缀是 App 自绘的、
        不是被污染的查询词（实测提交后的查询词就是「咖啡」）。比较前做归一化
        （去分隔符、统一大小写），覆盖手机号/金额这类**自动格式化**字段。
        """
        if not text:
            return True
        readback, known = self._read_text()
        if not known:
            return True  # 读不到不判死，见 `_read_text`
        if self._is_password():
            return True
        return bool(readback) and _norm(text) in _norm(readback)

    def clear_text(self) -> None:
        """清空输入框：`clear_text()` → `set_text("")` → 按键通道，都无效则记 `note`。

        为什么非要能清空：内容型 App 的搜索框把**占位/热词写在 text 里**
        （tx「网友捕捉绝美"火箭云"！」、xhs「搜索, 」），`set_text` 会**拼在后面**
        ⇒ 输入「世界杯」实际得到「火箭世界杯」，搜出来的结果与意图无关，
        而用例侧完全看不出来（引擎的回读校验也认为"有内容"）。

        **三级兜底都是真机逼出来的**（2026-10-25/26/27 实测）：

        1. u2 的 **`XPathSelector` 根本没有 `clear_text`** —— 直接
           `AttributeError: ('Invalid attr', 'clear_text')`；
        2. u2 的 **`UiObject.clear_text` 存在，但内部走 ADB 广播**
           （`am broadcast -a ADB_KEYBOARD_CLEAR_TEXT`）—— 输入框不是
           `ExtractEditText` 时抛 `AdbBroadcastError`。**「有这个方法」不等于「调得通」**，
           所以调用本身也要包 try（实测一个输入框不是 `ExtractEditText` 的用例就
           栽在这：广播抛错、引擎虽标了 `false_red`，步骤照样是红的）；
        3. **两级都"成功"、文本一点没变**（xhs 实测：`clearTextField` RPC 不抛异常，
           文本原样）⇒ 走按键通道（`MOVE_END` + N×`DEL`，与无障碍动作是两条路）。

        ⚠️ **不抛异常**：判「回读是否为空」是错的（xhs 占位「搜索, 」不是可编辑内容，
        清不掉），判「是否变化」对「本来就没什么可清」的框也分不出来。三级都不变时
        把结论记进 `note`（占位由 App 自绘，不是引擎能改的），由 runner 落到
        `result.warning` —— 让复核的人知道"这一步的清空不可信"。
        """
        if self.node is None:
            raise RuntimeError("坐标定位不支持清空输入")
        before, before_known = self._read_text()
        clear = getattr(self.node, "clear_text", None)
        if callable(clear):
            try:
                clear()
            except Exception:  # noqa: BLE001 —— ADB 广播失败/属性不支持，降级覆写空串
                pass
            else:
                if self._clear_landed(before, before_known):
                    return
        self.node.set_text("")
        if self._clear_landed(before, before_known):
            return
        # 两级都"成功"却一点没变：再试按键通道（确定性，一次 shell 下发）
        size = len(before or "")
        escalator = getattr(self.drv, "clear_by_keyevents", None) if self.drv is not None else None
        if size and callable(escalator) and escalator(size):
            after, after_known = self._read_text()
            if after_known and after != before:
                return
        if before_known and (before or "").strip():
            self.note = (f"清空后回读仍为 {before!r}（三级手段都试过）——该输入框的占位/前缀"
                         f"由 App 自绘，不是可编辑内容；若后续输入被拼接，请改断结果态")

    def _clear_landed(self, before: str | None, before_known: bool) -> bool:
        """清空是否生效：回读与清空前**不同**才算。读不到一律放过（不判死）。"""
        if not before_known:
            return True
        after, after_known = self._read_text()
        if not after_known:
            return True
        return after != before

    def get_text(self) -> str | None:
        if self.node is None:
            return None
        if hasattr(self.node, "get_text"):
            return self.node.get_text()
        return getattr(self.node, "text", None)

    # ⚠️ `get_text()` 会抛，**调用方必须经 `_read_text()`**（2026-10-25 真机评测）：
    # 定位命中 **hint** 后输入新值，提示文案消失 ⇒ 同一选择器不再匹配，u2 的
    # `get_text()` 直接抛 UiObjectNotFoundError（method=wait）。这是**正常时序**
    # （输入本来就会改掉 hint），不是执行失败；不吞就会把成功输入记成 `unknown`
    # 失败（InstaDemo 登录用例实测）。注意这里**不能**把 None 一并当"未知"：
    # iOS 的空输入框 `get_text()` 正常返回 None（WdaNode 的 value/text 都为空），
    # 那恰恰是"回读为空"的证据，需继续走二级通道。

    def get_state(self, state: str) -> bool:
        """读控件状态（enabled/checked/selected，u2 info 同名键）。

        坐标定位（无控件树）与不暴露 info 的定位形态不支持——如实报错，
        由断言层转成 assert-fail，而不是静默当 True/False。
        """
        if self.node is None:
            raise RuntimeError(f"坐标定位不支持状态断言（{self.strategy}）")
        info = getattr(self.node, "info", None)
        if not isinstance(info, dict) or state not in info:
            raise RuntimeError(f"定位形态 {self.strategy} 不支持状态断言：节点无 {state} 状态")
        return bool(info[state])


# uiautomator2 的选择器统一走 Device.__call__（d(text="xx") / d(resourceId="xx")），
# 不能 getattr 取属性——Device 上并无 .text/.resourceId 方法（2026-09-27 真机首测发现）
_SELECTOR_KWARGS = {
    LocatorStrategy.RES_ID: "resourceId",
    LocatorStrategy.ID: "resourceId",  # 平台中立别名：iOS 驱动把 id 映射为 accessibility identifier
    LocatorStrategy.TEXT: "text",
    LocatorStrategy.TEXT_CONTAINS: "textContains",
    LocatorStrategy.DESCRIPTION: "description",
}


def _label(loc: Locator) -> str:
    """定位的**可读标识**（报错/证据用）：带 index 时必须显示，否则两种失败长得一样。"""
    return (f"{loc.strategy.value}={loc.value}" if loc.index is None
            else f"{loc.strategy.value}={loc.value}#{loc.index}")


_NON_WORD = re.compile(r"[\W_]+")


def _norm(text: str) -> str:
    """回读比较用的归一化：去分隔符（空格/逗号/连字符）、统一大小写。

    为什么需要：输入框会**自动格式化**，`13800138000` 回读成「138 0013 8000」、
    `1000` 回读成「1,000」。按原样比较会把"输入成功"判成"未生效"——那是假红，
    比不校验更坏（会让人不再信这个校验）。
    """
    return _NON_WORD.sub("", text).lower()


def _find(drv: BaseDriver, by: str, value: str, timeout_s: float,
          index: int | None) -> Any:
    """调驱动 `find`，**只在 index 非 None 时**才传该参数。

    为什么这么绕：`index` 是后加的可选参数，仓里十多个测试桩/历史实现仍是
    三参签名（`find(self, by, value, timeout)`）。无条件传 `index=` 会让它们全部 TypeError
    ——而那些桩验证的是定位链/失败分类，与 index 无关，不该被一个新特性打挂。
    """
    if index is None:
        return drv.find(by, value, timeout_s)
    return drv.find(by, value, timeout_s, index=index)


def resolve(drv: BaseDriver, locator: Locator, timeout_ms: int) -> Element:
    """沿 fallback 链逐级尝试；全部失败抛 LocateFailed（附各级证据）。"""
    chain: list[Locator] = []
    node: Locator | None = locator
    while node is not None:
        chain.append(node)
        node = node.fallback

    errors: list[str] = []
    deadline = time.monotonic() + timeout_ms / 1000
    per_strategy = max(0.5, timeout_ms / 1000 / len(chain))

    for loc in chain:
        remaining = max(0.3, deadline - time.monotonic())
        try:
            element = _try(drv, loc, min(per_strategy, remaining))
            if element.ok:
                return element
            errors.append(f"{_label(loc)} 未命中")
        except LocatorNotSupported as exc:
            errors.append(str(exc))
        except Exception as exc:  # 单策略异常不阻断链
            errors.append(f"{_label(loc)}: {exc}")

    raise LocateFailed("定位失败（策略链）: " + " / ".join(errors))


def heal_by_hints(drv: BaseDriver, hints: list[str], timeout_ms: int = 1500) -> Element | None:
    """用「图谱反查到的历史文案」重定位（零成本）。

    走 `TEXT` 策略，因此自动获得语义等价链（精确 → description 精确 → 包含 → 描述包含），
    所以文案本身也微调过时仍有机会命中。
    """
    for hint in hints:
        try:
            return resolve(drv, Locator(strategy=LocatorStrategy.TEXT, value=hint), timeout_ms)
        except LocateFailed:
            continue
    return None


def try_resolve(drv: BaseDriver, locator: Locator, timeout_ms: int) -> Element | None:
    """同 `resolve`，但**找不到时返回 None 而不是抛 LocateFailed**。

    **断言层必须用这个。** 因为 `resolve` 只在命中时才返回，`element.ok` 恒为真；
    于是 `not-exists` 断言永远无法成立：元素在 → 断言失败；元素不在 → 抛 LocateFailed。
    而 `not-exists` 的**正常情况恰恰就是找不到**。
    （历史 bug：`not-exists` 是 DSL 公开承诺的断言类型，却从没有测试覆盖，一直是死的。）
    """
    try:
        return resolve(drv, locator, timeout_ms)
    except LocateFailed:
        return None


def _xpath_literal(value: str) -> str:
    """把任意字符串变成 XPath 1.0 字符串字面量（无转义机制，按引号拆分后 concat）。"""
    if '"' not in value and "'" not in value:
        return f'"{value}"'
    parts: list[str] = []
    buf = ""
    for ch in value:
        if ch in "\"'":
            if buf:
                parts.append(f'"{buf}"')
                buf = ""
            parts.append("'\"'" if ch == '"' else "\"'\"")
        else:
            buf += ch
    if buf:
        parts.append(f'"{buf}"')
    return "concat(" + ", ".join(parts) + ")"


def _exists(node: Any) -> bool:
    """安全读 `node.exists`：u2 部分版本的 `exists` 在未命中时会抛异常。"""
    try:
        return bool(node.exists)
    except Exception:  # noqa: BLE001 —— 未命中就是 False，不该把定位失败升级成 unknown
        return False


def _nth_xpath(expr: str, index: int | None) -> str:
    """把 XPath 表达式收成「第 N 个匹配」：`(expr)[N+1]`（XPath 1.0 是 1 基）。

    加括号是必须的：`//a/b[2]` 是"每个 a 的第二个 b"，`(//a/b)[2]` 才是"全屏第二个"。
    """
    return expr if index is None else f"({expr})[{index + 1}]"


def _try(drv: BaseDriver, loc: Locator, timeout_s: float) -> Element:
    if loc.strategy == LocatorStrategy.POINT:
        x, y = (int(v.strip()) for v in loc.value.split(",", 1))
        return Element(strategy=loc.strategy.value, value=loc.value, point=(x, y), drv=drv)

    if loc.strategy == LocatorStrategy.XPATH:
        selector = drv.find_xpath(_nth_xpath(loc.value, loc.index), timeout_s)
        return Element(strategy=loc.strategy.value, value=loc.value, node=selector, drv=drv,
                       index=loc.index)

    if loc.strategy == LocatorStrategy.IMAGE_TEMPLATE:
        return _match_template(drv, loc.value, timeout_s)

    # vision-llm **不能**在 DSL 定位链里声明：它由自愈兜底触发（runner._vision_locate），
    # 且按次计费、默认关闭。此前这里没有分支 → 抛通用 LocatorNotSupported 被 resolve 吞掉
    # → 用户写了它却既不生效也不报错（静默空操作）。现在显式报错并给出正确用法。
    if loc.strategy == LocatorStrategy.VISION_LLM:
        raise LocatorNotSupported(VISION_LLM_MISUSE_HINT)

    if loc.strategy == LocatorStrategy.TEXT:
        # text 语义等价链（真机首测教训：现代 App 的底部导航/图标类控件，
        # 文字常在 content-desc 而 text 为空，如某些 App 的"我的"标签）：
        # text 精确 → description 精确 → textContains → descriptionContains
        # `index` 逐级透传：位置语义在整条链上一致（第 N 个"含该文案"的控件），
        # 不会出现"精确匹配取第 2 个、包含匹配又退回第 1 个"的错位。
        budget = max(0.5, timeout_s / 5)
        for attr, mark in (("text", None), ("description", "description"),
                           ("textContains", "contains"), ("descriptionContains", "descContains")):
            node = _find(drv, attr, loc.value, budget, loc.index)
            if _exists(node):
                via = f"text→{mark}" if mark else "text"
                return Element(strategy=via, value=loc.value, node=node, drv=drv, index=loc.index)
        # hint 兜底：输入框**有默认值/预填值**时 text 不为空、提示文案只留在 hint 属性
        # （2026-10-25 真机评测：text="alice" / hint="账号" ⇒ 上面四条全未命中，
        # tx-news 搜索框同理）。UiSelector 没有 hint 属性 ⇒ 只能走 XPath；
        # 平台不支持 XPath/hint 时异常被吞，行为与旧版一致（最后一个 return 仍会记录未命中）。
        try:
            node = drv.find_xpath(
                _nth_xpath(f"//*[@hint={_xpath_literal(loc.value)}]", loc.index), budget)
            if _exists(node):
                return Element(strategy="text→hint", value=loc.value, node=node, drv=drv,
                               index=loc.index)
        except Exception:  # noqa: BLE001 —— 平台不支持 ⇒ 跳过，不影响既有策略链
            pass
        return Element(strategy="text", value=loc.value,
                       node=_find(drv, "text", loc.value, 0, loc.index), drv=drv,
                       index=loc.index)

    if loc.strategy in _SELECTOR_KWARGS:
        node = _find(drv, _SELECTOR_KWARGS[loc.strategy], loc.value, timeout_s, loc.index)
        return Element(strategy=loc.strategy.value, value=loc.value, node=node, drv=drv,
                       index=loc.index)

    raise LocatorNotSupported(f"当前未实现定位策略: {loc.strategy.value}（规划中）")


# -- 自愈 v0（子集） -------------------------------------------

def heal_tokens(locator: Locator) -> list[str]:
    """从原定位派生模糊匹配线索（纯函数，单测覆盖）。

    res-id / id「com.a.b:id/login_btn」→ 资源名末段及其变体；text/description → 原值（contains 语义）。
    `id` 是平台中立别名（`_SELECTOR_KWARGS`/`dsl.py` 均声明与 res-id 同义）——
    自愈线索派生必须同样认它，否则用 `id` 写的用例少了「短名派生」一级，模糊命中面变窄（评审 I3）。
    """
    value = locator.value.strip()
    tokens: list[str] = []
    if locator.strategy in (LocatorStrategy.RES_ID, LocatorStrategy.ID):
        tail = value.split(":")[-1].split("/")[-1]
        if tail and tail != value:
            tokens.append(tail)
            tokens.append(tail.replace("_", " ").replace("-", " "))
    tokens.append(value)

    seen: list[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.append(token)
    return seen


def heal_fuzzy(drv: BaseDriver, locator: Locator) -> Element:
    """自愈 v0：定位失败后用模糊线索本地重定位（不调云端、不自动回写用例）。

    命中返回 strategy=fuzzy:* 的元素，由 runner 记录为自愈提案（报告中待人工确认）。
    """
    tokens = heal_tokens(locator)
    for token in tokens:
        for key in ("textContains", "descriptionContains"):
            node = drv.find(key, token, 2)
            if _exists(node):
                return Element(strategy=f"fuzzy:{key}", value=token, node=node, drv=drv)
    raise LocateFailed(f"自愈模糊匹配未命中: {', '.join(tokens)}")


def _match_template(drv: BaseDriver, spec: str, timeout_s: float) -> Element:
    """image-template 定位：历史截图裁剪模板在当前屏幕上做模板匹配（策略链中的一级）。

    spec 格式：`模板路径` 或 `模板路径@阈值`（默认 0.8）。需要 OpenCV：
    pip install 'mat-uitest[image]'
    """
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise LocatorNotSupported(
            "image-template 需要 OpenCV：pip install 'mat-uitest[image]'（opencv-python-headless）"
        ) from exc

    path, _, threshold_text = spec.rpartition("@")
    threshold = float(threshold_text) if threshold_text else 0.8
    if not path:
        path, threshold = spec, 0.8
    template = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if template is None:
        raise LocateFailed(f"模板文件无法读取: {path}")

    deadline = time.monotonic() + timeout_s
    best_score = -1.0
    while True:
        frame = drv.screenshot_pil()  # PIL Image（RGB）
        screen = cv2.cvtColor(np.array(frame), cv2.COLOR_RGB2BGR)
        result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        best_score = max(best_score, max_val)
        if max_val >= threshold:
            height, width = template.shape[:2]
            center = (max_loc[0] + width // 2, max_loc[1] + height // 2)
            return Element(
                strategy="image-template", value=spec, point=center, drv=drv,
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    raise LocateFailed(f"模板匹配未命中: {path}（最高分 {best_score:.2f} < 阈值 {threshold}）")
