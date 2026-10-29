"""强断言覆盖率（B2b）——包内单一实现（2026-10-28 自 scripts/eval_assertion_strength.py 转正，
与首穿率转正同模式：scripts 改薄壳，口径单一来源）。

## 为什么要拆

最初只报一条 B2b（"预期里含可判定条件（存在/不显示/选中/等于/状态）的步骤/总步骤"），
但 `exists` 也算"可判定" ⇒ **这条指标可以被刷**：给每步写一句"断言某某存在"就能拿 1.00。

2026-10-22 的假绿率复核把这件事从"理论漏洞"变成"实测事故"：

    tx-news 4 条「频道切换」用例全部报绿，截图显示页面**压根没切**（还停在 7x24）。
    它们每步都"有断言"—— 断言 `exists text=要闻`，而频道栏本来就一直显示"要闻"。

⇒ 分成五层，每层都要单独报：

| 层 | 判据 | 能证明什么 |
|---|---|---|
| `strong` | `not-exists` / `text`(等于/包含) / `selected` / `enabled` / `disabled` / `count` | 业务结果对错 |
| `exists` | `exists` 且目标是**别的**控件（页面内容/独有入口） | 某个控件在（可能是页面骨架）|
| `skeleton` | `exists` 且目标是**页面骨架**（排序/筛选栏、搜索框、导航 tab、页面标题） | **只能证明"到了某页"**：搜什么词、提交成没成功，它都在 |
| `tautology` | `exists` 且目标就是**本步刚操作的那个**控件（R23） | **什么都不能**：能点到它就说明它在 |
| `none` | 本步没有断言 | 什么都不能 |

### `skeleton` 为什么要单独一层（2026-10-28）

复核实测里两条确认假绿都是这个形态：

    input 咖啡 → tap 搜索 → assert exists text=综合        ← 结果页排序栏，任何结果页都有
    input 火锅 → tap 搜索 → assert exists text=综合排序    ← 同形（实测样本）

断言写得不假，`综合` 确实在；但它证明不了"搜到了咖啡"——**换了词、搜空了、甚至输入没生效，
它照样在**。这类断言在 `exists` 里混着，会被读成"弱但有信息"，实际是**零区分度**。

⚠️ **这是启发式、且是下界**，两条限制写清楚：
- 词表只收**跨 App 通用**的 chrome 词，App 私有的 tab 名会漏（漏了只是少报，不会误报成 strong）；
- `已选中X` 形态要额外判"动作能不能改变它"：`已选中热点榜`（用例刚点了「热点」）**是有效断言**，
  `已选中综合`（默认就在综合）**不是**——同一个控件，语义由动作决定，所以要看本用例的动作定位值。

`scripts/eval_assertion_strength.py` 是薄壳 CLI（含 `--index` 页面区分度与 `--self-test`）。

⚠️ 页面区分度（`--index`）是**另一个信号**，不与 `skeleton` 合并：它抓"文案跨很多页通用"
（实测某列表页 `综合排序` 36/117 页），`skeleton` 抓"控件本身是页面结构"。两者互补，各自报。

**分母固定为"总步骤"**（含 launch/wait），不做"只看动作步骤"的换算——换算一次就得
在每份对外材料里解释一次，口径越复杂越容易变形。
"""

from __future__ import annotations

import json
from pathlib import Path

# 单一实现，别抄第二份：`_action_labels` 就是 R23 用的那套"本步指向了哪几个标识"。
from .case_quality import _action_labels
from .dsl import AssertionType

STRONG_TYPES = frozenset(t.value for t in AssertionType if t is not AssertionType.EXISTS)
#: 存在性断言：弱档，但确实能证明"有个东西在"。
EXISTS_TYPES = frozenset({AssertionType.EXISTS.value})

#: **页面骨架**（chrome）文案：排序/筛选栏、搜索框、导航 tab、页面标题。
#:
#: 判据是"任何同页状态都成立"——搜什么词、提交成没成功，这些控件都在，所以断它
#: 证明不了动作效果。词表刻意用**精确匹配**（不是子串）：`新增关注` 含"关注"，
#: 但它是消息页的**内容**，子串匹配会把它误判成骨架。
#:
#: ⚠️ 只收**跨 App 通用**的 chrome 词 + 实测语料里已确认是 chrome 的（`7x24` 是
#: 某资讯 App 的频道 tab，首页恒在）。加词前先问："这句话在任何同页状态下都成立吗？"
SKELETON_WORDS = frozenset({
    # 排序 / 筛选栏
    "综合", "综合排序", "默认排序", "销量", "价格", "筛选", "全部", "最新",
    # 搜索框 / 搜索页骨架
    "搜索", "搜索框", "搜索栏", "历史搜索", "猜你想搜",
    # 导航 tab / 频道栏
    "首页", "发现", "关注", "消息", "我的", "市集", "推荐", "热点", "热点榜", "7x24",
})
#: ⚠️ 详情页操作栏（返回/分享/评论/收藏/点赞）**刻意不收**：它们虽然"进页面就有"，
#: 但同一个词也常是**动作的结果**（长按列表行弹出的上下文菜单里就有「收藏」）——
#: 收进来会实测误报一条（`列表：长按行出现上下文菜单`）。宁可漏，不要误报。

#: 断言文案里的装饰性尾缀（`搜索, ` 这种 App 自绘前缀要还原成 `搜索` 才能查表）
_TRAILING = " ,，。、·|"


def _is_skeleton(value: str, case_labels: set[str]) -> bool:
    """这条 `exists` 断言是不是在断**页面骨架**。

    `已选中X` 形态要额外判"动作能不能改变它"：
    - `已选中热点榜`（本用例刚点了「热点」）⇒ 选中态是动作的**效果** ⇒ 有效断言；
    - `已选中综合`（搜索页默认就在综合）⇒ 换词/搜空/输入没生效它都在 ⇒ 骨架。
    同一个控件，语义由动作决定，所以要拿**整个用例**的动作定位值来比。
    """
    if not value:
        return False
    core = value.strip(_TRAILING)
    if core.startswith("已选中"):
        core = core[len("已选中"):].strip(_TRAILING)
        if core not in SKELETON_WORDS:
            return False
        related = any(core in label or label in core
                      for label in case_labels if label)
        return not related
    return core in SKELETON_WORDS


def _case_labels(steps: list[dict]) -> set[str]:
    """整个用例里所有动作指向过的标识（`params.text` 也算，见 `_action_labels`）。"""
    out: set[str] = set()
    for step in steps:
        out |= _action_labels(step)
    return out


def classify_step(step: dict, case_labels: set[str] | None = None) -> tuple[str, str]:
    """单步 → (层, 理由)。层：strong / exists / skeleton / tautology / none。

    `case_labels` 只影响 `skeleton` 里 `已选中X` 那一支（见 `_is_skeleton`）；
    不传就退化成"任何 `已选中X` 都算骨架"。
    """
    assertions = step.get("assertions") or []
    if not assertions:
        return "none", "无断言"
    labels = _action_labels(step)
    scope = case_labels if case_labels is not None else labels
    best = ("none", "无断言")
    for assertion in assertions:
        if not isinstance(assertion, dict):
            continue
        atype = str(assertion.get("type") or "")
        target = assertion.get("target") or {}
        value = str((target or {}).get("value") or "").strip()
        if atype in STRONG_TYPES:
            return "strong", f"{atype}({value})"
        if atype in EXISTS_TYPES:
            if value and value in labels:
                # 断言的就是本步刚点的那个控件 —— 必然通过（R23）
                best = max(best, ("tautology", f"exists({value}) 即本步动作目标"),
                           key=lambda x: _ORDER.index(x[0]))
            elif _is_skeleton(value, scope):
                best = max(best, ("skeleton", f"exists({value}) 是页面骨架"),
                           key=lambda x: _ORDER.index(x[0]))
            else:
                best = max(best, ("exists", f"exists({value})"),
                           key=lambda x: _ORDER.index(x[0]))
    return best


#: 从差到好。`tautology` 最差（必然通过）；`skeleton` 次差（只能证明到了某页，
#: 但比 tautology 多一条信息：确实在目标页上）。
_ORDER = ["none", "tautology", "skeleton", "exists", "strong"]


def _texts_of(step: dict) -> list[str]:
    out = []
    for assertion in step.get("assertions") or []:
        target = (assertion or {}).get("target") or {}
        value = str((target or {}).get("value") or "").strip()
        if value:
            out.append(value)
    return out


def _page_text_df(index_path: Path | None) -> dict[str, int]:
    """断言文案 → 出现在多少个页面里（页面区分度的分母）。"""
    if not index_path or not index_path.exists():
        return {}
    doc = json.loads(index_path.read_text(encoding="utf-8"))
    df: dict[str, int] = {}
    for page in doc.get("pages") or []:
        seen = set()
        for control in page.get("controls") or []:
            for key in ("text", "desc"):
                value = str(control.get(key) or "").strip()
                if value:
                    seen.add(value)
        for value in seen:
            df[value] = df.get(value, 0) + 1
    return df


def analyze(case: dict, df: dict[str, int] | None = None) -> dict:
    steps = case.get("steps") or []
    tiers: dict[str, int] = {"strong": 0, "exists": 0, "skeleton": 0,
                             "tautology": 0, "none": 0}
    rows = []
    scope = _case_labels(steps)
    seen_input = False
    for step in steps:
        tier, why = classify_step(step, scope)
        tiers[tier] += 1
        rows.append({"step": step.get("id"), "action": step.get("action"),
                     "tier": tier, "why": why,
                     # 「输入之后」的骨架断言风险更高：它本该证明"搜到了什么"，
                     # 实际只证明了"到了结果页"。到达类用例里的骨架断言是正常用法。
                     "after_input": seen_input})
        if step.get("action") == "input":
            seen_input = True
    total = len(steps)
    spread = []
    if df:
        for step in steps:
            for value in _texts_of(step):
                n = df.get(value)
                if n is not None:
                    spread.append((value, n))
    return {
        "case": case.get("name") or case.get("caseId") or "?",
        "package": (case.get("target") or {}).get("package"),
        "steps": total,
        "tiers": tiers,
        "rows": rows,
        # B2a：有断言的步骤（最粗的一条，谁都能刷）
        "b2a": (total - tiers["none"]) / total if total else 0.0,
        # B2b（原口径）：含"可判定条件"的步骤 —— exists 也算
        "b2b_doc": (total - tiers["none"]) / total if total else 0.0,
        # B2b'（本脚本口径）：强断言，排除 exists / skeleton / tautology
        "b2b_strong": tiers["strong"] / total if total else 0.0,
        # 看起来有断言、实际不可能失败的那部分
        "tautology_rate": tiers["tautology"] / total if total else 0.0,
        # 只能证明"到了某页"的那部分（骨架断言）
        "skeleton_rate": tiers["skeleton"] / total if total else 0.0,
        "assert_spread": spread,
    }


def aggregate(results: list[dict], df: dict[str, int] | None = None) -> dict:
    steps = sum(r["steps"] for r in results) or 1
    keys = ("strong", "exists", "skeleton", "tautology", "none")
    tiers = {k: sum(r["tiers"][k] for r in results) for k in keys}
    spread = [pair for r in results for pair in r["assert_spread"]]
    # 页面区分度：断言文案出现在 ≥5 页 ⇒ 该断言无法证明"到了某一页"
    wide = [p for p in spread if p[1] >= 5]
    skeleton_cases = [
        {"case": r["case"], "step": row["step"], "why": row["why"],
         "after_input": row["after_input"]}
        for r in results for row in r["rows"] if row["tier"] == "skeleton"
    ]
    return {
        "cases": len(results),
        "steps": steps,
        "tiers": tiers,
        "b2a": (steps - tiers["none"]) / steps,
        "b2b_doc": (steps - tiers["none"]) / steps,
        "b2b_strong": tiers["strong"] / steps,
        "tautology_rate": tiers["tautology"] / steps,
        "skeleton_rate": tiers["skeleton"] / steps,
        "skeleton_cases": skeleton_cases,
        "spread_checked": len(spread),
        "spread_wide": len(wide),
        "spread_wide_samples": sorted(wide, key=lambda p: -p[1])[:10],
        "results": results,
    }


def render(agg: dict) -> str:
    t = agg["tiers"]
    lines = [f"用例 {agg['cases']} 条 / 步骤 {agg['steps']} 步", ""]
    lines.append(f"{'层':10s} {'步数':>5s} {'占比':>7s}  说明")
    for key, note in (("strong", "可判定条件（not-exists/text/selected/状态）"),
                      ("exists", "存在性断言，目标是别的控件"),
                      ("skeleton", "骨架断言（排序/筛选栏、搜索框、导航 tab）⇒ 只证明到了某页"),
                      ("tautology", "断言的就是本步刚点的控件 ⇒ 必然通过"),
                      ("none", "本步没有断言")):
        lines.append(f"{key:10s} {t[key]:>5d} {t[key] / agg['steps']:>6.0%}  {note}")
    lines.append("")
    lines.append(f"B2a  有断言步骤         = {agg['b2a']:.2f}")
    lines.append(f"B2b  原口径（exists 也算）= {agg['b2b_doc']:.2f}   ← 可被刷，别单独引用")
    lines.append(f"B2b' 强断言             = {agg['b2b_strong']:.2f}")
    lines.append(f"平凡断言占比            = {agg['tautology_rate']:.2f}")
    lines.append(f"骨架断言占比            = {agg['skeleton_rate']:.2f}"
                 "   ← 只能证明到了某页，证明不了动作效果")
    if agg["skeleton_cases"]:
        risky = [c for c in agg["skeleton_cases"] if c["after_input"]]
        lines.append("")
        lines.append(f"骨架断言清单（{len(agg['skeleton_cases'])} 条）：这些只能证明「到了某页」——"
                     "当**到达判据**用是对的，当**结果判据**用就会假绿")
        for item in agg["skeleton_cases"]:
            mark = "  ⚠️ 输入之后" if item["after_input"] else ""
            lines.append(f"   · {item['case'][:26]:28s} {str(item['step']):4s} "
                         f"{item['why']}{mark}")
        if risky:
            lines.append(f"   ⚠️ 其中 {len(risky)} 条在**输入之后**：它本该证明「搜到了什么」，"
                         "实际只证明了「到了结果页」")
    if agg["spread_checked"]:
        lines.append("")
        lines.append(f"页面区分度：{agg['spread_wide']}/{agg['spread_checked']} 条断言目标"
                     "出现在 ≥5 页（无法证明「到了某一页」）")
        for value, n in agg["spread_wide_samples"]:
            lines.append(f"   · {value[:36]:38s} 出现在 {n} 页")
    lines.append("")
    lines.append("逐条：")
    for r in agg["results"]:
        lines.append(f"  {r['case'][:30]:32s} {r['steps']:>2d}步  "
                     f"strong {r['tiers']['strong']} / exists {r['tiers']['exists']} / "
                     f"骨架 {r['tiers']['skeleton']} / "
                     f"平凡 {r['tiers']['tautology']} / 无 {r['tiers']['none']}")
    return "\n".join(lines)
