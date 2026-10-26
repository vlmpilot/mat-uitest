#!/usr/bin/env python3
"""从**真机失败现场**给断言提候选，并（可选）写回用例。

## 为什么需要它（2026-10-24）

A2（断言与导航耦合）/A3（内容快照）这类缺陷，**正确的断言只能从那一屏推出来**：

    tx-news「首页打开早晚报」= `tap 早晚报` + `assert text-contains 早晚报`
    → 点完入口不在了 ⇒ 断言恒假；正确答案是"到了早晚报页"，而那一页上有「订阅」「热点聚焦」。

`case_audit.rewrite_coupled_assertions`（R25）只能改**图谱里有落点页**的那一半，
覆盖率实测 **1/10** —— 因为「早晚报」这类入口压根没被探索到——图谱没探索到入口页时改写帮不上。
真机跑一次反而**一定有**那一屏，所以这条路不依赖图谱覆盖率。

前提是那一屏的**控件树**得留档：`Runner` 从 2026-10-24 起把失败步的动作前后两屏摘要写进
`screens/step_XX_<id>_aN.json`（截图里没有 res-id，机器用不了）。

## 判据（"哪个控件能证明到了目标页"）

动作**后**新出现、且稳定的那一个。四条硬过滤 + 打分：

| 过滤 | 为什么 |
|---|---|
| 动作**前**就有 | 那是 tab/底栏一类常驻控件 ⇒ 断言它恒真（R23/R24 的坑）|
| 长文案 / 含标点 / 动态文本 | 信息流标题，跨运行必变（R4）|
| 不可见字符 / 图标字形 | 定位链必然全不命中（R3）|
| 就是刚点的那个控件 | 点完还在才可能"新"，但断言它等于没断（R23）|

打分：有 res-id > 短文案 > 唯一 > 靠上（标题/页头通常在上半屏）。

## 用法

    # 只看建议（默认，不写文件）
    python scripts/fix_assertions_from_run.py /tmp/mat-rerun-1023/out/*/run.json

    # 落成修复后的用例（保留原文件）
    python scripts/fix_assertions_from_run.py /tmp/mat-rerun-1023/out/*/run.json --out /tmp/fixed

⚠️ **这是"提候选"不是"自动改"**：它不知道业务语义（「订阅」能不能证明到了早晚报页，
只有人知道）。所以默认只打印，`--out` 也要人工看一眼再跑。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from insta_engine.case_quality import (
    INVISIBLE_CHARS,
    SNAPSHOT_MIN_LEN,
    SNAPSHOT_PUNCT,
    STATE_ASSERTION_TYPES,
)
from insta_engine.page_index import is_content_label, is_dynamic_text

#: 文案最大长度：再长就大概率是内容（标题/摘要），跨运行会变。
MAX_LABEL_LEN = 12
#: 打分门槛：低于它的不推荐（宁可说"给不出候选"，也别推个垃圾）。
MIN_SCORE = 3


def _label_of(control: dict) -> str:
    return str(control.get("text") or control.get("desc") or "").strip()


def _key_of(control: dict) -> tuple[str, str]:
    """控件身份：优先 res-id（文案会轮换），没有 rid 才用文案。"""
    rid = str(control.get("res-id") or "").strip()
    return (rid, "") if rid else ("", _label_of(control))


def _stable_label(text: str) -> bool:
    """这条文案能不能当断言目标（跨运行稳定、定位链命中得了）。"""
    if not text or len(text) > MAX_LABEL_LEN:
        return False
    if INVISIBLE_CHARS.search(text):
        return False
    if is_dynamic_text(text) or is_content_label(text):
        return False
    return not (len(text) >= SNAPSHOT_MIN_LEN and SNAPSHOT_PUNCT.search(text))


def candidates(screen: dict, tapped_label: str) -> list[dict]:
    """动作后**新出现**的稳定控件，按"适合当到达断言"打分排序。"""
    before_keys = {_key_of(c) for c in screen.get("before") or []}
    before_labels = {_label_of(c) for c in screen.get("before") or [] if _label_of(c)}
    seen: dict[str, int] = {}
    for control in screen.get("after") or []:
        label = _label_of(control)
        if label:
            seen[label] = seen.get(label, 0) + 1

    out: list[dict] = []
    for order, control in enumerate(screen.get("after") or []):
        label = _label_of(control)
        rid = str(control.get("res-id") or "").strip()
        if not label and not rid:
            continue
        if _key_of(control) in before_keys:
            continue                                  # 动作前就有 ⇒ 常驻控件，断言它恒真
        if label and label in before_labels:
            continue                                  # 同文案换了 rid 也算"本来就在"
        if label and label == tapped_label:
            continue                                  # 就是刚点的那个
        if label and not _stable_label(label):
            continue                                  # 内容快照 / 不可见 / 太长
        score = 0
        if rid:
            score += 3
        if label:
            score += 2 if len(label) <= 6 else 1
        if label and seen.get(label) == 1:
            score += 1
        if score < MIN_SCORE:
            continue
        out.append({
            "score": score,
            "order": order,
            "target": ({"strategy": "res-id", "value": rid} if rid
                       else {"strategy": "text", "value": label}),
            "label": label,
            "rid": rid,
            "clickable": bool(control.get("clickable")),
        })
    out.sort(key=lambda c: (-c["score"], c["order"]))     # 同分按屏上位置（标题/页头在上）
    return out


def _coupled(step: dict) -> bool:
    """这一步的断言是不是"点完又断言它存在"（A2 形态）。"""
    target = step.get("target") or {}
    value = str(target.get("value") or "").strip()
    if not value:
        return False
    for assertion in step.get("assertions") or []:
        at = assertion.get("target") or {}
        if (str(at.get("value") or "").strip() == value
                and str(assertion.get("type") or "") not in (*STATE_ASSERTION_TYPES, "not-exists")):
            return True
    return False


def propose(run_json: Path) -> list[dict]:
    """一条跑测里**每个失败步**的建议。返回 [{step_id, current, proposal, alternatives}]。"""
    doc = json.loads(run_json.read_text(encoding="utf-8"))
    case = doc.get("case") or {}
    run = doc.get("run") or {}
    steps = {s.get("id"): s for s in case.get("steps") or []}
    base = run_json.parent

    out: list[dict] = []
    for result in run.get("steps") or []:
        screen_rel = result.get("screen")
        if result.get("status") != "failed" or not screen_rel:
            continue
        screen = json.loads((base / screen_rel).read_text(encoding="utf-8"))
        step = steps.get(result.get("step_id")) or {}
        tapped = str((screen.get("target") or {}).get("value") or "")
        ranked = candidates(screen, tapped)
        out.append({
            "run": str(run_json),
            "case_id": run.get("case_id"),
            "step_id": result.get("step_id"),
            "action": screen.get("action"),
            "tapped": tapped,
            "coupled": _coupled(step),
            "page_changed": screen.get("before_page") != screen.get("after_page"),
            "current": [a for a in step.get("assertions") or []],
            "proposal": ranked[0] if ranked else None,
            "alternatives": ranked[1:4],
            "screen": screen_rel,
            "screen_doc": screen,
        })
    return out


def _why_no_candidate(proposal: dict) -> str:
    """给不出候选时，说清**为什么** —— 别让人猜（三种原因处置完全不同）。

    实测（2026-10-24）：tx-news「早晚报」那一跳动作后 `after` 是空的，起初只说
    "整屏都是内容标题"，其实是**那一屏不属于被测 App**（前台换了包）。说错原因比不说更坏。
    """
    screen = proposal["screen_doc"]
    after = screen.get("after") or []
    if after:
        return "动作后没有「新出现且稳定」的控件（多半整屏都是内容标题，只能人工指定）"
    foreground = str(screen.get("foreground") or "")
    packages = screen.get("after_packages") or []
    pkg = str(screen.get("package") or "")
    if foreground and foreground != pkg:
        return (f"动作后**前台是 `{foreground}`，不是被测 App**（`{pkg}`）"
                "—— 这一步跳出了 App / 落到了外部页面，断言该写那个页面的控件")
    if pkg and pkg in packages:
        # 2026-10-24 实测（tx-news「早晚报」）：整屏**看得见**（订阅/热点聚焦/正文都在），
        # 但控件树里一个可读节点都没有 —— Hippy/WebView 自绘，a11y 不暴露。
        # 这不是"没找对控件"，是**这一类页面根本写不出 a11y 断言**。
        return ("这一屏**是被测 App 的，但控件树里没有任何可读节点**"
                f"（{screen.get('after_nodes')} 个节点，0 个有 text/desc）—— "
                "Hippy/WebView 自绘页，a11y 不暴露 ⇒ 到达断言写不出来；"
                "只能改测别的页面，或等视觉断言（视觉当前只可操作、不可断言）")
    if packages:
        return ("动作后这一屏没有被测 App 的控件（屏上只有 "
                + "、".join(f"`{p}`" for p in packages) + " 的节点）")
    if not screen.get("after_page"):
        return "动作后**没拿到控件树**（dump 失败/空屏），无法给候选"
    return "动作后控件树里没有被测 App 的节点（可能是系统弹窗/输入法盖住）"


def _describe(proposal: dict) -> str:
    def fmt(a) -> str:
        target = (a or {}).get("target") or {}
        return f"{a.get('type')}({target.get('strategy')}={target.get('value')})"

    if proposal["proposal"] is None:
        return f"    ⚠ 给不出候选：{_why_no_candidate(proposal)}"
    head = f"    → 建议断言 {fmt({'type': 'exists', 'target': proposal['proposal']['target']})}"
    lines = [head]
    if proposal["alternatives"]:
        lines.append("      备选：" + "、".join(
            fmt({"type": "exists", "target": a["target"]}) for a in proposal["alternatives"]))
    lines.append("      ⚠ 需人工确认：这条断言要**能证明到了目标页**，"
                 "不只是「屏上有这个控件」")
    return "\n".join(lines)


def apply_fix(run_json: Path, proposal: dict, out_dir: Path) -> Path | None:
    """把建议写回用例（保留原文件，写到 `out_dir`）。返回新文件路径。"""
    doc = json.loads(run_json.read_text(encoding="utf-8"))
    case = doc.get("case") or {}
    step = next((s for s in case.get("steps") or [] if s.get("id") == proposal["step_id"]), None)
    if step is None or proposal["proposal"] is None:
        return None
    new_target = dict(proposal["proposal"]["target"])
    assertions = [a for a in step.get("assertions") or []]
    replaced = False
    for assertion in assertions:
        if proposal["coupled"] and \
                str((assertion.get("target") or {}).get("value") or "").strip() == proposal["tapped"]:
            assertion["type"] = "exists"
            assertion["target"] = new_target
            assertion.pop("expected", None)
            replaced = True
            break
    if not replaced:                                  # 非 A2 形态：换掉最后一条断言的目标
        if assertions:
            assertions[-1]["type"] = "exists"
            assertions[-1]["target"] = new_target
            assertions[-1].pop("expected", None)
            replaced = True
        else:
            step.setdefault("assertions", []).append({"type": "exists", "target": new_target})
            replaced = True
    note = (f"[R2] 断言已按**真机失败现场**改写为 exists("
            f"{new_target['strategy']}={new_target['value']})——原断言在本步之后不成立；"
            f"证据：{proposal['screen']}")
    case.setdefault("meta", {}).setdefault("quality_issues", []).append(note)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{case.get('caseId') or run_json.parent.name}.json"
    out.write_text(json.dumps(case, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="从真机失败现场给断言提候选（默认只看不改）")
    ap.add_argument("runs", nargs="+", help="run.json 路径（支持 shell 通配）")
    ap.add_argument("--out", help="把修复后的用例写到这个目录（默认不写）")
    ap.add_argument("--json", help="把建议汇总写成 JSON")
    args = ap.parse_args()

    rows: list[dict] = []
    for raw in args.runs:
        path = Path(raw)
        if not path.exists():
            print(f"⚠ 跳过（不存在）：{path}", file=sys.stderr)
            continue
        for proposal in propose(path):
            rows.append(proposal)
            flag = "A2" if proposal["coupled"] else "  "
            print(f"{flag} {proposal['case_id']} · {proposal['step_id']} "
                  f"{proposal['action']}({proposal['tapped']})  当前断言 "
                  f"{len(proposal['current'])} 条")
            print(_describe(proposal))
            if args.out:
                saved = apply_fix(path, proposal, Path(args.out))
                if saved:
                    print(f"      ✏️ 已写入 {saved}")

    if not rows:
        print("没有可分析的失败步（需要 run.json 里带 `screen` 证据；"
              "2026-10-24 之前的跑测没有这份证据，重跑一次即可）")
    else:
        ok = sum(1 for r in rows if r["proposal"])
        print(f"\n{len(rows)} 个失败步，{ok} 个给得出候选"
              f"（{ok / len(rows):.0%}）。⚠️ 候选只是**素材**：断言还得能证明「到了目标页」。")
    if args.json:
        slim = [{k: v for k, v in row.items() if k != "screen_doc"} for row in rows]
        Path(args.json).write_text(json.dumps(slim, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
