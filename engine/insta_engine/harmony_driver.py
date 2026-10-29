"""HarmonyOS NEXT 驱动(`--platform harmony`)——第三驱动实现。

## 设计核心

**dump() 产出 uiautomator 兼容 XML**:鸿蒙 `uitest dumpLayout` 原生输出 JSON,这里在
驱动内转换成 MAT 全链消费的 `<node …/>` 形状(`ui_tree.parse_nodes` 正则同款)——
于是 parse_nodes / 页面图谱 / 报告 / explore / gen **零改动**。

## 待真机校准点(spike 阶段记录)

命令与字段按华为公开文档实现;以下三处以宽容解析先行,真机跑一遍后按实况收敛:
1. dumpLayout JSON 的属性名(`id`/`key`、`bounds` 的两种格式);
2. 前台判定的 `aa dump` 输出解析;
3. 中文 `inputText` 可靠性(失败走 `fallback_input` 降级,与 Android 读回校验同哲学)。

## 明确的降级(与 iOS 同先例)

- `start_evidence` 不做:鸿蒙日志(`hilog`)流式采集留待后续,`evidence_tail` 返回空
  ⇒ runner 暴露 warning(「没有证据源」≠「没有日志」),不阻塞主链路;
- `find_xpath` 首版不支持:清晰报错指向 text/id(gen 模板本就 text 优先)。
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from . import harmony_hdc as hdc
from .driver_base import BaseDriver

#: dumpLayout 在设备上的落盘路径(固定,反复覆盖)
_LAYOUT_REMOTE = hdc.DEVICE_TMP + "mat_layout.json"

#: uitest dumpLayout 属性的别名表(不同版本字段名有差;真机 spike 后收敛)
_ATTR_ALIASES = {"text": ("text",), "id": ("id", "key"), "type": ("type", "className"),
                 "bounds": ("bounds", "rect"),
                 "content-desc": ("content-desc", "description")}
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_BOUNDS_ALT_RE = re.compile(r"(-?\d+)\s*,\s*(-?\d+)\s*[-~]\s*(-?\d+)\s*,\s*(-?\d+)")


def parse_bounds(raw: str) -> str:
    """把鸿蒙 bounds 的各种写法归一成 uiautomator 形状 ``[x1,y1][x2,y2]``。

    兼容三种已见写法:「[x1,y1][x2,y2]」「x1,y1-x2,y2」「x1,y1~x2,y2」;解析失败
    返回空串(节点按无 bounds 处理,不炸整棵树)。
    """
    if not raw:
        return ""
    m = _BOUNDS_RE.search(raw) or _BOUNDS_ALT_RE.search(raw)
    if not m:
        return ""
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    if x2 < x1 or y2 < y1:  # 写法是「中心点-尺寸」时不是;保守按非法处理
        return ""
    return f"[{x1},{y1}][{x2},{y2}]"


def _pick(attrs: dict, canonical: str) -> str:
    """按别名表取属性(宽容解析:缺字段当空串,不让单节点拖垮整树)。"""
    for name in _ATTR_ALIASES[canonical]:
        v = attrs.get(name)
        if v:
            return str(v)
    return ""


def _layout_to_xml(node: dict, bundle: str = "") -> str:
    """dumpLayout 的 JSON 节点 → uiautomator 风格 <node>。递归含 children。"""
    attrs = node.get("attributes") or {}
    bounds = parse_bounds(_pick(attrs, "bounds"))
    fields = {
        "class": _pick(attrs, "type") or "Node",
        "text": _pick(attrs, "text"),
        "content-desc": _pick(attrs, "content-desc"),
        "resource-id": _pick(attrs, "id"),
        "clickable": "true" if str(attrs.get("clickable", "")).lower() == "true" else "false",
        "package": str(attrs.get("bundleName", "")) or bundle,
        "bounds": bounds,
    }
    head = " ".join(f'{k}="{_xml_escape(str(v))}"' for k, v in fields.items())
    children = node.get("children") or []
    if children:
        inner = "".join(_layout_to_xml(c, bundle) for c in children)
        return f"<node {head}>{inner}</node>"
    return f"<node {head}/>"


class HarmonyNode:
    """find() 的返回包装——鸭子兼容 Runner 消费的 u2 节点面(exists/wait/center/click…)。"""

    def __init__(self, driver: HarmonyDriver, node):
        # node 是 ui_tree.Node(parse_nodes 产物)或 {"bounds": …} 形状的缺席构造
        self._d = driver
        self.node = node
        self.bounds = node["bounds"] if isinstance(node, dict) else node.bounds
        self._text = "" if isinstance(node, dict) else node.text
        self._rid = "" if isinstance(node, dict) else node.rid
        self.exists = bool(self.bounds)

    def center(self) -> tuple[int, int]:
        m = _BOUNDS_RE.match(self.bounds)
        if not m:
            raise ValueError(f"节点无有效 bounds: {self.bounds!r}")
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    def wait(self, timeout: float = 0, raise_error: bool = False) -> bool:
        """兼容 u2 的 wait 签名:本节点的存在性已由 find 轮询保证,恒 True。"""
        return True

    def get_text(self) -> str:
        """TEXT_EQUALS/CONTAINS 断言消费(locator.Element 转发到这里)。"""
        return self._text

    def get_rid(self) -> str:
        return self._rid

    def set_text(self, text: str) -> None:
        """INPUT 动作(Runner 调 element.set_text):先点聚焦,再**带坐标** inputText
        ——真机校准:焦点版不上屏,坐标版实测走通登录流程。"""
        x, y = self.center()
        self._d.click(x, y)
        self._d._input_text_at(x, y, text)
        self._d._last_xy = (x, y)

    def clear_text(self) -> None:
        """CLEAR 动作:inputText 空串(替换语义)——真机未单独验证,失败会如实
        走 locate/env 分类而非静默。"""
        self.set_text("")

    def click(self) -> None:
        x, y = self.center()
        self._d.click(x, y)

    def long_click(self) -> None:
        x, y = self.center()
        self._d.long_click(x, y, 1.0)


class HarmonyDriver(BaseDriver):
    """鸿蒙 NEXT 驱动。serial = `hdc list targets` 的 target key(单设备可空)。"""

    platform = "harmony"

    def __init__(self, serial: str | None = None):
        if not hdc.hdc_available():
            raise RuntimeError(
                "hdc 不可用——鸿蒙链路需要华为 HDC(随 DevEco Studio/命令行工具分发;"
                "已支持 DevEco 标准安装位置自动发现,非标准安装可设 MAT_HDC 指向 hdc)。"
                "只测 Android 的话忽略此提示。")
        if not serial:
            targets = hdc.list_targets()
            if not targets:
                raise RuntimeError("没有连接的鸿蒙设备/模拟器(hdc list targets 为空)")
            if len(targets) > 1:
                raise RuntimeError(f"多台鸿蒙设备在线 {targets}——请用 --serial 指定")
            serial = targets[0]
        self.serial = serial
        self._size_cache: tuple[int, int] | None = None
        self._last_xy: tuple[int, int] | None = None   # 最近点击位置(inputText 焦点)

    # -- 树与截图 ---------------------------------------------------------------

    def dump(self) -> str:
        """控件树:uitest dumpLayout(JSON)→ 转成 uiautomator 风格 XML。

        两步走(设备落盘 → file recv)是 uitest 的标准用法;文件固定路径反复覆盖。
        """
        hdc.shell(f"uitest dumpLayout -p {_LAYOUT_REMOTE}", serial=self.serial, timeout=30)
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "layout.json"
            hdc.recv(_LAYOUT_REMOTE, str(local), serial=self.serial)
            data = json.loads(local.read_text(encoding="utf-8"))
        # 归属传播:dumpLayout 输出的是**当前前台窗口**的树,根节点带 bundleName——
        # 传给整树,parse_nodes 的 in_package 过滤才能正确区分被测/其他 App 的控件
        # (离场后 dump 到别的 App 树时,explore 不会在人家里面乱点)。真机校准 2026-10-27。
        bundle = str((data.get("attributes") or {}).get("bundleName") or "")
        return _layout_to_xml(data, bundle)

    def screenshot(self, path: str) -> None:
        with tempfile.TemporaryDirectory() as td:
            remote = hdc.DEVICE_TMP + "mat_shot.jpeg"
            local = Path(td) / "shot.jpeg"
            hdc.shell(f"snapshot_display -f {remote}", serial=self.serial, timeout=30)
            hdc.recv(remote, str(local), serial=self.serial)
            # snapshot 出 jpeg;统一转 png(报告侧按 .png 引用,避免内容/后缀不一致)
            try:
                from PIL import Image  # uiautomator2 依赖已带 pillow
                Image.open(local).save(path)
            except ImportError:
                Path(path).write_bytes(local.read_bytes())  # 无 pillow:原样落盘(jpeg 内容)

    def screenshot_pil(self):
        from PIL import Image  # uiautomator2 依赖已带 pillow

        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "shot.png"
            self.screenshot(str(local))
            return Image.open(local)

    # -- 交互 -------------------------------------------------------------------

    def click(self, x: int, y: int) -> None:
        hdc.shell(f"uitest uiInput click {x} {y}", serial=self.serial)
        self._last_xy = (x, y)   # send_keys/fallback_input 需要焦点坐标

    def long_click(self, x: int, y: int, duration: float) -> None:
        # uitest 的 longClick 语义为按住;毫秒时长参数在不同版本有差,待真机校准
        hdc.shell(f"uitest uiInput longClick {x} {y}", serial=self.serial)
        self._last_xy = (x, y)

    def send_keys(self, text: str) -> None:
        """真机校准(模拟器 API24,2026-10-27):inputText **必须带坐标**(焦点版
        不生效——实测登录流程无坐标版字段不上屏);Runner 先 click 再 send_keys
        ⇒ 用「上次点击位置」。没有焦点坐标时明确失败(fallback_input 接手)。"""
        if not getattr(self, "_last_xy", None):
            raise RuntimeError("send_keys 需要先点击输入框获得焦点(或用带坐标的 set_text)")
        self._input_text_at(*self._last_xy, text)

    def _input_text_at(self, x: int, y: int, text: str) -> None:
        hdc.shell(f'uitest uiInput inputText {x} {y} "{text}"', serial=self.serial,
                  timeout=30)

    def fallback_input(self, text: str) -> bool:
        """Android 同款降级语义。鸿蒙侧同一 uitest 通道(带坐标 inputText 已实测
        走通登录流程);ASCII 已验证,中文上屏依输入法实况——失败返回 False。"""
        if getattr(self, "_last_xy", None):
            self._input_text_at(*self._last_xy, text)
            return True
        return False

    def swipe_ext(self, direction: str) -> None:
        w, h = self.window_size()
        cx, cy = w // 2, h // 2
        d = {"up": (cx, cy, cx, int(cy * 0.2)), "down": (cx, int(cy * 0.2), cx, cy),
             "left": (int(w * 0.8), cy, int(w * 0.2), cy),
             "right": (int(w * 0.2), cy, int(w * 0.8), cy)}
        if direction not in d:
            raise ValueError(f"未知滑动方向: {direction}")
        x1, y1, x2, y2 = d[direction]
        hdc.shell(f"uitest uiInput swipe {x1} {y1} {x2} {y2} 500", serial=self.serial)

    def press(self, key: str) -> None:
        keymap = {"back": "Back", "home": "Home"}
        if key not in keymap:
            raise ValueError(f"鸿蒙首版仅支持 back/home 按键,收到: {key}")
        hdc.shell(f"uitest uiInput keyEvent {keymap[key]}", serial=self.serial)

    def back(self) -> None:
        self.press("back")

    def home(self) -> None:
        self.press("home")

    def open_recents(self) -> None:
        raise NotImplementedError("鸿蒙首版未实现 recents 手势")

    def open_settings(self) -> None:
        from .dsl import TestCase  # noqa: F401 —— 占位:设置应用包名 S5 校准后接 aa start
        raise NotImplementedError("鸿蒙首版未实现直达设置(用 aa start 待校准)")

    def wake_and_unlock(self) -> None:
        try:
            hdc.shell("power-shell wakeup", serial=self.serial, timeout=10)
        except RuntimeError:
            pass  # 常亮态唤醒失败不阻塞(对齐 Android 的 best-effort)
        # 无密码锁:send Wit/划屏解锁留待真机校准;有密码锁 WDA 同款不可代解

    # -- App 生命周期 -----------------------------------------------------------

    def app_start(self, package: str, activity: str | None = None) -> None:
        """启动应用。**真机校准结论**(模拟器 API24 实测,两轮):

        ① `aa start -b` 隐式启动必失败(10103101),必须带 `-a <ability>`;
        ② ability 名**以 `bm dump -n` 元数据里的 name 原样为准**——不同 App 形态不同
        (系统设置=全名 `com.huawei.hmos.settings.MainAbility`;新建工程=短名
        `EntryAbility`),**拼接前缀会报 10104001**。调用方给的值与元数据匹配时原样用。"""
        ability = self._resolve_ability(package, activity)
        hdc.shell(f"aa start -b {package} -a {ability}", serial=self.serial, timeout=30)
        self._wait_foreground(package)

    def _bm_abilities(self, package: str) -> list[str]:
        """`bm dump -n` 元数据里的 ability 名单(name 含 Ability 的条目,原样形态)。"""
        try:
            info = hdc.shell(f"bm dump -n {package}", serial=self.serial, timeout=20)
        except RuntimeError:
            return []
        return [a for a in re.findall(r'"name"\s*:\s*"([\w.]+)"', info)
                if "Ability" in a]

    def _resolve_ability(self, package: str, activity: str | None) -> str:
        """解析可启动的 ability 名:**元数据形态原样**(短名/全名不做拼接——拼接在
        单模块工程报 10104001,真机实测)。MainAbility 优先;调用方给的值若与元数据
        匹配(全名相等或以「.短名」结尾)则原样尊重。结果按 bundle 缓存。"""
        cached = getattr(self, "_ability_cache", {}).get(package)
        if cached and not activity:
            return cached
        meta = self._bm_abilities(package)
        if activity:
            if not meta or activity in meta:
                return activity
            suffix_matches = [a for a in meta if a.endswith(f".{activity}")]
            if suffix_matches:
                return suffix_matches[0]
            return activity   # 元数据查不到:原样交给 aa start,让它的报错说话
        pick = next((a for a in meta if a.endswith("MainAbility")),
                    meta[0] if meta else "EntryAbility")
        self._ability_cache = {**getattr(self, "_ability_cache", {}), package: pick}
        return pick

    def app_stop(self, package: str) -> None:
        hdc.shell(f"aa force-stop {package}", serial=self.serial, timeout=20)

    def app_fingerprint(self, package: str) -> str:
        """靶场指纹(T76,鸿蒙版):`bm dump -n` 元数据里的 **appIdentifier** 哈希。

        appIdentifier 是华为分配的应用唯一标识(所有变体/版本恒定)——用它哈希
        与 Android 用 apk 摘要同语义:分辨不出具体版本号,但能分辨「测的是哪个 App」。
        取不到返回空串(eval 的 compare note 会提示口径差异)。"""
        try:
            info = hdc.shell(f"bm dump -n {package}", serial=self.serial, timeout=20)
        except RuntimeError:
            return ""
        m = re.search(r'"appIdentifier"\s*:\s*"(\w+)"', info)
        return m.group(1)[:16] if m else ""

    # -- 保活(对齐 WdaDriver 的线程模式;手段用 power-shell wakeup——已亮时无害) --

    def start_keepalive(self, interval: float = 25.0) -> None:
        if getattr(self, "_keepalive", None) is not None:
            return
        stop = threading.Event()

        def _beat() -> None:
            while not stop.wait(interval):
                try:
                    hdc.shell("power-shell wakeup", serial=self.serial, timeout=10)
                except Exception:  # noqa: BLE001 —— 单拍失败不退线程(下拍继续)
                    continue

        self._keepalive = stop
        threading.Thread(target=_beat, daemon=True).start()

    def stop_keepalive(self) -> None:
        stop = getattr(self, "_keepalive", None)
        if stop is not None:
            stop.set()
            self._keepalive = None

    def foreground_app(self) -> dict:
        """前台应用。**真机校准**(模拟器 API24 实测):`aa dump -l` 的 mission 列表,
        取第一个 `state #FOREGROUND` 的记录。解析失败返回 {}(上层离场检测按
        「取不到」保守处理,与 Android 同口径)。"""
        try:
            out = hdc.shell("aa dump -l", serial=self.serial, timeout=20)
        except RuntimeError:
            return {}
        # 记录块按 AbilityRecord 切,取第一个 FOREGROUND 的 bundle/ability
        for block in re.split(r"AbilityRecord ID", out):
            if "state #FOREGROUND" not in block:
                continue
            bm = re.search(r"bundle name \[([^\]]+)\]", block)
            if not bm:
                continue
            main = re.search(r"main name \[([^\]]+)\]", block)
            return {"package": bm.group(1),
                    "activity": main.group(1) if main else ""}
        return {}

    def _wait_foreground(self, package: str, tries: int = 10) -> None:
        """launch 后等前台到目标应用(对齐 Android 的导航守卫;取不到不判死)。"""
        for _ in range(tries):
            fg = self.foreground_app()
            if fg.get("package") == package:
                return
            time.sleep(1)

    def device_meta(self) -> dict:
        out = {}
        for key, param in (("model", "const.product.model"),
                           ("os_version", "const.ohos.apiversion")):
            try:
                out[key] = hdc.shell(f"param get {param}", serial=self.serial,
                                     timeout=10).strip()
            except RuntimeError:
                pass
        return out

    # -- 定位 -------------------------------------------------------------------

    def find(self, by: str, value: str, timeout: float, index: int | None = None) -> HarmonyNode:
        """轮询 dump 直到命中。策略映射:text→text、res-id→resource-id(id/key);
        index 同 Android 语义(0 基,同文案第 N 个)。"""
        deadline = time.monotonic() + timeout
        while True:
            nodes = self._nodes()
            hits = [n for n in nodes if self._match(n, by, value)]
            if index is not None:
                hits = hits[index:index + 1]
            if hits:
                return HarmonyNode(self, hits[0])
            if time.monotonic() >= deadline:
                return self._absent()
            time.sleep(0.5)

    def _nodes(self):
        from .ui_tree import parse_nodes

        return parse_nodes(self.dump())

    @staticmethod
    def _match(n, by: str, value: str) -> bool:
        """策略名对齐 locator 层的 `_find` 传参(RES_ID/ID 都映射为 **resourceId**,
        TEXT=text;真机校准:此前只认 res-id 导致断言静默未命中)。"""
        if by in ("text", "textContains"):
            hay = (n.text, n.desc, n.label)
            return value in hay if by == "text" else any(value in h for h in hay if h)
        if by in ("res-id", "resourceId", "id"):
            return n.rid == value or n.rid.endswith(f".{value}") or value in n.rid
        if by == "description":
            return value == n.desc or value in n.desc
        if by == "xpath":
            raise ValueError("鸿蒙首版不支持 xpath 定位——请用 text/res-id(gen 模板 text 优先)")
        return False

    def _absent(self) -> HarmonyNode:
        """未命中节点:exists=False(center 会抛)——对齐 _KeepaliveDriver 桩语义。"""
        node = HarmonyNode(self, {"bounds": ""})
        node.exists = False
        return node

    def find_xpath(self, expr: str, timeout: float) -> HarmonyNode:
        raise ValueError("鸿蒙首版不支持 xpath 定位——请用 text/res-id(gen 模板 text 优先)")

    # -- 窗口/证据/保活 ---------------------------------------------------------

    def window_size(self) -> tuple[int, int]:
        if self._size_cache is None:
            img = self.screenshot_pil()
            self._size_cache = img.size
        return self._size_cache

    def start_evidence(self) -> None:
        """鸿蒙首版不采 hilog(降级同 iOS 未装 pymobiledevice3):evidence_tail 返回空。"""

    def evidence_tail(self) -> str:
        return ""
