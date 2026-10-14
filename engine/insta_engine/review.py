"""人工复核：把图谱里的**页面标题**交给用户过一遍，并把结论落成**可重放的文件**。

## 为什么需要它

图谱的标题原先只有 AI 一个来源（`mat describe`），而 AI 的命名**粒度不可控**：
同一 Activity 下的一批同类页会被按各自的**内容词**分别命名（例如一批搜索结果页
被叫成「A搜索页」「B搜索页」「C搜索页」…，而不是统一的「搜索结果页」），
于是报告与生成的用例都跟着变差。用户**比 AI 更懂自己的 App**，
但此前**没有任何入口**让他把这件事说出来 —— 结论只能靠口头转达、由开发写一次性脚本落盘。

## 三个动作（对应三条命令）

1. `mat review <包名>` —— 渲染一张**自包含**的复核页（截图内联，不依赖网络/相对路径），
   每页一行：截图 + 可编辑的标题。改完点「导出复核结论」下载一份 JSON。
2. `mat review <包名> --apply <导出的 JSON>` —— 把结论落盘到 `<工作区>/review/<包名>.json`
   并应用到图谱（写 `title_source="manual"`，此后 `describe` 不会覆盖）。
3. `mat review <包名> --replay` —— 从 `<工作区>/review/<包名>.json` **重放**。
   ⚠️ 图谱被重建（重跑 `explore`）后人工标题会丢，重放是恢复路径。

## 设计上刻意的取舍

- **不覆盖原文件、不改 AI 的产出**：人工结论单独存一份，AI 的标题仍留在图谱里可追溯。
- **落盘的文件与图谱解耦**：图谱是派生物（会被重跑覆盖），结论是**用户资产**，必须独立保存。
- **重放是幂等的**：重复 `--replay` 结果一致；已经是目标标题的页记为 `unchanged`。
- **找不到的页要出声**：`page_key` 对不上的条目逐条报出来，不静默跳过
  （图谱重跑后 `page_key` 可能变化，静默丢弃等于用户的复核白做）。
"""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path

INDEX_DIR_NAME = Path("artifacts") / "page_index"
REVIEW_DIR_NAME = Path("review")
SHOT_WIDTH = 560          # 缩图宽度：版式与大字标题可辨，单张约 110KB
SHOT_QUALITY = 68


def _checked_pkg(pkg: str) -> str:
    """A3（2026-10-12 评审）：`<pkg>.json` 会直接拼进工作区路径——绝对路径可重置
    拼接结果写到 /etc 下。所有路径构建函数统一过这道闸（形状非法直接报错）。"""
    from .pkgname import safe_package_name

    try:
        return safe_package_name(pkg)
    except ValueError as exc:
        raise ValueError(f"review: {exc}") from exc


def index_path(pkg: str, root: Path) -> Path:
    return root / INDEX_DIR_NAME / f"{_checked_pkg(pkg)}.json"


def review_path(pkg: str, root: Path) -> Path:
    """人工结论的**权威存放位置**（用户资产，独立于图谱）。"""
    return root / REVIEW_DIR_NAME / f"{_checked_pkg(pkg)}.json"


def html_path(pkg: str, root: Path) -> Path:
    return root / "artifacts" / "review" / f"{_checked_pkg(pkg)}.html"


def load_index(pkg: str, root: Path) -> dict:
    path = index_path(pkg, root)
    if not path.exists():
        raise FileNotFoundError(
            f"没有页面图谱：{path}\n先跑 `mat index {pkg}` 或 `mat explore {pkg}` 生成图谱。")
    return json.loads(path.read_text(encoding="utf-8"))


def build_items(index: dict) -> list[dict]:
    """复核页的每一行：页面标识 + 当前标题 + 截图路径。

    `title_source` 一并带出来：配置台要据此标出「哪些标题是人改过的」，
    否则用户看不出自己改过什么（AI 的 `ai` / 人工的 `manual` 混在一起看不出来）。
    """
    out = []
    for p in index.get("pages") or []:
        out.append({
            "page_key": str(p.get("page_key") or ""),
            "title": (p.get("title") or "").strip(),
            "summary": (p.get("summary") or "").strip(),
            "title_source": p.get("title_source") or "",
            "summary_source": p.get("summary_source") or "",
            "activity": (p.get("activity") or "").split(".")[-1],
            "depth": p.get("depth"),
            "screenshot": p.get("screenshot") or "",
        })
    return out


def _thumb_b64(src: Path) -> str | None:
    """缩成小 JPEG 再 base64 —— 内联进 HTML，**不依赖任何外部请求**。

    ⚠️ 为什么必须内联：预览面板不保证按 HTML 所在 URL 解析相对路径，
    用相对路径会变成一排裂图（踩过）。没有 Pillow 时退回原图（体积大但能看）。
    """
    if not src.exists():
        return None
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        return base64.b64encode(src.read_bytes()).decode("ascii")
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            if im.width > SHOT_WIDTH:
                im = im.resize((SHOT_WIDTH, round(im.height * SHOT_WIDTH / im.width)))
            buf = BytesIO()
            im.save(buf, "JPEG", quality=SHOT_QUALITY)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:                      # 坏图不该让整页渲染失败
        return None


def render_html(pkg: str, items: list[dict], root: Path) -> str:
    """自包含复核页：截图内联 + 标题可编辑 + 「导出复核结论」按钮。"""
    shots_root = root / INDEX_DIR_NAME
    rows = []
    for it in items:
        b64 = _thumb_b64(shots_root / it["screenshot"]) if it["screenshot"] else None
        img = (f'<img src="data:image/jpeg;base64,{b64}" alt="">' if b64
               else '<span class="noimg">无截图</span>')
        meta = f'{html.escape(it["activity"])} · depth {it["depth"]}'
        rows.append(
            f'<tr data-title="{html.escape(it["title"])}">'
            f'<td class="s">{img}</td>'
            f'<td class="t"><input class="ti" data-key="{html.escape(it["page_key"])}" '
            f'value="{html.escape(it["title"])}" placeholder="（还没有标题）" '
            f'spellcheck="false"><div class="meta">{meta}</div>'
            f'<div class="sum">{html.escape(it["summary"])}</div></td></tr>')

    named = sum(1 for it in items if it["title"])
    return f"""<!doctype html><html lang="zh"><meta charset="utf-8">
<title>页面复核 · {html.escape(pkg)}</title>
<style>
 body{{font:14px/1.6 -apple-system,"PingFang SC",sans-serif;background:#f6f7f9;color:#1c1f23;
      margin:0;padding:24px 4vw}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#5b6470;margin-bottom:16px}}
 .bar{{position:sticky;top:0;background:#f6f7f9;padding:10px 0;z-index:5;
      display:flex;gap:10px;align-items:center;flex-wrap:wrap;
      border-bottom:1px solid #e3e6ea;margin-bottom:14px}}
 button{{font:inherit;padding:7px 14px;border-radius:8px;border:1px solid #c9ced6;
      background:#fff;cursor:pointer}}
 button.primary{{background:#0a7d32;border-color:#0a7d32;color:#fff;font-weight:600}}
 input[type=search]{{font:inherit;padding:7px 12px;border-radius:8px;
      border:1px solid #c9ced6;min-width:240px}}
 table{{border-collapse:collapse;width:100%;background:#fff;border-radius:10px;
      overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 td{{padding:10px 12px;border-bottom:1px solid #eef0f3;vertical-align:top}}
 td.s{{width:120px}} td.s img{{width:110px;border:1px solid #e3e6ea;border-radius:6px;display:block}}
 .ti{{font:inherit;font-weight:600;width:100%;max-width:420px;padding:7px 10px;
      border:1px solid #c9ced6;border-radius:8px}}
 .ti.changed{{border-color:#0a7d32;background:#f2fbf5}}
 .meta{{color:#9aa3ad;font-size:12px;margin-top:4px}}
 .sum{{color:#5b6470;font-size:13px;margin-top:4px}}
 .noimg{{color:#9aa3ad;font-size:12px}}
 .warn{{background:#fff8e6;border-left:4px solid #e0a800;padding:10px 14px;
      border-radius:6px;margin:0 0 14px;color:#5b4200}}
</style>
<h1>页面复核 · {html.escape(pkg)}</h1>
<div class="sub">共 <b>{len(items)}</b> 页 · 已有标题 <b>{named}</b> 页。
改完点「导出复核结论」，再执行 <code>mat review {html.escape(pkg)} --apply &lt;下载的文件&gt;</code>。</div>
<div class="warn">这些标题是 <b>AI 起的</b>，可能粒度不对（同一类页被按内容词拆成很多名字）。
你比 AI 更懂这个 App ⇒ <b>把同一类页改成同一个名字</b>（例如全部叫「搜索结果页」）最有价值。
留空表示「这页不需要标题」，不会被当成修改。</div>

<div class="bar">
  <button class="primary" id="exp">导出复核结论（下载 JSON）</button>
  <span id="cnt" class="sub" style="margin:0"></span>
  <input type="search" id="q" placeholder="按标题/页面标识筛选…">
  <button id="clr">撤销全部修改</button>
</div>
<table id="tb">{''.join(rows)}</table>

<script>
var PKG = {json.dumps(pkg, ensure_ascii=False)};
function inputs() {{ return Array.prototype.slice.call(document.querySelectorAll('.ti')); }}
function refresh() {{
  var n = 0;
  inputs().forEach(function (i) {{
    var changed = i.value.trim() !== i.getAttribute('data-orig');
    i.classList.toggle('changed', changed);
    if (changed) n++;
  }});
  document.getElementById('cnt').textContent = n ? ('已改 ' + n + ' 页') : '还没有修改';
}}
inputs().forEach(function (i) {{ i.setAttribute('data-orig', i.value); }});
inputs().forEach(function (i) {{ i.addEventListener('input', refresh); }});
refresh();

document.getElementById('clr').onclick = function () {{
  inputs().forEach(function (i) {{ i.value = i.getAttribute('data-orig'); }});
  refresh();
}};
document.getElementById('q').oninput = function () {{
  var q = this.value.trim().toLowerCase();
  Array.prototype.forEach.call(document.querySelectorAll('#tb tr'), function (tr) {{
    var hit = !q || tr.textContent.toLowerCase().indexOf(q) >= 0;
    tr.style.display = hit ? '' : 'none';
  }});
}};
document.getElementById('exp').onclick = function () {{
  var corrections = {{}};
  inputs().forEach(function (i) {{
    var v = i.value.trim();
    if (v !== i.getAttribute('data-orig')) corrections[i.getAttribute('data-key')] = v;
  }});
  var n = Object.keys(corrections).length;
  if (!n) {{ alert('还没有任何修改。'); return; }}
  var payload = {{package: PKG, corrections: corrections,
                  note: '由 mat review 生成；用 mat review <包名> --apply <本文件> 应用'}};
  var blob = new Blob([JSON.stringify(payload, null, 1)], {{type: 'application/json'}});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = PKG + '.review.json';
  document.body.appendChild(a); a.click(); a.remove();
  alert('已导出 ' + n + ' 条修改。\\n接着执行：\\nmat review ' + PKG + ' --apply <下载的文件>');
}};
</script>
</html>"""


def load_corrections(path: Path) -> dict:
    """读一份复核结论。兼容两种形状：`{corrections:{k:v}}` 与裸的 `{k:v}`。

    描述结论（`summary_corrections`，2026-10-13 起）是**平级可选字段**——旧文件
    没有该键时返回空 dict，老调用方（CLI --apply / 配置台列表）零改动。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("复核结论必须是一个 JSON 对象")
    inner = data.get("corrections")
    corrections = inner if isinstance(inner, dict) else data
    summaries = data.get("summary_corrections")
    if not isinstance(summaries, dict):
        summaries = {}
    return {"package": data.get("package") or "", "corrections": corrections,
            "summary_corrections": summaries}


def apply_corrections(index: dict, corrections: dict,
                      summary_corrections: dict | None = None) -> dict:
    """把结论应用到图谱（**改内存里的 dict**，由调用方落盘）。返回逐项统计。

    - `applied`：确实改了标题的页
    - `unchanged`：标题已经是目标值（幂等）
    - `missing`：`page_key` 在图谱里找不到（图谱重跑后 key 可能变）—— **逐条报出来**
    - `summary_applied` / `summary_unchanged`：功能描述的对应统计（标 `manual`，
      此后 `describe` 与 merge 都不覆盖——与人工标题同一条保护规则）。
      描述结论与标题结论共用一个 `missing` 集合：page_key 对不上的页
      无论改的是标题还是描述都算「找不到」，用户看到的告警语义一致。
    """
    by_key = {str(p.get("page_key")): p for p in index.get("pages") or []}
    applied, unchanged, missing = [], [], []
    for key, title in corrections.items():
        page = by_key.get(str(key))
        if page is None:
            missing.append(str(key))
            continue
        new = (title or "").strip()
        if (page.get("title") or "").strip() == new:
            unchanged.append(str(key))
            continue
        page["title"] = new
        page["title_source"] = "manual"      # 人工 ⇒ describe / merge 都不覆盖
        applied.append(str(key))

    summary_applied: list[str] = []
    summary_unchanged: list[str] = []
    for key, summary in (summary_corrections or {}).items():
        page = by_key.get(str(key))
        if page is None:
            if str(key) not in missing:
                missing.append(str(key))
            continue
        new = (summary or "").strip()
        if (page.get("summary") or "").strip() == new:
            summary_unchanged.append(str(key))
            continue
        page["summary"] = new
        page["summary_source"] = "manual"    # 人工 ⇒ describe / merge 都不覆盖
        summary_applied.append(str(key))
    return {"applied": applied, "unchanged": unchanged, "missing": missing,
            "summary_applied": summary_applied, "summary_unchanged": summary_unchanged}


def save_review(pkg: str, corrections: dict, root: Path, note: str = "",
                summary_corrections: dict | None = None) -> Path:
    path = review_path(pkg, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"package": pkg, "note": note, "corrections": corrections}
    if summary_corrections:                  # 无描述结论时不写键，旧文件形状不变
        payload["summary_corrections"] = summary_corrections
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
    return path
