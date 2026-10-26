"""Maestro flow YAML → MAT DSL（P1-3，2026-10-19）+ 反向导出。

**为什么是确定性映射，不走 AI**：Maestro 的命令是封闭集合，映射表固定——
AI 翻译只会引入不确定性还烧额度。导入零成本、可重复、warnings 可枚举
（实施方案 §3.7：不支持的命令必须明确列出，不静默丢弃）。

支持矩阵（对齐方案 §3.7.2 的映射表；判定是「能落成 MAT 步骤且语义不倒退」）：

| Maestro | MAT | 备注 |
|---|---|---|
| launchApp | launch | clearState:true → cold（冷启动语义） |
| tapOn | tap | 字符串=text；{id}=res-id；{text}=text；{point}=不支持（百分比↔像素） |
| longPressOn | long-press | 同 tapOn 的选择器 |
| inputText / insertText | input | **借上一条 tapOn 的定位器**（Maestro 是焦点语义，MAT 是控件语义） |
| eraseText | clear | 同上；无定位器时给出明确 warning |
| assertVisible / assertNotVisible | wait 步 + exists/not-exists 断言 | 复合选择器（id+text）按 id 映射并记 note |
| back | back | |
| pressKey | key-event | Back/Home/Enter/Tab/Delete；其余 warning |
| swipe | swipe | 仅方向形；坐标点对不支持 |
| scroll / scrollUntilVisible | scroll（+exists 断言） | 方案：近似映射，标注「需人工确认」 |
| waitForAnimationToEnd | wait | 近似（默认 3s），标注 |
| runFlow / runScript / evalScript / repeat 等 | — | **不支持**，进 warnings 清单 |

反向导出（MAT → Maestro）：逻辑与 `benchmarks/compare/gen_targets.py::to_maestro`
同源（基准对照的既有翻译）；提升进引擎包是因为 benchmarks/ 不随 wheel 分发——
单份实现放在这里，gen_targets 后续可改为引用本模块（先不动它，避免基准漂移）。

依赖：`pyyaml` 为**可选 extra** `mat-uitest[maestro]`；缺依赖时报人话
（`pip install 'mat-uitest[maestro]'`），不影响基础安装。
"""

from __future__ import annotations

import re
from pathlib import Path

from .dsl import Action, Assertion, AssertionType, Locator, LocatorStrategy, Step, TestCase

#: scrollUntilVisible / waitForAnimationToEnd 的近似映射提示前缀
_APPROX_TAG = "近似映射"

_KEYEVENT_MAP = {"back": "back", "home": "home", "enter": "enter", "tab": "tab",
                 "delete": "delete", "del": "delete"}
_SWIPE_MAP = {"up": "up", "down": "down", "left": "left", "right": "right"}
#: 子流程/脚本/重复/环境类命令：语义在 MAT 里没有对应物，明确列出而不是静默丢弃
_UNSUPPORTED_VERBS = {"runflow", "runscript", "evalscript", "repeat", "repeattimes",
                      "setlocation", "openlink", "openscheme", "takescreenshot",
                      "startscreenrecording", "stopscreenrecording", "addmedia",
                      "runscriptfile", "definevariables", "subroutine",
                      "copytextfrom", "pastetext"}


def _need_yaml():
    try:
        import yaml  # noqa: PLC0415 —— 可选依赖：进函数再 import
    except ImportError as exc:  # pragma: no cover —— venv 里装了 pyyaml，演练路径见测试
        raise ValueError(
            "需要 pyyaml 支持 Maestro 导入：pip install 'mat-uitest[maestro]'") from exc
    return yaml


def _yaml_load_docs(text: str) -> tuple[dict, list]:
    """Maestro flow = 配置文档（appId…）+ `---` + 命令序列；返回 (config, commands)。"""
    yaml = _need_yaml()
    try:
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
    except Exception as exc:  # yaml.YAMLError 及其子类
        raise ValueError(f"YAML 解析失败: {exc}") from exc
    if not docs:
        raise ValueError("空 flow 文件")
    config = docs[0] if isinstance(docs[0], dict) else {}
    commands: list = []
    for doc in docs[1:]:
        if isinstance(doc, list):
            commands.extend(doc)
        elif isinstance(doc, dict):
            commands.append(doc)
    if not commands and isinstance(config, list):
        # 单文档直接是命令列表（无 appId 头）——警告由调用方补
        commands = config
        config = {}
    return config, commands


def _selector(sel, warnings: list[str], where: str) -> tuple[Locator | None, list[str]]:
    """Maestro 选择器 → MAT Locator；返回 (locator, 需人工确认的 notes)。

    复合选择器（id+text 等）只能保留一个主条件——**宁可少表达也不要错表达**，
    被丢掉的条件进 notes。
    """
    if isinstance(sel, str):
        return (Locator(strategy=LocatorStrategy.TEXT, value=sel), []) if sel.strip() else (None, [])
    if not isinstance(sel, dict):
        warnings.append(f"{where}：不认识的选择器形态 {sel!r}——已跳过")
        return None, []
    notes: list[str] = []
    locator: Locator | None = None
    primary = None
    for key in ("id", "text", "point", "textRegex", "index", "below", "above",
                "leftOf", "rightOf", "containsDescendants", "containsChildren"):
        if key in sel:
            primary = key
            break
    if primary == "id":
        locator = Locator(strategy=LocatorStrategy.RES_ID, value=str(sel["id"]))
    elif primary == "text":
        locator = Locator(strategy=LocatorStrategy.TEXT, value=str(sel["text"]))
    elif primary == "point":
        warnings.append(f"{where}：point 选择器是**屏幕百分比**（{sel['point']!r}），"
                        "MAT point 是像素坐标——请人工换算后手写")
    elif primary is None:
        warnings.append(f"{where}：选择器缺 id/text（{sel!r}）——已跳过")
    if locator is not None:
        dropped = [k for k in sel if k != primary]
        if dropped:
            notes.append(f"{where}：复合选择器的 {('、'.join(dropped))} 条件未表达"
                         "（MAT 单定位器）——请人工确认是否需要 fallback 链")
    return locator, notes


class _Importer:
    """单条 flow 的导入状态机：跟踪「上一条 tap 的定位器」供焦点类命令借用。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.steps: list[Step] = []
        self._last_tap: Locator | None = None
        self._n = 0

    def _next_id(self) -> str:
        self._n += 1
        return f"s{self._n}"

    def _add(self, step: Step) -> None:
        self.steps.append(step)

    def _selector(self, sel, where: str) -> Locator | None:
        loc, notes = _selector(sel, self.warnings, where)
        self.notes.extend(notes)
        return loc

    def command(self, verb: str, arg) -> None:
        lv = verb.strip().lower()
        where = f"第 {self._n + 1} 条命令 {verb.strip()!r}"
        if lv == "launchapp":
            params: dict = {}
            if isinstance(arg, dict):
                if arg.get("clearState"):
                    params["cold"] = True
            self._add(Step(id=self._next_id(), intent="启动 App（Maestro 导入）",
                           action=Action.LAUNCH, params=params))
        elif lv in ("tapon",):
            loc = self._selector(arg, where)
            if loc is not None:
                self._last_tap = loc
                self._add(Step(id=self._next_id(), intent=f"点击 {loc.value!r}（Maestro 导入）",
                               action=Action.TAP, target=loc))
        elif lv in ("longpresson",):
            loc = self._selector(arg, where)
            if loc is not None:
                self._last_tap = loc
                self._add(Step(id=self._next_id(), intent=f"长按 {loc.value!r}（Maestro 导入）",
                               action=Action.LONG_PRESS, target=loc))
        elif lv in ("inputtext", "inserttext"):
            if isinstance(arg, dict):
                text = str(arg.get("text") or "")
                # 剩余的键才是选择器（"text" 键本身是输入值，不是定位条件）
                sel = {k: v for k, v in arg.items() if k != "text"} or None
            else:
                text = str(arg or "")
                sel = None
            target = self._selector(sel, where) if sel is not None else self._last_tap
            if target is None:
                self.warnings.append(
                    f"{where}：inputText 无前置 tapOn 且未带选择器——MAT 的 input 需要"
                    "目标控件（Maestro 是焦点语义）；步骤已生成但 target 为空，"
                    "请人工补 target 后再跑")
            self._add(Step(id=self._next_id(), intent=f"输入 {text!r}（Maestro 导入）",
                           action=Action.INPUT, target=target, params={"text": text}))
        elif lv == "erasetext":
            target = self._last_tap
            if target is None:
                self.warnings.append(
                    f"{where}：eraseText 无前置 tapOn——MAT 的 clear 需要目标控件，"
                    "步骤已生成但 target 为空，请人工补")
            self._add(Step(id=self._next_id(), intent="清空输入框（Maestro 导入）",
                           action=Action.CLEAR, target=target))
        elif lv in ("assertvisible", "assertnotvisible"):
            loc = self._selector(arg, where)
            if loc is None:
                self.warnings.append(f"{where}：无可解析选择器——断言已跳过")
                return
            atype = AssertionType.EXISTS if lv == "assertvisible" else AssertionType.NOT_EXISTS
            # 承载步：wait 不需要设备元素，断言挂在它上面（与 MAT「断言附属于步骤」同构）
            self._add(Step(id=self._next_id(),
                           intent=f"断言{'可见' if atype == AssertionType.EXISTS else '不可见'}"
                                 f" {loc.value!r}（Maestro 导入）",
                           action=Action.WAIT, params={"ms": 100},
                           assertions=[Assertion(type=atype, target=loc)]))
        elif lv == "scrolluntilvisible":
            elem = arg.get("element") if isinstance(arg, dict) else arg
            direction = str((arg or {}).get("direction") or "down").lower() \
                if isinstance(arg, dict) else "down"
            loc = self._selector(elem, where)
            assertions = [Assertion(type=AssertionType.EXISTS, target=loc)] if loc else []
            self.notes.append(f"{where}：scrollUntilVisible 是「滚动直到可见」语义，"
                              "MAT 的 scroll+exists 是近似（滚动一次后校验）——需人工确认")
            self._add(Step(id=self._next_id(), intent="滚动直到目标可见（近似，Maestro 导入）",
                           action=Action.SCROLL, params={"direction": _SWIPE_MAP.get(direction, "down")},
                           assertions=assertions))
        elif lv == "scroll":
            direction = str(arg or {}).get("direction") if isinstance(arg, dict) else arg
            self._add(Step(id=self._next_id(), intent="滚动（Maestro 导入）",
                           action=Action.SCROLL,
                           params={"direction": _SWIPE_MAP.get(str(direction or "down").lower(), "down")}))
        elif lv == "swipe":
            direction = arg.get("direction") if isinstance(arg, dict) else arg
            key = str(direction or "").lower()
            if key not in _SWIPE_MAP:
                self.warnings.append(f"{where}：仅支持方向形 swipe（UP/DOWN/LEFT/RIGHT），"
                                     f"坐标点对/速度参数不支持——已跳过")
                return
            self._add(Step(id=self._next_id(), intent=f"滑动 {key}（Maestro 导入）",
                           action=Action.SWIPE, params={"direction": key}))
        elif lv == "back":
            self._add(Step(id=self._next_id(), intent="返回（Maestro 导入）", action=Action.BACK))
        elif lv == "presskey":
            key = str(arg or "").strip().lower()
            mapped = _KEYEVENT_MAP.get(key)
            if mapped is None:
                self.warnings.append(f"{where}：按键 {arg!r} 不在映射表"
                                     f"（Back/Home/Enter/Tab/Delete）——已跳过")
                return
            self._add(Step(id=self._next_id(), intent=f"按键 {mapped}（Maestro 导入）",
                           action=Action.KEY_EVENT, params={"key": mapped}))
        elif lv in ("waitforanimationtoend",):
            ms = 3000
            if isinstance(arg, dict) and arg.get("timeout"):
                try:
                    ms = int(arg["timeout"])
                except (TypeError, ValueError):
                    ms = 3000
            self.notes.append(f"{where}：waitForAnimationToEnd 以固定等待近似（{ms}ms）——需人工确认")
            self._add(Step(id=self._next_id(), intent="等待动画结束（近似，Maestro 导入）",
                           action=Action.WAIT, params={"ms": ms}))
        elif lv == "stopapp":
            self.warnings.append(f"{where}：MAT 无「仅停止」动作（launch 的 cold 会先停）"
                                 "——已跳过")
        elif lv == "hidekeyboard":
            self.warnings.append(f"{where}：MAT 无显式收键盘动作（back 可近似）——已跳过")
        elif lv in _UNSUPPORTED_VERBS:
            self.warnings.append(f"{where}：不支持（子流程/脚本/重复/环境类命令不翻译）——已跳过")
        else:
            self.warnings.append(f"{where}：未知命令——已跳过（请查 maestro_import.py 的支持矩阵）")


def import_maestro(source: str | Path, case_id: str | None = None) -> tuple[TestCase, list[str]]:
    """Maestro flow 文件 → (TestCase, warnings)。

    warnings 是**全部未翻译/需注意项**的人话清单（CLI 打印 + 进 meta）；
    case_id 缺省取文件名 stem。
    """
    path = Path(source)
    if not path.exists():
        raise ValueError(f"flow 文件不存在: {path.resolve()}")
    text = path.read_text(encoding="utf-8")
    config, commands = _yaml_load_docs(text)
    app_id = str(config.get("appId") or "") if isinstance(config, dict) else ""
    if not app_id:
        raise ValueError("flow 缺 appId（Maestro 配置头）——MAT 的 target.package 必填；"
                         "可在配置文档补 appId，或改用 --package 传包名")

    importer = _Importer()
    for cmd in commands:
        if isinstance(cmd, str):
            importer.command(cmd, None)
        elif isinstance(cmd, dict):
            for verb, arg in cmd.items():
                importer.command(str(verb), arg)
        else:
            importer.warnings.append(f"无法识别的命令行形态: {cmd!r}——已跳过")

    case_id = re.sub(r"[^0-9A-Za-z_\-]+", "-", case_id or path.stem).strip("-") or "maestro-case"
    case = TestCase(
        case_id=case_id,
        name=str(config.get("name") or case_id),
        target={"package": app_id},
        steps=importer.steps,
        meta={"source": "maestro", "warnings": importer.warnings, "notes": importer.notes,
              "maestro_file": str(path)},
        preconditions=["Maestro 导入：语义为「按 flow 顺序在已启动的 App 上回放」"
                       "（无 launchApp 时请确认起始页）"],
    )
    if not case.steps:
        importer.warnings.append("flow 里没有可映射的命令——生成空用例已拒绝")
        raise ValueError("flow 里没有可映射为 MAT 步骤的命令（warnings 见上）")
    return case, importer.warnings


# -- 反向导出（MAT → Maestro）-------------------------------------------------

def _yaml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _to_maestro_selector(loc: Locator | None) -> str | None:
    """MAT Locator → Maestro 选择器（内联 YAML 片段）；翻不了返回 None。

    与 benchmarks/compare/gen_targets.py::_maestro_selector 同源；point 是像素、
    Maestro 是百分比 ⇒ 不翻译（注释行兜底）。
    """
    if loc is None:
        return None
    st, v = str(loc.strategy.value), str(loc.value)
    if st == "res-id" or st == "id":
        return f"{{id: {_yaml_str(v)}}}"
    if st in ("text", "textContains"):
        return _yaml_str(v)
    if st in ("description",):
        return _yaml_str(v)  # Maestro 的 text 选择器同时匹配 content-desc
    return None


def to_maestro(case: TestCase) -> str:
    """MAT 用例 → Maestro flow YAML；无法翻译的步骤落成 `#` 注释（不静默丢弃）。"""
    lines = [f"appId: {case.target.get('package', '')}", "---"]
    for s in case.steps:
        act, tgt = s.action, s.target
        params = s.params or {}
        if act in (Action.LAUNCH, Action.WAIT):
            pass  # 不生成动作（Maestro 启动由 appId 承担）；断言照常处理
        elif act == Action.BACK:
            lines.append("- back")
        elif act == Action.KEY_EVENT:
            lines.append(f"- pressKey: {str(params.get('key', 'enter')).capitalize()}")
        elif act in (Action.TAP, Action.LONG_PRESS, Action.INPUT, Action.CLEAR):
            sel = _to_maestro_selector(tgt)
            if sel is None:
                lines.append(f"# MAT: 步骤 {s.id}（{act.value}）定位器翻译不了："
                             f"{tgt.strategy.value}={tgt.value!r}" if tgt else
                             f"# MAT: 步骤 {s.id}（{act.value}）无目标控件")
            else:
                verb = {"tap": "tapOn", "long-press": "longPressOn",
                        "input": "tapOn", "clear": "tapOn"}[act.value]
                lines.append(f"- {verb}: {sel}")
                if act == Action.INPUT:
                    lines.append(f"- inputText: {_yaml_str(str(params.get('text', '')))}")
                elif act == Action.CLEAR:
                    lines.append("- eraseText")
        elif act in (Action.SWIPE, Action.SCROLL):
            direction = str(params.get("direction", "up")).upper()
            lines.append(f"- swipe: {direction}")
        else:
            lines.append(f"# MAT: 步骤 {s.id}（{act.value}）无 Maestro 对应命令")
        for a in s.assertions:
            at, atgt = str(a.type.value), a.target
            if at == "not-exists":
                sel = _to_maestro_selector(atgt)
                if sel:
                    lines.append(f"- assertNotVisible: {sel}")
            else:
                sel = _to_maestro_selector(atgt)
                if sel is None:
                    if at == "text-equals":
                        lines.append(f"- assertVisible: {_yaml_str(a.expected or '')}")
                    continue
                if at == "exists":
                    lines.append(f"- assertVisible: {sel}")
                elif at == "text-equals":
                    lines.append(f"- assertVisible: {_yaml_str(a.expected or '')}")
                # 其余断言类型（enabled/checked…）Maestro 无对应命令 → 注释
                else:
                    lines.append(f"# MAT: 断言 {s.id}/{at} 无 Maestro 对应命令")
    return "\n".join(lines) + "\n"
