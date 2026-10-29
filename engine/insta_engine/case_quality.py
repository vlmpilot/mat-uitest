"""用例质量闸门 —— 「产用例」的两条路径共用一套。

## 为什么必须共享

两条路径各自实现一套 ⇒ **必然漂移**（同一个事实两个口径，迟早对不上）。实测现状（2026-10-13）：

  · 导入路径（`mat import`，手工用例 → DSL）：**三道闸**（target 消毒 / 断言消毒 / 质量检查）
  · 生成路径（`mat gen`，自然语言 → DSL）：**零道闸** —— LLM 产物校验完 schema 直接落盘

而**对 52 条历史跑测的失败逐条归因**（内嵌用例 + 失败步报错 + "当前页面可见控件"现场）：
37 条失败里 **27 条带确定性用例缺陷，没有一条能归到"定位能力不足"**。
「生成」与「转化」出来的用例**最终跑在同一台设备上**，质量闸门没有理由只有一条路径有。

## 两类动作（务必分清）

① **消毒**（`sanitize_*`，**会自动改**）：改掉的都是"留着就必然失败"的东西
   —— 状态后缀剥离、断言缺定位时继承本步目标、继承不了就删。
② **检查**（`quality_issues`，**只报不改**）：把"大概率必失败"的结构性缺陷报成 issue，
   参与分级 / 打印给用户。**改文案是人的决定**，闸门只负责让它可见。

## 判据来源（全部来自真实跑测的失败归因）

| 编号 | 缺陷 | 为什么必失败 |
|---|---|---|
| A1 | 断言缺定位 | `runner` 抛 `断言 exists 缺少 target` |
| A2 | 断言与导航耦合 | `tap(X)` 后又断言 `X` 存在 —— 点完页面已变，入口控件不在 |
| A3 | 定位文案是内容快照 | 具体新闻标题/单集名，跨运行必变 |
| A4 | 定位文案带状态后缀 | `视频，可选中` —— 状态说明被当成了控件文案 |
| A5 | 没切到被测 App | 无 `launch` / `launch` 无 `package` ⇒ 整条跑在别的 App 上 |
| A6 | 起始状态无人保证 | 首步不是 `launch` 且无前置条件 ⇒ 依赖"设备恰好停在那儿" |
| A7 | 定位为空白或不可见字符 | 空串 / 图标字体私有区字形（U+E000–F8FF）/ 零宽字符 |

⚠️ 上表**就是判据本身**（不是"摘录"）：新增一条判据前先问
「**它拦的是哪一类真实失败**」——没有实测支撑的规则会让闸门变成噪音，人就不看了。
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .dsl import LocatorStrategy

__all__ = [
    "BLOCK", "INFO", "RULES", "STATE_ASSERTION_TYPES", "WARN", "clean_locator", "ensure_launch",
    "fmt_issue", "grade_of", "quality_issues", "review", "rule_code_of", "sanitize_assertions",
    "sanitize_targets", "severity_of",
]

#: 合法定位策略 —— **从枚举派生**，不手抄（手抄必然与 `dsl.Locator` 漂移）。
VALID_STRATEGIES = {s.value for s in LocatorStrategy}
#: 文本类定位策略（只有它们才存在"文案"这回事）。
TEXT_STRATEGIES = ("text", "textContains", "description")
#: 导航类动作：点完页面通常已经变了。
NAV_ACTIONS = ("tap", "long-press")

#: **状态类断言**：断的是"状态变了"，不是"控件还在"。
#: 它们与"动作指向同一个控件"**不冲突** —— `tap 我的` + `selected 我的` 是语料里
#: 最好的一条断言（底部 tab 一直都在，但选中态会变）。所以 R2/R23 都豁免它们
#: （2026-10-24：此前 `tap 关注` + `selected 关注` 会被 R2 判成"必失败"，是误报）。
STATE_ASSERTION_TYPES = ("selected", "checked", "enabled", "disabled")

#: accessibility 状态后缀（`视频，可选中`）—— 它是**状态说明**，不是控件文案。
#: uiautomator / ATX 会把**控件状态**拼进 text / description，状态一变文案就变 ⇒
#: 拿它当定位器**必然跨运行失败**。这里做**确定性剥离**——比只报 issue 有用：
#: 剥完往往就是个本来就可用的定位器。
STATE_SUFFIX = re.compile(
    r"[，,]\s*(?:可选中|已选中|不可选中|未选中|可点击|不可点击|可编辑|可用|不可用"
    r"|已勾选|未勾选|已启用|已禁用|已展开|已折叠|已聚焦|未聚焦)\s*$")

#: 内容快照的启发式：够长 + 含标点 ⇒ 大概率是"某次运行的具体内容"。
SNAPSHOT_MIN_LEN = 12
SNAPSHOT_PUNCT = re.compile(r"[，。！？、；：“”‘’丨|（）]")

#: 私有区（图标字体）+ 零宽 + 控制字符。**人看着是空白，机器也匹配不到**。
#: 实测 `{"strategy":"text","value":"\ue917"}`（分享图标字形）跑出来是 `text= 未命中`，
#: 光看报错会误判成"定位文案为空"。
INVISIBLE_CHARS = re.compile(r"[\ue000-\uf8ff\u200b-\u200f\u2060\ufeff\u0000-\u001f]")


# -- 规则登记表（R16：**每条 issue 都必须能指回一个编号**） --------------------------
#
# 为什么必须挂编号：issue 是**给人看的话**，但质量趋势要**能统计**。
# 没有编号时，"这个月问题变多了"只能靠人肉读，也无法回答"哪一类最常犯"
#（同族 D46：一个分类若指向两个相反的修复方向，它就不是判据、只是标签）。
#
# 严重度决定**分级**（见 `grade_of`）：
#   · `BLOCK` 留着必然失败（或跑不起来）        ⇒ 等级 C
#   · `WARN`  需要人看一眼，不必然失败          ⇒ 等级 B
#   · `INFO`  **闸门已完全处置**，仅供知情        ⇒ 不影响等级（仍会打印出来）
#
# ⚠️ `INFO` 与 `WARN` 的分界是**"闸门的处置完不完整"**，不是"改动大不大"：
#   · `R5` 剥状态后缀是**完全**处置（控件文案本来就是 `视频`，后缀是 uiautomator 拼的）
#     ⇒ INFO；
#   · `R6` 补 launch 只是**部分**处置（起点对了，但用例原本假设"自己已在某页"，
#     那些步骤从首页仍然走不到）⇒ **WARN**。
#   ⚠️ 这个分界是**真机数据纠正出来的**：R6 原定为 INFO，于是 14 条缺 launch 的用例
#     全被算成 A 级，而它们实测 **13/14 全红** ⇒ A 级通过率被拖到 43%（见
#     `scripts/validate_quality_gate.py`）。**严重度不是设计出来的，是量出来的。**
BLOCK, WARN, INFO = "block", "warn", "info"

#: 「首启弹窗确认」的 res-id 形态（`tv_dialog_confirm` / `btn_agree` / `dialog_ok`）。
#: 只用于**启动后前 2 步**的消毒（见 `sanitize_splash_steps`），不参与运行期判定。
_DIALOG_CONFIRM_RE = re.compile(
    r"(?i)(?:dialog|popup|alert)[_./-]*(?:confirm|ok|agree|allow|positive)"
    r"|(?:confirm|agree)[_./-]*(?:btn|button|dialog)")

RULES: dict[str, tuple[str, str]] = {
    "R1": (WARN, "断言必须有定位（缺则继承本步目标；继承不了就删）"),
    "R2": (BLOCK, "断言不得与导航耦合（点完页面已变，入口控件不在）"),
    "R3": (BLOCK, "定位文案不得是空白/不可见字符（策略链必然全不命中）"),
    "R4": (WARN, "定位文案疑似内容快照（跨运行会变）"),
    "R5": (INFO, "定位文案带 accessibility 状态后缀（已**完全**剥离）"),
    "R6": (WARN, "缺 launch 已自动补，但**起点仍需人确认**（原用例假设自己已在某页）"),
    "R7": (BLOCK, "launch 必须带 params.package（否则整条跑在别的 App 上）"),
    "R9b": (WARN, "可置备页面状态：需展开成导航步骤或人工置备/确认路径"),
    "R9c": (WARN, "前置条件是随机事件（用例不该依赖）"),
    "R12": (WARN, "每条用例至少一条断言"),
    "R14": (WARN, "模型自评低，需重点核对（自评**不参与**分级）"),
    "R17": (WARN, "前置条件含**不可置备的系统态**：执行时会被拒跑，需人工置备或显式放行"),
    # R18/R19 由 `case_audit` 产出（那两条需要页面索引 / 弹窗词表，`case_quality` 判不了）
    "R18": (WARN, "定位引用的文案/res-id 不在控件清单里（可能是幻觉，也可能索引未覆盖该页）"),
    "R20": (WARN, "定位在**本步所在页**不存在（图谱其他页有）——很可能跨页/碎片，运行期必定位失败"),
    "R22": (INFO, "「点跳过广告」类步骤已自动改为可选（onFailure=continue）——"
                  "引擎 launch 后已统一关广告，重跑时该目标必然不存在"),
    "R21": (WARN, "text 定位在该页不唯一，或只匹配到 content-desc 控件（图标入口）——"
                  "TEXT 策略是 text 精确优先，会先命中同名的内容标签"),
    "R23": (WARN, "断言与**本步动作指向同一个控件**（平凡断言：能点到它就说明它在）——"
                  "该步骤等于没有断言，App 行为错了也照样绿"),
    "R24": (WARN, "断言目标**没有页面区分度**（该文案在 ≥10% 的页面里都出现）——"
                  "断言它「存在」证明不了到了目标页"),
    # R25 由 `case_audit.rewrite_coupled_assertions` 产出（要图谱才能判"点完控件还在不在"）。
    # INFO 而不是 WARN：它把"必失败/恒真"的断言**换成了真断言**，缺陷已被完全处置，
    # 只留痕迹让人知道产物与手写原稿不一样了（同 R5/R22 的口径）。
    "R25": (INFO, "导航耦合断言已改写为**状态断言**（`selected`）——"
                  "原写法点完即失效或恒真，等于没断"),
    # R26 是 R24 的**兜底**（`case_audit.nondiscriminating_assertions`）：走链在落点页
    # 断掉时 R24 会弃权，此时改用「目标在本用例**已知前序页**上就有」判。单开编号是为了
    # 不破坏既有基线的可比性（直接放宽 R24 会让"R24 命中数"这个指标失去历史对照）。
    "R26": (WARN, "断言在**已知前序页**上就成立（落点页不在图谱、走链弃权）——"
                  "同样证明不了页面变了"),
    # R28（2026-10-28）：步骤覆盖自检（`step_coverage`）产出——需求里的动作没有步骤承载。
    # 只报不改：字符覆盖是启发式，误判时不该阻断生成。
    "R28": (WARN, "需求里的某个动作没有对应步骤（漏步骤会让用例跑完仍报通过）"),
    # R27（2026-10-28）：与 R26 分开编号是**为了保住可比性**——R26 的口径是"落点页未知"，
    # 这条的口径是"落点页已知，但状态断言的目标在动作前就已选中"。混进 R26 会让
    # 「R26 命中数」同时指向两个相反的修复方向（同族 D46）。
    # ⚠️ 判据依赖图谱里的 `selected`（★）。**老图谱没采这个字段 ⇒ 本规则静默不生效**，
    # 所以"没报 R27"不等于"没问题"，要连图谱年龄一起看。
    "R27": (WARN, "状态断言的目标在**动作前就已选中**（图谱 ★ 可证）——"
                  "动作 no-op（点的就是已选中的 tab）时也照样绿"),
    "R19": (WARN, "用例依赖一次性系统/首启权限弹窗（重跑通常不再出现）"),
    "LLM": (WARN, "模型自己报的问题（未映射到规则编号）"),
    "X0": (BLOCK, "转换失败（未产出可用草稿）"),
}


def fmt_issue(code: str, text: str) -> str:
    """给 issue 挂上规则编号。

    ⚠️ 编号未登记时**直接报错**（不是静默放过）：R16 要求"每条 issue 都能指回一个编号"，
    而新增一处 issue 却忘了登记，正是这条要求最容易失效的地方。
    """
    if code not in RULES:
        raise KeyError(f"未知规则编号 {code}：新增 issue 前先登记到 case_quality.RULES")
    return f"[{code}] {text}"


def rule_code_of(issue: str) -> str:
    """读一条 issue 的规则编号（**认不出来返回 `"?"`**，便于统计时把它暴露出来）。"""
    m = re.match(r"^\[([A-Za-z0-9]+)\]", str(issue or ""))
    return m.group(1) if m else "?"


def severity_of(issue: str) -> str:
    """读一条 issue 的严重度。**认不出来按 WARN**（fail-safe：不放行未知问题）。"""
    return RULES.get(rule_code_of(issue), (WARN, ""))[0]


def grade_of(issues: list[str]) -> str:
    """按**确定性判据**给等级 —— ⚠️ **不看模型自评**（见 `convert_case` 的说明）。

    A = 无 WARN/BLOCK（INFO 是"闸门已替你处置"，不算缺陷）
    B = 有 WARN
    C = 有 BLOCK
    """
    severities = {severity_of(i) for i in issues}
    if BLOCK in severities:
        return "C"
    if WARN in severities:
        return "B"
    return "A"


# -- ① 消毒（自动改） -------------------------------------------------------------

def _invisible_only(text: str) -> bool:
    """整串都是不可见字符（或本来就是空白）。"""
    if not text.strip():
        return True
    return not INVISIBLE_CHARS.sub("", text).strip()


def clean_locator(target) -> dict | None:
    """单个 target 的消毒：文案剥离状态后缀 → 校验 strategy/value → 保留 fallback 链。

    非法一律返回 `None`（草稿允许无定位，运行期走正常失败分类；不因模型格式瑕疵把整条判 C）。
    主定位非法时**降级到 fallback**（链塌缩成那一级）。
    """
    if not isinstance(target, dict):
        return None
    strategy, value = target.get("strategy"), target.get("value")
    if strategy in TEXT_STRATEGIES and isinstance(value, str):
        value = STATE_SUFFIX.sub("", value).strip()
    if strategy and value and strategy in VALID_STRATEGIES:
        cleaned: dict = {"strategy": strategy, "value": value}
        # `index`（第 N 个匹配）必须原样留下：本函数是**重建**定位字典，
        # 不显式搬运的字段会被静默丢掉（`preconditions` 在 IR→DSL 边界上丢过一次）。
        # 非法值（负数 / 非整数 / bool）丢弃并退回"取第一个"——
        # 严格校验在 `dsl.Locator` 上，这里只保证不把脏值带进运行期。
        index = target.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            cleaned["index"] = index
        fallback = clean_locator(target.get("fallback"))
        if fallback:
            cleaned["fallback"] = fallback
        return cleaned
    return clean_locator(target.get("fallback"))


def _value_of(locator) -> str:
    return str(locator.get("value") or "") if isinstance(locator, dict) else ""


def sanitize_targets(raw_case: dict) -> list[str]:
    """步骤**与断言**的 target 消毒（含状态后缀剥离）。返回 issues（R5）。

    ⚠️ 旧版只处理 `step.target`，**不管 `assertions[].target`** —— 于是
    `视频，可选中` 这类带状态后缀的断言文案原样留到了运行期。

    ⚠️ 消毒**也要报 issue**（R5，INFO 级）：它是自动改的，但"闸门替你改了什么"
    必须可见 —— 否则人无从知道产物与手工用例的原话已经不一样了（同族 C17：
    消毒与报 issue 是两条通路，**两条都要走**）。
    """
    issues: list[str] = []
    for step in raw_case.get("steps", []):
        holders = [(step, "步骤"), *((a, "断言") for a in (step.get("assertions") or [])
                                     if isinstance(a, dict))]
        for holder, label in holders:
            before = holder.get("target")
            after = clean_locator(before)
            old_value, new_value = _value_of(before), _value_of(after)
            if old_value and old_value != new_value:
                issues.append(fmt_issue(
                    "R5", f"{label}定位文案 `{old_value}` 带 accessibility 状态后缀，"
                          f"已自动剥离为 `{new_value}`"))
            holder["target"] = after
    return issues


def sanitize_splash_steps(raw_case: dict) -> list[str]:
    """把「点跳过广告」类步骤标成**可选**（`onFailure=continue`）。返回 issues。

    为什么（2026-10-21 真机实测，tx-news 4/16 用例）：
    引擎在**每次 launch 后都会 `_skip_splash_ad`** —— 关广告是引擎职责（用例不该负责），
    于是用例里那一步「点击跳过」重跑时**目标必然不存在**。而 DSL 默认 `onFailure=abort`
    ⇒ 整条用例在第 2 步就废（实测 4 条全部停在 25% 步骤，报「text=跳过 未命中」）。

    这条不是"放宽标准"：点「跳过」本来就是**如果还在就点掉**，不是断言；
    把它当硬失败等于让用例依赖"广告一定会出现"。
    """
    from .safety import is_splash_skip_label

    issues: list[str] = []
    # 只在**启动后的前 2 个动作步骤**里认「首启弹窗确认」：再往后可能是流程里真正的确认
    # （删除/提交/授权），改成可选会削弱用例。
    startup_budget = 2
    for step in raw_case.get("steps", []):
        if str(step.get("action") or "").strip().lower() != "tap":
            continue
        if startup_budget <= 0:
            continue
        startup_budget -= 1
        target = step.get("target") or {}
        value = str(target.get("value") or "").strip()
        if not value:
            continue
        splash = is_splash_skip_label(value)
        # res-id 形态的「弹窗确认」：R19 的词表只认文案（「同意」），认不出 `tv_dialog_confirm`
        # 这类；实测 tx-news 4 条用例的第 3 步都栽在这里（隐私弹窗首次同意后再不出现）。
        dialog = (bool(_DIALOG_CONFIRM_RE.search(value))
                  and str(target.get("strategy") or "").strip().lower() in ("res-id", "id"))
        if not (splash or dialog):
            continue
        current = str(step.get("onFailure") or step.get("on_failure") or "").strip().lower()
        if current == "continue":
            continue
        step["onFailure"] = "continue"
        what = "开屏广告" if splash else "首启弹窗（隐私/引导）"
        issues.append(fmt_issue(
            "R22", f"步骤 {step.get('id') or '?'}：点击「{value}」是{what}的一次性动作——"
                   "引擎在 launch 后已统一清理启动噪声，且这类弹窗首次同意后再不出现 ⇒ "
                   "重跑时该目标必然不存在；已自动改为 onFailure=continue（目标不在就跳过）"))
    return issues


def sanitize_assertions(raw_case: dict) -> list[str]:
    """断言消毒：**缺定位的断言不许留到运行期**（返回要记录的 issues）。

    留着的后果是**必然失败**：`runner._check_assertions` 会抛 `断言 exists 缺少 target`。

    规则（确定性，不依赖模型自觉）：
      ① 断言 target 非法、而**本步有合法 target** ⇒ **继承本步 target**
         （"验证刚操作的那个控件"——也是模型有时会自己做的事）；
      ② 本步也没有 ⇒ **删掉**该断言并记 issue（**宁缺毋假**：没有断言也比一条必失败的断言好）。
    """
    issues: list[str] = []
    for step in raw_case.get("steps", []):
        assertions = step.get("assertions")
        if not isinstance(assertions, list):
            step["assertions"] = []
            continue
        step_target = step.get("target")
        kept: list[dict] = []
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            if isinstance(assertion.get("target"), dict):
                kept.append(assertion)
            elif isinstance(step_target, dict):
                kept.append({**assertion, "target": dict(step_target)})
                issues.append(fmt_issue("R1", f"断言 {assertion.get('type')} 缺定位，已继承本步操作目标"))
            else:
                issues.append(fmt_issue("R1", f"删除 1 项缺定位且本步无锚可继承的断言（{assertion.get('type')}）"))
        step["assertions"] = kept
    return issues


# -- ② 检查（只报不改） -----------------------------------------------------------

def ensure_launch(raw_case: dict) -> list[str]:
    """补 `launch` 首步（**确定性**，自动改）：用例没切 App 时，设备停在哪儿完全随机。

    实测：37 条失败里 **14 条**属这类，其中 3 条 demo 用例整条跑在**别的 App** 上。

    为什么是"自动改"而不是"只报"：不补的话这条用例**永远无法可靠执行**
    （依赖上一个用例把设备留在哪儿）。补上之后它至少是**确定性**的 ——
    要么过，要么每次都因为同一个原因失败（可调试）。这正是"用例必须自己建立起始状态"。

    边界：拿不到 `target.package` 时**不改**（补不出正确的 launch），交给质量闸门报 issue。
    """
    steps = raw_case.get("steps")
    if not isinstance(steps, list) or not steps:
        return []
    if any(str(s.get("action") or "") == "launch" for s in steps if isinstance(s, dict)):
        return []
    # **纯 API 用例不补**（2026-10-23）：补 launch 的目的是把设备带到确定起点，
    # 而纯接口用例根本不碰设备（`mat run` 对它们跳过设备连接）。给它们塞一个 launch
    # 会让"离线可跑"变成"必须有设备"——实测直接把 `test_cmd_run_pure_api_case_needs_no_device` 打红。
    if all(str(s.get("action") or "") == "api" for s in steps if isinstance(s, dict)):
        return []
    package = str((raw_case.get("target") or {}).get("package") or "").strip()
    if not package:
        return []
    # ⚠️ step id 必须唯一（重复 id 会让重试时截图/证据互相覆盖）⇒ 取一个没被占用的
    used = {str(s.get("id") or "") for s in steps if isinstance(s, dict)}
    sid = next(f"s{i}" for i in range(0, len(used) + 2) if f"s{i}" not in used)
    steps.insert(0, {"id": sid, "intent": "启动被测 App", "action": "launch",
                     "target": None, "params": {"package": package}, "assertions": []})
    return [fmt_issue("R6", f"已自动补 `launch`（package={package}）：原用例没有指定起始 App，"
                        "跑起来会停在设备当前页面（实测 14/37 条失败属这类）")]


def _start_state_issues(steps: list[dict], preconditions: list[str]) -> list[str]:
    """起始状态类缺陷。两个**互不替代**的家族，各自至多报一条。

    **家族一：前置条件需要置备（③ 可置备的页面状态）**
      ⚠️ 这条与"有没有 launch"**无关**：`launch` 只保证"冷启动到首页"，
      **不保证**到达前置条件描述的那个页面。所以不能因为补了 launch 就不报。
      实测：xlsx 批次 8 条声明了「已进入搜索结果页」的用例，跑测**全部照红**。

    **家族二：起始状态没人建立**
      · 没有 `launch` ⇒ 设备停在哪个 App/页面无人保证（顺序依赖）；
      · `launch` 缺 `params.package` ⇒ 不会切到被测对象；
      · 首步不是 `launch` 且没声明前置条件 ⇒ 假设设备已在某页，而没人保证。

    ⚠️ 分类走 `preconditions.classify`（**默认归 ③，fail-loud**）：
    把环境前提误判成 ③ 只是多一条提示；把页面状态误判成 ① 会让闸门**沉默**、用例继续照红。
    """
    from .preconditions import PAGE, RANDOM, SYSTEM, classify_all

    issues: list[str] = []
    buckets = classify_all(preconditions)
    if buckets[SYSTEM]:
        # ⚠️ 这条以前**没人报**：不可置备的系统态只在**执行前**被拒跑，
        #    于是"会被拒跑"的用例在导入报告里仍然是 A 级（"无已知缺陷"）——
        #    而它其实**一次都跑不成**（实测 2 条位置授权用例就是这么被算成 A 的，
        #    见 scripts/validate_quality_gate.py 的盲区清单）。
        pre = "、".join(f"「{p}」" for p in buckets[SYSTEM][:3])
        issues.append(fmt_issue(
            "R17", f"前置条件 {pre} 是**不可置备的系统态**：MAT 的动作集改不了系统权限 ⇒ "
                   "执行时会被**拒跑**（需人工置备后加 --allow-unmet-preconditions 放行）；"
                   "等级不该是 A —— 它按现状一次都跑不成"))
    page_states = buckets[PAGE]
    if page_states:
        pre = "、".join(f"「{p}」" for p in page_states[:3])
        issues.append(fmt_issue(
            "R9b",
            f"前置条件 {pre} 属**可置备的页面状态**：MAT 的动作集（launch/tap/input/clear/"
            "swipe/scroll/back/home/key/wait）**改不了页面状态** ⇒ 只能展开成导航步骤，"
            "或人在执行前手工置备；否则从冷启动执行必失败"
            "（实测：声明了这类前置条件的 8 条用例全部照红）"))
    if buckets[RANDOM]:
        pre = "、".join(f"「{p}」" for p in buckets[RANDOM][:3])
        issues.append(fmt_issue(
            "R9c",
            f"前置条件 {pre} 描述的是**随机事件**：是否发生不由 MAT 也不由人决定 ⇒ "
            "既不能置备、也不能展开成导航步骤；用例不该依赖它"
            "（建议改成条件式写法，或删掉该前置；实测「开屏广告点击跳过」那条就属这类）"))

    acts = [str(s.get("action") or "") for s in steps]
    if not acts:
        return issues
    if "launch" not in acts:
        issues.append(fmt_issue(
            "R6",
            "用例没有 `launch` 步骤：设备停在哪个 App / 哪个页面**无人保证** —— "
            "实测这类用例会因「上一个用例把设备留在别处」而随机红；"
            "请补 `launch`，或声明前置条件并在执行前把设备置备好"))
        return issues
    for step in steps:
        if step.get("action") == "launch" and not (step.get("params") or {}).get("package"):
            issues.append(fmt_issue(
                "R7",
                "`launch` 步骤缺 `params.package`：App **不会切到被测对象**，"
                "整条用例会跑在设备当前停留的 App 上（实测 3 条用例因此全红）"))
            return issues
    if acts[0] != "launch" and not preconditions:
        issues.append(fmt_issue(
            "R6",
            f"首步不是 `launch`（是 `{acts[0]}`）且**未声明前置条件**："
            "用例假设设备已经在某个页面，而没有任何机制保证这一点（顺序依赖）"))
    return issues


def _action_labels(step: dict) -> set[str]:
    """本步动作**指向的那几个标识**：能点到它，就说明它在。

    `params.text` 必须算进去 —— 容器 + 文案的组合定位（`res-id=channelbar_text` +
    `params.text=要闻`）在现代 App 里是常态，只看 `target.value` 会把这种写法漏掉。
    """
    labels: set[str] = set()
    target = step.get("target")
    if isinstance(target, dict) and target.get("value"):
        labels.add(str(target["value"]).strip())
    params = step.get("params")
    if isinstance(params, dict):
        for key in ("text", "description", "label", "contentDescription"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                labels.add(value.strip())
    return {v for v in labels if v}


def quality_issues(raw_case: dict, preconditions: list[str] | None = None) -> list[str]:
    """**大概率在运行期必失败**的用例缺陷。只报 issue，不自动改。

    `preconditions` 只影响「起始状态」那条判据（声明了前置条件的用例不算"无人保证"）。
    """
    issues: list[str] = []
    steps = raw_case.get("steps", []) or []
    pre = list(preconditions or raw_case.get("preconditions") or [])

    for step in steps:
        target = step.get("target")
        if isinstance(target, dict):
            value = str(target.get("value") or "")
            strategy = target.get("strategy")
            is_text = strategy in TEXT_STRATEGIES
            # A7：文案为空白 / 不可见字符（图标字体私有区、零宽）—— 策略链必然全不命中
            if is_text and _invisible_only(value):
                shown = "空" if not value.strip() else f"不可见字符（{value!r}）"
                issues.append(fmt_issue(
                    "R3",
                    f"定位文案为{shown}：策略链必然全不命中；"
                    "图标请改用 `description` 或 `resourceId`（实测报错会显示成 `text= 未命中`，极易误判成漏写文案）"))
            # A3：内容快照
            if is_text and len(value) >= SNAPSHOT_MIN_LEN and SNAPSHOT_PUNCT.search(value):
                issues.append(fmt_issue(
                    "R4",
                    f"定位文案疑似**内容快照**（来自某次运行的具体内容，跨运行会变）：{value[:40]}"
                    "；建议换成稳定的 res-id 或通用入口文案"))
            # A2：断言与导航耦合 —— ⚠️ 对**所有策略**成立（旧版只看 text，
            #     于是 `tap(res-id)` + `assert exists(同一 res-id)` 漏网）
            if step.get("action") in NAV_ACTIONS and value:
                labels = _action_labels(step)
                for assertion in step.get("assertions") or []:
                    at = assertion.get("target") if isinstance(assertion, dict) else None
                    atype = str(assertion.get("type") or "") if isinstance(assertion, dict) else ""
                    # 状态类断言**豁免**（`tap 我的` + `selected 我的` 是推荐写法：
                    # tab 一直在，但选中态会变 ⇒ 有区分度）。不加这条，R2 会把
                    # "最该这么写"的断言判成必失败 —— 实测语料里最好的一条就是它。
                    state_assertion = atype in STATE_ASSERTION_TYPES
                    # `not-exists` 是**正确**写法（"点跳过 → 跳过按钮应当消失"），不报。
                    if (not state_assertion and isinstance(at, dict) and at.get("value") == value
                            and at.get("strategy") == strategy
                            and assertion.get("type") != "not-exists"):
                        issues.append(fmt_issue(
                            "R2",
                            f"断言与导航耦合：本步 `{step.get('action')}({value})` 之后又断言该控件存在"
                            " —— 两种结局都不好：入口控件**不在**了 ⇒ 断言必失败；"
                            "还在（tab/底栏这类）⇒ 断言恒真，页面没切也照样绿。"
                            "建议改断「到了哪一页」的独有控件，或该控件的**状态**（`selected`）"))
                        continue
                    # A8/R23：**语义同一** —— 断言指向的控件就是本步点/长按的那个。
                    # 与 R2 的区别：R2 是"点完它就不在了 ⇒ 必失败"；R23 是"它一定还在
                    # （能点到就说明它在）⇒ 断言必然通过，等于没断"。实测（2026-10-22
                    # 假绿复核）：tx-news 4 条频道切换用例全是这个形态 —— 点
                    # `res-id=channelbar_text`（7 个 tab 共用，实际点中的是「7x24」），
                    # 断言 `exists text=要闻`，页面压根没切也照样绿。
                    av = str(at.get("value") or "").strip() if isinstance(at, dict) else ""
                    if (av and av in labels and not state_assertion
                            and assertion.get("type") not in ("not-exists", "notExists")):
                        issues.append(fmt_issue(
                            "R23",
                            f"断言与动作指向**同一个控件**（`{av}`）：本步 `{step.get('action')}` "
                            "刚点过它，断言它「存在」必然通过 —— App 行为错了也照样绿。"
                            "建议改断**状态**（`selected`/`enabled`）或**到了哪一页**"
                            "（结果页的业务控件）"))

    issues += _start_state_issues(steps, pre)
    # R12：整条用例**没有任何断言** —— 它跑绿只证明"步骤没抛异常"，不证明任何业务结果。
    # 这条原先只写在导入路径（`manual_import`）里，生成路径没有 ⇒ 实测漏掉过
    # `gen-225111`（某生活服务 App「启动应用并进入个人中心」，2 步 0 断言，真机必绿）。
    if steps and not any(step.get("assertions") for step in steps):
        issues.append(fmt_issue(
            "R12",
            "整条用例**没有任何断言**：跑绿只能证明「步骤没报错」，不证明任何业务结果"
            "（App 坏了它照样绿）。至少给最后一步加一条可判定的断言"))
    return issues


def review(raw_case: dict, preconditions: list[str] | None = None,
           expand: Callable[[dict], list[str]] | None = None,
           rewrite: Callable[[dict], list[str]] | None = None) -> list[str]:
    """**一站式**：保证起始状态 → （可选）展开前置条件 → 消毒 → （可选）要图谱的改写 → 检查。

    ⚠️ **顺序是刻意的，别调换**：
      ① `ensure_launch` 先补起点 —— 展开出来的导航必须从某个**确定的地方**出发；
      ② `expand` 再插导航步骤（可选，只有导入路径需要）；
      ③ 消毒 / 检查**最后** —— **展开出来的步骤也要过闸门**。
      ④ `rewrite`（可选）夹在消毒与检查**之间** —— 它要图谱（`case_audit`），
         改的是"断言指向哪个控件"，所以必须跑在 `sanitize_assertions`（补定位）**之后**、
         `quality_issues`（判 R2/R23）**之前**：改写掉了就不该再报那条缺陷，
         否则一条用例会同时背"已修好"的改动和"未修好"的 issue（同族 C17：消毒与报 issue
         是两条通路，但**缺陷被处置后不该继续报**）。
    第 ③ 点最容易漏：如果展开发生在闸门之后，插进去的步骤就**没人检查**
    （而它是 LLM 产出，一样会幻觉、一样会写出内容快照或不可见字符）。

    产用例的两条路径都应该走这个（而不是各自挑几个子函数调）——
    "各自挑"就是漂移的起点：`convert_case` 曾经就漏掉了 `ensure_launch` 与
    "前置条件需置备"两条判据（因为它只挑了三个子函数调）。
    """
    issues = ensure_launch(raw_case)
    if expand is not None:
        issues += expand(raw_case)
    issues += sanitize_targets(raw_case)
    issues += sanitize_splash_steps(raw_case)   # 依赖上一步：文案先剥掉状态后缀才能全等判「跳过」
    issues += sanitize_assertions(raw_case)
    if rewrite is not None:
        issues += rewrite(raw_case)
    issues += quality_issues(raw_case, preconditions)
    return issues
