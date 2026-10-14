"""HTML 报告：单用例报告（步骤表 + 截图 + 类别 + 自愈提案 + logcat 链接）与套件聚合页。

"结果可信"的体验版落点：每步必有截图；失败必分类、必带 logcat 证据；
正式报告中心（归因卡/视频/性能曲线）RPT-*（增强项）。
安全：所有动态字段（含被测 App 界面文本与模型输出）经 html.escape 后才入 HTML。
"""

from __future__ import annotations

import base64
import html
import json
import math
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote as _quote

from .runner import RunResult

# 截图内联预算：超过后剩余截图回退相对链接（防止单文件失控；≤20MB 可邮件/IM 传输）
_INLINE_BUDGET_BYTES = 20 * 1024 * 1024

_CSS = """
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:32px auto;max-width:960px;color:#1f2328}
h1{font-size:20px} h2{font-size:16px;margin-top:28px} .meta{color:#57606a;font-size:13px;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border:1px solid #d0d7de;padding:8px 10px;text-align:left;vertical-align:top}
th{background:#f6f8fa} code{background:#f6f8fa;padding:1px 4px;border-radius:4px}
img{max-width:280px;border:1px solid #d0d7de;border-radius:6px}
a{color:#0969da}
.pass{color:#16a34a;font-weight:600}.fail{color:#dc2626;font-weight:600}
.cancelled{color:#b45309;font-weight:600}
.warn{color:#b45309}.err{color:#b91c1c;white-space:pre-wrap}.retry{color:#2563eb}
.cat{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;color:#fff}
.cal{display:inline-block;padding:1px 6px;border-radius:10px;font-size:11px;
     background:#e7f6ec;color:#0a7d32;border:1px solid #b7e0c5}
.cat-app-crash{background:#b91c1c}.cat-env-failure{background:#b45309}
.cat-assert-fail{background:#dc2626}.cat-locate-fail{background:#c2410c}.cat-unknown{background:#6b7280}
.heal{background:#f3e8ff;border:1px solid #d8b4fe;border-radius:6px;padding:6px 8px;margin-top:6px;font-size:12px}
.head{display:flex;align-items:center;gap:20px;margin-bottom:14px}
.donut{width:88px;height:88px;border-radius:50%;display:flex;align-items:center;justify-content:center;flex:none}
.donut>div{width:60px;height:60px;border-radius:50%;background:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:16px}
.bar{display:flex;height:12px;border-radius:6px;overflow:hidden;background:#e5e7eb;max-width:420px}
.bar span{display:block;height:100%}
.bar-legend{font-size:11px;color:#57606a;margin-top:4px}
details.logcat{margin-top:4px;font-size:12px}
details.logcat pre{background:#0d1117;color:#c9d1d9;padding:8px;border-radius:6px;overflow:auto;max-height:260px;white-space:pre-wrap}
@media print{body{margin:8mm}.meta{font-size:11px}img{max-width:160px}details.logcat{display:none}}
"""

_CATEGORY_LABELS = {
    "app-crash": "产品 bug(崩溃)",
    "env-failure": "环境问题",
    "assert-fail": "断言失败",
    "locate-fail": "定位失败",
    "unknown": "待归因",
}

_CATEGORY_COLORS = {
    "app-crash": "#b91c1c",
    "env-failure": "#b45309",
    "assert-fail": "#dc2626",
    "locate-fail": "#c2410c",
    "unknown": "#6b7280",
}


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _data_uri(artifacts_dir: Path, rel: str, budget: list[int]) -> str | None:
    """截图 → base64 data URI（自包含）；文件缺失或超预算返回 None（回退相对链接）。"""
    try:
        data = (artifacts_dir / rel).read_bytes()
    except OSError:
        return None
    if not data or len(data) > budget[0]:
        return None
    budget[0] -= len(data)
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _img(artifacts_dir: Path, rel: str | None, budget: list[int]) -> str:
    if not rel:
        return "（无截图）"
    uri = _data_uri(artifacts_dir, rel, budget)
    src = uri or _esc(rel)
    return f'<img src="{src}" alt="step screenshot">'


def _logcat(artifacts_dir: Path, rel: str | None) -> str:
    """失败步骤的 logcat：文件存在→内联到 <details>（报告自包含）；缺失→保留链接。"""
    if not rel:
        return "—"
    try:
        content = (artifacts_dir / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f'<a href="{_esc(rel)}">logcat</a>'
    if not content.strip():
        return f'<a href="{_esc(rel)}">logcat</a>'
    return (f'<details class="logcat"><summary>logcat（{_esc(rel)}）</summary>'
            f"<pre>{_esc(content[-8000:])}</pre></details>")


def _donut(rate: float) -> str:
    # NaN/Inf（如 0 步用例的成功率）会让 round() 抛错或渲染出越界样式；先钳到 [0,1]（评审 L13）。
    if not math.isfinite(rate):
        rate = 0.0
    pct = round(min(max(rate, 0.0), 1.0) * 100)
    return (f'<div class="donut" style="background:conic-gradient(#16a34a 0% {pct}%,#e5e7eb 0%)">'
            f"<div>{pct}%</div></div>")


def _cat_bar(failed_categories: dict) -> str:
    """失败分类堆叠条：宽度按占比；全绿（无失败）显示细绿条。"""
    total = sum(failed_categories.values())
    if not total:
        return '<div class="bar"><span style="width:100%;background:#16a34a"></span></div>'
    spans = "".join(
        f'<span style="width:{v / total * 100:.1f}%;background:{_CATEGORY_COLORS.get(k, "#6b7280")}"></span>'
        for k, v in sorted(failed_categories.items(), key=lambda kv: -kv[1]))
    legend = " · ".join(f"{_CATEGORY_LABELS.get(k, k)}×{v}" for k, v in
                        sorted(failed_categories.items(), key=lambda kv: -kv[1]))
    return f'<div class="bar">{spans}</div><div class="bar-legend">{_esc(legend)}</div>'


def _badge(status: str) -> str:
    if status == "passed":
        return '<span class="pass">✅ 通过</span>'
    if status == "skipped":
        return '<span class="muted">⏭ 未执行</span>'
    return '<span class="fail">❌ 失败</span>'


def _category_html(category: str | None, source: str | None = None) -> str:
    if not category:
        return "—"
    label = _CATEGORY_LABELS.get(category, category)
    # `source="human"` = 人工校准改判过 ⇒ 标出来（否则看不出这条是自己判的还是机器判的）
    mark = (' <span class="cal" title="人工校准改判">人工</span>'
            if source == "human" else "")
    return f'<span class="cat cat-{_esc(category)}">{_esc(label)}</span>{mark}'


def _load_attributions(artifacts_dir: Path) -> dict:
    """读取 diagnose.json（LLM 归因卡，mat diagnose 产物）；缺失/损坏返回空。"""
    try:
        data = json.loads((artifacts_dir / "diagnose.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_ATTR_CARD_STYLE = ('margin-top:6px;border:1px solid #0969da33;background:#f0f6ff;'
                    'border-radius:6px;padding:6px 8px;font-size:12px')


def _attr_card(attr: dict | None) -> str:
    """归因卡：LLM 四分类 + 置信度 + 证据 + 建议（错误成本不对称：低置信必标人工）。"""
    if not attr:
        return ""
    label = {"app-bug": "真 bug", "ui-change": "UI 改版", "env-flake": "环境抖动",
             "precondition": "前置缺失"}.get(attr.get("category", ""), attr.get("category", "—"))
    conf = attr.get("confidence")
    conf_text = f"{conf:.0%}" if isinstance(conf, (int, float)) else "—"
    need = ' <span style="color:#b45309">⚠ 建议人工确认</span>' if attr.get("need_human") else ""
    evidence = "".join(f'<div style="color:#57606a">证据: {_esc(e)}</div>'
                       for e in attr.get("evidence", []))
    suggestion = (f'<div>建议: {_esc(attr["suggestion"])}</div>'
                  if attr.get("suggestion") else "")
    source = "AI 归因" if attr.get("source") == "llm" else "人工回退"
    return (f'<div class="attr" style="{_ATTR_CARD_STYLE}">'
            f'<b>🧭 {source}·{_esc(label)}</b> · 置信度 {conf_text}{need}'
            f'{evidence}{suggestion}</div>')


def _preconditions_html(preconditions: list[str]) -> str:
    """前置条件单独一块显示 —— 它是**设备状态**，不是步骤。

    为什么要显示：这一列此前是**消失**的（导入器读出来了、DSL 边界丢掉了），
    于是失败归因时会去查一个不存在的定位问题（见 `preconditions.py` 的模块说明）。
    """
    if not preconditions:
        return ""
    items = "".join(f"<li>{_esc(p)}</li>" for p in preconditions)
    return ("<div class='meta'>前置条件（跑之前设备该处于的状态）："
            f"<ul style='margin:4px 0 0 16px;padding:0'>{items}</ul></div>")
def _norm_error(error: str | None) -> str:
    """失败原因归一化：取首行（首现场）、数字抹平、空白折叠——同类异常聚成一个 key。

    数字抹平的依据：错误串里的数字多为步骤号/超时毫秒/重试次数，差异不代表异常不同；
    「期望 X 实际 Y」的文本里含数字会被一起抹平——可接受的误合（下方明细表仍逐条可查）。
    """
    text = (error or "").strip()
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "N", text.splitlines()[0])).strip()


def _first_line(error: str | None) -> str:
    """错误串首行（首现场）；空错误安全返回空串（splitlines 对 '' 返回 []，不能直接 [0]）。"""
    return ((error or "").strip().splitlines() or [""])[0]


def _fail_groups(steps: list) -> list[dict]:
    """失败步骤按 (分类, 归一化 error) 聚类——「同一个页面同一个异常」合并的判定核心。

    保持首现顺序；调用方只把它用于**汇总展示**，逐条明细（证据链）不动。
    """
    groups: list[dict] = []
    index: dict[tuple, dict] = {}
    for s in steps:
        if s.status != "failed":
            continue
        key = (s.category or "unknown", _norm_error(s.error))
        if key in index:
            index[key]["steps"].append(s)
        else:
            g = {"category": s.category or "unknown", "steps": [s]}
            index[key] = g
            groups.append(g)
    return groups


def _group_attr(group: list, attributions: dict) -> dict | None:
    """失败组顶部归因卡取数：优先组内首个 LLM 归因卡，否则首个有卡的步骤。

    为什么放汇总区（发起人 2026-10-13「报告里没看到归因卡」）：归因卡原来只挂在
    步骤明细表的证据列——大报告（内联截图数 MB）里失败行在文档最底部，等于看不见；
    顶部「失败原因汇总」才是打开报告的第一落点。逐条明细卡的入口保留不动。"""
    cards = [attributions.get(s.step_id) for s in group]
    cards = [c for c in cards if c]
    for c in cards:
        if c.get("source") == "llm":
            return c
    return cards[0] if cards else None


def _merge_findings(findings: list[dict]) -> list[list[dict]]:
    """探索发现同因合并（发起人 2026-10-13）：同 type 且满足任一 ⇒ 同一个异常：
    ① 复现路径同主干（一条 path 是另一条的前缀延伸——同一条深入路径上的层层白屏）；
    ② 同 page_key（同一个页面复访再次报同一类异常）。
    只合**展示层**——explore.json 原始 findings 不动；列表计数口径 2026-10-13 起同改
    为合并后数目（workspace.list_explores 复用本函数，保证列表与报告头部数字一致）。
    """
    def same(a: dict, b: dict) -> bool:
        if a.get("type") != b.get("type"):
            return False
        pa, pb = str(a.get("path") or ""), str(b.get("path") or "")
        if pa and pb and (pa == pb or pa.startswith(pb + " →") or pb.startswith(pa + " →")):
            return True
        k1, k2 = a.get("page_key"), b.get("page_key")
        return bool(k1 and k2 and k1 == k2)

    groups: list[list[dict]] = []
    for f in findings:
        for g in groups:
            if same(g[0], f):
                g.append(f)
                break
        else:
            groups.append([f])
    return groups


def render_html(run: RunResult, artifacts_dir: Path) -> Path:
    budget = [_INLINE_BUDGET_BYTES]
    attributions = _load_attributions(artifacts_dir)
    # 执行录屏：段文件在 artifacts/video/，报告相对引用——
    # 视频不内联（单文件预算 20MB 承受不了），录屏关闭时目录不存在 ⇒ 此节为空。
    video_dir = artifacts_dir / "video"
    video_html = ""
    if video_dir.is_dir():
        vids = sorted(video_dir.glob("*.mp4"))
        if vids:
            players = "".join(
                f'<video controls preload="metadata" src="video/{_esc(v.name)}" '
                f'style="max-width:420px;width:100%;display:block;margin:8px 0"></video>'
                for v in vids)
            video_html = f'<div class="video"><h3>执行录屏</h3>{players}</div>'
    rows: list[str] = []
    heal_count = 0
    for step in run.steps:
        shot = _img(artifacts_dir, step.screenshot, budget)
        warn = f'<div class="warn">⚠ {_esc(step.warning)}</div>' if step.warning else ""
        err = f'<div class="err">{_esc(step.error)}</div>' if step.error else ""
        # 校准标记（发起人 2026-10-13）：可忽略 / 误判 / 校准原因——只加标注，
        # 不改 status、不改成功率统计（报告列表对账口径不变）
        cal_marks = ""
        if step.ignored:
            cal_marks += (' <span class="cal" style="background:#eceff4;color:#57606a;'
                          'border-color:#d0d7de" title="人工校准：可忽略">已忽略</span>')
        if step.false_positive:
            cal_marks += (' <span class="cal" style="background:#fdecec;color:#b91c1c;'
                          'border-color:#f5c2c2" title="人工校准：通过是误判">误判</span>')
        note = (f'<div class="warn">✎ 校准原因: {_esc(step.calibration_note)}</div>'
                if step.calibration_note else "")
        retry = ' <span class="retry">(重试通过)</span>' if step.retried or step.retried_on_failure else ""
        strategy = f" <code>{_esc(step.used_strategy)}</code>" if step.used_strategy else ""
        # 失败步骤直达修复（发起人 2026-10-12 P1）：报告目录名 = 用例文件 stem——
        # 套件条目目录带 NN_ 前缀，配置台深链解析侧会剥前缀兜底；CLI 直跑的报告
        # 链接可能无对应用例文件，配置台会提示「用例不存在」，无害。
        edit_link = ""
        if step.status == "failed":
            edit_link = (f' <a href="/#/cases/{_quote(artifacts_dir.name)}.json'
                         f'?step={_quote(step.step_id)}">编辑此步</a>')
        logcat = _logcat(artifacts_dir, step.logcat_file)
        heal = ""
        if step.heal:
            heal_count += 1
            via = step.heal.get("via", "local-fuzzy")
            via_label = "vision-llm 视觉兜底" if via == "vision-llm" else "本地模糊重定位"
            heal = (
                f'<div class="heal">🩹 自愈提案（{_esc(via_label)}，待确认回写）: '
                f"<code>{_esc(step.heal['original'])}</code> → <code>{_esc(step.heal['match'])}</code></div>"
            )
        rows.append(
            "<tr>"
            f"<td><code>{_esc(step.step_id)}</code></td>"
            f"<td>{_esc(step.intent)}{retry}{edit_link}</td>"
            f"<td><code>{_esc(step.action)}</code>{strategy}</td>"
            f"<td>{_badge(step.status)}{cal_marks}<br>{_category_html(step.category, step.category_source)}</td>"
            f"<td>{step.duration_ms}ms</td>"
            f"<td>{shot}{err}{warn}{note}{heal}{_attr_card(attributions.get(step.step_id))}</td>"
            f"<td>{logcat}</td>"
            "</tr>"
        )

    # 失败原因汇总（同类合并，发起人 2026-10-13）：同分类且归一化同因的失败步骤并成一行
    # ——「同一个页面同一个异常」一眼看全；下方步骤明细表保持逐条证据链不动。
    # AI 归因列（发起人 2026-10-13）：归因卡原来只在步骤明细表证据列（大报告最底部，
    # 等于看不见）——提升到打开报告的第一落点，h2 挂 id=attr 供归因完成直达链接锚跳。
    fail_html = ""
    if (fg := _fail_groups(run.steps)):
        grows = "".join(
            "<tr>"
            f"<td>{_category_html(g['category'])}</td>"
            f"<td><span title='{_esc(g['steps'][0].error or '')}'>"
            f"{_esc(_first_line(g['steps'][0].error)[:160])}</span></td>"
            f"<td>{len(g['steps'])} 步"
            f"<details class='logcat'><summary>明细</summary>"
            + "".join(f"<div><code>{_esc(s.step_id)}</code> {_esc(s.intent)}</div>"
                      for s in g["steps"])
            + "</details></td>"
            f"<td>{_attr_card(_group_attr(g['steps'], attributions))}</td>"
            f"<td>{_img(artifacts_dir, g['steps'][0].screenshot, budget)}</td>"
            "</tr>"
            for g in fg)
        fail_html = ("<h2 id='attr'>失败原因汇总（同类合并）</h2>"
                     "<table><tr><th>分类</th><th>失败原因（首现场）</th>"
                     "<th>影响步骤</th><th>AI 归因</th><th>证据</th></tr>" + grows + "</table>")

    perf_parts = []
    if run.perf.get("launch_ms") is not None:
        perf_parts.append(f"启动 {run.perf['launch_ms']}ms")
    if run.perf.get("mem_pss_mb") is not None:
        perf_parts.append(f"内存 {run.perf['mem_pss_mb']:.0f}MB")
    perf_text = (" · " + " · ".join(perf_parts)) if perf_parts else ""
    heal_text = f" · 自愈提案 {heal_count} 条" if heal_count else ""
    case_label = _esc(run.case_name or run.case_id)
    status_label = {"passed": "通过", "failed": "失败",
                    "skipped": "未执行"}.get(run.status, "已中止")
    title = f"{case_label} — {status_label}"
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>{_CSS}</style></head><body>"
        f"<h1>{title}</h1>"
        "<div class='head'>"
        + _donut(run.step_success_rate)
        + "<div><div class='meta'>case " + _esc(run.case_id) + " · 用时 " f"{run.duration_ms}ms"
        + (f" · 执行人 {_esc(run.operator)}" if getattr(run, "operator", "") else "")
        + f"{perf_text}{heal_text}"
        + (f" · 已中止（完成 {len(run.steps)} 步）" if run.status == "cancelled" else "")
        + " · MAT 报告</div>"
        + _cat_bar(run.failed_categories)
        + _preconditions_html(run.preconditions)
        + """<div class='share'><button onclick='navigator.clipboard.writeText(location.href).then(
          function(){var b=document.querySelector(".share button");b.textContent="已复制链接"});
          '>复制本报告链接</button>
          <span>（控制台以 <code>--share</code> 启动时，同一 Wi-Fi 的同事打开此链接即可查看；数据不出内网）</span>
        </div>"""
        + "</div></div>"
        + video_html
        + fail_html
        + "<table><tr><th>步骤</th><th>意图</th><th>动作/定位</th><th>结果/分类</th><th>耗时</th><th>证据</th><th>日志</th></tr>"
        + "".join(rows)
        + "</table></body></html>"
    )

    # 分享入口样式（随 doc 内联，不进全局 _CSS——后者被多渲染器共用）
    s_share = (
        "    .share{margin:10px 0 4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}\n"
        "    .share button{font:12px -apple-system,sans-serif;padding:4px 10px;border-radius:6px;"
        "border:1px solid #c9ced6;background:#fff;cursor:pointer}\n"
        "    .share span{color:#8a919f;font-size:12px}\n")
    out = artifacts_dir / "report.html"
    out.write_text(doc.replace("<style>", "<style>" + s_share, 1)
                   if ".share{" not in doc else doc, encoding="utf-8")
    return out


def render_index_html(suite_name: str, entries: list[tuple[str, str, RunResult]], artifacts_dir: Path) -> Path:
    """套件聚合页：entries = [(用例名, 相对报告路径, RunResult)]。

    批次聚合报告增强（发起人 2026-10-12「第二步」）：自包含单文件、可直接发给开发定位——
    汇总条（通过/失败/中止计数 + 总耗时 + 设备 + 生成时间）+ 逐条明细加
    「失败步骤」与「失败原因」列（取首个失败步骤 error 的第一现场摘要）。
    """
    import json as _json

    # 设备 best-effort：从各条产物 run.json 收集 device_serial（CLI/配置台双入口都写），
    # 读不到不影响生成（如仅拿到内存 entries 的测试场景）。
    # 路径基准：rel 相对**聚合页所在目录**——console 批次产物在 artifacts/ 顶层、
    # 聚合页在 artifacts/<套件名>/，rel 带 ../ 前缀（兄弟目录）⇒ 必须整段拼接后取
    # parent，不能只取首段（首段是 ".." 时拼出来的 run.json 永远不存在，设备列恒空）
    devices: list[str] = []
    for _name, rel_report, _r in entries:
        try:
            run_json = (artifacts_dir / rel_report).parent / "run.json"
            serial = _json.loads(run_json.read_text(encoding="utf-8")).get("device_serial")
            if serial and serial not in devices:
                devices.append(str(serial))
        except Exception:
            continue

    total_ms = sum(r.duration_ms for _, _, r in entries)
    passed = sum(1 for _, _, r in entries if r.status == "passed")
    cancelled = sum(1 for _, _, r in entries if r.status == "cancelled")
    failed = len(entries) - passed - cancelled
    gen_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    rows: list[str] = []
    for name, rel_report, run in entries:
        cats = " · ".join(f"{k}×{v}" for k, v in run.failed_categories.items()) or "—"
        failed_steps = sum(1 for s in run.steps if s.status == "failed")
        total_steps = len(run.steps)
        first_err = next(
            (s.error or "" for s in run.steps if s.status == "failed" and s.error), "")
        err_cell = (f"<span title='{_esc(first_err)}'>{_esc(first_err[:100])}</span>"
                    if first_err else "—")
        rows.append(
            "<tr>"
            f"<td>{_esc(name)}</td><td><code>{_esc(run.case_id)}</code></td>"
            f"<td>{_badge(run.status)}</td>"
            f"<td>{run.step_success_rate:.0%}</td>"
            f"<td>{failed_steps}/{total_steps}</td>"
            f"<td>{_esc(cats)}</td>"
            f"<td>{err_cell}</td>"
            f"<td>{run.duration_ms}ms</td>"
            f"<td><a href='{_esc(rel_report)}'>报告</a></td>"
            "</tr>"
        )

    # 失败原因汇总（同类合并，发起人 2026-10-13）：按「首个失败步骤的分类+归一化原因」
    # 聚类用例——同因 N 条并一行（点开列出各条报告链接）；逐条明细表保持不动
    case_groups: dict[tuple, list[tuple[str, str, RunResult]]] = {}
    case_order: list[tuple] = []
    for name, rel_report, run in entries:
        first_fail = next((s for s in run.steps if s.status == "failed" and s.error), None)
        if first_fail is None:
            continue
        key = (first_fail.category or "unknown", _norm_error(first_fail.error))
        if key in case_groups:
            case_groups[key].append((name, rel_report, first_fail))
        else:
            case_groups[key] = [(name, rel_report, first_fail)]
            case_order.append(key)
    case_summary = ""
    if case_order:
        grows = "".join(
            "<tr>"
            f"<td>{_category_html(k[0])}</td>"
            f"<td><span title='{_esc(case_groups[k][0][2].error or '')}'>"
            f"{_esc(_first_line(case_groups[k][0][2].error)[:160])}</span></td>"
            f"<td>{len(case_groups[k])} 条"
            f"<details class='logcat'><summary>明细</summary>"
            + "".join(f"<div><a href='{_esc(rel)}'>{_esc(nm)}</a></div>"
                      for nm, rel, _s in case_groups[k])
            + "</details></td>"
            "</tr>"
            for k in case_order)
        case_summary = ("<h2>失败原因汇总（同类合并）</h2>"
                        "<table><tr><th>分类</th><th>失败原因（首现场）</th>"
                        "<th>影响用例</th></tr>" + grows + "</table>")

    dev_bit = f" · 设备 {_esc('、'.join(devices))}" if devices else ""
    title = f"{_esc(suite_name)} — {passed}/{len(entries)} 通过"
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>{_CSS}</style></head><body>"
        f"<h1>{title}</h1>"
        "<div class='head'>" + _donut(passed / max(len(entries), 1)) + "<div>"
        f"<div class='meta'>MAT 套件聚合报告 · 生成于 {gen_at}{dev_bit}</div>"
        f"<div class='meta'>通过 {passed} · 失败 {failed} · 中止 {cancelled}"
        f" · 总耗时 {total_ms / 1000:.1f}s</div></div></div>"
        "<table><tr><th>用例</th><th>case</th><th>结果</th><th>步骤成功率</th>"
        "<th>失败步骤</th><th>失败分类</th><th>失败原因</th><th>耗时</th><th></th></tr>"
        + "".join(rows)
        + "</table>"
        + case_summary
        + "</body></html>"
    )

    out = artifacts_dir / "index.html"
    out.write_text(doc, encoding="utf-8")
    return out


def render_junit_xml(run: RunResult) -> str:
    """JUnit XML（CI 集成的通用协议：Jenkins/GitLab/GitHub Actions 均可消费）。

    每个步骤映射为一个 testcase；失败步骤带 category 属性与 failure 消息。
    ElementTree 自动完成 XML 转义。
    """
    import xml.etree.ElementTree as ET

    suite = ET.Element(
        "testsuite",
        name=run.case_name or run.case_id,
        tests=str(len(run.steps)),
        failures=str(sum(1 for s in run.steps if s.status == "failed")),
        time=f"{run.duration_ms / 1000:.3f}",
    )
    for step in run.steps:
        case = ET.SubElement(
            suite,
            "testcase",
            classname=run.case_id,
            name=f"{step.step_id}: {step.intent}" if step.intent else step.step_id,
            time=f"{step.duration_ms / 1000:.3f}",
        )
        if step.status == "skipped":
            ET.SubElement(case, "skipped", message=step.error or "")
            continue
        if step.status != "passed":
            failure = ET.SubElement(
                case,
                "failure",
                message=f"[{step.category or 'unknown'}] {step.warning or ''}".strip(),
                type=step.category or "unknown",
            )
            failure.text = step.error or ""
    ET.indent(suite)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(suite, encoding="unicode")


def _page_label(page: dict) -> str:
    """页面显示名：AI/人工标题 > Activity 短名 > page_key 前缀（title 属 P1 可选增强）。"""
    title = page.get("title") or ""
    if title:
        return f"{title}（AI）" if page.get("title_source") == "ai" else title
    activity = page.get("activity") or ""
    short = activity.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    return short or page.get("page_key", "")[:8]


def render_explore_html(report: dict, artifacts_dir: Path) -> Path:
    """探索测试报告：深度分布 + 发现（含复现路径）+ 访问页面（含结构与跳转图）。截图内联。

    发现区同因合并展示（发起人 2026-10-13）：同 type 且路径同主干/同页面的多条
    finding 并成一行（「N 处」可展开），截图取首处；explore.json 原始数据不动。
    校准标注：人工「确认/忽略」后重渲染，行上挂徽标。
    """
    budget = [_INLINE_BUDGET_BYTES]
    findings = report.get("findings", [])
    depth_stats = report.get("depth_stats", {})
    depth_line = " · ".join(f"{k}:{v}页" for k, v in
                            sorted(depth_stats.items(), key=lambda kv: int(kv[0][1:]))) or "—"

    def _cal_badge(verdict: str) -> str:
        if verdict == "confirmed":
            return (" <span class='cal' style='background:#fdecec;color:#b91c1c;"
                    "border-color:#f5c2c2' title='人工校准：确认是真异常'>已确认</span>")
        if verdict == "ignored":
            return (" <span class='cal' style='background:#eceff4;color:#57606a;"
                    "border-color:#d0d7de' title='人工校准：误报，可忽略'>已忽略</span>")
        return ""

    def _finding_row(group: list[dict]) -> str:
        first = group[0]
        verdicts = [str((f.get("calibration") or {}).get("verdict") or "") for f in group]
        # 组徽标：任一处已确认 ⇒「已确认」；全部已忽略 ⇒「已忽略」（部分忽略不标）
        group_badge = ""
        if "confirmed" in verdicts:
            group_badge = _cal_badge("confirmed")
        elif verdicts and all(v == "ignored" for v in verdicts):
            group_badge = _cal_badge("ignored")
        places = "".join(
            f"<div style='margin:2px 0'>{_esc(f.get('path') or '')}"
            f" · {_esc(f.get('detail') or '')}{_cal_badge(v)}"
            + (f"<div>校准原因: {_esc((f.get('calibration') or {}).get('note') or '')}</div>"
               if (f.get("calibration") or {}).get("note") else "")
            + "</div>"
            for f, v in zip(group, verdicts, strict=True))
        multi = (f"<details class='logcat'><summary>共 {len(group)} 处（同因合并）</summary>"
                 f"{places}</details>" if len(group) > 1 else "")
        shot_rel = next((f.get("screenshot") for f in group if f.get("screenshot")), None)
        return ("<tr>"
                f"<td><span class='cat cat-{_esc(first.get('type', 'unknown'))}'>"
                f"{_esc(first.get('type', ''))}</span>{group_badge}</td>"
                f"<td>{_esc(first.get('detail', ''))}</td>"
                f"<td class='muted'>{_esc(first.get('path', ''))}{multi}</td>"
                f"<td>{_img(artifacts_dir, shot_rel, budget)}</td></tr>")

    groups = _merge_findings(findings)
    rows = "".join(_finding_row(g) for g in groups) or \
        "<tr><td colspan='4' class='muted'>本轮未发现异常</td></tr>"
    page_rows = "".join(
        f"<tr><td>L{p.get('depth', '?')}</td><td>{_esc(_page_label(p))}</td>"
        f"<td class='muted'>{_esc(' / '.join((p.get('structure') or {}).get('key_elements', [])[:4]))}</td>"
        f"<td class='muted'>{_esc(p.get('parent_action', '') or '启动/首页')}</td>"
        f"<td>{_img(artifacts_dir, p.get('screenshot'), budget)}</td></tr>"
        for p in sorted(report.get("pages", []), key=lambda x: (x.get("depth", 0), x.get("step", 0))))
    edge_count = len(report.get("edges", []))
    ignored_n = sum(1 for f in findings
                    if (f.get("calibration") or {}).get("verdict") == "ignored")
    ignored_bit = f" · 已忽略 {ignored_n} 处" if ignored_n else ""
    # 计数口径与列表对齐（发起人 2026-10-13）：头部「发现 N 处」用合并后数目，
    # 原始处数并入括注——列表、头部、下方合并表三个数字不再打架
    merge_bit = (f"（原始 {len(findings)} 处，同因合并）" if len(groups) < len(findings) else "")
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'><title>探索测试报告</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>探索测试报告 · {_esc(report.get('package', ''))}</h1>"
        f"<div class='meta'>步数 {report.get('steps')} · 页面 {report.get('page_count')}（{depth_line}，上限 L{report.get('max_depth', '-')}） · "
        f"跳转 {edge_count} 条 · 用时 {report.get('duration_seconds')}s · 发现 {len(groups)} 处{ignored_bit}{merge_bit} · {_esc(report.get('generated_at', ''))}</div>"
        "<h2>发现</h2><table><tr><th>类型</th><th>说明</th><th>复现路径</th><th>证据</th></tr>" + rows + "</table>"
        "<h2>访问页面（按层级）</h2><table><tr><th>层级</th><th>页面</th><th>关键元素</th><th>到达动作</th><th>截图</th></tr>"
        + page_rows + "</table></body></html>")
    out = artifacts_dir / "report.html"
    out.write_text(doc, encoding="utf-8")
    return out
