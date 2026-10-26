"""覆盖缺口分析（防漏闸）——「图谱里有、用例没碰」的控件/页面清单。

这是《AI 时代测试流程重构方案》里五道质量闸的第四道（防漏闸），也是阶段 1 验收判据
「图谱覆盖率」的度量工具。**本模块的价值主要不在代码（核心计算几十行），而在口径**——
口径错了，指标会失真到毫无意义，甚至驱动错误行为。以下是四条刻意定下的口径：

## 口径 ①：覆盖率单位是**控件值**，不是「页」

图谱的 `page_key` 不稳（同屏会记成多条；实测某 App 177 页按 `identities` 去重后仍有 170，
而真实功能页远少于此）。以「页」为分母会让覆盖率随记录噪声漂移 ⇒ **改用控件值**
（`text` / `desc` / `res-id`）作单位，至少是可去重、可解释的。

## 口径 ②：控件级分母 = 可点击 + 非动态文案 + 非内容快照 + 非不可见字符

探索抓到的图谱包含广告位、推荐流、动态入口、装饰控件。把它们全算进分母，覆盖率永远是
零点几个百分点（实测：全部控件口径 0.3%–10%，加过滤后 0.5%–37.5%），
「覆盖 ≥90%」这类判据**永远不可能通过** ⇒ 团队为了达标只会去灌水用例
（度量失真 ⇒ 行为扭曲——评测线总结过的「弱正向假信号」同源教训）。

判据**复用 `case_quality` 的单一来源**（R4 内容快照 / R3 不可见字符），不写第二份实现，
否则两处口径必然漂移（C9/C15 的教训）。`--all-controls` 可切回全量（**仅供诊断，不要用于验收**）。

⚠️ **已知局限（实测两条，都是"内容型文案漏进分母"）**：
1. R4 是「够长 + 含标点」⇒ **不含标点的短中文标题判不出来**（"2岁女童在景区被蟒蛇咬伤"）；
2. `is_dynamic_text` 是**锚定**模式（`^…$`）⇒ **复合日期串**（"2026年10月16日 12:30"）不在其列。

⇒ 控件级清单仍会漏进这类文案。**这正是口径 ① 把页面级放前面的原因**：
页面级只问"这页有没有被碰过"，与文案是不是内容无关。

## 口径 ③：页面级分两档——「碰过」≠「覆盖」

`page_coverage` 的页面级判定是**该页任一控件命中即算碰过**——用例点一下首页 tab，
首页就算覆盖。验收若只看「碰过」，会被"点一页就走"的用例过掉 ⇒ 页面级分两档：

- **碰过**：该页任一控件值被任一用例引用（下限，只证明"来过"）；
- **主操作覆盖**：该页**前 `MAIN_OPS_TOP_N` 个可操作控件**（口径 ② 过滤，按图谱记录序
  去重）被用例**命中 ≥ 半数**（上口径 ② 同源 ⇒ 广告位/动态文案不进这个分母）。
  该页可操作控件为 0 时永远不可能主操作覆盖。

覆盖率判据已冻结（2026-10-09 定稿）——改口径先改文档再改代码。

## 口径 ④：验收用「核心页面清单」收窄分母

`--core <文件>`（每行一个页名，支持 `#` 注释）只统计清单里的页面，页面级输出
**两档明细**（主操作覆盖 / 仅碰过 / 盲区）。分母可控、可解释、能直接驱动行动
——这是**建议作为验收判据**的口径；全图谱口径仅用于看大盘，不得用于验收。

## 用法

    mat coverage com.example.app                     # 默认口径，出缺口清单
    mat coverage com.example.app --core core.txt     # 只统计核心页面（推荐验收口径）
    mat coverage com.example.app --json              # 机读（给 CI / 看板）
    mat coverage com.example.app --min 0.9           # 低于阈值退出码 2（CI 卡口）

⚠️ **本命令只读**：不写图谱、不改用例、不调模型。
"""

from __future__ import annotations

import json
from pathlib import Path

#: 参与覆盖率统计的定位策略 → 图谱里的字段名
_KIND_FIELD = {"text": "text", "desc": "desc", "res-id": "res-id"}
#: 用例里的策略 → 上面的 kind（textContains 是包含语义，按 text 归集）
_STRATEGY_KIND = {"text": "text", "textContains": "text",
                  "description": "desc", "res-id": "res-id", "id": "res-id"}
#: 「主操作覆盖」档的分母上限：每页取口径 ② 过滤后的**前 N 个**可操作控件（记录序）。
#: N=8 是一屏主要可点控件的经验上限——再多说明该按功能拆页，而不是继续堆分母。
MAIN_OPS_TOP_N = 8


def case_targets(cases: list[dict]) -> dict[str, set[str]]:
    """收集用例里的定位值（含 fallback 链与断言 target），按 kind 归集。

    ⚠️ **断言也要算覆盖**：一条只做断言、不点击的用例，同样"覆盖"了那个控件。
    """
    out: dict[str, set[str]] = {k: set() for k in _KIND_FIELD}

    def walk(loc) -> None:
        while isinstance(loc, dict) and loc.get("strategy"):
            kind = _STRATEGY_KIND.get(str(loc.get("strategy")))
            value = (loc.get("value") or "").strip()
            if kind and value:
                out[kind].add(value)
            loc = loc.get("fallback")
        if isinstance(loc, list):  # fallback 也可能是列表
            for item in loc:
                walk(item)

    for case in cases or []:
        if not isinstance(case, dict):
            continue
        for step in case.get("steps") or []:
            walk(step.get("target"))
            for assertion in step.get("assertions") or []:
                walk((assertion or {}).get("target"))
    return out


def load_cases_for(package: str, root: Path | str | None = None) -> tuple[list[dict], list[str]]:
    """读 `<root>/cases/**/*.json` 里属于该包的用例；返回 (用例, 文件名列表)。

    坏文件**跳过而不是崩**：`cases/` 是用户可手改的目录（与 `heals.py` 对脏产物的态度一致）。
    ⚠️ rglob（2026-10-25 覆盖率口径修复）：用例会按 App 分子目录组织
    （某内容社区 App 的 6 条全在子目录 ⇒ 一层 glob 显示「用例 0 条」，口径直接失真）。
    套件文件（*.suite.json / 无 target.package）天然被包名过滤排除。
    """
    base = Path(root) if root is not None else Path("cases")
    cases: list[dict] = []
    names: list[str] = []
    for path in sorted(base.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if (data.get("target") or {}).get("package") != package:
            continue
        cases.append(data)
        names.append(path.name)
    return cases, names


def _is_actionable(control: dict, value: str, is_dynamic) -> bool:
    """默认口径：可点击 + 文案非动态 + 非内容快照 + 非不可见字符（见模块说明 口径 ②）。

    ⚠️ **「内容快照」必须排除，否则缺口清单会被新闻标题淹没**（2026-10-16 实测：某资讯 App
    的缺口前 12 条里 8 条是「2026年诺贝尔物理学奖揭晓」这类标题/单集名）。
    判据**复用 `case_quality` 的单一来源**（R4：够长 + 含标点；R3：私有区/零宽字符）——
    这里不写第二份实现，否则两处口径会漂移（C9/C15 的教训）。
    """
    from .case_quality import INVISIBLE_CHARS, SNAPSHOT_MIN_LEN, SNAPSHOT_PUNCT

    if not control.get("clickable") or len(value) <= 1:
        return False
    if is_dynamic(value) or INVISIBLE_CHARS.search(value):
        return False
    return not (len(value) >= SNAPSHOT_MIN_LEN and SNAPSHOT_PUNCT.search(value))


def graph_controls(pages: list[dict], all_controls: bool = False) -> dict[tuple[str, str], dict]:
    """图谱 → `{(kind, value): {"clickable": bool, "pages": set[str]}}`（按值去重）。"""
    from .page_index import is_dynamic_text

    out: dict[tuple[str, str], dict] = {}
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        label = (page.get("title") or "").strip() or (page.get("page_key") or "")[:8]
        for control in page.get("controls") or []:
            if not isinstance(control, dict):
                continue
            for kind, field in _KIND_FIELD.items():
                value = (control.get(field) or "").strip()
                if not value:
                    continue
                if not all_controls and not _is_actionable(control, value, is_dynamic_text):
                    continue
                entry = out.setdefault((kind, value), {"clickable": bool(control.get("clickable")),
                                                       "pages": set()})
                entry["pages"].add(label)
    return out


def page_coverage(pages: list[dict], cases: list[dict]) -> tuple[list[dict], list[dict]]:
    """**页面级**覆盖，两档口径（返回 (碰过, 盲区)，条目带主操作覆盖字段）。

    - **碰过**：该页任一控件值被任一用例引用（`matched`）——只证明"来过"；
    - **主操作覆盖**：该页前 `MAIN_OPS_TOP_N` 个可操作控件（口径 ② 过滤，记录序去重）
      命中 ≥ 半数（`main_ops_covered`）——这才是"这页的功能被测过"。

    条目字段：`title` / `controls`（全部控件数）/ `matched` / `samples` / `depth` /
    `main_ops_total` / `main_ops_hit` / `main_ops_covered`。
    见模块说明 口径 ③：「碰过 ≠ 覆盖」——验收若只看碰过，会被"点一页就走"过掉
    （实测：某大型本地生活 App 页面级碰过 77.4%，控件级覆盖率只有 0.5%。）

    ⚠️ 页面按 `title` 去重（`page_key` 不稳，同屏会记多条）；无 title 的页用 page_key 前 8 位。
    """
    from .page_index import is_dynamic_text

    targets = case_targets(cases)
    hit_values: set[str] = set()
    for kind in _KIND_FIELD:
        hit_values |= targets[kind]

    by_title: dict[str, dict] = {}
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        title = (page.get("title") or "").strip() or (page.get("page_key") or "")[:8]
        entry = by_title.setdefault(title, {"title": title, "controls": 0, "matched": 0,
                                           "samples": [], "depth": page.get("depth"),
                                           "_ops": []})
        for control in page.get("controls") or []:
            if not isinstance(control, dict):
                continue
            entry["controls"] += 1
            pairs = [(kind, (control.get(field) or "").strip())
                     for kind, field in _KIND_FIELD.items()]
            matched = next((v for _k, v in pairs if v and v in hit_values), None)
            if matched:
                entry["matched"] += 1
                if len(entry["samples"]) < 3:
                    entry["samples"].append(matched)
            # 主操作候选：口径 ② 过滤（与控件级分母同源）；一个控件记一组定位值，
            # 同页重复记录去重，按图谱记录序只留前 MAIN_OPS_TOP_N 个
            if len(entry["_ops"]) < MAIN_OPS_TOP_N:
                op_pairs = frozenset((kind, value) for kind, value in pairs
                                     if value and _is_actionable(control, value,
                                                                 is_dynamic_text))
                if op_pairs and op_pairs not in entry["_ops"]:
                    entry["_ops"].append(op_pairs)

    for entry in by_title.values():
        ops = entry.pop("_ops")
        entry["main_ops_total"] = len(ops)
        entry["main_ops_hit"] = sum(1 for op in ops
                                    if any(value in targets[kind] for kind, value in op))
        # 命中 ≥ 半数才算覆盖；可操作控件为 0 的页永远不可能主操作覆盖
        entry["main_ops_covered"] = bool(ops) and entry["main_ops_hit"] * 2 >= len(ops)

    covered = [e for e in by_title.values() if e["matched"]]
    gap = [e for e in by_title.values() if not e["matched"]]
    gap.sort(key=lambda e: (e["depth"] if isinstance(e["depth"], int) else 99, e["title"]))
    return covered, gap


def analyze(pages: list[dict], cases: list[dict], core_pages: list[str] | None = None,
            all_controls: bool = False) -> dict:
    """覆盖率与缺口清单（**纯函数**，不读盘、不调模型，便于单测）。

    返回：
      - `pages_covered` / `pages_gap`：**页面级**覆盖（主口径，见 `page_coverage`，
        条目含「主操作覆盖」两档字段）
      - `pages_main_covered`：主操作覆盖的页数（⊆ pages_covered）
      - `denominator` / `covered` / `rate`：控件级分母、已覆盖数、覆盖率
      - `gap`：未覆盖**控件**清单 `[{"kind","value","clickable","pages"}]`（可点击优先）
      - `covered_extra`：用例引用了、但图谱里没有的定位值（**幻觉候选**，另一类信号）
      - `controls_all`：全量口径分母（对照用）；`pages_total` / `cases` / `core_pages`
    """

    targets = case_targets(cases)
    wanted = {p.strip() for p in (core_pages or []) if p.strip()}
    scoped = pages
    if wanted:
        scoped = [p for p in pages or []
                  if isinstance(p, dict) and ((p.get("title") or "").strip() in wanted)]
    controls = graph_controls(scoped, all_controls=all_controls)
    all_controls_map = controls if all_controls else graph_controls(scoped, all_controls=True)

    gap = []
    covered = 0
    for (kind, value), meta in controls.items():
        if value in targets[kind]:
            covered += 1
            continue
        gap.append({"kind": kind, "value": value,
                    "clickable": meta["clickable"],
                    "pages": sorted(meta["pages"])[:3]})
    # 可点击优先、再按出现页数少→多（少页的更可能是具体功能入口，不是通用导航）
    gap.sort(key=lambda g: (not g["clickable"], len(g["pages"]), g["value"]))

    covered_extra = sorted(
        {v for kind in _KIND_FIELD for v in targets[kind]
         if v and (kind, v) not in all_controls_map})
    denominator = len(controls)
    pages_covered, pages_gap = page_coverage(scoped, cases)
    return {
        "denominator": denominator,
        "covered": covered,
        "rate": (covered / denominator) if denominator else None,
        "gap": gap,
        "pages_covered": pages_covered,
        "pages_gap": pages_gap,
        "pages_main_covered": sum(1 for e in pages_covered if e.get("main_ops_covered")),
        "covered_extra": covered_extra[:50],
        "controls_all": len(all_controls_map),
        "pages_total": len(scoped or []),
        "cases": len(cases or []),
        "core_pages": sorted(wanted),
    }


def render_text(pkg: str, result: dict, limit: int = 30) -> str:
    """人读输出：**先给页面级盲区**（可行动），再给控件级缺口（细节）。"""
    lines = [f"覆盖缺口分析：{pkg}"]
    scope = "核心页面" if result["core_pages"] else "全图谱"
    lines.append(f"  统计范围: {scope}"
                 + (f"（{len(result['core_pages'])} 页）" if result["core_pages"] else "")
                 + f" · 用例 {result['cases']} 条 · 图谱页 {result['pages_total']}")

    covered_pages, gap_pages = result["pages_covered"], result["pages_gap"]
    total_pages = len(covered_pages) + len(gap_pages)
    page_rate = (len(covered_pages) / total_pages) if total_pages else None
    lines.append(f"  页面级: {len(covered_pages)}/{total_pages} 页被用例碰过"
                 f"（{'—' if page_rate is None else f'{page_rate:.1%}'}）"
                 f" ⇒ **盲区 {len(gap_pages)} 页**")
    lines.append(f"  主操作覆盖: {result.get('pages_main_covered', 0)}/{total_pages} 页"
                 f"（每页前 {MAIN_OPS_TOP_N} 个可操作控件命中 ≥ 半数）")
    if result["core_pages"]:
        # 核心页验收视图：每页两档状态，替代通用盲区清单（清单本身就该很短）
        lines.append("  核心页面两档明细（主操作覆盖 > 仅碰过 > 盲区）：")
        merged = sorted(covered_pages + gap_pages,
                        key=lambda e: (e["depth"] if isinstance(e["depth"], int) else 99,
                                       e["title"]))
        for item in merged[:limit]:
            hit, total = item.get("main_ops_hit", 0), item.get("main_ops_total", 0)
            if item.get("main_ops_covered"):
                tier = f"主操作覆盖 {hit}/{total}"
            elif item.get("matched"):
                tier = f"仅碰过（主操作 {hit}/{total}）"
            else:
                tier = "盲区"
            lines.append(f"    [{tier}] {item['title'][:34]}")
        if len(merged) > limit:
            lines.append(f"    …… 另 {len(merged) - limit} 页（--json 拿全量）")
        # 反灌水：清单里的页若图谱没有，不会出现在任何档里——必须点名，否则
        # 「把没建过图谱的页写进核心清单」就能把验收做漂亮
        missing = [p for p in result["core_pages"]
                   if p not in {e["title"] for e in merged}]
        if missing:
            lines.append(f"  ⚠️ 核心清单里 {len(missing)} 页在图谱中不存在"
                         f"（先 mat index / mat explore 补图谱，验收时不得忽略）："
                         f"{'、'.join(missing[:5])}")
    else:
        for item in gap_pages[:limit]:
            depth = f"L{item['depth']}" if isinstance(item["depth"], int) else "L?"
            lines.append(f"    {depth:3s} {item['title'][:34]:36s}"
                         f"（{item['controls']} 控件，无任何用例触及）")
        if len(gap_pages) > limit:
            lines.append(f"    …… 另 {len(gap_pages) - limit} 页（--json 拿全量）")

    rate = result["rate"]
    rate_txt = "—" if rate is None else f"{rate:.1%}"
    lines.append(f"  控件级: 分母 {result['denominator']}"
                 f"（全量口径对照 {result['controls_all']}）· 已覆盖 {result['covered']}"
                 f" ⇒ 覆盖率 {rate_txt}")
    gap = result["gap"]
    lines.append(f"  未覆盖控件: {len(gap)} 个" + (f"，列前 {limit} 个：" if gap else ""))
    for item in gap[:limit]:
        mark = "可点击" if item["clickable"] else "不可点"
        pages = "/".join(item["pages"]) or "—"
        lines.append(f"    [{item['kind']:6s}] {item['value'][:40]:42s} {mark} · 出现于 {pages}")
    if len(gap) > limit:
        lines.append(f"    …… 另 {len(gap) - limit} 个（--json 拿全量）")
    if result["covered_extra"]:
        extra = result["covered_extra"]
        lines.append(f"  ⚠️ 用例引用了但图谱里没有的定位值 {len(extra)} 个（幻觉/改版候选）：")
        for value in extra[:10]:
            lines.append(f"    {value[:60]}")
    return "\n".join(lines)


def read_core_pages(path: Path | str) -> list[str]:
    """读核心页面清单（每行一个页名，`#` 注释与空行忽略）。"""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]
