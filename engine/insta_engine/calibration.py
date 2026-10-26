"""报告校准：把「失败归类判错了」这件事**交给人来回答**，并**记住**（下次不再犯）。

## 为什么需要它

失败归类是**运行时一次定死**的：`runner.classify_failure()` 按规则给出五分类
（`app-crash` / `env-failure` / `assert-fail` / `locate-fail` / `unknown`），写进 `run.json`，
报告只是把它渲染出来。`mat diagnose` 会再叠一层 LLM 四分类（责任归属），
并且**在没把握时明确标 `need_human`**。

⚠️ 但此前**没有任何通道让人回答那个 `need_human`**：
报告是只读 HTML，`run.json` 里的分类没有改判入口，`classify_failure` 的规则写在代码里。
⇒ 人知道"这是环境问题，不是产品缺陷"，但**说不出来**；而**下一个 run 会一模一样地再判错**。

**真实会判错的例子**（这类才是本模块存在的理由）：
探索撞上风控页 ⇒ 找不到控件 ⇒ 内置规则判成 `assert-fail`（"断言失败"），
而真相是 `env-failure`（被风控挡住了，不是产品缺陷）。人一眼能看出，机器看不出
——`classify_failure` 只看错误串与 logcat 里的几个标记。

⚠️ 顺带说明：**"崩溃被误判成 assert-fail"那个老问题（台账 D36）已经修好了**
（修的是 logcat 窗口，内置规则现在能认出 `FATAL EXCEPTION`）⇒ 本模块的价值在**其它**
误判上，别拿已修的例子当卖点。

## 两个层级（别混：它们的产出完全不同）

| 层级 | 改什么 | 产出 | 效果 |
|---|---|---|---|
| **① 失败归类** | `run.json` 的 `step.category` + 规则表 | 报告里分类正确、标「人工」 | **避免误提单**；⚠️ **用例仍然是失败的** |
| **② 断言校准** | `cases/*.json` 的 `assertion.expected` / `assertion.type` | `cases/<名>-calibrated.json` | **结果本身变对** ⇒ 下次真的通过 |

⚠️ **只做 ① 是不够的**：分类只是**标签**，把它改对，用例**照样失败**。
真正"校准测试结果"（让通过/失败正确）必须落到 **②**。
② 的资料来自 `runner._check_assertions` 抛的错误串（`期望 X，实际 Y`）——
`StepResult` 没有单独的断言字段，**不抠出来就没法给人看**。

## 闭环三步（与 `mat review` 同构）

1. `mat calibrate <产物目录>` —— 渲染一张**自包含校准页**：逐失败步骤列出
   「规则分类 + LLM 归因（含证据/置信度/need_human）+ 错误信息 + logcat 摘录」，
   可**改判分类**、可**写规则条件**，抠得到「期望/实际」时还会给**断言校准**选项。
2. `mat calibrate <产物目录> --apply <导出的 JSON>` —— ① 改 `run.json` 的分类并**重渲染报告**；
   ② 带条件的条目**落成规则**到 `<工作区>/calibration/rules.json`；
   ③ 断言校准写 `cases/<名>-calibrated.json`（**不覆盖原用例**，照 `mat heal` 的先例）。
3. 之后每次 `mat run`，`classify_failure` 会**先查规则表** ⇒ 同类失败自动判对。

## 刻意的取舍

- **规则必须带内容条件**（`error_contains` 或 `logcat_contains`）：只写「所有 assert-fail
  都改成 app-crash」会**静默地把全部断言失败改判成崩溃**，那比不校准更危险 ⇒ 拒绝这种规则。
- **不做自动规则推断**：从单个样本猜"特征"极易过拟合；条件由人显式给出，人负责。
- **改判留痕**：`run.json` 里写 `category_source="human"` + 原始分类，报告上标出来。
- **不改原用例**：断言校准写 `-calibrated.json` 新文件（`heal` 的既定约定）——
  自动改用户的源文件不可逆，风险太高。
- **不静默**：规则一条都没命中、或找不到对应用例文件时，都会明确报出来。
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

RULES_DIR = Path("calibration")
RULES_FILE = "rules.json"
RULES_VERSION = 1

#: 六分类（规则口径）—— 人工改判的目标必须落在这里，报告与 `run.json` 才认得；
#: `api` 为 P1-1 新增（接口断言失败，与 runner.CATEGORIES 同源）
CATEGORIES = ("app-crash", "env-failure", "assert-fail", "locate-fail", "api", "unknown")
CATEGORY_LABELS = {
    "app-crash": "产品 bug（崩溃）",
    "env-failure": "环境问题",
    "assert-fail": "断言失败",
    "locate-fail": "定位失败",
    "api": "接口断言失败",
    "unknown": "待归因",
}
#: 四分类（`mat diagnose` 的责任归属口径）—— 只作展示，供人判断时参考
ATTR_LABELS = {
    "app-bug": "真 bug（App 缺陷）",
    "ui-change": "UI 改版（定位器过期）",
    "env-flake": "环境抖动",
    "precondition": "前置条件缺失",
}

_rules_cache: dict[str, tuple[float, list[dict]]] = {}

#: 断言失败的错误串形状（`runner._check_assertions` 抛的）：
#: `断言失败: 期望文本 '热搜'，实际 '热榜'` / `断言失败: 期望包含 '订单'，实际 '...'`
_ASSERT_RE = re.compile(r"断言失败:\s*期望(文本|包含)\s*(?P<exp>.+?)，实际\s*(?P<act>.+)$")


def parse_assertion_error(error: str) -> dict | None:
    """从失败步骤的错误串里抠出「期望 / 实际」（抠不到返回 None）。

    ⚠️ **为什么这是"校准测试结果"的关键**：报告里的**分类**只是标签 —— 把它改对，
    用例**仍然是失败的**，结果仍然是错的。真正让结果变对的是**改用例的断言**
    （`expected` 过期了 / 断言类型太严）。而断言失败的**实际值**只存在于这条错误串里
    （`StepResult` 没有单独的断言字段）⇒ 不抠出来就没法给人看。
    """
    m = _ASSERT_RE.search(error or "")
    if not m:
        return None

    def unquote(text: str) -> str:
        t = text.strip()
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
            return t[1:-1]
        return t

    return {"kind": "equals" if m.group(1) == "文本" else "contains",
            "expected": unquote(m.group("exp")), "actual": unquote(m.group("act"))}


def find_case_file(root: Path, case_id: str) -> Path | None:
    """在 `<root>/cases/*.json` 里按 `caseId` 找用例文件（找不到返回 None）。

    断言校准要**改用例**，而 `run.json` 只记了 `case_id`（不记文件路径）⇒ 得反查。
    同名多份时取第一份并让调用方报出来（不静默挑一个）。
    """
    if not case_id:
        return None
    cases_dir = root / "cases"
    if not cases_dir.exists():
        return None
    for path in sorted(cases_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("caseId") == case_id:
            return path
    return None


def patch_case(case: dict, fixes: dict[str, dict]) -> tuple[dict, list[str]]:
    """把「断言校准」应用到用例 dict（**改内存副本**，由调用方落盘）。

    `fixes` = `{step_id: {"action": "adopt-actual"|"loosen", "expected": ..., "actual": ...}}`
    - `adopt-actual`：把 `expected` 改成**实际值**（预期过期了）
    - `loosen`：把 `text-equals` 放宽成 `text-contains`（断言太严）

    返回 (新用例, 变更说明)。⚠️ **不改原对象** —— 照 `heals.apply_heals` 的先例，
    调用方写**新文件**，原用例保持不动。
    """
    new_case = json.loads(json.dumps(case, ensure_ascii=False))   # 深拷贝
    notes: list[str] = []
    by_id = {str(s.get("id")): s for s in new_case.get("steps") or []}
    for step_id, fix in fixes.items():
        step = by_id.get(str(step_id))
        if step is None:
            notes.append(f"{step_id}: 用例里找不到该步骤，跳过")
            continue
        assertions = step.get("assertions") or []
        action = str(fix.get("action") or "")
        if action not in ("adopt-actual", "loosen"):
            notes.append(f"{step_id}: 未知的校准动作 {action!r}，跳过")
            continue
        target_expected = str(fix.get("expected") or "")
        hit = None
        for a in assertions:
            if str(a.get("expected") or "") == target_expected:
                hit = a
                break
        if hit is None and len(assertions) == 1:
            hit = assertions[0]              # 只有一个断言时容错（expected 可能已被改过）
        if hit is None:
            notes.append(f"{step_id}: 找不到期望为 {target_expected!r} 的断言，跳过")
            continue
        if action == "adopt-actual":
            hit["expected"] = str(fix.get("actual") or "")
            notes.append(f"{step_id}: 期望 {target_expected!r} → {hit['expected']!r}（采纳实际值）")
        else:
            if str(hit.get("type")) == "text-equals":
                hit["type"] = "text-contains"
                notes.append(f"{step_id}: 断言 text-equals → text-contains（放宽为包含）")
            else:
                notes.append(f"{step_id}: 断言类型是 {hit.get('type')!r}，无需放宽，跳过")
    return new_case, notes


def rules_path(root: Path) -> Path:
    return root / RULES_DIR / RULES_FILE


def calibration_path(run_dir: Path) -> Path:
    return run_dir / "calibration.json"


def html_path(run_dir: Path) -> Path:
    return run_dir / "calibration.html"


# -- 规则表 -----------------------------------------------------------------------

def load_rules(root: Path) -> list[dict]:
    """读 `<root>/calibration/rules.json`。**带 mtime 缓存**（每次失败都读盘太浪费）。

    读不动就返回空表 —— 校准规则坏掉**不该让执行挂掉**（与 diagnose 的兜底同一条原则）。
    """
    path = rules_path(root)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    key = str(path)
    cached = _rules_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rules = data.get("rules") if isinstance(data, dict) else None
        rules = [r for r in (rules or []) if isinstance(r, dict)]
    except (OSError, ValueError):
        rules = []
    _rules_cache[key] = (mtime, rules)
    return rules


def validate_rule(rule: dict) -> str | None:
    """返回错误信息（合法则 None）。**规则必须带内容条件**，否则拒绝。

    为什么：只写「所有 assert-fail → app-crash」会静默把**全部**断言失败改判成崩溃。
    那比不校准更危险 —— 它会把真断言失败藏起来。所以至少要有一个内容条件。
    """
    when = rule.get("when") or {}
    if not isinstance(when, dict):
        return "`when` 必须是对象"
    if rule.get("set_category") not in CATEGORIES:
        return f"`set_category` 必须是 {CATEGORIES} 之一"
    has_content = bool(str(when.get("error_contains") or "").strip()
                       or str(when.get("logcat_contains") or "").strip())
    if not has_content:
        return "规则至少要带一个内容条件（`error_contains` 或 `logcat_contains`）——" \
               "否则会把**同类全部**失败改判，比不校准更危险"
    if when.get("category") and when["category"] not in CATEGORIES:
        return f"`when.category` 必须是 {CATEGORIES} 之一"
    return None


def match_rule(rules: list[dict], category: str, error: str = "", logcat: str = "") -> dict | None:
    """**纯函数**：第一条命中的规则（无命中返回 None）。"""
    err = (error or "").lower()
    log = (logcat or "").lower()
    for rule in rules:
        if validate_rule(rule) is not None:
            continue                      # 非法规则一律跳过（不猜它的意图）
        when = rule["when"]
        if when.get("category") and when["category"] != category:
            continue
        if (m := str(when.get("error_contains") or "").strip()) and m.lower() not in err:
            continue
        if (m := str(when.get("logcat_contains") or "").strip()) and m.lower() not in log:
            continue
        return rule
    return None


def apply_rules(category: str, error: str = "", logcat: str = "",
                rules: list[dict] | None = None) -> tuple[str, dict | None]:
    """**纯函数**：把人工规则叠在规则分类之上，返回 (最终分类, 命中的规则或 None)。

    ⚠️ 规则**只改分类，不改判定**——它不会让失败的步骤变成通过。
    """
    hit = match_rule(rules or [], category, error, logcat)
    return (hit["set_category"], hit) if hit else (category, None)


def save_rules(root: Path, new_rules: list[dict], source: str = "") -> Path:
    """把新规则**追加**到规则表（已有的保留；完全相同的 when+set_category 去重）。"""
    path = rules_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            existing = [r for r in (data.get("rules") or []) if isinstance(r, dict)]
        except (OSError, ValueError):
            existing = []

    def key(r: dict) -> tuple:
        w = r.get("when") or {}
        return (w.get("category"), w.get("error_contains"), w.get("logcat_contains"),
                r.get("set_category"))

    seen = {key(r) for r in existing}
    added = 0
    for r in new_rules:
        if validate_rule(r) is not None or key(r) in seen:
            continue
        existing.append(r)
        seen.add(key(r))
        added += 1
    payload = {"version": RULES_VERSION, "note": source, "rules": existing}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    _rules_cache.pop(str(path), None)     # 立刻失效缓存，避免同进程内读到旧表
    return path


# -- 校准页 -----------------------------------------------------------------------

def build_items(run_payload: dict, run_dir: Path,
                include_passed: bool = False) -> list[dict]:
    """逐**失败步骤**列出校准所需的一切。诊断卡（`diagnose.json`）有就带上。

    `include_passed=True`（控制台校准，发起人 2026-10-13）：成功用例也要能校准——
    把 passed 步骤也列出来，供人标记「误判」（没验证到/侥幸通过）；CLI 校准页
    保持只列失败步骤（`include_passed` 缺省 False）。
    """
    diagnoses: dict = {}
    dpath = run_dir / "diagnose.json"
    if dpath.exists():
        try:
            diagnoses = json.loads(dpath.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            diagnoses = {}

    items = []
    for step in (run_payload.get("run") or {}).get("steps") or []:
        if step.get("status") == "passed" and not include_passed:
            continue
        step_id = str(step.get("step_id") or "")
        logcat = ""
        fname = step.get("logcat_file")
        if fname:
            try:
                logcat = (run_dir / fname).read_text(encoding="utf-8", errors="replace")[-2000:]
            except OSError:
                logcat = ""
        items.append({
            "step_id": step_id,
            "intent": step.get("intent") or "",
            "action": step.get("action") or "",
            "status": step.get("status") or "",
            "category": step.get("category") or "unknown",
            "category_source": step.get("category_source") or "rules",
            "error": (step.get("error") or "")[:600],
            "logcat": logcat,
            "screenshot": step.get("screenshot") or "",
            "diagnosis": diagnoses.get(step_id) or {},
            # 已有的校准标记（回显，供人撤销/确认）
            "ignored": bool(step.get("ignored")),
            "false_positive": bool(step.get("false_positive")),
            "calibration_note": str(step.get("calibration_note") or ""),
            # 断言校准所需的「期望/实际」（抠不到就是 None ⇒ 这一步没有断言校准可做）
            "assertion": parse_assertion_error(step.get("error") or ""),
        })
    return items


def _esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


def render_html(run_label: str, items: list[dict], logcat_hint: str = "") -> str:
    """自包含校准页：每步一个改判下拉 + 可选规则条件 + 「导出校准结论」按钮。

    ⚠️ **不依赖任何外部请求**（截图用相对路径由调用方决定，页面本身只做表单）——
    预览面板不保证解析相对路径，所以这里连图片都不引，只给 logcat 摘录（文本一定能看）。
    """
    options = "".join(f'<option value="{c}">{_esc(CATEGORY_LABELS[c])}（{c}）</option>'
                      for c in CATEGORIES)
    rows = []
    for it in items:
        d = it["diagnosis"] or {}
        attr = ATTR_LABELS.get(d.get("category", ""), d.get("category", "") or "—")
        conf = f"{d['confidence']:.0%}" if d.get("confidence") is not None else "—"
        need = '<span class="need">⚠ 建议人工确认</span>' if d.get("need_human") else ""
        evidence = "".join(f"<li>{_esc(e)}</li>" for e in (d.get("evidence") or []))
        # 断言校准块：只有抠得到「期望/实际」时才出（否则没什么可校准的）
        a = it.get("assertion")
        sid = _esc(it["step_id"])
        aattrs = (f' data-aexp="{_esc(a["expected"])}" data-aact="{_esc(a["actual"])}"'
                  if a else "")
        assert_block = ""
        if a:
            kind = "文本相等（text-equals）" if a["kind"] == "equals" else "文本包含（text-contains）"
            loosen = ""
            if a["kind"] == "equals":
                loosen = (f'<label class="opt"><input type="radio" name="fix-{sid}" value="loosen">'
                          f'放宽为「包含」（{_esc(a["expected"])} 只要出现就算过）</label>')
            assert_block = f"""
  <div class="assert">
    <div class="atitle">断言校准 · {_esc(kind)}</div>
    <div class="cmp"><span>期望</span><code>{_esc(a['expected'])}</code>
      <span>实际</span><code class="act">{_esc(a['actual'])}</code></div>
    <div class="opts">
      <label class="opt"><input type="radio" name="fix-{sid}" value="">不改（只改上面的分类）</label>
      <label class="opt"><input type="radio" name="fix-{sid}" value="adopt-actual">
        采纳实际值（把期望改成「{_esc(a['actual'])}」）</label>
      {loosen}
    </div>
    <div class="hint">⚠️ 「采纳实际值」会让这条用例<b>下次真的通过</b> —— 这是改<b>用例</b>（写
      <code>-calibrated.json</code>，不覆盖原用例）；上面的分类只改<b>报告标签</b>。</div>
  </div>"""
        else:
            assert_block = ""
        rows.append(f"""
<div class="card" data-step="{_esc(it['step_id'])}" data-orig="{_esc(it['category'])}"{aattrs}>
  <div class="hd">
    <b>{_esc(it['step_id'])}</b>
    <span class="intent">{_esc(it['intent'])}</span>
    <span class="cur">规则判定：<b>{_esc(CATEGORY_LABELS.get(it['category'], it['category']))}</b></span>
    {need}
  </div>
  <div class="row">
    <label>人工改判 <select class="cat">{options}</select></label>
    <label>错误里包含 <input class="ec" placeholder="（留空 = 不生成规则）"></label>
    <label>日志里包含 <input class="lc" placeholder="（留空 = 不生成规则）"></label>
  </div>
  <div class="note"><input class="nt" placeholder="备注（可选，写给下次的自己）"></div>
  {assert_block}
  <details><summary>证据</summary>
    <div class="kv"><span>LLM 归因</span><b>{_esc(attr)}</b> · 置信度 {_esc(conf)}</div>
    <div class="kv"><span>建议</span>{_esc(d.get('suggestion') or '—')}</div>
    {'<ul class="ev">' + evidence + '</ul>' if evidence else ''}
    <div class="kv"><span>错误</span><code>{_esc(it['error']) or '—'}</code></div>
    <div class="kv"><span>logcat 摘录</span><pre>{_esc(it['logcat'][-1200:]) or '（无）'}</pre></div>
  </details>
</div>""")

    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<title>报告校准 · {_esc(run_label)}</title>
<style>
 body{{font:14px/1.6 -apple-system,"PingFang SC",sans-serif;background:#f6f7f9;color:#1c1f23;
      margin:0;padding:24px 4vw}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#5b6470;margin-bottom:16px}}
 .bar{{position:sticky;top:0;background:#f6f7f9;padding:10px 0;z-index:5;
      display:flex;gap:12px;align-items:center;border-bottom:1px solid #e3e6ea;margin-bottom:14px}}
 button{{font:inherit;padding:7px 14px;border-radius:8px;border:1px solid #c9ced6;background:#fff;cursor:pointer}}
 button.primary{{background:#0a7d32;border-color:#0a7d32;color:#fff;font-weight:600}}
 .card{{background:#fff;border-radius:10px;padding:14px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 .hd{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:10px}}
 .intent{{color:#5b6470}} .cur{{color:#5b6470}} .need{{color:#b45309;font-weight:600}}
 .row{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px}}
 .row label{{display:flex;flex-direction:column;gap:3px;font-size:12px;color:#5b6470}}
 select,input{{font:inherit;padding:6px 9px;border:1px solid #c9ced6;border-radius:8px}}
 .ec,.lc{{width:260px}} .nt{{width:100%}}
 .assert{{margin:10px 0 4px;padding:10px 12px;background:#f7f9ff;
      border:1px solid #dbe3ff;border-radius:8px}}
 .atitle{{font-weight:600;font-size:13px;margin-bottom:6px}}
 .cmp{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;font-size:13px}}
 .cmp span{{color:#8a919f}} .cmp code.act{{background:#fff1f0;color:#b3261e}}
 .opts{{display:flex;gap:18px;flex-wrap:wrap;font-size:13px}}
 .opt{{display:flex;gap:5px;align-items:center;cursor:pointer}}
 .hint{{margin-top:8px;font-size:12px;color:#5b6470}}
 .kv{{margin:4px 0;font-size:13px}} .kv span{{color:#8a919f;margin-right:8px}}
 pre{{background:#fafbfc;border:1px solid #eef0f3;border-radius:6px;padding:8px;
      max-height:220px;overflow:auto;font-size:12px;white-space:pre-wrap}}
 code{{background:#fafbfc;padding:1px 5px;border-radius:4px;font-size:12px}}
 .warn{{background:#fff8e6;border-left:4px solid #e0a800;padding:10px 14px;border-radius:6px;
      margin:0 0 14px;color:#5b4200}}
 details{{margin-top:8px}} summary{{cursor:pointer;color:#5b6470;font-size:13px}}
</style>
<h1>报告校准 · {_esc(run_label)}</h1>
<div class="sub">共 <b>{len(items)}</b> 个失败步骤。改判后点「导出校准结论」，再执行
  <code>mat calibrate {_esc(run_label)} --apply &lt;下载的文件&gt;</code>。</div>
<div class="warn">分类判错<b>看起来完全正常</b>（报告照常给出一个分类）⇒ 请对着证据确认。
  <b>想让下次不再犯</b>：填上「错误里包含」或「日志里包含」——那就是一条<b>规则</b>，
  以后同类失败会自动按你的判定归类。<b>留空则只改这一次</b>。</div>
<div class="bar">
  <button class="primary" id="exp">导出校准结论（下载 JSON）</button>
  <span id="cnt" class="sub" style="margin:0"></span>
  <button id="clr">撤销全部修改</button>
</div>
{''.join(rows)}
<script>
var RUN = {json.dumps(run_label, ensure_ascii=False)};
var LOGHINT = {json.dumps(logcat_hint, ensure_ascii=False)};
var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
function refresh() {{
  var n = 0;
  cards.forEach(function (c) {{
    var changed = c.querySelector('.cat').value !== c.getAttribute('data-orig')
      || c.querySelector('.ec').value.trim() || c.querySelector('.lc').value.trim()
      || fixOf(c);
    if (changed) n++;
  }});
  document.getElementById('cnt').textContent = n ? ('已改 ' + n + ' 个步骤') : '还没有修改';
}}
function fixOf(c) {{
  var el = c.querySelector('input[name^="fix-"]:checked');
  return el && el.value ? el.value : '';
}}
cards.forEach(function (c) {{
  c.querySelector('.cat').value = c.getAttribute('data-orig');
  Array.prototype.forEach.call(c.querySelectorAll('input,select'), function (el) {{
    el.addEventListener('input', refresh); el.addEventListener('change', refresh);
  }});
}});
refresh();
document.getElementById('clr').onclick = function () {{
  cards.forEach(function (c) {{
    c.querySelector('.cat').value = c.getAttribute('data-orig');
    c.querySelector('.ec').value = ''; c.querySelector('.lc').value = '';
    c.querySelector('.nt').value = '';
  }});
  refresh();
}};
document.getElementById('exp').onclick = function () {{
  var steps = {{}};
  cards.forEach(function (c) {{
    var id = c.getAttribute('data-step'), orig = c.getAttribute('data-orig');
    var cat = c.querySelector('.cat').value, ec = c.querySelector('.ec').value.trim();
    var lc = c.querySelector('.lc').value.trim(), nt = c.querySelector('.nt').value.trim();
    var fx = fixOf(c);
    if (cat === orig && !ec && !lc && !nt && !fx) return;
    var entry = {{category: cat, from: orig,
                 error_contains: ec, logcat_contains: lc, note: nt}};
    if (fx) {{
      entry.assertion_fix = fx;
      entry.assertion_expected = c.getAttribute('data-aexp') || '';
      entry.assertion_actual = c.getAttribute('data-aact') || '';
    }}
    steps[id] = entry;
  }});
  var n = Object.keys(steps).length;
  if (!n) {{ alert('还没有任何修改。'); return; }}
  var payload = {{run: RUN, steps: steps,
                  note: '由 mat calibrate 生成；用 mat calibrate <产物目录> --apply <本文件> 应用'}};
  var blob = new Blob([JSON.stringify(payload, null, 1)], {{type: 'application/json'}});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = RUN.replace(/[\\/]/g, '_') + '.calibration.json';
  document.body.appendChild(a); a.click(); a.remove();
  alert('已导出 ' + n + ' 个步骤的校准结论。\\n接着执行：\\nmat calibrate ' + RUN + ' --apply <下载的文件>');
}};
</script>
</html>"""


# -- 应用 -------------------------------------------------------------------------

def load_calibration(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("steps"), dict):
        raise ValueError("校准结论必须是含 `steps` 对象的 JSON")
    return data


def apply_calibration(run_payload: dict, calibration: dict) -> dict:
    """把人工判定写回 `run_payload`（**改内存里的 dict**，由调用方落盘）。

    支持三类动作（可同条组合；发起人 2026-10-13 扩展）：
    - 分类改判：`category` ∈ CATEGORIES → 改判 + 标 human（原语义不变）；
    - 标记类：`ignore=True`（失败但可忽略）/ `false_positive=True`（通过是误判）/
      `note`（校准原因）——**独立生效，不要求给分类**，显式 `False`/清空 note 即撤销；
    - 断言校准：`assertion_fix` → 改用例（由调用方落 `<名>-calibrated.json`）。
    标记类**不改 status、不改成功率统计**（对账口径不变，报告只加标注）。

    返回逐项统计：`applied`（改判的步骤）/ `unchanged`（判定与原来一致）/
    `missing`（`step_id` 在 run 里找不到）/ `rules`（要落成规则的条目）/
    `marks`（打了标记类标注的步骤）。
    """
    steps = (run_payload.get("run") or {}).get("steps") or []
    by_id = {str(s.get("step_id")): s for s in steps}
    applied, unchanged, missing, rules, marks = [], [], [], [], []
    assertion_fixes: dict[str, dict] = {}
    for step_id, entry in (calibration.get("steps") or {}).items():
        step = by_id.get(str(step_id))
        if step is None:
            missing.append(str(step_id))
            continue
        # 断言校准（改**用例**，让结果本身正确）—— 与分类改判相互独立
        fix = str(entry.get("assertion_fix") or "")
        if fix in ("adopt-actual", "loosen"):
            assertion_fixes[str(step_id)] = {
                "action": fix,
                "expected": str(entry.get("assertion_expected") or ""),
                "actual": str(entry.get("assertion_actual") or ""),
            }
        # 标记类：True 打标 / False 显式撤销 / 缺省不动
        note = str(entry.get("note") or "").strip()
        did_mark = ("ignore" in entry or "false_positive" in entry
                    or "note" in entry or fix)
        if entry.get("ignore") is True:
            step["ignored"] = True
        elif entry.get("ignore") is False:
            step.pop("ignored", None)
        if entry.get("false_positive") is True:
            step["false_positive"] = True
        elif entry.get("false_positive") is False:
            step.pop("false_positive", None)
        if note:
            step["calibration_note"] = note
        elif "note" in entry:
            step.pop("calibration_note", None)
        if did_mark and any(k in step for k in ("ignored", "false_positive", "calibration_note")):
            marks.append(str(step_id))
        # 分类改判：给了 target 才改；没给且什么都没做 ⇒ 这条是空的（missing）
        target = str(entry.get("category") or "").strip()
        if target:
            if target not in CATEGORIES:
                missing.append(str(step_id))
                continue
            if step.get("category") == target:
                unchanged.append(str(step_id))
            else:
                step["category_before_calibration"] = step.get("category")
                step["category"] = target
                step["category_source"] = "human"      # 报告要能标出「这是人判的」
                applied.append(str(step_id))
        elif not did_mark:
            missing.append(str(step_id))
            continue
        if str(entry.get("error_contains") or "").strip() or \
                str(entry.get("logcat_contains") or "").strip():
            rule = {"when": {"error_contains": str(entry.get("error_contains") or "").strip(),
                             "logcat_contains": str(entry.get("logcat_contains") or "").strip()},
                    "set_category": target,
                    "why": note or f"人工校准（{step_id}）",
                    "from": str(calibration.get("run") or "")}
            rule["when"] = {k: v for k, v in rule["when"].items() if v}
            rules.append(rule)
    return {"applied": applied, "unchanged": unchanged, "missing": missing, "rules": rules,
            "assertion_fixes": assertion_fixes, "marks": marks}
