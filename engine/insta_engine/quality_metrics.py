"""质量指标卡（`mat metrics`）——把散落的指标收敛成一张有口径的卡（2026-10-26）。

## 定位（评估结论：要轻量的卡，不要"体系"）

每个指标都**从既有产物推导，零新采集**；缺产出的项如实标「未建立」而不是编 0。
每个数字带**可引用条件**（样本量/指纹/日期）——沿用 T34 纪律：数字与口径绑在一起。

| 组 | 指标 | 来源 |
|---|---|---|
| 产品质量 | 步骤成功率（评测基线）、崩溃任务占比、页面图谱覆盖 | eval baseline / runs / index+review |
| 工具能力 | 自愈恢复率（**步级 + 用例级**，G7 口径）、断言强度、生成首穿率 | 最近 runs 的 heal 字段 / cases / eval gen |
| 成本效率 | AI 额度（本月） | usage |

明确不做：趋势看板、历史库、告警。

## 目标值（2026-10-26 补）

指标卡从"记录"升级为"目标管理"：每指标带 target 与达标判定（✓/✗）。目标值来源
三种，**不拍脑袋**：① 历史最好水平（如执行链路基线中位 100%）；② 产品决策判据
（D35：一致率 ≥90%、unknown <10%）；③ 行业合理默认（自愈恢复率 ≥80%，标「暂定」
待实测校准）。没有依据的目标不设（页面覆盖是过程量、额度是成本不是质量）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .web_api.workspace import list_runs

#: 目标值表（按指标名前缀匹配）。met 判定用解析出的数值；无法解析（未建立）⇒ met=None。
#:
#: ⚠️ 自愈恢复率**只给步级定目标**：用例级（整条通过）在损伤场景实测 0/3，
#: 拿 ≥80% 去判它只会得到一个"未达标"的红字，而那个红字既不是缺陷也不可修
#: （单点自愈救不了级联失败）⇒ 用例级只报数、不设目标。
TARGETS: list[dict] = [
    {"match": "步骤成功率", "target": "≥ 95%", "kind": "pct", "floor": 0.95},
    {"match": "崩溃任务占比", "target": "= 0", "kind": "ratio_zero"},
    {"match": "自愈恢复率（步级", "target": "≥ 80%（暂定，待实测校准）",
     "kind": "pct", "floor": 0.80},
    {"match": "生成首穿率", "target": "≥ 70%（暂定）", "kind": "pct", "floor": 0.70},
]

#: 小样本下限：低于它只报数、不判达标。
#:
#: 2026-10-28 实测教训：卡上"自愈恢复率 100% [✓ 达标]"的分母是 **2 步**
#: （损伤场景 tx-f03/tx-f07），而同一批 run 的**用例级是 0/3**。n=2 打 ✓ 是把
#: "没数据"读成"达标"——目标值再合理也架不住分母太小。
MIN_SAMPLE = 10


def _met_for(name: str, value: str) -> bool | None:
    """按目标表判定达标；指标未建立（未建立/无数值）⇒ None（不判）。"""
    spec = next((t for t in TARGETS if t["match"] in name), None)
    if spec is None:
        return None
    m = re.search(r"([\d.]+)%", value or "")
    if spec["kind"] == "pct":
        return (float(m.group(1)) / 100) >= spec["floor"] if m else None
    if spec["kind"] == "ratio_zero":
        # "0/50" 形态（分子/分母）：达标只看**分子**——分母是样本量不是坏事件数
        nums = re.findall(r"\d+", value or "")
        return (int(nums[0]) == 0) if nums else None
    return None

def _eval_baselines(root: Path) -> list[dict]:
    """全部评测基线（版本化产物，进 git）。"""
    out = []
    for p in sorted((root / "artifacts" / "eval").glob("baseline-*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _heal_over_recent_runs(runs: list[dict], root: Path, limit: int = 30) -> dict:
    """最近 N 次 run 的自愈恢复率——**步级与用例级分开统计**，并按 via 分层。

    ## 为什么要两个口径（2026-10-28 实测）

    损伤场景（把 res-id 变异成 `_stale`）跑三条用例，结果是：

        步级  2/2 = 100%（tx-f03/tx-f07 的 s3 被 vision-llm 兜底点中、该步 passed）
        用例级 0/3 =   0%（三条全 failed——s3 兜底点中的是"搜索框"位置，但 App 没真
                        进结果页，s5 在错误的页面上断言，连环失败）

    同一个实验、两种口径差 100 个百分点。"步级与用例级是两个量，对外引用必须区分"
    这条纪律此前只在实验记录里，卡上只报步级、还打 ✓ 达标 ⇒ 这条口径补在这里。

    ## 为什么要按 via 分层

    三种兜底的语义完全不同：`index-hints`（图谱历史文案）/ `local-fuzzy`（零成本模糊）
    / `vision-llm`（计费）。更要紧的是 **`local-fuzzy` 在未损伤用例上命中 = 用例的
    定位值写得不精确**（实测 15 步全在 xhs：`description=消息` 精确不中、`description=收藏`
    同理）——那是**缺陷信号**，不是能力增益。混在一个分母里，修完用例缺陷反而让
    "自愈恢复率"下跌，方向就反了。
    """
    triggered = recovered = 0
    scanned = 0
    runs_with_heal = runs_passed = 0
    by_via: dict[str, dict] = {}
    for r in runs[:limit]:
        run_json = root / "artifacts" / (r.get("dir") or "") / "run.json"
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        scanned += 1
        run = data.get("run", {})
        run_healed = False
        for step in run.get("steps", []):
            heal = step.get("heal")
            if not heal:
                continue
            run_healed = True
            ok = step.get("status") == "passed"
            triggered += 1
            recovered += 1 if ok else 0
            via = str(heal.get("via") or "?")
            slot = by_via.setdefault(via, {"triggered": 0, "recovered": 0})
            slot["triggered"] += 1
            slot["recovered"] += 1 if ok else 0
        if run_healed:
            runs_with_heal += 1
            runs_passed += 1 if run.get("status") == "passed" else 0
    return {"triggered": triggered, "recovered": recovered,
            "rate": round(recovered / triggered, 4) if triggered else None,
            "runs_scanned": scanned,
            "runs_with_heal": runs_with_heal, "runs_passed": runs_passed,
            "case_rate": round(runs_passed / runs_with_heal, 4) if runs_with_heal else None,
            "by_via": by_via}


def _heal_caveat(heal: dict) -> str:
    """步级自愈那一行的口径串：触发/恢复/扫描 + via 分层 + local-fuzzy 的缺陷含义。"""
    if not heal["triggered"]:
        return "最近执行没有触发过兜底——需要定位器损伤场景"
    parts = [f"{via} {d['triggered']}"
             for via, d in sorted(heal["by_via"].items(), key=lambda kv: -kv[1]["triggered"])]
    caveat = (f"触发兜底 {heal['triggered']} 步 · 恢复 {heal['recovered']} 步 · "
              f"扫描 {heal['runs_scanned']} 次 run · via: " + " / ".join(parts))
    if (heal["by_via"].get("local-fuzzy") or {}).get("triggered"):
        caveat += ("；⚠️ local-fuzzy 命中 = 原定位值不精确（应回写精确值），"
                   "不是能力增益——它会随用例修好而减少")
    return caveat


def build_metrics(root: Path) -> dict:
    """汇总全部指标。**纯只读**；缺产物 ⇒ 指标值为 None + 明确的「未建立」。"""
    root = Path(root)
    runs = list_runs(root)
    crash_runs = [r for r in runs
                  if (r.get("failed_categories") or {}).get("app-crash")]
    baselines = _eval_baselines(root)
    heal = _heal_over_recent_runs(runs, root)

    products: list[dict] = []
    for b in baselines:
        products.append({
            "name": f"步骤成功率（{b.get('suite', '?')} 基线）",
            "value": f"{b['overall']['median']:.0%}" if b["overall"]["median"] == 1.0
                     else f"{b['overall']['median']:.1%}",
            "caveat": (f"{len(b.get('per_task', []))} 条 × {b.get('repeat')} 遍"
                       + (f" · 指纹 {'、'.join(b.get('app_fingerprints') or [])}"
                          if b.get("app_fingerprints") else "")
                       + (f" · 不稳 {len(b.get('unstable') or [])} 条"
                          if b.get("unstable") else "")),
        })
    if runs:
        products.append({
            "name": "崩溃任务占比（最近 50 次）",
            "value": f"{len(crash_runs)}/{len(runs)}",
            "caveat": "failed_categories 含 app-crash 的执行",
        })
    # 启动耗时（评测基线自带 perf 段，零成本消费）
    perf_line = next((f"{b['overall']['perf']['launch_ms_median']} ms（{b.get('suite', '?')} 基线）"
                      for b in baselines
                      if (b.get("overall") or {}).get("perf", {}).get("launch_ms_median")), None)
    if perf_line:
        products.append({"name": "启动耗时（评测基线中位）", "value": perf_line,
                         "caveat": "am start -W；compare 对劣化 >30% 守门"})
    indexes_total = 0
    covered = 0
    for idx in (root / "artifacts" / "page_index").glob("*.json"):
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pages = data.get("pages") or []
        indexes_total += len(pages)
        covered += sum(1 for p in pages if p.get("title_source") == "manual")
    products.append({
        "name": "页面图谱覆盖（人工复核/总页）",
        "value": f"{covered}/{indexes_total}",
        "caveat": "复核过的页面名会注入 AI 生成 prompt" if indexes_total else "还没有页面图谱",
    })

    tool: list[dict] = [{
        "name": "自愈恢复率（步级，G7 口径，最近 30 次）",
        "value": f"{heal['rate']:.0%}" if heal["rate"] is not None else "未建立",
        "caveat": _heal_caveat(heal),
        # 分母是"触发兜底的步数"，不是 run 数——样本太小就不该判达标（见 MIN_SAMPLE）
        "n": heal["triggered"], "min_n": MIN_SAMPLE,
    }]
    # 用例级（2026-10-28）：整条用例有没有救回来。与步级是两个量——损伤场景实测
    # 步级 2/2 而用例级 0/3。只在真的触发过兜底时才出这一行，否则卡片噪声。
    if heal["runs_with_heal"]:
        tool.append({
            "name": "自愈恢复率（用例级，最近 30 次）",
            "value": (f"{heal['runs_passed']}/{heal['runs_with_heal']} = "
                      f"{heal['case_rate']:.0%}"),
            "caveat": ("有兜底的用例中「整条通过」的比例；单点自愈救不了级联失败"
                       "（兜底点中了但页面没真切 ⇒ 后续步骤在错误页面上断言）"
                       "——与步级是两个量，引用时必须分开"),
            "n": heal["runs_with_heal"], "min_n": MIN_SAMPLE,
        })
    # 断言强度（2026-10-28）：工作区生成用例的四层分布——首穿率回答"能跑到"，
    # 这张卡回答"测得出"（exists 断言只能证明页面骨架在，A1 假绿教训）
    try:
        from .assertion_strength import aggregate as _as_agg
        from .assertion_strength import analyze as _as_analyze

        case_results = []
        for cp in sorted((root / "cases").glob("*.json")):
            if cp.name.endswith(".suite.json"):
                continue
            try:
                case_results.append(_as_analyze(json.loads(cp.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
        if case_results:
            agg = _as_agg(case_results)
            t = agg["tiers"]
            tool.append({
                "name": "断言强度（生成用例 strong 占比）",
                "value": f"{agg['b2b_strong']:.0%}",
                "caveat": (f"{agg['cases']} 条用例 · strong {t['strong']} / "
                           f"exists {t['exists']} / 骨架 {t['skeleton']} / "
                           f"平凡 {t['tautology']} / 无 {t['none']}"),
            })
        else:
            raise FileNotFoundError
    except (OSError, ValueError, FileNotFoundError):
        tool.append({"name": "断言强度（生成用例 strong 占比）", "value": "未建立",
                     "caveat": "工作区 cases/ 里还没有用例"})
    # 生成首穿率：mat eval first-run --save 的报告；k/n 与剔假红一并给
    fr_path = root / "artifacts" / "eval" / "first-run-report.json"
    try:
        fr = json.loads(fr_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fr = None
    if fr and fr.get("total"):
        fr_line = f"{fr['passed']}/{fr['total']} = {fr['passed'] / fr['total']:.0%}"
        caveats = []
        if fr.get("false_red"):
            caveats.append(f"剔除假红后 {fr.get('d3_ex_false_red', 0):.0%}")
        caveats.append(f"{len(fr.get('per_case') or [])} 条用例（k/n 口径）")
        caveats.append("前提:用例未经人工修改——首跑语义由跑批者保证")
        if fr.get("saved_at"):
            caveats.append(f"数据 {str(fr['saved_at'])[:10]}")
        tool.append({"name": "生成首穿率", "value": fr_line,
                     "caveat": "；".join(caveats)})
    else:
        tool.append({"name": "生成首穿率", "value": "未建立",
                     "caveat": "跑 `mat eval gen <产物目录> --save`（= eval first-run；"
                               "前提:用例未经人工修改）"})

    # AI 额度：与 state 同源（UsageStore 快照；server 模式的月度上限由官方代理判）
    usage_line, usage_caveat = "未建立", ""
    try:
        from .quota import UsageStore

        u = UsageStore().snapshot()
        if u.get("gen_limit") is not None:
            usage_line = f"{u.get('gen_count', 0):g}/{u['gen_limit']:g} 次（{u.get('month', '')}）"
            usage_caveat = "官方代理口径；自带 Key 不计"
        elif u.get("gen_count"):
            usage_line = f"{u.get('gen_count', 0):g} 次（{u.get('month', '')}）"
            usage_caveat = "自带 API Key——不设上限"
    except Exception:  # noqa: BLE001 —— 读不到按未建立
        pass
    cost: list[dict] = [{"name": "AI 生成额度（本月）", "value": usage_line,
                         "caveat": usage_caveat}]
    items = products + tool + cost
    for item in items:
        item["met"] = _met_for(item["name"], str(item["value"]))
        spec = next((t for t in TARGETS if t["match"] in item["name"]), None)
        item["target"] = spec["target"] if spec else None
        # 小样本保护：低于下限只报数不判达标，并把"为什么不判"写在卡上
        # （"没数据"被读成"达标"比不判更坏，见 MIN_SAMPLE）
        min_n = item.get("min_n")
        if min_n and (item.get("n") or 0) < min_n:
            item["met"] = None
            note = f"样本不足（n={item.get('n') or 0} < {min_n}）⇒ 不判达标"
            item["caveat"] = f"{item['caveat']}；{note}" if item["caveat"] else note
    return {"schema": "mat.metrics/1", "products": products, "tool": tool, "cost": cost}


def render_text(m: dict) -> str:
    """终端可读卡（每行：指标名 + 值 + 口径）。"""
    lines: list[str] = ["=== MAT 质量指标 ==="]
    for group, title in (("products", "产品质量"), ("tool", "工具能力"), ("cost", "成本效率")):
        lines.append(f"\n-- {title} --")
        for item in m[group]:
            mark = ""
            if item.get("met") is True:
                mark = " [✓ 达标]"
            elif item.get("met") is False:
                mark = " [✗ 未达标]"
            target = f"（目标 {item['target']}）" if item.get("target") else ""
            lines.append(f"  {item['name']}: {item['value']}{mark}{target}"
                         + (f"\n      （{item['caveat']}）" if item["caveat"] else ""))
    return "\n".join(lines)
