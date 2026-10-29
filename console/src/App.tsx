import {
  Alert, Badge, Button, Card, Col, Input, Layout, Menu, Modal, Row, Space, Tag, Tooltip, Typography, message,
} from "antd";
import type { MenuProps } from "antd";
type MenuPropsItems = NonNullable<MenuProps["items"]>[number];
import {
  BarChartOutlined, CheckCircleOutlined, FileTextOutlined, FolderOpenOutlined, MobileOutlined,
  PlayCircleOutlined, QuestionCircleOutlined, RobotOutlined, SearchOutlined, SolutionOutlined,
  SwapOutlined, ThunderboltOutlined, UserOutlined,
} from "@ant-design/icons";
import { ReactNode, useEffect, useRef, useState } from "react";
import {
  getJobs, getSeats, getState, getOperator, getVersionCheck, cancelJob, getMetrics, JobInfo,
  MetricItem, MetricsResp, openWorkspace, seatCheck, setOperator,
  StateResp, submitAuthKey, VersionCheck,
} from "./api";
import Audit from "./views/Audit";
import Cases from "./views/Cases";
import Heals from "./views/Heals";
import Runs from "./views/Runs";
import Reports from "./views/Reports";
import Generate from "./views/Generate";
import Settings from "./views/Settings";
import Review from "./views/Review";
import CommandPalette, { Nav } from "./components/CommandPalette";
import PageHeader from "./components/PageHeader";
import EnvDoctor from "./components/EnvDoctor";
import WorkspaceSwitcher from "./components/WorkspaceSwitcher";
import InviteNotice from "./components/InviteNotice";
import Screens, { KIND_LABEL } from "./views/Screens";
import { VIEWS, VIEW_BY_HASH, VIEW_BY_KEY, ViewKey, VIEW_GROUPS } from "./views";

// 控制台信息架构：
// 视图清单单一来源 src/views.ts（菜单分组 / ⌘K / 深链共用，修"三处手工同步漏项"）；
// 菜单按「准备 → 执行 → 资产 → 设置」工作流分组；全部视图支持 hash 深链。

function StatCard({ icon, tint, color, title, value, sub }: {
  icon: ReactNode; tint: string; color: string; title: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <Card style={{ height: "100%" }}>
      <div style={{ display: "flex", gap: 14, alignItems: "center" }}>
        <div style={{ width: 44, height: 44, borderRadius: 10, background: tint, color,
          display: "flex", alignItems: "center", justifyContent: "center", fontSize: 22, flex: "none" }}>
          {icon}
        </div>
        <div style={{ minWidth: 0 }}>
          <div style={{ color: "#697080", fontSize: 13 }}>{title}</div>
          {/* 长值（如 144.5/不限）降字号+禁换行——24px 在 span=6 列必换行（发起人 2026-10-12） */}
          <div style={{ fontSize: typeof value === "string" && value.length >= 8 ? 18 : 24,
            fontWeight: 600, lineHeight: "32px", whiteSpace: "nowrap" }}>{value}</div>
          {sub && <div style={{ fontSize: 12, color: "#8a919f", whiteSpace: "nowrap",
            overflow: "hidden", textOverflow: "ellipsis" }}>{sub}</div>}
        </div>
      </div>
    </Card>
  );
}

// P0 下一步建议卡(发起人 2026-10-13):按工作流依赖链(设备→AI→图谱→用例→执行→复盘)
// 从 /api/state 现有字段推断当前卡在哪一环,常驻给出唯一主行动与跳转——替代原 QuickStart
// 静态三步条(静态条不反映实际进度,老用户永远停在"连接设备",新用户看不出该先做什么)。
// 只读 state,不新增后端接口;全部就绪时转绿色就绪态并引导遍历探索。
function NextStepCard({ state, onNav }: { state: StateResp; onNav: (nav: Nav) => void }) {
  const online = state.devices.filter((d) => d.state === "device");
  const failed = state.runs.filter((r) => r.status && r.status !== "passed");
  const step: { icon: ReactNode; title: string; desc: string; action: string; view: string }
    | null = !online.length
    ? { icon: <MobileOutlined />, title: "第一步:连接设备",
        // iOS 仅 macOS 可用（Xcode 工具链）：其他平台不引导启动模拟器
        desc: state.ios_supported === false
          ? "Android 手机开启 USB 调试插上电脑;「设备/执行日志」页出现实时画面即就绪。"
          : "Android 手机开启 USB 调试插上电脑,或启动 iOS 模拟器;「设备/执行日志」页出现实时画面即就绪。",
        action: "去连接设备", view: "screens" }
    : !state.gen_ready
    ? { icon: <RobotOutlined />, title: "第二步:配置 AI",
        desc: "在「设置-用例凭据」填入模型凭据;用例生成、页面功能描述与视觉兜底都依赖它。",
        action: "去设置", view: "settings" }
    : !state.indexes.length
    ? { icon: <SolutionOutlined />, title: "第三步:让 AI 认识你的 App",
        desc: "在「页面复核」重建页面索引,生成页面图谱与控件清单——AI 生成用例与遍历探索都以它为基础。",
        action: "去重建索引", view: "review" }
    : !state.cases.length
    ? { icon: <ThunderboltOutlined />, title: "第四步:生成第一条用例",
        desc: "一句话描述测试场景,AI 按页面图谱生成结构化用例;也支持导入 Excel/XMind 手工用例。",
        action: "去生成", view: "generate" }
    : !state.runs.length
    ? { icon: <PlayCircleOutlined />, title: "第五步:运行用例",
        desc: "在「用例管理/执行」勾选用例运行,失败自动三级自愈,产出带证据链的 HTML 报告。",
        action: "去运行", view: "cases" }
    : failed.length
    ? { icon: <BarChartOutlined />, title: "复盘最近失败",
        desc: `最近有 ${failed.length} 次失败执行——打开报告查看 AI 归因与失败原因汇总,再校准或生成修复用例。`,
        action: "看报告", view: "reports" }
    : null;
  if (!step) {
    return (
      <Card size="small" style={{ marginTop: 16 }}
        title={<span><CheckCircleOutlined style={{ color: "#52c41a", marginRight: 8 }} />链路已就绪,可以开测</span>}
        extra={<Button size="small" onClick={() => onNav({ view: "runs" })}>去遍历探索找异常</Button>}>
        <Typography.Text type="secondary">
          设备、AI、页面图谱、用例、执行五环已通;可用「遍历探索」持续深挖异常,或回到用例页日常运行。
        </Typography.Text>
      </Card>
    );
  }
  return (
    <Card size="small" style={{ marginTop: 16 }}
      title={<span>{step.icon}<span style={{ color: "#4F5BFF", margin: "0 8px" }}>{step.title}</span></span>}
      extra={<Button type="primary" size="small" onClick={() => onNav({ view: step.view })}>{step.action}</Button>}>
      <Typography.Text type="secondary">{step.desc}</Typography.Text>
    </Card>
  );
}

// 质量指标卡（2026-10-27，发起人：「指标卡只有 CLI，GUI 概览页看不到」）：
// 直接消费 `/api/metrics`（与 `mat metrics --json` 同一实现）——缺产出项后端已标「未建立」，
// 前端只负责呈现达标 ✓/✗ 与口径 caveat，不在这里重算任何数字。
function QualityMetricsCard() {
  const [data, setData] = useState<MetricsResp | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    getMetrics().then(setData).catch((e) => setErr(String(e)));
  }, []);
  const groups: [string, MetricItem[] | undefined][] = [
    ["产品质量", data?.products], ["工具能力", data?.tool], ["成本效率", data?.cost],
  ];
  return (
    <Card size="small" title="质量指标" style={{ marginTop: 16 }}
      extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>
        与 mat metrics 同源 · 缺产出的项标「未建立」</Typography.Text>}>
      {err
        ? <Typography.Text type="danger" style={{ fontSize: 12 }}>指标读取失败：{err}</Typography.Text>
        : <Row gutter={16}>
          {groups.map(([title, items]) => (
            <Col span={8} key={title}>
              <div style={{ fontSize: 12, color: "#8a919f", marginBottom: 8 }}>{title}</div>
              {(items || []).map((it) => (
                <div key={it.name} style={{ marginBottom: 10 }}>
                  <div style={{ fontSize: 13 }}>
                    {it.name}: <b>{it.value}</b>
                    {it.met === true && <Tag color="green" bordered={false}
                      style={{ marginInlineStart: 6 }}>✓ 达标</Tag>}
                    {it.met === false && <Tag color="red" bordered={false}
                      style={{ marginInlineStart: 6 }}>✗ 未达标</Tag>}
                  </div>
                  {it.caveat && <div style={{ fontSize: 12, color: "#8a919f" }}>{it.caveat}</div>}
                  {it.target && <div style={{ fontSize: 12, color: "#8a919f" }}>目标 {it.target}</div>}
                </div>
              ))}
              {!items?.length && <div style={{ fontSize: 12, color: "#8a919f" }}>—</div>}
            </Col>
          ))}
        </Row>}
      {!data && !err && <Typography.Text type="secondary" style={{ fontSize: 12 }}>加载中…</Typography.Text>}
    </Card>
  );
}

function Overview({ state, onNav }: { state: StateResp; onNav: (nav: Nav) => void }) {
  const quota = state.usage;
  // 额度展示按**AI 接入**分（2026-10-24）：自带 API Key 不设上限，就别摆分母；
  // 官方代理如实显示 已用/上限。`ai_mode` 缺省（旧后端）时退回旧逻辑。
  const byok = state.ai_mode === "local";
  const genValue = byok ? "不限"
    : quota.gen_limit == null ? "无限" : `${quota.gen_count}/${quota.gen_limit}`;
  // 额度消耗口径的**单一来源**（2026-10-26 用户要求：各业务页的额度说明收敛到这里，
  // 数字后问号 tooltip 展示「哪里消耗多少」——与轻云 TASK_COST/TASK_BUCKET 同口径）
  const genTip = byok
    ? <>local(自带 API Key)不消耗官方 AI 额度。以下口径仅官方代理模式:
        <br/>· 用例生成 <b>1 次/任务</b>(同页多入口拆分不另计)
        <br/>· 导入转换 <b>0.5 次/用例</b>
        <br/>· AI 归因 <b>1 次/次</b>
        <br/>· 页面起名 <b>1 次/批</b>(一次调用描述一批,≤50 页)
        <br/>· 视觉兜底<b>单独额度</b>(与生成额度分池)</>
    : <>额度消耗口径:
        <br/>· 用例生成 <b>1 次/任务</b>(同页多入口拆分不另计)
        <br/>· 导入转换 <b>0.5 次/用例</b>
        <br/>· AI 归因 <b>1 次/次</b>
        <br/>· 页面起名 <b>1 次/批</b>(一次调用描述一批,≤50 页)
        <br/>· 视觉兜底<b>单独额度</b>:本月 {quota.vision_count}/{quota.vision_limit ?? "∞"}
        <br/>· 换自带 API Key(local)则不消耗官方额度</>;
  const online = state.devices.filter((d) => d.state === "device");
  const apps = new Set(Object.values(state.caseMeta).map((m) => m.app).filter(Boolean));
  const passed = state.runs.filter((r) => r.status === "passed").length;
  const [wsOpen, setWsOpen] = useState(false);
  const openWs = () => {
    openWorkspace(state.cwd).then(() => message.success("已在文件管理器打开工作区"))
      .catch((e) => message.error(String(e)));
  };
  return (
    <>
      <PageHeader title="概览" desc={<>环境、设备与最近活动的快照——回答「能不能开测、它在不在干活」。</>} />
      {/* 工作区模块紧跟页头(2026-10-11 改版:原在页尾;顶部切换入口同步移除,
          切换收进「根目录」后的按钮,效果与原顶部入口一致) */}
      {/* 用单行弹性布局（发起人 2026-10-12）：版本/最近探索紧排（内容多宽占多宽），
          根目录居中占剩余宽但**单行 ellipsis**（长路径 hover 看全文）——不再换行把
          打开/切换挤下二行，也不再因不收缩把右侧最近探索 Tag 截断出卡片 */}
      <Card title="工作区" size="small" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: 6, flex: "none" }}>
            <span style={{ color: "#8a919f", fontSize: 13 }}>版本</span>
            <span style={{ fontSize: 13 }}>v{state.version}</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 4,
            flex: "1 1 auto", minWidth: 0 }}>
            <span style={{ color: "#8a919f", fontSize: 13, flex: "none" }}>根目录</span>
            {/* copyable 会触发 antd ellipsis 的 JS 测量模式（needMeasureEllipsis 含 enableCopy），
                窄容器下测量失灵文本全展开——故用手动 CSS 单行截断 + Tooltip 全文（发起人 2026-10-12） */}
            <Tooltip title={state.cwd}>
              <Typography.Text copyable={{ text: state.cwd }}
                style={{ fontSize: 12, flex: "0 1 auto", minWidth: 0, display: "inline-block",
                  overflow: "hidden", whiteSpace: "nowrap", textOverflow: "ellipsis" }}>
                {state.cwd}
              </Typography.Text>
            </Tooltip>
            <Tooltip title="在本机文件管理器中打开工作区目录">
              <Button size="small" type="text" icon={<FolderOpenOutlined />}
                style={{ flex: "none", paddingInline: 6 }} onClick={openWs}>打开</Button>
            </Tooltip>
            <Tooltip title="更换到其他工作区目录">
              <Button size="small" type="text" icon={<SwapOutlined />}
                style={{ flex: "none", paddingInline: 6 }} onClick={() => setWsOpen(true)}>更换</Button>
            </Tooltip>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 4, flex: "none" }}>
            <span style={{ color: "#8a919f", fontSize: 13, flex: "none" }}>最近探索</span>
            <span>
              {state.explores.length
                ? state.explores.slice(0, 2).map((e) => (
                    <Tag key={e.id} style={{ marginInlineEnd: 4 }}>{e.id} · {e.steps}步/发现{e.findings}</Tag>))
                : "—"}
            </span>
          </div>
        </div>
      </Card>
      <WorkspaceSwitcher state={state} open={wsOpen} onClose={() => setWsOpen(false)} />
      {/* code_stale 是对象({stale,file,behind_seconds}),直接判真恒为 true——必须取 .stale */}
      {state.code_stale?.stale && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message="源码比进程新" description="请重启 console 后再执行写盘型任务(run/gen/explore)。" />
      )}
      {/* 邀请奖励到账提示：只在到账时冒一条，不常驻 */}
      <InviteNotice />
      <Row gutter={16}>
        <Col span={6}><StatCard icon={<MobileOutlined />} tint="#f0fdf4" color="#52c41a"
          title="在线设备" value={online.length}
          sub={online.length ? online.map((d) => d.model || d.serial).slice(0, 2).join(" · ") : "未连接设备"} /></Col>
        <Col span={6}><StatCard icon={<FileTextOutlined />} tint="#eef1ff" color="#4F5BFF"
          title="用例" value={state.cases.length}
          sub={apps.size ? `覆盖 ${apps.size} 个应用` : "暂无用例"} /></Col>
        <Col span={6}><StatCard icon={<RobotOutlined />} tint="#f9f0ff" color="#9254de"
          title="AI 生成(本月)"
          value={<>{genValue}
            <Tooltip title={genTip} overlayStyle={{ maxWidth: 420 }}>
              <QuestionCircleOutlined style={{ fontSize: 14, color: "#8a919f",
                marginLeft: 6, cursor: "help" }} />
            </Tooltip></>}
          sub={<Tag style={{ marginLeft: 0 }} color={state.gen_ready ? "green" : "orange"}
            bordered={false}>{state.gen_ready
            ? `${byok ? "local" : "server"} · ${byok ? "不限额度" : "官方代理额度"}`
            : "未配置"}</Tag>} /></Col>
        <Col span={6}><StatCard icon={<PlayCircleOutlined />} tint="#fff7e6" color="#fa8c16"
          title="最近执行" value={state.runs.length}
          sub={state.runs.length ? `通过 ${passed} · 失败 ${state.runs.length - passed}` : "暂无执行"} /></Col>
      </Row>
      <NextStepCard state={state} onNav={onNav} />
      <QualityMetricsCard />
      <EnvDoctor state={state} onGoSettings={() => onNav({ view: "settings" })} />
    </>
  );
}

function parseHash(): { view: ViewKey; caseName?: string; stepId?: string; jobId?: string } {
  const seg = location.hash.replace(/^#\/?/, "").split("/");
  // 用例深链支持 ?step= 定位失败步骤（报告「编辑此步」→ 编辑器滚动高亮，发起人 2026-10-12 P1）
  let caseName: string | undefined; let stepId: string | undefined;
  if (seg[0] === "cases") {
    const [raw, q = ""] = decodeURIComponent(seg[1] || "").split("?");
    caseName = raw || undefined;
    stepId = new URLSearchParams(q).get("step") || undefined;
  }
  return { view: VIEW_BY_HASH[seg[0]] ?? "overview", caseName, stepId,
           jobId: seg[0] === "screens" ? seg[1] || undefined : undefined };
}

// -- 团队协作 P1（发起人 2026-10-12）-----------------------------------------------
// AuthGate：--share 口令鉴权。api.ts 任何请求收到 401{auth:true} 派发事件，这里统一弹
// 口令 Modal；成功后服务端种 cookie（一年有效），reload 即放行。单机模式永不触发。
function AuthGate() {
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(false);
  useEffect(() => {
    const onReq = () => setOpen(true);
    window.addEventListener("mat-auth-required", onReq);
    return () => window.removeEventListener("mat-auth-required", onReq);
  }, []);
  const submit = () => {
    if (!key.trim()) return;
    setBusy(true);
    submitAuthKey(key).then(() => window.location.reload())
      .catch(() => setMsg(true)).finally(() => setBusy(false));
  };
  return (
    <Modal title="访问口令" open={open} closable={false} footer={null} width={380}>
      <Space direction="vertical" style={{ width: "100%" }} size={10}>
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          这台服务开启了局域网分享，需要访问口令（向跑 MAT 的同事要）。输入一次长期有效。
        </Typography.Text>
        <Space.Compact style={{ width: "100%" }}>
          <Input.Password value={key} autoFocus placeholder="访问口令"
            onChange={(e) => { setKey(e.target.value); setMsg(false); }}
            onPressEnter={submit} />
          <Button type="primary" loading={busy} disabled={!key.trim()} onClick={submit}>进入</Button>
        </Space.Compact>
        {msg && <Alert type="error" showIcon message="口令不对，再试一次" />}
      </Space>
    </Modal>
  );
}

// OperatorPrompt：触发者身份 + 成员登记（商业化：**免费基线 5 人**；更高上限来自
// 服务端按主机发的配额 —— 见 `seat_quota` 与 `scripts/manage_seat.py`，与激活码无关）。
// 首次进入填名字
// → 服务端成员登记（新名字占一个名额，已满 403 提示开通；老名字直接核销）。名字存本机
// localStorage；Header 常驻显示可改（改名=占新名额，同样过登记）。
// ⚠️ open 由 App 持有（Header 按钮 / 首次进入都会置位）——此前 open 是组件内部 state
// 且从未置 true，点右上角「设置名字」毫无反应（发起人 2026-10-12 反馈）。
// ⚠️ 逃生口按模式（2026-10-25 方案 B）：**共享模式（--share）**未命名时不给逃生口（无取消/关闭/
// ESC/点遮罩）——后端写接口全过人数闸，匿名做任何写操作都会 400，前端不该放行匿名状态
// （发起人 2026-10-12：匿名可绕名额）；**本机单人模式**名字已是可选（operator 兜底「本机」），
// 不可关闭的弹窗只剩陷阱意义——给全量逃生口。
function OperatorPrompt({ operator, open, onClose, onSaved, shared }: {
  operator: string; open: boolean; shared: boolean;
  onClose: () => void; onSaved: (n: string) => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {  // 打开时预填当前名字——改名字不必重敲
    if (open) setName(operator || "");
  }, [open, operator]);
  // 被名额拒过（403）后必须可关：提示文案的出路（设置页移除旧名字）在弹窗后面——
  // 关不掉就等于锁死（2026-10-25 评审：未授权 --share 的 1 席被旧名占住、换浏览器
  // 清了 localStorage 的场景，方案 B 的 canEscape 只豁免了非 shared）
  const [rejected, setRejected] = useState(false);
  const save = () => {
    const n = name.trim().slice(0, 40);
    if (!n || busy) return;
    setBusy(true); setErr(null);
    seatCheck(n).then(() => { setOperator(n); onSaved(n); onClose(); })
      .catch((e) => { setErr(String(e).replace("Error: ", "")); setRejected(true); })
      .finally(() => setBusy(false));
  };
  const canEscape = !!operator || !shared || rejected;  // 已命名可关；单人随时可关；被拒后给出路
  return (
    <Modal title={operator ? "修改名字" : "先打个招呼——你是谁？"} open={open}
      okText="进入" cancelText="取消" width={380}
      closable={canEscape} keyboard={canEscape} maskClosable={canEscape}
      cancelButtonProps={{ style: canEscape ? undefined : { display: "none" } }}
      onCancel={onClose} onOk={save}
      okButtonProps={{ disabled: !name.trim(), loading: busy }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginBottom: 12 }}>
        {shared
          ? "团队协作时任务会记录触发者，同事能看到「谁在跑什么」、报告按人区分。成员数默认最多5人，可在设置页管理。"
          : "单人模式可不设置名字——任务会记为「本机」。要和同事共用并按人区分，请用 mat console --share 启动。"}
      </Typography.Paragraph>
      <Input value={name} autoFocus maxLength={40} placeholder="如：张三 / Wei"
        onChange={(e) => { setName(e.target.value); setErr(null); }} onPressEnter={save} />
      {err && <Alert type="error" showIcon style={{ marginTop: 10 }} message={err} />}
    </Modal>
  );
}

// TaskBar：全局任务横幅（所有页面可见）——「谁 · 在哪台设备 · 跑什么 · 运行/排队第几位」。
// 单机单人时多数时间无活动任务、横幅自动隐藏；团队场景下它就是共享主机的状态灯。
// 2026-10-25 升级为**可操作**（发起人场景：任务在跑，人应该能去别的页干别的）：
// 每条任务带「日志」（跳投屏页定位）/「实时投屏」（/live 新标签）/「取消」（优雅停）；
// 任务从运行转为终态时全局 toast 携摘要——不用守在哪个页面等。
function TaskBar({ onOpenJob }: { onOpenJob: (id: string) => void }) {
  const [jobs, setJobs] = useState<JobInfo[]>([]);
  const [cancelling, setCancelling] = useState<string | null>(null);
  const seen = useRef<Map<string, string>>(new Map());  // id → 上次 status（完成检测）
  useEffect(() => {
    const tick = () => getJobs().then((d) => {
      setJobs(d.jobs);
      for (const j of d.jobs) {
        const prev = seen.current.get(j.id);
        if (prev === "running" || prev === "queued") {
          if (j.status === "done") message.success(`「${KIND_LABEL[j.kind] || j.kind}」完成：${(j.summary || "").slice(0, 60)}`);
          if (j.status === "error") message.error(`「${KIND_LABEL[j.kind] || j.kind}」失败：${((j.error || j.summary) || "详见日志").slice(0, 60)}`);
        }
        seen.current.set(j.id, j.status);
      }
    }).catch(() => {});
    tick();
    const t = setInterval(tick, 3000);
    return () => clearInterval(t);
  }, []);
  const active = jobs.filter((j) => j.status === "running" || j.status === "queued");
  if (active.length === 0) return null;
  const queuePos = (j: JobInfo): number =>
    active.filter((x) => x.status === "queued"
      && (x.serial ?? "") === (j.serial ?? "") && (x.platform ?? "") === (j.platform ?? ""))
      .findIndex((x) => x.id === j.id) + 1;
  const dev = (j: JobInfo) => j.serial || (j.platform === "ios" ? "iOS 模拟器" : "安卓");
  const cancel = (id: string) => {
    setCancelling(id);
    cancelJob(id).catch(() => {}).finally(() => setCancelling(null));
  };
  return (
    <Alert type="info" showIcon style={{ marginBottom: 16 }} message={
      <Space size={16} wrap direction="vertical" style={{ width: "100%" }}>
        {active.map((j) => (
          <Space key={j.id} size={8} wrap>
            <Typography.Text strong>{j.operator || "匿名"}</Typography.Text>
            {" · "}{KIND_LABEL[j.kind] || j.kind}{" · "}{dev(j)}
            {" · "}{j.status === "running" ? "运行中" : `排队第 ${queuePos(j)} 位`}
            <Button type="link" size="small" style={{ padding: 0 }}
              onClick={() => onOpenJob(j.id)}>日志</Button>
            <a href={`/live/${j.id}`} target="_blank" rel="noreferrer"
              style={{ fontSize: 12 }}>实时投屏</a>
            <Button type="link" size="small" danger style={{ padding: 0 }}
              loading={cancelling === j.id}
              onClick={() => cancel(j.id)}>取消</Button>
          </Space>
        ))}
      </Space>} />
  );
}

export default function App() {
  const [route, setRoute] = useState(parseHash);
  const [view, setView] = useState<ViewKey>(route.view);
  const [caseName, setCaseName] = useState<string | undefined>(route.caseName);
  const [stepFocus, setStepFocus] = useState<string | undefined>(route.stepId);
  const [focusJob, setFocusJob] = useState<string | undefined>(route.jobId);
  const [wsOpen, setWsOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);

  const navigate = (nav: Nav) => {
    const def = VIEW_BY_KEY[nav.view as ViewKey];
    const seg = def?.hash ?? "overview";
    const arg = nav.view === "cases" && nav.caseName ? "/" + encodeURIComponent(nav.caseName)
      : nav.view === "screens" && nav.jobId ? "/" + nav.jobId : "";
    location.hash = `#/${seg}${arg}`;
  };
  useEffect(() => {
    const onHash = () => {
      const r = parseHash();
      setRoute(r); setView(r.view); setCaseName(r.caseName); setStepFocus(r.stepId);
      if (r.jobId) setFocusJob(r.jobId);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPaletteOpen(true); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  // 下发任务(执行用例/套件、探索等)→ 跳「设备投屏」页,执行日志定位到该任务
  // (发起人 2026-10-11:自动跳转到对应测试设备投屏页面并定位日志)。
  const onLaunched = (id: string) => { navigate({ view: "screens", jobId: id }); };
  const [state, setState] = useState<StateResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [conn, setConn] = useState(true);
  // 团队协作 P1：触发者身份（首次进入弹窗填写；Header 常驻显示可改）
  const [operator, setOperatorState] = useState(getOperator());
  const [opModalOpen, setOpModalOpen] = useState(false);
  // 2026-10-25 方案 B：本机单人模式（未 --share）免名——服务端自动记 operator=本机，
  // 不再弹窗；仅共享模式（--share）要求实名。shared 一次拉取，弹窗逃生口与首次弹窗共用。
  const [shared, setShared] = useState(false);
  useEffect(() => {
    getSeats().then((s: { shared?: boolean }) => setShared(!!s.shared)).catch(() => { /* 查不到按单人处理 */ });
  }, []);
  useEffect(() => {  // 无名字才弹——改名字走 Header 按钮
    if (!getOperator() && shared) setOpModalOpen(true);
  }, [shared]);

  // 新版本检查（发起人 2026-10-14）：后端服务启动时查过一次 PyPI（失败 latest=null），
  // 前端只读结果；has_update ⇒ 左上角版本旁亮「新」badge，点击弹窗给对应渠道升级命令
  const [vc, setVc] = useState<VersionCheck | null>(null);
  const [vcOpen, setVcOpen] = useState(false);
  useEffect(() => { getVersionCheck().then(setVc).catch(() => { /* 查不到静默 */ }); }, []);

  useEffect(() => {
    getState().then((s) => { setState(s); setConn(true); }).catch((e) => { setErr(String(e)); setConn(false); });
    const t = setInterval(() => getState()
      .then((s) => { setState(s); setConn(true); setErr(null); })
      .catch(() => setConn(false)), 5000);
    return () => clearInterval(t);
  }, []);

  const menuItems: MenuPropsItems[] = VIEW_GROUPS.flatMap((group): MenuPropsItems[] => {
    if (group === "") {
      return VIEWS.filter((v) => v.group === "")
        .map((v) => ({ key: v.key, icon: v.icon, label: v.label }));
    }
    return [{
      key: `group-${group}`, type: "group" as const, label: group,
      children: VIEWS.filter((v) => v.group === group)
        .map((v) => ({ key: v.key, icon: v.icon, label: v.label })),
    }];
  });

  // 侧栏菜单上下渐变提示：菜单高度超出可视区 ⇒ 对应端盖白渐变暗示可滚动，
  // 滚到边界后该端渐变消失。onScroll 只在滚动时触发，首帧与窗口缩放靠 effect 补算。
  const menuScrollRef = useRef<HTMLDivElement>(null);
  const [menuFade, setMenuFade] = useState({ top: false, bottom: false });
  const updateMenuFade = () => {
    const el = menuScrollRef.current;
    if (!el) return;
    setMenuFade({
      top: el.scrollTop > 4,
      bottom: el.scrollHeight - el.clientHeight - el.scrollTop > 4,
    });
  };
  useEffect(() => {
    updateMenuFade();
    window.addEventListener("resize", updateMenuFade);
    return () => window.removeEventListener("resize", updateMenuFade);
  }, []);

  return (
    <Layout style={{ height: "100vh" }}>
      <Layout.Sider theme="light" width={208}
        style={{ borderRight: "1px solid #f0f0f0", display: "flex", flexDirection: "column",
          height: "100vh", position: "sticky", top: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "18px 16px 14px" }}>
          {/* 品牌 logo（发起人 2026-10-12：替换为 mark.svg 品牌图形——渐变圆角方块+白描边对勾），
              内联渲染：零静态请求、无闪烁，favicon 走 /assets/mark.svg 同一图形 */}
          <svg viewBox="0 0 64 64" role="img" aria-label="MAT" width={34} height={34}
            style={{ flex: "none", display: "block" }}>
            <defs>
              <linearGradient id="mat-mark-g" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stopColor="#4F5BFF" />
                <stop offset="1" stopColor="#6C7BFF" />
              </linearGradient>
            </defs>
            <rect width="64" height="64" rx="14" fill="url(#mat-mark-g)" />
            <rect x="19.5" y="10.5" width="25" height="43" rx="6.5" fill="none" stroke="#fff" strokeWidth="5" />
            <path d="M25.5 32.5 L30.5 38 L39 26.5" fill="none" stroke="#fff" strokeWidth="5"
              strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              {/* nowrap（发起人 2026-10-12）：加了套餐标签后侧栏宽度吃紧，标题不许换行 */}
              <span style={{ fontWeight: 700, fontSize: 15, lineHeight: "20px",
                whiteSpace: "nowrap" }}>MAT 控制台</span>
              {/* AI 接入标签（2026-10-24：此前把「模式」和「AI 接入」混成一维，
                  误导出「标准模式只能自带 API Key」的观感）——
                  使用方式（体验/标准/团队）由授权与 --share 决定，AI 接入选代理还是自带 API Key
                  是另一维：标准模式走代理、也没问题。 */}
              {state && (() => {
                // 左上角徽标 = **当前的模式名称**（2026-10-25 用户要求）：使用方式档位
                // （体验/标准/团队，只由授权决定，与 __main__._usage_mode_label 同映射）；
                // AI 接入（官方代理/自带 Key）是另一维，挪进 Tooltip 保留——额度行为不同。
                const plan = (state.license?.plan || "free").toLowerCase();
                const mode = plan === "free" ? "体验模式"
                  : (plan === "team" || plan === "private") ? "团队模式" : "标准模式";
                return (
                  <Tooltip title={`${mode} · AI 接入：${state.ai_mode === "server"
                    ? "官方代理（有月度上限）"
                    : "自带 API Key（本站不设上限，请求直连你选的服务商）"}`}>
                    <Tag color={state.ai_mode === "server" ? "default" : "gold"}
                      style={{ marginInlineEnd: 0, fontSize: 11, lineHeight: "16px",
                        paddingInline: 5, paddingBlock: 0, borderRadius: 4, flexShrink: 0 }}>
                      {mode}
                    </Tag>
                  </Tooltip>
                );
              })()}
            </div>
            {/* 版本号（发起人 2026-10-14：左上角常驻）——副标题行宽度只有 ~130px，
                「移动 AI 测试」与品牌语义重复让位给版本号；有新版时亮「新」badge 点击弹窗 */}
            <div style={{ display: "flex", alignItems: "center", gap: 4, color: "#8a919f",
              fontSize: 11, whiteSpace: "nowrap" }}>
              <span>v{vc?.current ?? state?.version ?? "…"}</span>
              <span>· 本机服务</span>
              {vc?.has_update && (
                <Tooltip title={`新版本 v${vc.latest} 可用，点击查看升级方式`}>
                  <Tag color="orange" onClick={() => setVcOpen(true)}
                    style={{ marginInlineEnd: 0, fontSize: 10, lineHeight: "14px",
                      paddingInline: 4, paddingBlock: 0, borderRadius: 4, cursor: "pointer",
                      flexShrink: 0 }}>新</Tag>
                </Tooltip>
              )}
            </div>
          </div>
        </div>
        {/* minHeight:0 是关键——flex 子项默认 min-height:auto 会撑开到内容高度，
            overflowY:auto 永不触发；矮窗口下菜单溢出侧栏且 body 已 overflow:hidden
            （白区修复）无处兜底，表现为「菜单不能上下滑动」（发起人 2026-10-12）。
            上下渐变（发起人 2026-10-13）：菜单溢出视口时顶部/底部盖一条白→透明渐变，
            暗示还有菜单可滚动；滚到边界后对应渐变消失（纯视觉层，pointerEvents:none
            不挡点击）。 */}
        <div style={{ position: "relative", flex: 1, minHeight: 0 }}>
          <div ref={menuScrollRef} onScroll={updateMenuFade}
            style={{ height: "100%", overflowY: "auto" }}>
            <Menu mode="inline" selectedKeys={[view]}
              onClick={(e) => navigate({ view: e.key as ViewKey })}
              items={menuItems}
              style={{ borderInlineEnd: "none", padding: "0 8px" }} />
          </div>
          {menuFade.top && (
            <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: 28,
              background: "linear-gradient(to bottom, #eceef2 20%, rgba(255,255,255,0))",
              pointerEvents: "none" }} />
          )}
          {menuFade.bottom && (
            <div style={{ position: "absolute", bottom: 0, left: 0, right: 0, height: 28,
              background: "linear-gradient(to top, #eceef2 20%, rgba(255,255,255,0))",
              pointerEvents: "none" }} />
          )}
        </div>
        <div style={{ flex: "none", padding: "10px 16px 12px",
          color: "#8a919f", fontSize: 12, borderTop: "1px solid #f0f0f0" }}>
          <Space size={4} wrap>
            {/* 使用手册（发起人 2026-10-13）：静态单页随服务伺服，替换原「仓库」外链 */}
            <a href="/manual.html" target="_blank" rel="noreferrer">使用手册</a>
            <span>·</span>
            <a href="https://mobileaitest.com" target="_blank" rel="noreferrer">官网</a>
          </Space>
        </div>
      </Layout.Sider>
      <Layout style={{ height: "100vh", overflow: "hidden" }}>
        <Layout.Header style={{ background: "#fff", padding: "0 24px", height: 52, lineHeight: "normal",
          display: "flex", alignItems: "center", gap: 12, borderBottom: "1px solid #f0f0f0", flex: "none" }}>
          {/* 工作区入口已收进「概览」页工作区模块(打开/切换),顶部只留搜索与连接状态 */}
          <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 12 }}>
            <Input size="small" prefix={<SearchOutlined style={{ color: "#b6bac3" }} />}
              placeholder="搜索用例 / 报告 / 设备" readOnly style={{ width: 210, cursor: "pointer" }}
              onClick={() => setPaletteOpen(true)}
              suffix={<Tooltip title="快捷键"><span style={{
                border: "1px solid #e5e6eb", borderRadius: 4, padding: "0 5px", fontSize: 11,
                color: "#8a919f", background: "#fafafa" }}>⌘K</span></Tooltip>} />
            <Tooltip title={conn ? "后端连接正常(5s 心跳)" : "后端连接已断开"}>
              <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#697080" }}>
                <Badge status={conn ? "success" : "error"} text={conn ? "已连接" : "已断开"} />
              </span>
            </Tooltip>
            <Tooltip title="点击修改你的名字——团队场景下任务会记录触发者（名字只存本机）">
              <Button size="small" type="text" icon={<UserOutlined />}
                onClick={() => setOpModalOpen(true)}>{operator || "设置名字"}</Button>
            </Tooltip>
          </div>
        </Layout.Header>
        <Layout.Content style={{ padding: 24, background: "#f5f6fa", minWidth: 0, overflowY: "auto",
          // 滚到边界再滑会被 macOS 弹性过滚动接管——链到根元素露出 html 白底，
          // 页面底部出现整块白区（发起人 2026-10-12）；contain 禁掉本容器的边界回弹，
          // 表格等内部滚动容器到边界后仍正常链到本容器，页面滚动不受影响
          overscrollBehavior: "contain" }}>
          <TaskBar onOpenJob={onLaunched} />
          {err && !state && <Alert type="error" message={`后端连接失败:${err}`} style={{ marginBottom: 16 }} />}
          {!state && !err && <Card loading style={{ minHeight: 300 }} />}
          {view === "overview" && state && <Overview state={state} onNav={navigate} />}
          {view === "cases" && state && <Cases state={state} onLaunched={onLaunched}
            openCaseName={caseName} focusStep={stepFocus} onOpenCase={(n) => navigate({ view: "cases", caseName: n })}
            onNav={navigate} onCasesChanged={() => getState().then(setState)} />}
          {view === "generate" && state && <Generate state={state} onCasesChanged={() => getState().then(setState)} />}
          {view === "runs" && state && <Runs state={state} onLaunched={onLaunched} />}
          {view === "screens" && state && <Screens state={state} focusJob={focusJob} />}
          {view === "reports" && state && <Reports state={state} onLaunched={onLaunched} />}
          {view === "heals" && state && <Heals state={state} />}
          {view === "review" && state && <Review state={state} />}
          {view === "settings" && state && <Settings state={state}
            onRefresh={() => getState().then(setState)} />}
          {view === "audit" && <Audit />}
          <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)}
            state={state} onNav={navigate} />
          <AuthGate />
          <OperatorPrompt operator={operator} open={opModalOpen} shared={shared}
            onClose={() => setOpModalOpen(false)} onSaved={(n) => setOperatorState(n)} />
          {/* 升级引导弹窗：运行中的进程无法可靠替换自己（pipx/uv/pip 各有安装位置），
              给对应渠道的升级命令由用户执行，升级后重启控制台生效（发起人 2026-10-14） */}
          <Modal open={vcOpen} onCancel={() => setVcOpen(false)} footer={null} width={460}
            title={<>发现新版本 <Tag color="orange" style={{ marginInlineEnd: 0 }}>v{vc?.latest}</Tag></>}>
            <Typography.Paragraph style={{ marginBottom: 8 }}>
              当前 <b>v{vc?.current}</b> → 最新 <b>v{vc?.latest}</b>
              <span style={{ color: "#8a919f" }}>（检查时间 {vc?.checked_at?.slice(0, 16).replace("T", " ")}）</span>
            </Typography.Paragraph>
            <Typography.Paragraph copyable={{ text: vc?.upgrade_cmd }}
              style={{ background: "#f6f8fa", border: "1px solid #e5e7eb", borderRadius: 6,
                padding: "8px 10px", fontFamily: "ui-monospace, monospace", fontSize: 12 }}>
              {vc?.upgrade_cmd}
            </Typography.Paragraph>
            <Typography.Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 12 }}>
              在终端执行上面的命令完成升级，然后重启控制台即可生效——本窗口不会自动更新，
              升级期间正在执行的任务不受影响。
            </Typography.Paragraph>
          </Modal>
        </Layout.Content>
      </Layout>
    </Layout>
  );
}
