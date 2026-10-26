/* MAT 本地配置台 v2（演示壳，已冻结：仅当 console_dist 缺失时兜底；正式配置台在 console/）——无构建单页；与 CLI 操作同一份数据（P6 双入口同构） */
"use strict";

const state = {
  tab: "home",
  data: null,
  activeJob: localStorage.getItem("instatest-job") || null,
  editorCase: null,
  editorContent: null,  // {name, value}：编辑器内容进状态，轮询重绘不丢
  editorView: "cards",
  lastPayload: null,
  shotTimer: null,
  jobTimer: null,
  exploreForm: { package: "", minutes: 2, steps: 30, max_depth: 3 },
  filterApp: null,
};

/* ── 基础设施 ─────────────────────────────────── */
async function api(path, opts) {
  const res = await fetch(path, opts);
  const text = await res.text();
  let data = text;
  try { data = JSON.parse(text); } catch (_) { /* 文本响应 */ }
  if (!res.ok) throw new Error((data && data.error) || text || ("HTTP " + res.status));
  return data;
}

async function apiText(path) {
  const res = await fetch(path);
  const text = await res.text();
  if (!res.ok) throw new Error(text || ("HTTP " + res.status));
  return text;
}

const post = (body) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => Math.round((x ?? 0) * 100) + "%";

function toast(msg, type = "info", ms = 3600) {
  const icon = { success: "✅", error: "❌", warn: "⚠️", info: "💡" }[type] || "💡";
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.innerHTML = `<span>${icon}</span><div>${esc(msg)}</div>`;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 320); }, ms);
}

function modal(contentHTML) {
  const layer = document.createElement("div");
  layer.id = "modal-layer";
  layer.setAttribute("role", "dialog");
  layer.setAttribute("aria-modal", "true");
  layer.innerHTML = `<div class="modal">${contentHTML}</div>`;
  layer.addEventListener("click", (e) => { if (e.target === layer) layer.remove(); });
  const onKey = (e) => { if (e.key === "Escape") { layer.remove(); document.removeEventListener("keydown", onKey); } };
  document.addEventListener("keydown", onKey);
  document.body.appendChild(layer);
  return layer;
}

function confirmBox(message) {
  return new Promise((resolve) => {
    const layer = modal(`<h3>确认操作</h3><p>${esc(message)}</p>
      <div style="display:flex;gap:10px;justify-content:flex-end">
        <button class="gray" data-x="0">取消</button><button data-x="1">确定</button>
      </div>`);
    layer.querySelectorAll("[data-x]").forEach((b) =>
      b.addEventListener("click", () => { resolve(b.dataset.x === "1"); layer.remove(); }));
  });
}

function promptBox(title, placeholder = "", value = "") {
  return new Promise((resolve) => {
    const layer = modal(`<h3>${esc(title)}</h3>
      <input id="modal-input" placeholder="${esc(placeholder)}" value="${esc(value)}">
      <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:14px">
        <button class="gray" data-x="0">取消</button><button data-x="1">确定</button>
      </div>`);
    const input = layer.querySelector("#modal-input");
    input.focus(); input.select();
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") layer.querySelector('[data-x="1"]').click(); });
    layer.querySelectorAll("[data-x]").forEach((b) =>
      b.addEventListener("click", () => { resolve(b.dataset.x === "1" ? input.value.trim() : null); layer.remove(); }));
  });
}

function lightbox(src) {
  const layer = document.createElement("div");
  layer.id = "lightbox";
  layer.innerHTML = `<img src="${esc(src)}" alt="preview">`;
  layer.addEventListener("click", () => layer.remove());
  document.body.appendChild(layer);
}

function fileToB64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.readAsDataURL(file);
  });
}

const alertErr = (err) => toast(typeof err === "string" ? err : (err.message || String(err)), "error", 5200);

/* ── 徽章 ─────────────────────────────────────── */
const badge = (s) => {
  const map = { passed: ["pass", "✅ 通过"], failed: ["fail", "❌ 失败"], done: ["pass", "✅ 完成"], error: ["fail", "❌ 出错"], running: ["run", "🔄 进行中"] };
  const [cls, label] = map[s] || ["run", esc(s)];
  return `<span class="b ${cls}">${label}</span>`;
};
const casesGroupsHTML = (d) => {
  const meta = d.caseMeta || {};
  const groups = {};
  d.cases.forEach((name) => {
    const pkg = (meta[name] || {}).package || "未分组";
    (groups[pkg] = groups[pkg] || []).push(name);
  });
  const entries = Object.entries(groups).filter(([pkg]) => !state.filterApp || pkg === state.filterApp);
  if (!entries.length) return `<div class="muted">没有匹配的用例</div>`;
  return entries.map(([pkg, names]) => `
    <div style="margin-bottom:10px"><div class="muted" style="margin-bottom:4px">📦 ${esc(pkg)}（${names.length}）</div>
    <table>${names.map((name) => `
      <tr><td><a href="javascript:void(0)" onclick="app.openCase('${esc(name)}')">${esc(name)}</a></td>
      <td style="text-align:right"><button onclick="app.runCase('${esc(name)}')">执行</button></td></tr>`).join("")}</table></div>`).join("");
};

const gradeBadge = (g) => g === "A" ? '<span class="b pass">A 高</span>' : g === "B" ? '<span class="b run">B 中</span>' : '<span class="b fail">C 低</span>';
const quotaHTML = (u) => {
  const chip = (label, count, limit, title) => {
    const text = limit == null ? `${count}/不限` : `${count}/${limit}`;
    const cls = limit != null && count >= limit ? "warn" : "";
    return `<span class="quota ${cls}" title="${title}">${label} ${text}</span>`;
  };
  return chip("AI", u.gen_count, u.gen_limit, `NL 生成月度配额（${esc(u.plan)}）`)
    + chip("视觉", u.vision_count ?? 0, u.vision_limit, `视觉兜底月度配额（${esc(u.plan)}）`);
};

/* ── 数据刷新：数据无变化不重绘；输入中不重绘 ── */
const payloadOf = (d) => JSON.stringify({
  c: d.cases, r: d.runs, i: d.imports, x: d.indexes, u: d.usage, v: d.version,
  g: d.gen_ready, e: d.explores, d: (d.devices || []).map((x) => x.serial + ":" + x.state),
});

async function refresh() {
  try {
    state.data = await api("/api/state");
    state.loadError = null;
  } catch (e) {
    state.loadError = String(e.message || e);
    renderError(state.loadError);
    return;
  }
  const payload = payloadOf(state.data);
  if (payload === state.lastPayload) return;
  state.lastPayload = payload;
  render();
}

function renderError(message) {
  document.getElementById("app").innerHTML = `<div class="empty"><span class="icon">⚠️</span>控制台数据加载失败<br>
    <span class="muted">${esc(message)}</span><br><br>
    <span class="muted">常见原因：adb 未安装或不在 PATH（<span class="kbd">adb version</span> 验证）、
    控制台进程已退出（重新执行 <span class="kbd">mat console</span>）</span></div>`;
}
async function refreshDevices() { await refresh(); }

function trackJob(jobId) {
  state.activeJob = jobId;
  localStorage.setItem("instatest-job", jobId);
  if (state.jobTimer) clearInterval(state.jobTimer);
  state.jobTimer = setInterval(async () => {
    const job = await api("/api/job?id=" + jobId).catch(() => null);
    if (!job) return;
    if (job.status === "running") { render(); return; }
    clearInterval(state.jobTimer); state.jobTimer = null;
    localStorage.removeItem("instatest-job");
    state.activeJob = null;
    await refresh();
    if (job.status === "done") {
      toast(job.summary || "任务完成", "success");
      if (job.link) window.open(job.link, "_blank");
    } else {
      toast(job.error || "任务失败", "error", 5200);
    }
  }, 1500);
  if (!["runs", "home", "explore"].includes(state.tab)) app.nav("runs");
  else render();
}

function render(force) {
  const d = state.data;
  if (!d) return;
  const appEl = document.getElementById("app");
  const active = document.activeElement;
  // 光标保护：输入中跳过重绘（与数据变化检测双保险）
  if (!force && active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA") && appEl.contains(active)) return;
  const scrollY = window.scrollY;
  document.getElementById("ver").textContent = "v" + d.version;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === state.tab));
  document.getElementById("quota").innerHTML = quotaHTML(d.usage);
  appEl.innerHTML = (views[state.tab] || views.home)(d);
  window.scrollTo(0, scrollY);
  state.lastPayload = payloadOf(d);
}

function nav(tab) {
  state.tab = tab;
  if (state.shotTimer) { clearInterval(state.shotTimer); state.shotTimer = null; }
  render();
  if (tab === "devices") { refreshDevices(); state.shotTimer = setInterval(rotateShots, 2500); }
  if (tab === "explore") refreshDevices();
}

// 投屏轮播：只更新 img.src，不经重绘（指纹不含屏幕画面，走重绘路径永远不会刷新）
function rotateShots() {
  document.querySelectorAll("img[data-serial]").forEach((img) => {
    img.src = `/api/screenshot?serial=${encodeURIComponent(img.dataset.serial)}&w=640&t=${Date.now()}`;
  });
}

/* ── 视图 ─────────────────────────────────────── */
const runsTable = (runs, limit) => {
  if (!runs.length) return `<div class="empty"><span class="icon">🛰️</span>还没有执行记录——从用例页选一条点【执行】</div>`;
  const rows = runs.slice(0, limit || 10).map((r) => `
    <tr><td>${esc(r.case_name || r.case_id)}</td><td><code>${esc(r.package || "-")}</code></td>
    <td>${badge(r.status)}</td><td>${pct(r.rate)}</td><td class="muted">${esc((r.finished_at || "").replace("T", " ").slice(0, 16))}</td>
    <td><a href="/artifacts/${esc(r.dir)}/report.html" target="_blank">报告 ↗</a></td></tr>`).join("");
  return `<table><tr><th>用例</th><th>包名</th><th>结果</th><th>步骤成功率</th><th>时间</th><th></th></tr>${rows}</table>`;
};

const jobPanel = () => state.activeJob
  ? `<div class="job-panel"><span class="spin"></span> 任务执行中（${esc(state.activeJob)}）——完成后自动通知并打开报告</div>` : "";

const views = {
  home: (d) => `
    <h2>工作台</h2>
    <div class="grid">
      <div class="card hover"><h3>① 连接设备</h3>
        <div>${d.devices.length ? `✅ <b>${d.devices.length}</b> 台在线` : "📱 未发现设备"}</div>
        <div class="muted mt">手机开启 USB 调试并插上电脑</div>
        <p><button onclick="app.nav('devices')">查看设备</button></p></div>
      <div class="card hover"><h3>② 准备用例</h3>
        <div>当前 <b>${d.cases.length}</b> 条用例 · <b>${(d.imports || []).length}</b> 个导入批次</div>
        <div class="muted mt">手写 DSL、一句话生成或导入手工用例</div>
        <p><button onclick="app.nav('cases')">用例中心</button></p></div>
      <div class="card hover"><h3>③ 执行与报告</h3>
        <div>最近执行 ${d.runs.length} 次${d.runs[0] ? ` · 最近 ${badge(d.runs[0].status)}` : ""}</div>
        <div class="muted mt">失败自动分类 + 截图/logcat 证据链</div>
        <p><button onclick="app.nav('reports')">查看报告</button></p></div>
    </div>
    <div class="card"><h3>✨ AI 一句话生成用例</h3>
      <p><textarea id="gen-nl" style="min-height:64px" placeholder="例：输入账号 alice 密码 pass1234，点击登录按钮，验证欢迎文案包含 alice"></textarea></p>
      <div style="display:flex;gap:10px">
        <input id="gen-pkg" placeholder="被测 App 包名，如 com.insta.demo">
        <button style="white-space:nowrap" onclick="app.submitGen()">生成用例</button>
      </div>
      <div class="muted mt">${d.gen_ready
        ? "✅ AI 已就绪 · 页面索引" + ((d.indexes || []).length ? "已构建（防幻觉生效）" : "未构建（index 命令构建后自动防幻觉）")
        : "❌ 未配置 GEN_* 环境变量（见设置页）；本地执行不受影响"}</div></div>
    ${jobPanel()}
    <div class="card"><h3>最近执行</h3>${runsTable(d.runs, 8)}</div>`,

  devices: (d) => `
    <h2>设备</h2>
    ${d.devices.length ? `<div class="grid">${d.devices.map((dev) => `
      <div class="card hover"><h3>${esc(dev.model || dev.serial)}</h3>
        <div class="muted">${esc(dev.serial)} · ${esc(dev.state)}</div>
        <p><img class="shot" data-serial="${esc(dev.serial)}" onclick="lightbox('/api/screenshot?serial=${esc(dev.serial)}&t=' + Date.now())"
               src="/api/screenshot?serial=${esc(dev.serial)}&w=640&t=${Date.now()}" alt="投屏预览"></p>
        <div class="muted">截图每 2.5s 自动刷新 · 点击放大 ·（WebRTC 实时投屏在 V1.5）</div></div>`).join("")}</div>`
    : `<div class="empty"><span class="icon">📱</span>未发现在线设备<br><span class="muted">手机开启 USB 调试插上电脑；WiFi 调试：<span class="kbd">adb connect ip:port</span></span></div>`}`,

  targets: (d) => {
    const pkgs = {};
    d.runs.forEach((r) => { if (r.package) pkgs[r.package] = (pkgs[r.package] || 0) + 1; });
    const list = Object.entries(pkgs);
    const idxCards = (d.indexes || []).map((i) => `
      <div class="card hover"><h3>📄 <code>${esc(i.package)}</code></h3>
        <div class="muted">索引 ${i.pages} 页 · ${esc((i.generated_at || "").replace("T", " ").slice(0, 16))}</div>
        <p><button class="gray" onclick="app.filterApp('${esc(i.package)}')">查看该 App 用例</button>
           <button class="gray" onclick="app.batchRun('${esc(i.package)}')">▶ 批量执行</button></p></div>`).join("");
    return `<h2>测试目标</h2>
      <div class="card"><h3>页面索引（gen/convert 防幻觉底座）</h3>
        ${idxCards || `<div class="muted">构建：<span class="kbd">mat index &lt;包名&gt;</span>（真机轻量爬取）——索引后 AI 生成只引用真实控件</div>`}</div>
      ${list.length ? `<div class="muted" style="margin:14px 0 10px">已执行过的目标</div><div class="grid">${list.map(([pkg, count]) => `
        <div class="card hover"><h3><code>${esc(pkg)}</code></h3>
          <div class="muted">关联执行 ${count} 次</div>
          <p><button class="gray" onclick="app.nav('cases')">查看关联用例</button></p></div>`).join("")}</div>`
      : `<div class="empty"><span class="icon">📦</span>还没有目标信息<br><span class="muted">先执行一次用例；安装 APK：<span class="kbd">adb install -r app.apk</span></span></div>`}`;
  },

  cases: (d) => `
    <h2>用例</h2>
    <div class="card"><h3>📥 导入手工用例（Excel/CSV/Markdown/XMind/文本 → DSL 草稿）</h3>
      <p><input type="file" id="import-file" accept=".csv,.xlsx,.xlsm,.md,.markdown,.txt,.xmind">
         <button class="gray" onclick="app.importPreview()">解析预览</button></p>
      <div id="import-preview" class="muted">上传后预览解析结果与列映射（启发式优先；XMind 直接解析树结构）</div>
      <p><input id="import-pkg" placeholder="被测 App 包名，如 com.insta.demo">
         <button onclick="app.importConvert()">开始转换（消耗 0.5 额度/条）</button></p>
      <div id="import-result"></div>
      ${(d.imports || []).length ? `<div class="muted mt">最近批次：${d.imports.map((i) =>
        `<a href="javascript:void(0)" onclick="app.viewImport('${esc(i.id)}')">${esc(i.id)}</a>（A${i.summary.A}/B${i.summary.B}/C${i.summary.C}）`).join(" · ")}</div>` : ""}</div>
    <div class="two-col">
      <div class="card"><h3>用例列表（${d.cases.length}）${state.filterApp ? ` <span class="b run">仅: ${esc(state.filterApp)} <a href="javascript:void(0)" onclick="app.clearFilter()">✕</a></span>` : ""}</h3>
        <p><select id="run-serial" style="margin-bottom:8px">
          <option value="">自动选择设备</option>
          ${(d.devices || []).map((dev) => `<option value="${esc(dev.serial)}">${esc(dev.model || dev.serial)}</option>`).join("")}
        </select></p>
        <p><button onclick="app.newCase()">+ 新建</button>
           ${(d.indexes || []).length ? `<button class="gray" onclick="app.batchRun('${esc((d.indexes[0] || {}).package || "")}')">▶ 批量执行该 App</button>` : ""}</p>
        ${casesGroupsHTML(d)}</div>
      <div class="card"><h3>${state.editorCase ? "编辑：" + esc(state.editorCase) : "编辑器"}
        ${state.editorCase ? `<span style="float:right"><button class="gray" onclick="app.toggleView()">${state.editorView === "cards" ? "✏️ 源码" : "🗂️ 步骤卡"}</button></span>` : ""}</h3>
        <div id="editor-body">${state.editorCase
          ? (state.editorView === "cards" ? renderCards() : `<p><textarea id="case-editor">${esc(state.editorContent?.name === state.editorCase ? state.editorContent.value : "")}</textarea></p>`)
          : `<div class="empty"><span class="icon">📝</span>从左侧选择用例，或点【+ 新建】<br><span class="muted">支持步骤卡视图 / 源码视图切换</span></div>`}</div>
        ${state.editorCase ? `<p><button onclick="app.saveCase()">保存</button>
          <button class="gray" onclick="app.runCase('${esc(state.editorCase)}')">执行</button>
          <button class="gray" onclick="app.pickElement()">📱 从屏幕选取控件</button></p>
          <div class="muted">与 CLI 同一份文件（cases/）；保存前做 schema 校验</div>` : ""}</div>
    </div>
    <div id="picker-result" class="card" style="display:none"><h3>当前屏幕可交互元素（点击插入 tap 步骤）</h3><div id="picker-list" class="muted">加载中…</div></div>`,

  explore: (d) => {
    const pkgOptions = [...new Set(d.runs.map((r) => r.package).filter(Boolean))];
    return `<h2>探索测试 <span class="b ai">规则驱动</span></h2>
      <div class="card"><h3>自动遍历 App，发现崩溃 / 白屏 / 异常</h3>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <input id="explore-pkg" style="flex:1;min-width:200px" placeholder="被测 App 包名" value="${esc(state.exploreForm.package)}" list="pkg-list">
        <select id="explore-minutes" style="width:120px">${[1, 2, 3, 5, 8].map((m) => `<option value="${m}" ${m === state.exploreForm.minutes ? "selected" : ""}>${m} 分钟</option>`).join("")}</select>
        <select id="explore-steps" style="width:120px">${[20, 30, 50, 80].map((s) => `<option value="${s}" ${s === state.exploreForm.steps ? "selected" : ""}>${s} 步上限</option>`).join("")}</select>
        <select id="explore-depth" style="width:120px">${[2, 3, 4, 5].map((l) => `<option value="${l}" ${l === state.exploreForm.max_depth ? "selected" : ""}>深度 ≤L${l}</option>`).join("")}</select>
        <button onclick="app.startExplore()">🚀 开始探索</button>
      </div>
      <div class="muted mt">时长与步数双上限；支付/结算/客服/订单等敏感控件自动跳过；发现项附截图与日志证据；执行前自动唤醒解锁</div></div>
    ${jobPanel()}
    <div id="explore-result"></div>
    <div class="card"><h3>最近探索</h3>
      ${(d.explores || []).length ? `<table><tr><th>批次</th><th>包名</th><th>步数</th><th>层级分布</th><th>发现</th><th></th></tr>
        ${d.explores.map((e2) => `<tr><td><code>${esc(e2.id)}</code></td><td><code>${esc(e2.package)}</code></td>
        <td>${e2.steps}</td>
        <td class="muted">${Object.entries(e2.depth_stats || {})
          .sort((a, b) => parseInt(a[0].slice(1), 10) - parseInt(b[0].slice(1), 10))
          .map(([k, v]) => `${k}:${v}`).join(" ")}</td>
        <td>${e2.findings}</td>
        <td><a href="/artifacts/explore/${esc(e2.id)}/report.html" target="_blank">报告 ↗</a></td></tr>`).join("")}</table>`
      : `<div class="muted">还没有探索记录</div>`}</div>
    <datalist id="pkg-list">${pkgOptions.map((p) => `<option value="${esc(p)}">`).join("")}</datalist>`; },

  runs: (d) => `
    <h2>执行</h2>${jobPanel()}
    <div class="card"><h3>历史任务</h3>${runsTable(d.runs, 50)}</div>
    <div class="card"><h3>命令行等价操作（P6 同构）</h3>
      <div class="muted"><span class="kbd">mat run &lt;用例|套件&gt;</span> ·
      <span class="kbd">--junit-xml out.xml</span> · <span class="kbd">--dataset all|组名</span>（数据驱动选组） · <span class="kbd">heal apply</span>（自愈回写）</div></div>`,

  reports: (d) => `
    <h2>报告</h2>
    <div class="card">${runsTable(d.runs, 50)}
      <p class="muted">自包含 HTML：每步截图 + 失败分类 + logcat + 自愈提案；数据驱动用例按数据组聚合</p></div>`,

  settings: (d) => `
    <h2>设置</h2>
    <div class="card"><h3>账号与配额</h3>
      <div>套餐：<b>${esc(d.usage.plan)}</b> · AI 生成（${esc(d.usage.month)}）：${d.usage.gen_count}${d.usage.gen_limit == null ? " / 不限" : " / " + d.usage.gen_limit}</div>
      <div>视觉兜底（${esc(d.usage.month)}）：${d.usage.vision_count ?? 0}${d.usage.vision_limit == null ? " / 不限" : " / " + d.usage.vision_limit} <span class="muted">（同屏缓存命中不计费）</span></div>
      <div class="muted mt">本地执行、自愈、报告全档不限量；体验版 AI 生成 50 次/月（导入转换按 0.5 计）、视觉兜底 20 次/月</div></div>
    <div class="card"><h3>AI 能力</h3>
      <div>文本生成：${d.gen_ready ? "✅ 已就绪" : "❌ 未配置（.env 写 GEN_BASE_URL/GEN_API_KEY/GEN_MODEL；用轻云代理时另加 GEN_PROMPT_MODE=server）"}</div>
      <div class="muted mt">视觉兜底：配置 <span class="kbd">GEN_VL_MODEL=qwen3-vl-plus</span> 后，定位失败自动用截图+意图找控件（置信度 &lt;0.7 拒绝执行）；探索测试同用该模型</div></div>
    <div class="card"><h3>CLI 接入（CI 模板）</h3>
      <textarea readonly style="min-height:110px">- run: pip install mat-uitest
- run: mat run cases/smoke.json --junit-xml result.xml
- uses: actions/upload-artifact@v4
  with: { name: mat-report, path: artifacts/ }</textarea></div>
    <div class="card"><h3>数据与隐私</h3>
      <div class="muted">工作区：${esc(d.cwd)} · 被测 App 包不上传（纯本地模式默认）
      · 用户状态 ~/.insta/（凭据 0600）· 页面索引 artifacts/page_index/</div></div>`,
};

/* ── 步骤卡渲染（cases 编辑器 cards 视图） ─────── */
function renderCards() {
  try {
    const caseData = JSON.parse(state.editorContent?.name === state.editorCase ? state.editorContent.value : "{}");
    const steps = caseData.steps || [];
    const cards = steps.map((s, i) => `
      <div class="stepcard"><div class="no">${i + 1}</div>
        <div style="flex:1"><b>${esc(s.intent || s.id || "步骤 " + (i + 1))}</b>
          <span class="act ${esc(s.action)}">${esc(s.action)}</span>
          ${s.onFailure === "heal" ? '<span class="b ai">可自愈</span>' : ""}
          ${s.onFailure === "retry" ? '<span class="b run">可重试</span>' : ""}
          <div class="muted">${s.target ? `定位 <code>${esc(s.target.strategy)}=${esc(s.target.value)}</code>` : ""}${s.params && Object.keys(s.params).length ? ` · 参数 <code>${esc(JSON.stringify(s.params))}</code>` : ""}</div>
          ${(s.assertions || []).map((a) => `<div class="muted">✔ 断言 ${esc(a.type)} ${a.target ? `<code>${esc(a.target.value)}</code>` : ""} ${a.expected ? "期望 " + esc(a.expected) : ""}</div>`).join("")}
        </div></div>`).join("");
    const meta = caseData.meta || {};
    return (cards || '<div class="muted">（无步骤）</div>') +
      `<div class="muted mt">${steps.length} 步 · 断言 ${steps.reduce((n, s) => n + (s.assertions || []).length, 0)} 个` +
      `${meta.module ? " · 模块 " + esc(meta.module) : ""}${meta.reviewed ? " · 已人工复核" : ""}</div>`;
  } catch (e) {
    return `<div class="muted">JSON 解析失败：${esc(e.message)}（切到源码视图修正）</div>`;
  }
}

/* ── 事件处理 ─────────────────────────────────── */
const app = {
  nav,
  toggleView() { state.editorView = state.editorView === "cards" ? "source" : "cards"; render(true); },
  openCase(name) {
    state.tab = "cases";
    apiText("/api/case?name=" + encodeURIComponent(name)).then((content) => {
      state.editorCase = name;
      state.editorContent = { name, value: content };
      state.editorView = "cards";
      render(true);
    }).catch(alertErr);
  },
  async newCase() {
    const name = await promptBox("用例文件名（.json 结尾）", "case-new.json");
    if (!name) return;
    state.tab = "cases";
    state.editorCase = name;
    state.editorView = "source";
    state.editorContent = { name, value: JSON.stringify({
      caseId: "case_" + Math.random().toString(36).slice(2, 8),
      name: "新用例",
      target: { package: "com.example.app" },
      steps: [{ id: "s1", intent: "启动应用", action: "launch", params: { package: "com.example.app" } }],
    }, null, 2) };
    render(true);
  },
  async saveCase() {
    const name = state.editorCase;
    const content = document.getElementById("case-editor").value;
    try {
      await api("/api/case", post({ name, content }));
      state.editorContent = { name, value: content };
      state.data.cases = [...new Set([...state.data.cases, name])].sort();
      toast("已保存 " + name, "success");
      render(true);
    } catch (e) { alertErr(e); }
  },
  runCase(name) {
    if (!name) return toast("请先选择用例", "warn");
    const sel = document.getElementById("run-serial");
    const serial = sel ? sel.value : "";
    api("/api/run", post({ case: name, serial })).then((res) => trackJob(res.job_id)).catch(alertErr);
  },
  filterApp(pkg) { state.filterApp = pkg; app.nav("cases"); },
  clearFilter() { state.filterApp = null; render(true); },
  batchRun(pkg) {
    if (!pkg) return toast("该 App 没有页面索引", "warn");
    api("/api/run", post({ app: pkg })).then((res) => trackJob(res.job_id)).catch(alertErr);
  },
  async pickElement() {
    const panel = document.getElementById("picker-result");
    panel.style.display = "block";
    panel.scrollIntoView({ behavior: "smooth" });
    const data = await api("/api/device/elements").catch(alertErr);
    if (!data) return;
    const items = data.elements || [];
    window._elements = items;
    document.getElementById("picker-list").innerHTML = items.length
      ? `<table><tr><th>文案/desc</th><th>res-id</th><th>class</th><th></th></tr>${items.slice(0, 40).map((el, i) => `
        <tr><td>${esc(el.text || el.desc || "(无文案)")}</td><td><code>${esc(el.rid || "-")}</code></td>
        <td class="muted">${esc(el.cls)}</td>
        <td><button class="gray" onclick="app.insertStep(${i})">+ 插入 tap</button></td></tr>`).join("")}</table>`
      : `<div class="muted">当前屏幕无可交互元素（先在设备页确认亮屏解锁）</div>`;
  },
  insertStep(i) {
    const el = (window._elements || [])[i];
    if (!el) return;
    if (!state.editorCase) return toast("请先打开或新建用例", "warn");
    try {
      // 步骤卡视图下 #case-editor 不存在——直接写状态
      const caseData = JSON.parse(state.editorContent?.value || "{}");
      caseData.steps = caseData.steps || [];
      const strategy = el.rid ? "res-id" : (el.text ? "text" : (el.desc ? "description" : "point"));
      const value = el.rid || el.text || el.desc || `${el.cx},${el.cy}`;
      caseData.steps.push({ id: "s" + (caseData.steps.length + 1), intent: el.text || el.desc || "点击元素",
        action: "tap", target: { strategy, value } });
      const content = JSON.stringify(caseData, null, 2);
      state.editorContent = { name: state.editorCase, value: content };
      state.editorView = "cards";  // 插入后回步骤卡视图，立即可见新步骤
      render(true);
      toast("已插入 tap 步骤（记得保存）", "success");
    } catch (e) { alertErr(e); }
  },
  submitGen() {
    const nl = document.getElementById("gen-nl").value.trim();
    const pkg = document.getElementById("gen-pkg").value.trim();
    if (!nl || !pkg) return toast("请填写场景描述与包名", "warn");
    api("/api/gen", post({ nl, package: pkg })).then((res) => trackJob(res.job_id)).catch(alertErr);
  },
  async importPreview() {
    const f = document.getElementById("import-file").files[0];
    if (!f) return toast("请选择文件（Excel/CSV/Markdown/XMind/文本）", "warn");
    const b64 = await fileToB64(f);
    const res = await api("/api/import/preview", post({ name: f.name, content_b64: b64 })).catch(alertErr);
    if (!res) return;
    window._import = { name: f.name, b64 };
    document.getElementById("import-preview").innerHTML =
      `解析到 <b>${res.count}</b> 条 · 映射 <code>${esc(JSON.stringify(res.template))}</code>` +
      (res.titles.length ? `<br>样例：${esc(res.titles.slice(0, 5).join(" / "))}` : "");
  },
  async importConvert() {
    if (!window._import) return toast("请先解析预览", "warn");
    const pkg = document.getElementById("import-pkg").value.trim();
    if (!pkg) return toast("请填写被测 App 包名", "warn");
    const res = await api("/api/import/convert", post({
      name: window._import.name, content_b64: window._import.b64, package: pkg })).catch(alertErr);
    if (res && res.job_id) trackJob(res.job_id);
  },
  async viewImport(id) {
    const data = await api("/api/import?id=" + encodeURIComponent(id)).catch(alertErr);
    if (!data) return;
    const box = document.getElementById("import-result");
    if (!box) return;
    const rows = (data.results || []).map((r) => `
      <tr><td>${esc(r.title)}</td><td>${gradeBadge(r.grade)} ${pct(r.confidence)}</td>
      <td class="muted">${esc((r.issues || []).join("；") || "-")}</td>
      <td>${r.case_file ? `<a href="javascript:void(0)" onclick="app.openCase('${esc(r.case_file)}')">${esc(r.case_file)}</a>` : "仅保留原文"}</td></tr>`).join("");
    box.innerHTML = `<h3>批次 ${esc(data.import_id)}（来源 ${esc(data.source_file)}）</h3>
      <table><tr><th>用例</th><th>分级/置信度</th><th>问题</th><th>草稿</th></tr>${rows}</table>
      <p class="muted">A/B 已写入 cases/（点草稿名编辑）；C 级需补全信息后重转</p>`;
  },
  async startExplore() {
    const pkg = document.getElementById("explore-pkg").value.trim();
    const minutes = parseInt(document.getElementById("explore-minutes").value, 10);
    const steps = parseInt(document.getElementById("explore-steps").value, 10);
    const max_depth = parseInt(document.getElementById("explore-depth").value, 10);
    if (!pkg) return toast("请填写被测 App 包名", "warn");
    state.exploreForm = { package: pkg, minutes, steps, max_depth };
    const res = await api("/api/explore", post({ package: pkg, minutes, steps, max_depth })).catch(alertErr);
    if (res && res.job_id) { toast("探索已启动（预算 " + minutes + " 分钟）", "info"); trackJob(res.job_id); }
  },
  async viewExplore(id) {
    const data = await api("/api/explore?id=" + encodeURIComponent(id)).catch(alertErr);
    if (!data) return;
    const box = document.getElementById("explore-result");
    if (!box) return;
    const findings = (data.findings || []);
    box.innerHTML = `<h3>探索批次 ${esc(data.explore_id || id)}</h3>
      <div class="muted mb">步数 ${data.steps} · 页面 ${data.page_count} · 发现 ${findings.length} 个</div>
      ${findings.length ? `<table><tr><th>类型</th><th>说明</th><th>证据</th></tr>${findings.map((f) => `
        <tr><td><span class="b fail">${esc(f.type)}</span></td><td class="muted">${esc(f.detail || "")}</td>
        <td>${f.screenshot ? `<a href="${esc(f.screenshot)}" target="_blank">截图</a>` : "-"} ${f.logcat ? `<a href="${esc(f.logcat)}">日志</a>` : ""}</td></tr>`).join("")}</table>`
      : `<div class="muted">本轮探索未发现异常</div>`}`;
  },
};
window.app = app;
window.renderCards = renderCards;

/* ── 编辑器输入进状态（轮询重绘后内容不丢） ────── */
document.getElementById("app").addEventListener("input", (e) => {
  if (e.target.id === "case-editor" && state.editorCase) {
    state.editorContent = { name: state.editorCase, value: e.target.value };
  }
});

/* ── 启动 ─────────────────────────────────────── */
refresh();
setInterval(refresh, 5000);
