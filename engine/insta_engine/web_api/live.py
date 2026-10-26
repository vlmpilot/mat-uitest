"""任务实时视图页：`/live/<job_id>`（2026-10-24，T4 演示包组件）。

给**人**看 agent/控制台跑长任务时的旁路视图：设备投屏（复用 `/api/screenshot`）
+ 步骤事件流（复用 `/api/job/events` 增量轮询）。**agent 自己不需要它**——它的
ground truth 是工具返回值；这个页面是监督/演示用的人机界面。

## 门控口径（2026-10-25 评审 P1 修正）

本页**只在口令门之后可用**（`--share` 下走门槛页，与报告直达链接同待遇）——
页面 HTML 内嵌 BOOT 元数据（serial/job_id/link），serial 是设备唯一标识，未认证
不可读。⚠️ 教训：首版注释曾声称"被同一道门罩住"，但 `_auth_gate_or_none` 是
**路径白名单**（当时只有 /api//artifacts//screenshots/），/live 不在其中 ⇒
未认证直读（真机探针实证后才修）。给本页加任何新能力前，先核对白名单。
非分享模式（本机单人）不受影响：无口令即无门。

## 为什么不做真·视频投屏

scrcpy 视频流只有 Android 且基建重；UI 自动化监督要的是「现在到哪一步」，
截图+事件已回答（详见评审讨论与 CONTRIBUTING 的 console 测试口径）。
"""

from __future__ import annotations

import json
import re

from .runtime import _JOB_LOCK, _JOBS

#: \Z 而非 $：Python 的 $ 允许末尾换行（"abc\n" 也能过），白名单要封死（2026-10-25 评审 P3-3）
_JOB_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}\Z")

_TERMINAL = ("done", "error", "cancelled")


def _view(job_id: str) -> dict | None:
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        return {k: v for k, v in job.items() if k not in ("cancel", "events")}


def latest_job_id() -> str:
    """最近启动的任务（`/live` 不带 id 时的落点）；没有任务返回空串。"""
    with _JOB_LOCK:
        if not _JOBS:
            return ""
        return max(_JOBS.items(), key=lambda kv: kv[1].get("started_ts", 0))[0]


def render_live(job_id: str) -> tuple[int, str]:
    """渲染实时视图页。job_id 为空 ⇒ 取最近任务；不存在 ⇒ 404 页（同样是 HTML）。"""
    if not job_id:
        job_id = latest_job_id()
    if not job_id or not _JOB_ID_RE.match(job_id):
        return 404, _NOT_FOUND
    view = _view(job_id)
    if view is None:
        return 404, _NOT_FOUND
    meta = view.get("meta") or {}
    boot = {
        "id": job_id,
        "kind": view.get("kind", ""),
        "status": view.get("status", "running"),
        "serial": meta.get("serial") or "",
        "platform": meta.get("platform") or "android",
        "terminal": view.get("status") in _TERMINAL,
        "link": view.get("link") or "",
    }
    # </ 转义防 </script> 逃逸；serial 等字段来自请求方，不信任
    blob = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    return 200, _PAGE.replace("__BOOT__", blob)


_NOT_FOUND = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>实时视图 — 任务不存在</title><style>
body{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;background:#f5f5f5;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;color:#333}
.box{background:#fff;border-radius:8px;padding:32px 40px;box-shadow:0 2px 8px rgba(0,0,0,.08)}
code{background:#f0f0f0;padding:2px 6px;border-radius:4px}
</style></head><body><div class="box">
<h2 style="margin-top:0">任务不存在</h2>
<p>这个任务 ID 没有登记（可能已被清理，任务记录只保留最近一批）。</p>
<p>不带 ID 打开 <code>/live</code> 可跳到最近一次任务。</p>
</div></body></html>"""

_PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MAT 实时视图</title>
<style>
:root{--ok:#52c41a;--bad:#ff4d4f;--run:#1677ff;--skip:#8c8c8c}
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;
margin:0;background:#f5f5f5;color:#333;display:flex;flex-direction:column;height:100vh}
header{background:#fff;border-bottom:1px solid #e8e8e8;padding:10px 16px;display:flex;
align-items:center;gap:12px;flex:none}
header .t{font-weight:600;font-size:15px}
#badge{font-size:12px;padding:2px 10px;border-radius:10px;background:#e6f4ff;color:#1677ff}
#badge.done{background:#f6ffed;color:var(--ok)}
#badge.error,#badge.cancelled{background:#fff2f0;color:var(--bad)}
#reportlink{margin-left:auto;font-size:13px;color:#1677ff;text-decoration:none;display:none}
main{flex:1;display:flex;min-height:0}
/* 投屏盒**贴内容**（align-self:flex-start + 宽高自适应 img）：AI 助手右侧栏这类
   窄/矮窗口里，不留上下黑边（2026-10-25 适配）。日志区吃满剩余高度、内部滚动。 */
#screenbox{flex:none;align-self:flex-start;display:flex;align-items:center;justify-content:center;
position:relative;background:#111;margin:12px;border-radius:8px;overflow:hidden}
#screen{display:none;max-width:min(430px,42vw);max-height:calc(100vh - 110px);width:auto;height:auto}
#screenhint{color:#aaa;font-size:13px;padding:16px;text-align:center;min-width:180px}
#logwrap{flex:1;display:flex;flex-direction:column;margin:12px 12px 12px 0;background:#fff;
border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06);min-width:0}
#logwrap h3{margin:0;padding:10px 14px;font-size:13px;color:#666;border-bottom:1px solid #f0f0f0;flex:none}
#log{flex:1;overflow-y:auto;padding:6px 0;font-size:13px}
.row{display:flex;gap:8px;padding:4px 14px;line-height:1.5;border-bottom:1px solid #fafafa}
.row .seq{color:#bbb;flex:none;width:28px;text-align:right}
.row .step{flex:none;width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#555}
.st{flex:none;width:56px;font-weight:600}
.st.passed,.st.healed{color:var(--ok)}.st.failed{color:var(--bad)}
.st.running{color:var(--run)}.st.skipped,.st.refused{color:var(--skip)}
.st.error{color:var(--bad)}
.detail{flex:1;color:#666;word-break:break-all}
.dur{flex:none;color:#bbb;width:64px;text-align:right}
footer{flex:none;padding:6px 16px;font-size:12px;color:#999;background:#fff;border-top:1px solid #eee}
/* 窄窗（侧栏形态）：上下堆叠，投屏贴合图片并**水平居中**（竖屏截图在窄栏里
   不可能既满宽又不超高——高度封顶后居中，留空对称化而不是堆在一侧），
   日志吃满剩余高度并内部滚动，整页不滚动 */
@media(max-width:720px){
main{flex-direction:column;overflow:hidden}
#screenbox{margin:12px auto 0;max-height:52vh}
#screen{max-width:calc(100vw - 24px);max-height:52vh}
#logwrap{flex:1;min-height:0;margin:12px}}
</style></head><body>
<header><span class="t">MAT 实时视图</span><span id="kind"></span>
<span id="badge">…</span><a id="reportlink" target="_blank" rel="noreferrer">查看报告 ↗</a></header>
<main>
<div id="screenbox"><img id="screen" alt="设备画面">
<div id="screenhint">设备画面加载中…</div></div>
<div id="logwrap"><h3>步骤事件</h3><div id="log"></div></div>
</main>
<footer>只读旁路视图：投屏与事件均来自本机服务 · 任务结束后自动停止轮询</footer>
<script>
const BOOT = __BOOT__;
const $ = (s)=>document.querySelector(s);
const badge = $("#badge"), log = $("#log");
$("#kind").textContent = BOOT.kind ? "[" + BOOT.kind + "]" : "";
badge.textContent = BOOT.status;
badge.className = BOOT.status;
let seq = 0, terminal = BOOT.terminal, screenTimer = null;

function setStatus(s){
  badge.textContent = s; badge.className = s;
  if (["done","error","cancelled"].includes(s)){
    terminal = true;
    if (screenTimer){clearInterval(screenTimer); screenTimer = null;}
    // 终态后补取一次任务视图，拿 summary/报告链接
    fetch("/api/job?id=" + encodeURIComponent(BOOT.id)).then(r=>r.json())
      .then(v=>{
        if (v.summary) badge.textContent = s + " · " + v.summary;
        if (v.link){const a=$("#reportlink"); a.href=v.link; a.style.display="inline";}
      }).catch(()=>{});
  }
}

function addRow(e){
  const row = document.createElement("div"); row.className = "row";
  const seqEl = document.createElement("span"); seqEl.className="seq"; seqEl.textContent=e.seq;
  const step = document.createElement("span"); step.className="step"; step.textContent=e.step_id||"";
  const st = document.createElement("span"); st.className="st "+(e.status||"");
  st.textContent = e.status||"";
  const det = document.createElement("span"); det.className="detail"; det.textContent=e.detail||"";
  const dur = document.createElement("span"); dur.className="dur";
  dur.textContent = e.duration_ms != null ? e.duration_ms + "ms" : "";
  row.append(seqEl, step, st, det, dur);
  log.appendChild(row);
  while (log.children.length > 300) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

async function poll(){
  try{
    const r = await fetch("/api/job/events?id=" + encodeURIComponent(BOOT.id) + "&since=" + seq);
    if (r.status === 404){
      // 任务记录已被清理（服务端重启 / 滚动淘汰）：停表并如实说明——
      // 否则页面永远停在旧状态、每 1.5s 空转一个 404（2026-10-25 评审 P2-2）
      terminal = true;
      if (screenTimer){clearInterval(screenTimer); screenTimer = null;}
      badge.textContent = "已结束（记录已清理）";
      badge.className = "done";
      return;
    }
    if (!r.ok) return;
    const v = await r.json();
    (v.events||[]).forEach(addRow);
    if (typeof v.seq === "number") seq = v.seq;
    if (v.status && v.status !== badge.textContent.split(" · ")[0]) setStatus(v.status);
  }catch(err){/* 网络抖动：下轮再试 */}
}

function refreshScreen(){
  if (terminal || !BOOT.serial){return;}
  const img = $("#screen"), hint = $("#screenhint");
  img.onerror = ()=>{img.style.display="none"; hint.style.display="block";
    hint.textContent = "设备画面不可用（设备离线或繁忙）";}
  img.onload = ()=>{img.style.display="block"; hint.style.display="none";}
  img.src = "/api/screenshot?serial=" + encodeURIComponent(BOOT.serial)
          + "&platform=" + encodeURIComponent(BOOT.platform) + "&width=430&t=" + Date.now();
}

if (BOOT.serial){
  refreshScreen();
  screenTimer = setInterval(refreshScreen, 2000);
}else{
  $("#screenhint").textContent = "该任务不使用设备（如纯 API 用例 / 生成任务）";
}
poll();
setInterval(()=>{ if (!terminal) poll(); }, 1500);
</script></body></html>"""
