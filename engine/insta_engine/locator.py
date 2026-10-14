"""定位策略链：按 DSL fallback 链逐级降级（成本递增）。

spike 实现 res-id / text / description / textContains / xpath / point / image-template；
**vision-llm 不在本链内**——它由 runner 的自愈兜底触发（按次计费、默认关闭），在本模块显式拒绝，
避免写了却不生效的静默空操作。
"""

from __future__ import annotations

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

    @property
    def ok(self) -> bool:
        if self.point is not None:
            return True
        return bool(self.node is not None and self.node.exists)

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
        self.node.click()
        self.node.set_text(text)
        # 厂商安全键盘会拦截无障碍文本注入（华为实测：set_text 静默失败、字段全空、
        # 后续登录必报错）。回读校验，仍空则走驱动的**二级输入通道**
        # `fallback_input`：Android=华为回退（adb input text，只对 ASCII 可靠，CJK 无效
        # 但失败会如实暴露）；iOS 无二级通道 ⇒ 如实报「输入未生效」（评审 I1）。
        if self._read_back_empty():
            self.node.click()
            time.sleep(0.3)
            if self.drv is None or not self.drv.fallback_input(text):
                platform = self.drv.platform if self.drv is not None else "unknown"
                raise RuntimeError(
                    f"输入未生效（回读为空，策略 {self.strategy}）：{platform} 无二级输入通道")

    def _read_back_empty(self) -> bool:
        if (self.get_text() or "").strip():
            return False
        time.sleep(0.2)
        return not (self.get_text() or "").strip()

    def clear_text(self) -> None:
        if self.node is None:
            raise RuntimeError("坐标定位不支持清空输入")
        self.node.clear_text()

    def get_text(self) -> str | None:
        if self.node is None:
            return None
        if hasattr(self.node, "get_text"):
            return self.node.get_text()
        return getattr(self.node, "text", None)

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
            errors.append(f"{loc.strategy.value}={loc.value} 未命中")
        except LocatorNotSupported as exc:
            errors.append(str(exc))
        except Exception as exc:  # 单策略异常不阻断链
            errors.append(f"{loc.strategy.value}={loc.value}: {exc}")

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


def _try(drv: BaseDriver, loc: Locator, timeout_s: float) -> Element:
    if loc.strategy == LocatorStrategy.POINT:
        x, y = (int(v.strip()) for v in loc.value.split(",", 1))
        return Element(strategy=loc.strategy.value, value=loc.value, point=(x, y), drv=drv)

    if loc.strategy == LocatorStrategy.XPATH:
        selector = drv.find_xpath(loc.value, timeout_s)
        return Element(strategy=loc.strategy.value, value=loc.value, node=selector, drv=drv)

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
        budget = max(0.5, timeout_s / 4)
        for attr, mark in (("text", None), ("description", "description"),
                           ("textContains", "contains"), ("descriptionContains", "descContains")):
            node = drv.find(attr, loc.value, budget)
            if node.exists:
                via = f"text→{mark}" if mark else "text"
                return Element(strategy=via, value=loc.value, node=node, drv=drv)
        return Element(strategy="text", value=loc.value, node=drv.find("text", loc.value, 0), drv=drv)

    if loc.strategy in _SELECTOR_KWARGS:
        node = drv.find(_SELECTOR_KWARGS[loc.strategy], loc.value, timeout_s)
        return Element(strategy=loc.strategy.value, value=loc.value, node=node, drv=drv)

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
            if node.exists:
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
