// platform 缺省 android（历史字段无它）；iOS USB 真机由 /api/state 下发
// （serial=udid，state="device"），前端按它分流到 WDA 真机链路（2026-10-19）。
export type Device = { serial: string; state: string; model?: string; os?: string;
  platform?: "android" | "ios" };
export type RunInfo = { dir: string; case_id?: string; case_name?: string; package?: string;
  status?: string; rate?: number; finished_at?: string; operator?: string;
  platform?: "android" | "ios";
  failed_categories?: Record<string, number>;
  batch?: { id: string; suite: string; index: number; total: number };
  diagnosed?: boolean; calibrated?: boolean; human_marked?: boolean };
export type JobInfo = { id: string; kind: string; status: string; started_at: string;
  summary?: string | null; error?: string | null; link?: string | null; seq: number;
  serial?: string | null; platform?: string; operator?: string };
export type JobEvent = { seq: number; step_id: string; status: string; ts: string;
  duration_ms?: number | null; detail?: string; screenshot?: string | null };
export type StateResp = {
  version: string; cwd: string; cases: string[];
  caseMeta: Record<string, { app?: string; title?: string; caseId?: string; no?: number }>;
  runs: RunInfo[];
  explores: { id?: string; package?: string; steps?: number; findings?: number;
    calibrated?: boolean; finished_at?: string }[];
  indexes: { package: string; pages: number; generated_at?: string }[];
  usage: { gen_count: number; gen_limit: number | null; vision_count: number; vision_limit: number | null };
  /** AI 接入方式：server=官方代理（体验模式）/ local=自带 Key（标准模式） */
  ai_mode?: "server" | "local";
  // 使用方式页「套餐」行展示到期日：plan=pro 时有 expires_at（Unix 秒）；free/过期时为 null。
  // 激活/续期（含时长卡核销换码）写盘 license.json 后，重拉 state 自然拿到新日期。
  license?: { plan: string; expires_at: number | null };
  code_stale: { stale: boolean; file?: string; behind_seconds?: number }; gen_ready: boolean;
  devices: Device[]; console?: "pro" | "legacy"; prompt_mode?: "server" | "local";
  // iOS 仅 macOS 可用（Xcode 工具链）：Windows/Linux 下隐藏环境卡、iOS 运行平台与模拟器入口
  ios_supported?: boolean;
  // 遍历探索建议（readiness.EXPLORE_TIP 单一来源下发；2026-10-26 去术语留判据）
  explore_tip?: string;
};

// 触发者身份（团队协作 P1，发起人 2026-10-12）：所有 POST 自动携带 operator——
// 各触发 API（run/gen/import/...）零改动；名字为空则不带（后端记 ""）。
export const getOperator = (): string => localStorage.getItem("mat_operator") || "";
export const setOperator = (name: string) => localStorage.setItem("mat_operator", name.trim());

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  if (init?.method === "POST" && typeof init.body === "string"
      && init.body.startsWith("{")) {
    try {  // JSON body 注入 operator（Form/文件上传等非 JSON body 不动）
      const parsed = JSON.parse(init.body);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)
          && parsed.operator === undefined && getOperator()) {
        parsed.operator = getOperator();
        init = { ...init, body: JSON.stringify(parsed) };
      }
    } catch { /* 非 JSON body 原样放行 */ }
  }
  const r = await fetch(url, init);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    // --share 口令鉴权（团队协作 P1）：服务端要求口令 → 全局弹口令 Modal（App 监听）
    if ((body as { auth?: boolean }).auth) window.dispatchEvent(new Event("mat-auth-required"));
    throw new Error((body as { error?: string }).error || `${r.status}`);
  }
  return body as T;
}

// JSON POST 统一走这里：只有 j() 会注入 operator——绕过它的裸 fetch（此前 Generate 页
// 的 /api/gen、/api/import/*）请求体里没有名字，人数闸按匿名 400「请先在右上角设置名字」，
// 用户明明设过名字却到处被拒（2026-10-13 实测踩中）。
export const postJson = <T,>(url: string, body: Record<string, unknown>): Promise<T> =>
  j<T>(url, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) });

// 按应用的测试说明（app_notes/<包名>.md，黑/白名单等负责人补充）——
// 生成与润色时后端自动注入 prompt；此处只是控制台的编辑入口
export const getAppNote = (pkg: string) =>
  j<{ package: string; notes: string }>(`/api/app/note?package=${encodeURIComponent(pkg)}`);
export const saveAppNote = (pkg: string, notes: string) =>
  j<{ ok: boolean }>("/api/app/note", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ package: pkg, notes }) });

// 口令校验（--share）：成功后服务端种 cookie，之后所有请求自动携带
export const submitAuthKey = (key: string) =>
  j<{ ok: boolean }>("/api/auth", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }) });

// 写操作审计（团队协作 P1）：设置页「操作记录」数据源——谁改的/谁下发的
export type AuditItem = { ts: string; operator: string; action: string;
  target?: string; ip?: string };
export const getAudit = (limit = 50) =>
  j<{ items: AuditItem[]; total: number }>(`/api/audit?limit=${limit}`);

// 团队成员（2026-10-25 起：共享名额**只随团队授权提供**——标准/体验授权面向单人使用，
// `--share` 下上限恒 1）：`shared` 由后端按实际监听地址判断，前端据此决定提示与新增入口；
// `team_authorized` 区分「共享了但没授权」（提示去拿团队授权）与「团队授权」（正常成员管理）
// ——不能拿 limit 推授权状态：团队合同席位可以是 1。
export type Seat = { name: string; first_seen: string; last_seen: string };
export type SeatsResp = { seats: Seat[]; limit: number; shared?: boolean; team_authorized?: boolean };
export const getSeats = () => j<SeatsResp>("/api/seats");
export const seatCheck = (name: string) =>
  j<{ ok: boolean; returning: boolean } & SeatsResp>(
    "/api/seats/check", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
export const seatRemove = (name: string) =>
  j<{ ok: boolean } & SeatsResp>("/api/seats/remove",
    { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });

export const getState = () => j<StateResp>("/api/state");

// 新版本检查（发起人 2026-10-14）：服务启动时后端查过一次 PyPI（失败 latest=null 静默），
// 前端只读结果；has_update ⇒ 左上角版本旁亮「新」badge，点击给对应渠道的升级命令
export type VersionCheck = {
  current: string;
  latest: string | null;
  has_update: boolean;
  upgrade_cmd: string;
  checked_at: string | null;
  enabled: boolean;
};
export const getVersionCheck = () => j<VersionCheck>("/api/version/check");
// App 显示名（用例列表分组行展示 displayName；无 aapt/设备时回空——前端回落包名）
export const getAppLabels = (packages: string[]) =>
  j<{ labels: Record<string, string> }>(
    `/api/app/labels?packages=${encodeURIComponent(packages.join(","))}`);
// 已安装第三方 App 包名（遍历探索输入候选；`pm list packages -3`，系统包不混入）
export const listInstalledApps = (serial?: string | null) =>
  j<{ serial: string; packages: string[] }>(
    `/api/apps/installed${serial ? `?serial=${encodeURIComponent(serial)}` : ""}`);
export const getCase = (name: string) => j<Record<string, unknown>>(`/api/case?name=${encodeURIComponent(name)}`);
// 保存返回启发式警告（res-id 不在控件清单/params 缺失等；不阻断落盘，编辑器展示）
export const saveCase = (name: string, content: unknown) =>
  j<{ ok: boolean; warnings?: string[] }>("/api/case", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, content }) });
// page_index 控件清单（编辑器 target 下拉候选；无索引返回空数组，前端降级手填）
export type ControlItem = { res_id: string; label: string; page: string; clickable: boolean };
export const getControls = (pkg: string) =>
  j<{ package: string; count: number; controls: ControlItem[] }>(
    `/api/controls?package=${encodeURIComponent(pkg)}`);
export const runCase = (payload: { case?: string; app?: string; cases?: string[];
  rerun_dir?: string;
  serial?: string | null; platform?: string; record?: boolean; reset?: "off" | "cold" }) =>
  j<{ job_id: string }>("/api/run", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
export const getJobs = () => j<{ jobs: JobInfo[] }>("/api/jobs");
export const getJobEvents = (id: string, since: number) =>
  j<{ seq: number; events: JobEvent[]; status: string }>(`/api/job/events?id=${id}&since=${since}`);
export const cancelJob = (id: string) =>
  j<{ requested: boolean }>("/api/job/cancel", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
export const startExplore = (payload: { package: string; minutes: number; steps: number;
  max_depth: number; serial?: string | null; platform?: string }) =>
  j<{ job_id: string }>("/api/explore", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
export const startIndex = (payload: { package: string; refresh?: boolean; serial?: string | null; platform?: string }) =>
  j<{ job_id: string }>("/api/index", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
export const startDescribe = (payload: { package: string; limit?: number; refresh?: boolean }) =>
  // noop：所有页面都已填充时的正常返回（无 job_id），message 是给用户的轻提示文案
  j<{ job_id: string; noop?: boolean; message?: string }>("/api/describe", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
// 从本地安装包解析包名（.app 读 Info.plist / .apk 用 aapt）——解析失败静默回空
export const probeApp = (path: string) =>
  j<{ package: string; label: string; version: string }>(
    `/api/app/probe?path=${encodeURIComponent(path)}`);
export const getJob = (id: string) =>
  j<{ status: string; summary?: string | null; error?: string | null; link?: string | null }>(`/api/job?id=${id}`);

// -- 演示增强包(F2 环境监测 / F3 设备操控,mat-console-design §3-§4) ---------------------------
export type DoctorCheck = { id: string; label: string; ok: boolean; critical: boolean;
  status: "ok" | "warn" | "fail"; detail?: string; hint?: string;
  // 一键修复（发起人 2026-10-11）：只带 action id，命令在后端 FIX_ACTIONS 白名单
  fix?: { action: string; kind: "run" | "open" | "wda"; label: string } };
export type DoctorResp = { checks: DoctorCheck[]; ran_at: string; duration_ms: number };
export const getDoctor = (platform: "android" | "ios") =>
  j<DoctorResp>(`/api/doctor?platform=${platform}`);
// iOS 模拟器枚举与单活切换（发起人 2026-10-12）：booted 可直接投屏（WDA 常驻 localhost:8100），
// 未启动的一键启动（boot 目标 + shutdown 其余，保证 8100 服务对象唯一）
export type IosSim = { udid: string; name: string; state: "booted" | "shutdown"; os: string };
export const listIosSims = () => j<{ simulators: IosSim[] }>("/api/ios/simulators");
export const bootIosSim = (payload: { udid: string }) =>
  j<{ ok: boolean; booted: string; shutdown: string[];
      wda: { started: boolean; detail: string } }>("/api/ios/sim/boot",
    { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload) });
// 排除阻塞：kind=run 后端执行安装命令；kind=open 打开（缺失则以模板创建）.env
export const doctorFix = (action: string) =>
  j<{ ok: boolean; action: string; path?: string; output?: string }>("/api/doctor/fix",
    { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }) });

// -- Prompt 文件管理（发起人 2026-10-14：控制台里可选择使用的 prompt 文件） ---------------
// 候选=工作区 prompts/ 全部文件 + 包内模板；选择落 prompts/.choice.json（解析优先级最高）；
// server 模式下选择不参与生效（prompt 由服务端下发），前端提示并禁用。
export type PromptCandidate = { file: string; source: "workspace" | "package";
  size: number; mtime: number; active: boolean };
// stale:工作区模板副本落后于包内新版(模板冻结);append_file/has_append:追加段
export type PromptSlot = { name: string; title: string; selected: string | null;
  active: { file: string; source: string; mtime: number; size: number } | null;
  candidates: PromptCandidate[];
  stale?: { file: string; package_mtime: number } | null;
  append_file: string; has_append: boolean };
export type PromptsResp = { mode: string; server: boolean; slots: PromptSlot[] };
export const getPrompts = () => j<PromptsResp>("/api/prompts");
export const getPromptContent = (file: string) =>
  j<{ file: string; source: string; content: string }>(
    `/api/prompts/content?file=${encodeURIComponent(file)}`);
export const selectPrompt = (name: string, file: string | null) =>
  postJson<{ ok: boolean; slot: PromptSlot }>("/api/prompts/select", { name, file });
export const savePrompt = (filename: string, content: string) =>
  postJson<{ ok: boolean; existed: boolean }>("/api/prompts/save", { filename, content });
// 过期模板副本升级:改名备份回落包内新版(追加段是独立文件不受影响)
export const upgradePrompt = (name: string) =>
  postJson<{ ok: boolean; backup: string; slot: PromptSlot }>("/api/prompts/upgrade", { name });
export type KeyAction = "home" | "back" | "recents" | "settings";
export const deviceKey = (payload: { action: KeyAction; serial?: string | null; platform?: string }) =>
  j<{ ok: boolean; action: KeyAction }>("/api/device/key", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
export const launchScrcpy = (serial?: string | null) =>
  j<{ ok: boolean; serial: string | null }>("/api/device/scrcpy", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ serial }) });

// -- 页面复核(把 AI 起的页名交给人过一遍) ------------------------------------------------
// 为什么有这块:AI 起名的**粒度不可控**(同一 Activity 下的一批同类页会被按内容词
// 分别命名),而用户比 AI 更懂自己的 App。改过的标题标 manual,`describe` 不再覆盖。
export type ReviewPage = {
  page_key: string; title: string; summary: string; title_source: string;
  summary_source: string;
  activity: string; depth?: number | null; screenshot_url: string;
};
export type ReviewResp = {
  package: string; pages: ReviewPage[];
  saved: Record<string, string>;      // 已存过的复核结论(page_key → 标题)
  manual: number;                     // 其中人工改过的页数
  review_path: string;
};
// 图谱准备度（2026-10-17）：只读状态 + 一条建议，**不是闸门**（服务端 readiness 模块有完整理由）。
export type ReadinessResp = {
  package: string; has_index: boolean; pages: number; reviewed: number;
  unreviewed: number; text: string; hint: string | null; explore_tip: string;
};
export const getReadiness = (pkg: string) =>
  j<ReadinessResp>(`/api/readiness?package=${encodeURIComponent(pkg)}`);
export const getReview = (pkg: string) =>
  j<ReviewResp>(`/api/review?package=${encodeURIComponent(pkg)}`);
export const saveReview = (
  pkg: string, corrections: Record<string, string>,
  summaryCorrections?: Record<string, string>) =>
  j<{ ok: boolean; applied: number; unchanged: number;
      summary_applied?: number; summary_unchanged?: number; promoted?: number;
      missing: string[] }>(
    "/api/review", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ package: pkg, corrections,
        summary_corrections: summaryCorrections }) });

// -- 自愈提案(定位失败被自愈救回的步骤 → 人工确认生成修复用例) -----------
export type HealProposal = { original: string; match: string; via: string; step?: string };
export type HealRun = { run: string; case: string; case_name?: string | null;
  status?: string; proposals: HealProposal[] };
export const getHeals = () => j<{ runs: HealRun[] }>("/api/heals");
export const applyHeal = (run: string) =>
  j<{ ok: boolean; out: string; notes: string[] }>("/api/heals/apply", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run }) });

// -- 报告分享(T55 P0:局域网零出域;配置台显性入口) --------------------------------------
export type ShareResp = { shared: boolean; host: string; port: number;
  urls: string[]; hint: string };
export const getShare = () => j<ShareResp>("/api/share");

// -- 工作区切换(服务端目录浏览 + 迁移档位;分享模式下后端禁用) ---------
export type FsListing = { path: string; parent: string | null; dirs: string[]; files?: string[] };
export const getFs = (path: string, ext?: string) =>
  j<FsListing>(`/api/fs?path=${encodeURIComponent(path || "")}${ext ? `&ext=${encodeURIComponent(ext)}` : ""}`);
export const openWorkspace = (path: string) =>
  j<{ ok: boolean; path: string }>("/api/fs/open", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
export const switchWorkspace = (payload: { path: string; migrate: "none" | "graph" | "full" }) =>
  j<{ ok: boolean; root: string; migrated: string[]; hint: string }>("/api/workspace/switch",
    { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload) });

// -- LLM 失败归因(对一次执行的失败步骤做四分类归因,重渲染报告) ----------
export const startDiagnose = (run: string) =>
  j<{ job_id: string }>("/api/diagnose", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run }) });

// -- 凭据(仅本机;用例中以 ${cred.名称} 引用;值永不回传) ---------------------------------
export const getCreds = () => j<{ names: string[] }>("/api/cred");
export const writeCred = (payload: { action: "set" | "del"; name: string; value?: string }) =>
  j<{ ok: boolean; names: string[] }>("/api/cred", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });

// -- 数据导出(PIPL 数据可携带权,zip 打包) ------------------------------------------------
export const startExport = (media: boolean) =>
  j<{ job_id: string }>("/api/export", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ media }) });

// -- 报告校准(分类改判 + 断言校准 + 规则落盘,三层与 CLI 同源) ----------
// 标记类扩展(发起人 2026-10-13):ignore=失败可忽略 / false_positive=通过是误判 /
// note=校准原因——只加标注不改 status,成功用例也能校准
export type CalibrateItem = {
  step_id: string; intent: string; action: string; status: string;
  category: string; category_source: string; error: string; logcat: string;
  screenshot: string; screenshot_url?: string;
  ignored?: boolean; false_positive?: boolean; calibration_note?: string;
  diagnosis?: { category?: string; confidence?: number; need_human?: boolean;
    suggestion?: string; evidence?: string[] };
  assertion?: { kind: "equals" | "contains"; expected: string; actual: string } | null;
};
export type CalibrateData = { run: string; run_label: string; items: CalibrateItem[];
  categories: string[]; category_labels: Record<string, string>;
  attr_labels: Record<string, string>; rules_count: number };
export type CalibrateStep = {
  category?: string; error_contains?: string; logcat_contains?: string; note?: string;
  ignore?: boolean; false_positive?: boolean;
  assertion_fix?: "" | "adopt-actual" | "loosen";
  assertion_expected?: string; assertion_actual?: string;
};
export const getCalibrate = (run: string) =>
  j<CalibrateData>(`/api/calibrate?run=${encodeURIComponent(run)}`);
export const applyCalibrate = (run: string, steps: Record<string, CalibrateStep>) =>
  j<{ ok: boolean; applied: number; unchanged: number; missing: string[];
      marked?: number;
      rules_saved: number; calibrated?: { file: string; notes: string[] } | null;
      warning?: string | null }>("/api/calibrate/apply", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run, steps }) });

// -- 探索异常校准(发起人 2026-10-13):确认/忽略逐条标记,回写 explore.json + 重渲染 -----
export type ExploreFinding = {
  type: string; detail?: string; page_key?: string; path?: string; depth?: number;
  screenshot?: string;
  calibration?: { verdict: "confirmed" | "ignored"; note?: string;
    operator?: string; at?: string } | null;
};
export const getExplore = (id: string) =>
  j<{ explore_id?: string; findings: ExploreFinding[]; [k: string]: unknown }>(
    `/api/explore?id=${encodeURIComponent(id)}`);
export const applyExploreCalibrate = (
  exploreId: string,
  findings: Record<number, { verdict?: "confirmed" | "ignored" | ""; note?: string }>) =>
  postJson<{ ok: boolean; confirmed: number; ignored: number; cleared: number;
    missing: string[]; warning?: string | null }>(
    "/api/explore/calibrate", { explore_id: exploreId, findings });

// -- 人工截屏(迭代:存工作区固定目录 <工作区>/screenshots/) ---------------------
export const captureScreenshot = (payload: { serial: string | null; platform: string }) =>
  j<{ ok: boolean; file: string; url: string; bytes: number }>("/api/screenshots/capture",
    { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload) });

// -- 被测 App 安装/卸载(测试准入;安装可选回显版本+包指纹 T76) ---------------
export const installApp = (payload: { serial: string | null; platform: string; path: string;
  package?: string }) =>
  j<{ job_id: string }>("/api/apps/install", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
export const uninstallApp = (payload: { serial: string | null; platform: string; package: string }) =>
  j<{ ok: boolean; package: string }>("/api/apps/uninstall", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });

// -- 套件（批量运行）：*.suite.json 的管理（资产名=套件/动作=批量运行） -------
export type SuiteInfo = { file: string; title: string; count: number };
export const getSuites = () => j<{ suites: SuiteInfo[] }>("/api/suites");
export const getSuite = (name: string) =>
  j<{ file: string; title: string; cases: string[] }>(`/api/suite?name=${encodeURIComponent(name)}`);
export const saveSuite = (payload: { file?: string | null; title: string; cases: string[] }) =>
  j<{ ok: boolean; file: string }>("/api/suites/save", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
export const deleteSuite = (payload: { file: string }) =>
  j<{ ok: boolean }>("/api/suites/delete", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
export const deleteCase = (payload: { name: string }) =>
  j<{ ok: boolean; deleted: string }>("/api/case/delete", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
// 重命名（目录功能的轻量替代）：套件引用自动跟随（后端同步替换）
export const renameCase = (payload: { from: string; to: string }) =>
  j<{ ok: boolean; file: string; suites_updated: string[] }>("/api/case/rename",
    { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload) });
