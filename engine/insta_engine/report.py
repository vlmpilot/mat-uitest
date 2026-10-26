"""HTML 报告：单用例报告（步骤表 + 截图 + 类别 + 自愈提案 + logcat 链接）与套件聚合页。

"结果可信"的体验版落点：每步必有截图；失败必分类、必带 logcat 证据；
正式报告中心（归因卡/视频/性能曲线）RPT-*（增强项）。
安全：所有动态字段（含被测 App 界面文本与模型输出）经 html.escape 后才入 HTML。
"""

from __future__ import annotations

import base64
import html
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote as _quote

from .runner import RunResult

# 截图内联预算：超过后剩余截图回退相对链接（防止单文件失控；≤20MB 可邮件/IM 传输）
_INLINE_BUDGET_BYTES = 2 * 1024 * 1024  # P0-5：20MB 报告发不出去也读不动；缩略图内联+原图链接后 2MB 够用
#: 原图内联上限：小于它的直接内联原图，更大的内联 640px 缩略图 + 原图链接
_INLINE_ORIGINAL_MAX = 200_000
_THUMB_WIDTH = 640

_CSS = """
:root{--bg:#f4f5f7;--surface:#fff;--surface2:#eef0f4;
 --fg:#1a1e26;--fg2:#4a525e;--fg3:#8a919c;
 --line:#dfe3e9;--line2:#c9cfd8;
 --accent:#2b5fd9;
 --ok:#15803d;--ok-bg:#e7f4ec;--ok-line:#b8e0c8;
 --bad:#bb2d2d;--bad-bg:#fdeeee;--bad-line:#f2c4c4;
 --warn:#92580a;--warn-bg:#fdf3df;--warn-line:#efd9ae;
 --info:#1d4fd7;--info-bg:#eef3fe;--info-line:#c9d9f6;
 --mono:ui-monospace,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace;
 --r:12px;--rs:8px;--sh:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.07)}
*{box-sizing:border-box}
body{margin:0 auto;max-width:1080px;background:var(--bg);color:var(--fg);
 font:14px/1.6 -apple-system,'PingFang SC','Microsoft YaHei','Segoe UI',sans-serif;
 padding:0 20px 64px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:22px;font-weight:600;letter-spacing:-.01em;margin:0}
h2.sec{font-size:12px;font-weight:600;color:var(--fg3);letter-spacing:.08em;
 margin:32px 0 12px;padding-top:16px;border-top:1px solid var(--line)}
code{font-family:var(--mono);font-size:.92em;background:var(--surface2);
 padding:1px 5px;border-radius:5px;word-break:break-all}
.ic{width:14px;height:14px;flex:none;vertical-align:-2px}
/* 顶栏 */
.top{position:sticky;top:0;z-index:9;display:flex;align-items:center;gap:14px;
 background:rgba(244,245,247,.93);backdrop-filter:blur(6px);
 border-bottom:1px solid var(--line);margin:0 -20px 22px;padding:9px 20px}
.top .brand{font-weight:600;font-size:13px;display:flex;gap:8px;align-items:center}
.top .brand .logo{width:20px;height:20px;border-radius:6px;background:var(--fg);color:#fff;
 display:inline-flex;align-items:center;justify-content:center;font-size:10px;font-weight:700}
.top nav{display:flex;gap:2px;margin-left:auto}
.top nav a{color:var(--fg2);font-size:12.5px;padding:5px 10px;border-radius:7px}
.top nav a:hover{background:var(--surface2);color:var(--fg);text-decoration:none}
/* 锚点落点预留 sticky 顶栏高度，否则模块标题被顶栏遮挡 */
#summary,#attr,#steps,#cases,#video{scroll-margin-top:54px}
@media(prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}}
.share button{font:12.5px -apple-system,'PingFang SC',sans-serif;padding:5px 12px;
 border-radius:7px;border:1px solid var(--line2);background:var(--surface);
 color:var(--fg);cursor:pointer}
.share button:hover{border-color:var(--accent);color:var(--accent)}
.share span{color:var(--fg3);font-size:12px}
/* 结论 hero */
.hero{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
 box-shadow:var(--sh);padding:20px 22px;margin-bottom:14px}
.vline{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.vline .vicon{width:38px;height:38px;border-radius:10px;display:inline-flex;
 align-items:center;justify-content:center;flex:none}
.vline .vicon .ic{width:20px;height:20px}
.vline .vicon.ok{background:var(--ok-bg);color:var(--ok)}
.vline .vicon.bad{background:var(--bad-bg);color:var(--bad)}
.vline .vicon.skip{background:var(--surface2);color:var(--fg2)}
.vline .vicon.mid{background:var(--warn-bg);color:var(--warn)}
.vline .vt h1{font-size:23px;line-height:1.2}
.vline .sub{color:var(--fg2);font-size:13.5px;margin-top:3px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:10px}
.stat{background:var(--surface2);border-radius:var(--rs);padding:10px 12px}
.stat .k{font-size:11px;color:var(--fg3);letter-spacing:.05em;margin-bottom:2px}
.stat .v{font-size:19px;font-weight:600;font-family:var(--mono);letter-spacing:-.01em}
.stat .v small{font-size:12px;font-weight:500;color:var(--fg2);margin-left:2px}
.stat .s{font-size:11.5px;color:var(--fg3);margin-top:2px}
.stat.ok .v{color:var(--ok)}.stat.bad .v{color:var(--bad)}
.metaline{margin-top:14px;color:var(--fg2);font-size:12.5px;display:flex;
 flex-wrap:wrap;gap:6px 16px;align-items:center}
.metaline b{color:var(--fg);font-weight:500}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--surface2);margin-top:14px}
.bar-legend{font-size:12px;color:var(--fg2);margin-top:6px}
/* 分类 chip（浅底深字，替代白字饱和底） */
.chip{display:inline-flex;align-items:center;gap:4px;padding:1.5px 9px;border-radius:999px;
 font-size:12px;font-weight:500;border:1px solid transparent;white-space:nowrap}
.chip .ic{width:12px;height:12px}
.chip.ok{background:var(--ok-bg);color:var(--ok);border-color:var(--ok-line)}
.chip.bad{background:var(--bad-bg);color:var(--bad);border-color:var(--bad-line)}
.chip.skip{background:var(--surface2);color:var(--fg2);border-color:var(--line)}
.chip.cal{background:var(--warn-bg);color:var(--warn);border-color:var(--warn-line)}
.chip.gray{background:var(--surface2);color:var(--fg2);border-color:var(--line)}
.cat-app-crash{background:#fdeaea;color:#a32626;border-color:#f3c8c8}
.cat-env-failure{background:#fdf3df;color:#8a5a0a;border-color:#efdcae}
.cat-assert-fail{background:#fdeaef;color:#a12854;border-color:#f3c9d8}
.cat-locate-fail{background:#fdf0e4;color:#9c5312;border-color:#f0d6b4}
.cat-api{background:#eeeafb;color:#5b45b4;border-color:#d9cdf4}
.cat-unknown{background:var(--surface2);color:var(--fg2);border-color:var(--line)}
.chip.cat-app-crash,.chip.cat-env-failure,.chip.cat-assert-fail,
.chip.cat-locate-fail,.chip.cat-api,.chip.cat-unknown{border:1px solid}
.pre{margin-top:12px;font-size:12.5px;color:var(--fg2);background:var(--surface2);
 border-radius:var(--rs);padding:8px 12px}
.pre ul{margin:2px 0 0 16px;padding:0}
/* 证据子卡（无彩边，薄底 + 标头行） */
.evcard{border:1px solid var(--line);border-radius:var(--rs);background:var(--surface);
 padding:8px 11px;margin-top:8px;font-size:12.5px}
.evcard .eh{display:flex;gap:6px;align-items:center;font-weight:600;font-size:12px;
 margin-bottom:4px;color:var(--fg2)}
.evcard .eh .ic{width:12px;height:12px}
.evcard.heal{background:var(--info-bg);border-color:var(--info-line)}
.evcard.heal .eh{color:var(--info)}
.evcard.attr{background:var(--info-bg);border-color:var(--info-line)}
.evcard.attr .eh{color:var(--info)}
.evcard .dim{color:var(--fg3)}
.evgrid{display:grid;gap:8px}
/* 失败原因组卡 */
.fcard{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
 box-shadow:var(--sh);padding:14px 16px;margin-bottom:10px}
.fcard .fh{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.fcard .fcount{font-size:12px;color:var(--fg2)}
.fcard .ferr{font-family:var(--mono);font-size:12.5px;color:var(--bad);
 word-break:break-all;margin-bottom:8px}
.fcard .fbody{display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap}
.fcard .fbody>*{min-width:0}
.fcard .fbody .fsteps{flex:1;min-width:220px}
.fcard .fbody .fshot{flex:none;max-width:300px}
/* 表格 */
table{border-collapse:separate;border-spacing:0;width:100%;background:var(--surface);
 border:1px solid var(--line);border-radius:var(--r);overflow:hidden;font-size:13px}
th{background:var(--surface2);text-align:left;font-size:11px;font-weight:600;
 letter-spacing:.05em;color:var(--fg3);padding:8px 11px;white-space:nowrap}
td{padding:10px 11px;border-top:1px solid var(--line);vertical-align:top}
tbody tr:hover td{background:#fafbfd}
table.steps th{background:var(--surface2)}
table.steps tbody tr:hover td{background:#f6f8fb}
table.steps tr.row-fail td{background:#fffcfc}
table.steps tr.row-fail:hover td{background:#fdf6f6}
table.steps tr.hide-ok{display:none}
td.dur{font-family:var(--mono);font-size:12px;color:var(--fg2);white-space:nowrap}
.err{color:var(--bad);white-space:pre-wrap;font-family:var(--mono);font-size:12px;
 word-break:break-all;margin-top:6px}
.warn{color:var(--warn);font-size:12px;margin-top:6px}
.retry{color:var(--fg3);font-size:12px}
.mini{height:6px;border-radius:3px;background:var(--surface2);overflow:hidden;max-width:110px}
.mini span{display:block;height:100%;border-radius:3px}
td.rate{white-space:nowrap}
td.rate .pct{font-family:var(--mono);font-size:12px;margin-right:6px}
/* 工具条 */
.toolbar{display:flex;align-items:center;gap:14px;margin:0 0 10px}
.ftoggle{display:inline-flex;gap:6px;align-items:center;font-size:12.5px;
 color:var(--fg2);cursor:pointer;user-select:none}
.ftoggle input{accent-color:var(--accent)}
td .apihead{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
td .sid,.sid{display:inline-flex;align-items:center;justify-content:center;
 min-width:22px;height:22px;padding:0 5px;border-radius:7px;background:var(--surface2);
 font-family:var(--mono);font-size:12px;font-weight:600;color:var(--fg2);
 white-space:nowrap}
table.steps td:first-child{white-space:nowrap}
table.steps td:first-child .sid{background:var(--fg);color:#fff}
table.steps tr.row-fail td:first-child .sid{background:var(--bad)}
td .am{font-weight:600;font-family:var(--mono);font-size:11px;color:var(--fg);
 background:var(--surface2);padding:1px 6px;border-radius:5px}
td .aurl{font-family:var(--mono);font-size:11.5px;word-break:break-all}
td .astatus{font-family:var(--mono);font-size:11.5px}
td ul.aex{margin:4px 0 0;padding-left:2px;list-style:none}
td ul.aex li{padding:1px 0}
td ul.aex li.ok{color:var(--ok)}td ul.aex li.bad{color:var(--bad)}
td .asave{color:var(--fg3);margin-top:4px;font-size:11.5px}
details.logcat{margin-top:8px}
details.logcat summary{cursor:pointer;color:var(--fg2);font-size:12px}
details.logcat summary:hover{color:var(--fg)}
details.logcat pre{background:#10141b;color:#c9d3e0;padding:10px 12px;
 border-radius:var(--rs);overflow:auto;max-height:280px;white-space:pre-wrap;
 font-size:11.5px;line-height:1.55;margin-top:6px}
details.abody summary{cursor:pointer;color:var(--fg2);font-size:11.5px}
details.abody pre{background:var(--surface2);padding:8px;border-radius:var(--rs);
 overflow:auto;max-height:180px;font-size:11px;margin-top:4px}
details.lglist summary{cursor:pointer;color:var(--fg2);font-size:12px}
.untested summary{cursor:pointer;color:var(--fg2);font-size:12.5px}
.untested table{margin-top:6px}
.untested th,.untested td{padding:4px 8px;font-size:12px}
.hint{color:var(--fg3);font-size:12px;margin:8px 0 0}
a.orig{font-size:11px;margin-left:5px}
.video h3{font-size:13px;margin:0 0 6px}
.video video{max-width:430px;width:100%;display:block;margin:8px 0;border-radius:var(--rs);
 border:1px solid var(--line)}
.muted{color:var(--fg3)}
img{max-width:200px;border:1px solid var(--line);border-radius:var(--rs);display:block;
 cursor:zoom-in;transition:border-color .12s}
img:hover{border-color:var(--accent)}
#lightbox{position:fixed;inset:0;background:rgba(10,12,16,.8);display:none;
 align-items:center;justify-content:center;z-index:99;cursor:zoom-out}
#lightbox.on{display:flex}
#lightbox img{max-width:92vw;max-height:92vh;width:auto;border-color:var(--line2);
 box-shadow:0 12px 48px rgba(0,0,0,.4);cursor:default}
@media(max-width:760px){body{padding:0 12px 48px}.top{margin:0 -12px 18px;padding:9px 12px}
 .top nav{display:none}.stats{grid-template-columns:repeat(2,1fr)}
 .fcard .fbody{flex-direction:column}}
@media print{body{background:#fff;max-width:none;padding:0 6mm}
 #lightbox{display:none!important}img{cursor:default;border-color:var(--line)}
 .top,.toolbar,.share{display:none}
 .hero,.fcard{box-shadow:none;break-inside:avoid}
 img{max-width:160px}details.logcat{display:none}
 h2.sec{border-top:none;padding-top:8px;margin-top:18px}}
"""

_CATEGORY_LABELS = {
    "app-crash": "产品 bug(崩溃)",
    "env-failure": "环境问题",
    "assert-fail": "断言失败",
    "locate-fail": "定位失败",
    "api": "接口断言失败",
    "unknown": "待归因",
}

_CATEGORY_COLORS = {
    "app-crash": "#c03434",
    "env-failure": "#c8912a",
    "assert-fail": "#c74d74",
    "locate-fail": "#d78a3c",
    "api": "#7a63d2",
    "unknown": "#8a919c",
}


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


# 单色 SVG 图标（1.7px stroke / currentColor）——替代 emoji 当 UI 元素（反 AI 味清单 #3）
_IC_PATHS = {
    "ok": '<path d="M4 12.5l5 5 11-11"/>',
    "bad": '<path d="M6 6l12 12M18 6L6 18"/>',
    "skip": '<path d="M5 5l7 7-7 7M13 5l7 7-7 7"/>',
    "warn": '<path d="M12 3L2 21h20L12 3z"/><path d="M12 10v5M12 18.2v.1"/>',
    "heal": '<path d="M14.7 6.3a4.5 4.5 0 00-6.4 6.4L3 18v3h3l8.3-5.3a4.5 4.5 0 006.4-6.4l-3.2 3.2-2.8-2.8 3-3.4z"/>',
    "attr": '<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5 5-2z"/>',
    "api": '<path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18"/><circle cx="12" cy="12" r="9"/>',
}


def _ic(name: str, cls: str = "ic") -> str:
    return (f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.7" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            f"{_IC_PATHS[name]}</svg>")


# 大图灯箱：点击证据截图 → 全屏覆盖层放大显示（复用内嵌 data URI，不依赖旁边产物文件）；
# Esc / 点击任意处关闭。仅 ~0.6KB，两个含图渲染器共用。
_LB_JS = ("""<script>(function(){
var lb=document.createElement('div');lb.id='lightbox';lb.innerHTML='<img alt="大图预览">';
lb.addEventListener('click',function(){lb.classList.remove('on')});
document.addEventListener('keydown',function(e){if(e.key==='Escape')lb.classList.remove('on')});
document.addEventListener('click',function(e){
var t=e.target;
if(t.tagName==='IMG'&&t.hasAttribute('data-lb')){lb.querySelector('img').src=t.currentSrc||t.src;lb.classList.add('on')}});
document.body.appendChild(lb);})();</script>""")


_STEP_ID_RE = re.compile(r"s(\d+)")


def _step_num(step_id: str) -> str:
    """步骤显示名：默认生成格式 s<N> 只显示数字（表格窄列不换行、扫读快）；
    导入用例的自定义 id 原样显示。完整 id 保留在 title 供追溯（校准/编辑深链仍用它）。"""
    m = _STEP_ID_RE.fullmatch(step_id.strip())
    return m.group(1) if m else _esc(step_id)


def _verdict_icon(status: str) -> str:
    return _ic({"passed": "ok", "failed": "bad", "skipped": "skip"}
               .get(status, "warn"))


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


def _thumb_jpeg(data: bytes) -> bytes | None:
    """大图 → 640px JPEG 缩略图（内联用）；坏图/无 PIL 返回 None（调用方回退文件链接）。"""
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(data))
        im.thumbnail((_THUMB_WIDTH, 10_000))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=70)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 —— 报告生成不能被单张坏图打死
        return None


def _img(artifacts_dir: Path, rel: str | None, budget: list[int]) -> str:
    if not rel:
        return "（无截图）"
    path = artifacts_dir / rel
    try:
        data = path.read_bytes()
    except OSError:
        return f'<a href="{_esc(rel)}">截图（缺失）</a>'
    if data and len(data) <= _INLINE_ORIGINAL_MAX and len(data) <= budget[0]:
        budget[0] -= len(data)
        src = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        return (f'<img data-lb src="{src}" alt="step screenshot" '
                f'title="点击查看大图（完整分辨率）">')
    thumb = _thumb_jpeg(data)
    if thumb and len(thumb) <= budget[0]:
        budget[0] -= len(thumb)
        src = "data:image/jpeg;base64," + base64.b64encode(thumb).decode("ascii")
        return (f'<img data-lb src="{src}" alt="step screenshot（缩略图）" '
                f'title="点击查看大图（640px 缩略分辨率；旁链原图需同目录产物）">'
                f'<a class="orig" href="{_esc(rel)}" target="_blank">原图</a>')
    return f'<a href="{_esc(rel)}" target="_blank">截图（文件）</a>'


def _screen_link(rel: str | None) -> str:
    """失败步的**控件树证据**（动作前后两屏的 text/desc/res-id）。

    为什么不内联：它是 JSON（一份 30-50KB），内联进报告会把体积预算吃掉 —— 而截图才是
    第一眼要看的东西。给个相对链接（同「原图」的口径），要看的人自己点。

    为什么必须有入口：断言写错时，"正确的断言该写什么"只能从这一屏推出来
    （`scripts/fix_assertions_from_run.py` 就是吃它的）。藏在目录里等于没有。
    """
    if not rel:
        return ""
    return (f'<div class="evcard"><a href="{_esc(rel)}" target="_blank">控件树证据</a>'
            f"<span class='eh'>（动作前后两屏；可用 `mat` 脚本提断言候选）</span></div>")


def _logcat(artifacts_dir: Path, rel: str | None) -> str:
    """失败步骤的**系统日志**：文件存在→内联到 <details>（报告自包含）；缺失→保留链接。

    文件名沿用 `*.logcat.txt`（历史产物兼容），但内容平台相关：Android=logcat，
    iOS=设备 syslog（P2.2）。所以**展示文案统一叫「系统日志」**，不写死 logcat。
    """
    if not rel:
        return "—"
    try:
        content = (artifacts_dir / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f'<a href="{_esc(rel)}">系统日志</a>'
    if not content.strip():
        return f'<a href="{_esc(rel)}">系统日志</a>'
    return (f'<details class="logcat"><summary>系统日志（{_esc(rel)}）</summary>'
            f"<pre>{_esc(content[-8000:])}</pre></details>")


def _cat_bar(failed_categories: dict) -> str:
    """失败分类堆叠条：宽度按占比；全绿（无失败）显示细绿条。"""
    total = sum(failed_categories.values())
    if not total:
        return '<div class=\'bar\'><span style=\'width:100%;background:var(--ok)\'></span></div>'
    spans = "".join(
        f'<span style="width:{v / total * 100:.1f}%;background:{_CATEGORY_COLORS.get(k, "#8a919c")}"></span>'
        for k, v in sorted(failed_categories.items(), key=lambda kv: -kv[1]))
    legend = " · ".join(f"{_CATEGORY_LABELS.get(k, k)}×{v}" for k, v in
                        sorted(failed_categories.items(), key=lambda kv: -kv[1]))
    return f'<div class=\'bar\'>{spans}</div><div class=\'bar-legend\'>{_esc(legend)}</div>'


def _badge(status: str) -> str:
    if status == "passed":
        return f'<span class="chip ok">{_ic("ok")}通过</span>'
    if status == "skipped":
        return f'<span class="chip skip">{_ic("skip")}未执行</span>'
    return f'<span class="chip bad">{_ic("bad")}失败</span>'


def _category_html(category: str | None, source: str | None = None) -> str:
    if not category:
        return "—"
    label = _CATEGORY_LABELS.get(category, category)
    # `source="human"` = 人工校准改判过 ⇒ 标出来（否则看不出这条是自己判的还是机器判的）
    mark = (' <span class="chip cal" title="人工校准改判">人工</span>'
            if source == "human" else "")
    return f'<span class="chip cat-{_esc(category)}">{_esc(label)}</span>{mark}'


def _api_card(api: dict | None) -> str:
    """P1-1：api 步骤的接口证据卡（方法/URL/状态/断言明细/变量提取/响应体片段）。

    api 字典内的头已由 api_client 打码、响应体已截断——这里只做展示。
    """
    if not api:
        return ""
    method = _esc(str(api.get("method") or "GET"))
    url = _esc(str(api.get("url") or "")[:180])
    status = api.get("status")
    status_text = _esc(str(status)) if status is not None else "—"
    parts = [f"<div class='apicard evcard'><div class='apihead'>"
             f"<span class='am'>{method}</span>"
             f"<span class='aurl'>{url}</span>"
             f"<span class='astatus'>→ <b>{status_text}</b> · {int(api.get('duration_ms') or 0)}ms</span></div>"]
    if api.get("error"):
        parts.append(f"<div class='apierr err'>{_esc(api['error'])}</div>")
    exps = api.get("expect") or []
    if exps:
        items = "".join(
            f"<li class='{'ok' if e.get('passed') else 'bad'}'>{_esc(e.get('desc') or '')}</li>"
            for e in exps)
        parts.append(f"<ul class='aex'>{items}</ul>")
    saved = api.get("saved") or {}
    if saved:
        kv = "、".join(f"{_esc(k)}←{_esc(v)}" for k, v in saved.items())
        parts.append(f"<div class='asave'>变量提取: {kv}</div>")
    snippet = api.get("body_snippet")
    if snippet:
        parts.append(f"<details class='abody'><summary>响应体片段</summary>"
                     f"<pre>{_esc(snippet)}</pre></details>")
    parts.append("</div>")
    return "".join(parts)


def _load_attributions(artifacts_dir: Path) -> dict:
    """读取 diagnose.json（LLM 归因卡，mat diagnose 产物）；缺失/损坏返回空。"""
    try:
        data = json.loads((artifacts_dir / "diagnose.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _attr_card(attr: dict | None) -> str:
    """归因卡：LLM 四分类 + 置信度 + 证据 + 建议（错误成本不对称：低置信必标人工）。"""
    if not attr:
        return ""
    label = {"app-bug": "真 bug", "ui-change": "UI 改版", "env-flake": "环境抖动",
             "precondition": "前置缺失"}.get(attr.get("category", ""), attr.get("category", "—"))
    conf = attr.get("confidence")
    conf_text = f"{conf:.0%}" if isinstance(conf, (int, float)) else "—"
    need = (f' <span class="chip cal">{_ic("warn")}建议人工确认</span>'
            if attr.get("need_human") else "")
    evidence = "".join(f'<div class="dim">证据: {_esc(e)}</div>'
                       for e in attr.get("evidence", []))
    suggestion = (f'<div>建议: {_esc(attr["suggestion"])}</div>'
                  if attr.get("suggestion") else "")
    source = "AI 归因" if attr.get("source") == "llm" else "人工回退"
    return (f'<div class="evcard attr">'
            f'<div class="eh">{_ic("attr")}{source} · {_esc(label)}'
            f'<span style="color:var(--fg)">置信度 {conf_text}</span>{need}</div>'
            f'{evidence}{suggestion}</div>')


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


def render_summary_md(run: RunResult) -> str:
    """给人和 Agent 读的结论摘要（与报告同口径；报告生成时一并落 summary.md）。"""
    total = len(run.steps)
    passed = sum(1 for s in run.steps if s.status == "passed")
    failed = sum(1 for s in run.steps if s.status == "failed")
    skipped = sum(1 for s in run.steps if s.status == "skipped")
    verdict = {"passed": "通过", "failed": "未通过",
               "skipped": "未执行"}.get(run.status, "已中止")
    cats = run.failed_categories or {}
    cat_text = "、".join(f"{k} ×{v}"
                          for k, v in sorted(cats.items(), key=lambda kv: -kv[1])) or "—"
    lines = [
        f"# MAT 执行结论 — {run.case_name or run.case_id} — {verdict}",
        "",
        f"- 步骤: {passed} 通过 / {failed} 失败 / {skipped} 未执行（共 {total}）",
        f"- 步骤成功率: {run.step_success_rate:.0%}",
        f"- 失败分类: {cat_text}",
        f"- 耗时: {run.duration_ms}ms",
    ]
    device = getattr(run, "device", None) or {}
    dev_bits = " · ".join(str(device[k]) for k in ("model", "platform", "os_version")
                            if device.get(k))
    if dev_bits:
        lines.append(f"- 设备: {dev_bits}")
    shots = sum(1 for s in run.steps if s.screenshot)
    failed_steps = [s for s in run.steps if s.status == "failed"]
    logs = sum(1 for s in failed_steps if s.logcat_file)
    lines.append(f"- 证据覆盖: 截图 {shots}/{total} · 失败步系统日志 {logs}/{len(failed_steps)}")
    lines.append("")
    if skipped:
        lines.append("## 未测/未执行")
        lines.append("")
        for s in run.steps:
            if s.status == "skipped":
                reason = (s.error or s.warning or "")[:120]
                lines.append(f"- `{s.step_id}` {s.intent or ''} — {reason}")
        lines.append("")
        lines.append("> 以上步骤本轮未取得结论；引用回归结论时必须连同本清单一起说。")
    else:
        lines.append("（本轮没有跳过的步骤——不存在「没测就下结论」的项。）")
    return "\n".join(lines) + "\n"


def render_html(run: RunResult, artifacts_dir: Path) -> Path:
    budget = [_INLINE_BUDGET_BYTES]
    attributions = _load_attributions(artifacts_dir)
    # 执行录屏：段文件在 artifacts/video/，报告相对引用——
    # 视频不内联（单文件预算装不下），录屏关闭时目录不存在 ⇒ 此节为空。
    video_dir = artifacts_dir / "video"
    video_html = ""
    if video_dir.is_dir():
        vids = sorted(video_dir.glob("*.mp4"))
        if vids:
            players = "".join(
                f'<video controls preload="metadata" src="video/{_esc(v.name)}"></video>'
                for v in vids)
            video_html = (f"<section class='video' id='video'><h2 class='sec'>执行录屏</h2>"
                          f"{players}</section>")
    rows: list[str] = []
    heal_count = 0
    for step in run.steps:
        shot = _img(artifacts_dir, step.screenshot, budget)
        warn = f'<div class="warn">{_ic("warn")} {_esc(step.warning)}</div>' if step.warning else ""
        err = f'<div class="err">{_esc(step.error)}</div>' if step.error else ""
        # 校准标记（发起人 2026-10-13）：可忽略 / 误判 / 校准原因——只加标注，
        # 不改 status、不改成功率统计（报告列表对账口径不变）
        cal_marks = ""
        if step.ignored:
            cal_marks += ' <span class="chip gray" title="人工校准：可忽略">已忽略</span>'
        if step.false_positive:
            cal_marks += ' <span class="chip bad" title="人工校准：通过是误判">误判</span>'
        note = (f'<div class="warn">校准原因: {_esc(step.calibration_note)}</div>'
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
                f'<div class="evcard heal"><div class="eh">{_ic("heal")}自愈提案 · '
                f'{_esc(via_label)} · 待确认回写</div>'
                f"<code>{_esc(step.heal['original'])}</code> → <code>{_esc(step.heal['match'])}</code></div>"
            )
        api_card = _api_card(getattr(step, "api", None))
        screen_link = _screen_link(getattr(step, "screen", None))
        evcards = (f"{err}{warn}{note}{screen_link}{heal}{api_card}"
                   f"{_attr_card(attributions.get(step.step_id))}")
        ev_cell = (f'<div class="evgrid">{shot}{evcards}</div>'
                   if (shot != "（无截图）" or evcards) else shot)
        row_cls = "row-fail" if step.status == "failed" else "row-ok"
        data_ok = "0" if step.status == "failed" else "1"
        rows.append(
            f"<tr class=\"{row_cls}\" data-ok=\"{data_ok}\">"
            f"<td><span class='sid' title='步骤 id: {_esc(step.step_id)}'>"
            f"{_step_num(step.step_id)}</span></td>"
            f"<td>{_esc(step.intent)}{retry}{edit_link}</td>"
            f"<td><code>{_esc(step.action)}</code>{strategy}</td>"
            f"<td>{_badge(step.status)}{cal_marks}<br>{_category_html(step.category, step.category_source)}</td>"
            f"<td class=\"dur\">{step.duration_ms}ms</td>"
            f"<td>{ev_cell}</td>"
            f"<td>{logcat}</td>"
            "</tr>"
        )

    # 失败原因汇总（同类合并，发起人 2026-10-13）：同分类且归一化同因的失败步骤并成一组卡
    # ——「同一个页面同一个异常」一眼看全；下方步骤明细表保持逐条证据链不动。
    # AI 归因提升到组卡内（原在明细表最底部等于看不见）；section 挂 id=attr 供锚跳。
    fail_html = ""
    if (fg := _fail_groups(run.steps)):
        cards = "".join(
            "<article class='fcard'>"
            f"<div class='fh'>{_category_html(g['category'])}"
            f"<span class='fcount'>{len(g['steps'])} 步</span></div>"
            f"<div class='ferr' title='{_esc(g['steps'][0].error or '')}'>"
            f"{_esc(_first_line(g['steps'][0].error)[:220])}</div>"
            f"{_attr_card(_group_attr(g['steps'], attributions))}"
            "<div class='fbody'><div class='fsteps'>"
            f"<details class='lglist'><summary>涉及步骤（{len(g['steps'])}）</summary>"
            + "".join(f"<div style='margin:3px 0'><span class='sid'>{_step_num(s.step_id)}</span> "
                      f"{_esc(s.intent)}</div>" for s in g["steps"])
            + "</details></div>"
            f"<div class='fshot'>{_img(artifacts_dir, g['steps'][0].screenshot, budget)}</div>"
            "</div></article>"
            for g in fg)
        fail_html = ("<section id='attr'><h2 class='sec'>失败原因汇总（同类合并）</h2>"
                     + cards + "</section>")

    perf_parts = []
    if run.perf.get("launch_ms") is not None:
        perf_parts.append(f"<span>启动 {run.perf['launch_ms']}ms</span>")
    if run.perf.get("mem_pss_mb") is not None:
        perf_parts.append(f"<span>内存 {run.perf['mem_pss_mb']:.0f}MB</span>")
    perf_html = ("".join(perf_parts)) if perf_parts else ""
    heal_meta = f"<span>自愈提案 <b>{heal_count}</b> 条</span>" if heal_count else ""
    cancelled_meta = (f"<span>已中止（完成 {len(run.steps)} 步）</span>"
                      if run.status == "cancelled" else "")
    case_label = _esc(run.case_name or run.case_id)
    status_label = {"passed": "通过", "failed": "失败",
                    "skipped": "未执行"}.get(run.status, "已中止")
    title = f"{case_label} — {status_label}"

    # 结论 hero（P0-5 升级）：一屏回答「过没过、挂在哪、证据全不全、什么没测」
    total = len(run.steps)
    passed = sum(1 for s in run.steps if s.status == "passed")
    failed = sum(1 for s in run.steps if s.status == "failed")
    skipped = sum(1 for s in run.steps if s.status == "skipped")
    shots = sum(1 for s in run.steps if s.screenshot)
    failed_steps = [s for s in run.steps if s.status == "failed"]
    logs = sum(1 for s in failed_steps if s.logcat_file)
    verdict_text = {"passed": "通过", "failed": "未通过",
                    "skipped": "未执行"}.get(run.status, "已中止")
    verdict_cls = {"passed": "ok", "failed": "bad",
                   "skipped": "skip"}.get(run.status, "mid")
    tiles = (
        f"<div class='stat ok'><div class='k'>通过</div><div class='v'>{passed}</div></div>"
        f"<div class='stat bad'><div class='k'>失败</div><div class='v'>{failed}</div></div>"
        f"<div class='stat'><div class='k'>未执行</div><div class='v'>{skipped}</div></div>"
        f"<div class='stat'><div class='k'>步骤成功率</div><div class='v'>{run.step_success_rate:.0%}</div></div>"
        f"<div class='stat'><div class='k'>耗时</div><div class='v'>{run.duration_ms}<small>ms</small></div></div>"
        f"<div class='stat'><div class='k'>证据覆盖</div><div class='v'>{shots}<small>/{total} 截图</small></div>"
        f"<div class='s'>失败步系统日志 {logs}/{len(failed_steps)}</div></div>"
    )
    if skipped:
        skipped_rows = "".join(
            "<tr>"
            f"<td><span class='sid' title='步骤 id: {_esc(s.step_id)}'>{_step_num(s.step_id)}</span></td>"
            f"<td>{_esc(s.intent or '')}</td>"
            f"<td>{_esc((s.error or s.warning or '')[:120])}</td>"
            "</tr>"
            for s in run.steps if s.status == "skipped")
        untested = ("<details class='untested'>"
                    f"<summary>未测/未执行（{skipped}）</summary>"
                    "<table><tr><th>步骤</th><th>意图</th><th>原因</th></tr>"
                    f"{skipped_rows}</table>"
                    "<p class='hint'>以上步骤本轮未取得结论；"
                    "引用回归结论时必须连同本清单一起说。</p></details>")
    else:
        untested = "<p class='hint'>本轮没有跳过的步骤——不存在「没测就下结论」的项。</p>"
    pre_html = ""
    if run.preconditions:
        items = "".join(f"<li>{_esc(p)}</li>" for p in run.preconditions)
        pre_html = ("<div class='pre'>前置条件（跑之前设备该处于的状态）："
                    f"<ul>{items}</ul></div>")
    nav_attr = "<a href='#attr'>失败原因</a>" if fail_html else ""
    nav_video = "<a href='#video'>录屏</a>" if video_html else ""
    doc = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title><style>{_CSS}</style></head><body>"
        "<header class='top'>"
        "<span class='brand'><span class='logo'>M</span>MAT · 测试报告</span>"
        f"<nav><a href='#summary'>结论</a>{nav_attr}<a href='#steps'>步骤明细</a>{nav_video}</nav>"
        "<div class='share'><button onclick='navigator.clipboard.writeText(location.href).then("
        "function(){var b=document.querySelector(\".share button\");"
        "b.textContent=\"已复制链接\"});'"
        " title='控制台以 --share 启动时，同一 Wi-Fi 的同事打开此链接即可查看；数据不出内网'>"
        "复制链接</button></div>"
        "</header>"
        "<main>"
        "<section class='hero' id='summary'>"
        f"<div class='vline'><span class='vicon {verdict_cls}'>"
        f"{_verdict_icon(run.status)}</span>"
        f"<div class='vt'><h1>{verdict_text}</h1><div class='sub'>{case_label}</div></div></div>"
        f"<div class='stats'>{tiles}</div>"
        f"<div class='metaline'><span>case <b>{_esc(run.case_id)}</b></span>"
        + (f"<span>执行人 <b>{_esc(run.operator)}</b></span>"
           if getattr(run, "operator", "") else "")
        + f"{perf_html}{heal_meta}{cancelled_meta}"
        + "<span>MAT 报告</span></div>"
        + _cat_bar(run.failed_categories)
        + untested
        + pre_html
        + "</section>"
        + video_html
        + fail_html
        + "<section id='steps'><h2 class='sec'>步骤明细（" + str(total) + "）</h2>"
        + "<div class='toolbar'><label class='ftoggle'>"
        + "<input type='checkbox' onchange='for(var r of document.querySelectorAll(\"tr[data-ok]\"))"
        + "r.classList.toggle(\"hide-ok\",this.checked&&r.dataset.ok===\"1\")'>只看失败</label>"
        + f"<span class='muted' style='font-size:12px'>共 {total} 步 · 失败行的「编辑此步」直达用例对应步骤</span></div>"
        + "<table class='steps'><thead><tr><th>步骤</th><th>意图</th><th>动作/定位</th>"
        "<th>结果/分类</th><th>耗时</th><th>证据</th><th>日志</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></section>"
        + "</main>" + _LB_JS + "</body></html>"
    )

    out = artifacts_dir / "report.html"
    out.write_text(doc, encoding="utf-8")
    try:
        (artifacts_dir / "summary.md").write_text(render_summary_md(run), encoding="utf-8")
    except OSError:
        pass  # 报告主体已落盘；摘要写失败不影响主流程
    return out


def render_index_html(suite_name: str, entries: list[tuple[str, str, RunResult]], artifacts_dir: Path) -> Path:
    """套件聚合页：entries = [(用例名, 相对报告路径, RunResult)]。

    批次聚合报告增强（发起人 2026-10-12「第二步」）：自包含单文件、可直接发给开发定位——
    汇总 tiles（通过/失败/中止计数 + 总耗时 + 设备 + 生成时间）+ 逐条明细加
    「失败步骤」「失败原因」列与步骤成功率迷你条。
    """
    import json as _json

    # 设备 best-effort：从各条产物 run.json 收集 device_serial（CLI/配置台双入口都写），
    # 读不到不影响生成（如仅拿到内存 entries 的测试场景）。
    # 路径基准：rel 相对**聚合页所在目录**——console 批次产物在 artifacts/ 顶层、
    # 聚合页在 artifacts/<套件名>/，rel 带 ../ 前缀（兄弟目录）⇒ 必须整段拼接后取
    # parent，不能只取首段（首段是 ".." 时拼出来的 run.json 永远不存在，设备列恒空）
    devices: list[str] = []
    dev_meta: dict = {}
    for _name, rel_report, _r in entries:
        try:
            data = _json.loads(((artifacts_dir / rel_report).parent / "run.json")
                               .read_text(encoding="utf-8"))
            serial = data.get("device_serial")
            if serial and serial not in devices:
                devices.append(str(serial))
            if not dev_meta and isinstance(data.get("device"), dict):
                dev_meta = {**data["device"], "platform": data.get("platform", "")}
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
        rate = run.step_success_rate
        bar_color = "var(--ok)" if failed_steps == 0 else "var(--bad)"
        rows.append(
            "<tr>"
            f"<td>{_esc(name)}</td><td><code>{_esc(run.case_id)}</code></td>"
            f"<td>{_badge(run.status)}</td>"
            f"<td class='rate'><span class='pct'>{rate:.0%}</span>"
            f"<div class='mini'><span style='width:{rate * 100:.0f}%;background:{bar_color}'></span></div></td>"
            f"<td>{failed_steps}/{total_steps}</td>"
            f"<td>{_esc(cats)}</td>"
            f"<td>{err_cell}</td>"
            f"<td class='dur'>{run.duration_ms}ms</td>"
            f"<td><a href='{_esc(rel_report)}'>报告</a></td>"
            "</tr>"
        )

    # 失败原因汇总（同类合并）：按「首个失败步骤的分类+归一化原因」聚类用例——
    # 同因 N 条并一行（点开列出各条报告链接）；逐条明细表保持不动
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
        cards = "".join(
            "<article class='fcard'>"
            f"<div class='fh'>{_category_html(k[0])}"
            f"<span class='fcount'>{len(case_groups[k])} 条用例</span></div>"
            f"<div class='ferr' title='{_esc(case_groups[k][0][2].error or '')}'>"
            f"{_esc(_first_line(case_groups[k][0][2].error)[:220])}</div>"
            "<div class='fsteps'><details class='lglist'>"
            f"<summary>涉及用例（{len(case_groups[k])}）</summary>"
            + "".join(f"<div style='margin:3px 0'><a href='{_esc(rel)}'>{_esc(nm)}</a></div>"
                      for nm, rel, _s in case_groups[k])
            + "</details></div></article>"
            for k in case_order)
        case_summary = ("<section><h2 class='sec'>失败原因汇总（同类合并）</h2>"
                        + cards + "</section>")

    dev_bit = f"<span>设备 <b>{_esc('、'.join(devices))}</b></span>" if devices else ""
    if dev_meta:
        bits = [dev_meta.get("model") or ""]
        osv = dev_meta.get("os_version") or ""
        if osv:
            # 平台名按惯例大小写（"ios 26.5.2" 看着像内部字段漏出来了）
            plat = {"ios": "iOS", "android": "Android"}.get(
                (dev_meta.get("platform") or "").lower(), dev_meta.get("platform") or "OS")
            bits.append(f"{plat} {osv}")
        bits = [b for b in bits if b]
        if bits:
            dev_bit += f"<span>（{_esc(' · '.join(bits))}）</span>"
    title = f"{_esc(suite_name)} — {passed}/{len(entries)} 通过"
    overall_cls = "ok" if failed == 0 and cancelled == 0 else "bad"
    doc = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title><style>{_CSS}</style></head><body>"
        "<header class='top'>"
        "<span class='brand'><span class='logo'>M</span>MAT · 套件聚合报告</span>"
        "<nav><a href='#summary'>汇总</a><a href='#cases'>用例</a>"
        + ("<a href='#attr'>失败原因</a>" if case_summary else "")
        + "</nav></header>"
        "<main>"
        "<section class='hero' id='summary'>"
        f"<div class='vline'><span class='vicon {overall_cls}'>{_verdict_icon('passed' if failed == 0 and cancelled == 0 else 'failed')}</span>"
        f"<div class='vt'><h1>{_esc(suite_name)}</h1>"
        f"<div class='sub'>{passed}/{len(entries)} 通过 · 生成于 {gen_at}</div></div></div>"
        "<div class='stats'>"
        f"<div class='stat ok'><div class='k'>通过</div><div class='v'>{passed}</div></div>"
        f"<div class='stat bad'><div class='k'>失败</div><div class='v'>{failed}</div></div>"
        f"<div class='stat'><div class='k'>中止</div><div class='v'>{cancelled}</div></div>"
        f"<div class='stat'><div class='k'>总耗时</div><div class='v'>{total_ms / 1000:.1f}<small>s</small></div></div>"
        "</div>"
        f"<div class='metaline'>{dev_bit}<span>MAT 套件聚合报告</span></div>"
        "</section>"
        "<section id='cases'><h2 class='sec'>用例明细</h2>"
        "<table><thead><tr><th>用例</th><th>case</th><th>结果</th><th>步骤成功率</th>"
        "<th>失败步骤</th><th>失败分类</th><th>失败原因</th><th>耗时</th><th></th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></section>"
        + case_summary
        + "</main></body></html>"
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
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'><title>探索测试报告</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>探索测试报告 · {_esc(report.get('package', ''))}</h1>"
        f"<div class='metaline' style='margin:6px 0 0'><span>步数 {report.get('steps')}</span>"
        f"<span>页面 {report.get('page_count')}（{depth_line}，上限 L{report.get('max_depth', '-')})</span>"
        f"<span>跳转 {edge_count} 条</span><span>用时 {report.get('duration_seconds')}s</span>"
        f"<span>发现 {len(groups)} 处{ignored_bit}{merge_bit}</span>"
        f"<span>{_esc(report.get('generated_at', ''))}</span></div>"
        "<section><h2 class='sec'>发现</h2><table><tr><th>类型</th><th>说明</th><th>复现路径</th><th>证据</th></tr>" + rows + "</table></section>"
        "</section><section><h2 class='sec'>访问页面（按层级）</h2><table><tr><th>层级</th><th>页面</th><th>关键元素</th><th>到达动作</th><th>截图</th></tr>"
        + page_rows + "</table></section>" + _LB_JS + "</body></html>")
    out = artifacts_dir / "report.html"
    out.write_text(doc, encoding="utf-8")
    return out
