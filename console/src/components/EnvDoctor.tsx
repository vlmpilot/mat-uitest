import { Button, Card, Col, Row, Tag, Tooltip, message } from "antd";
import { useCallback, useEffect, useState } from "react";
import { DoctorCheck, DoctorResp, doctorFix, getDoctor, StateResp } from "../api";
import PromptManager from "./PromptManager";

// F2 环境卡片组(mat-console-design §3):回答「我能用吗」——`mat doctor` 与本卡片
// 是同一份检查的两个渲染层(检查逻辑在服务端 doctor.py,此处只做红/黄/绿三态)。
// 三张卡:Android(关键 4 项定结论,与 CLI 退出码同源)/ iOS(整组可选,分态降级)/
// AI 生成(gen/vl/quota/prompt,从 Android 检查组里分出来——线框 §4.4 的三卡布局)。
// 一键修复(发起人 2026-10-11):阻塞项在提示右侧直接给排除按钮——装依赖的执行安装
// 命令、缺配置的打开 .env(命令在后端 FIX_ACTIONS 白名单,前端只传 action id)。

const STATUS_DOT: Record<DoctorCheck["status"], string> = { ok: "#52c41a", warn: "#faad14", fail: "#ff4d4f" };

const AI_IDS = new Set(["gen", "vl", "quota", "prompt"]);
const isAi = (c: DoctorCheck) => AI_IDS.has(c.id);

// gen 未配置时的两种方式(截图文案):直接铺在行下,不藏 tooltip——配置内容是
// 用户要照抄的,不是补充说明。与 doctor.py gen_hint、ENV_TEMPLATE 同源同文案。
const GEN_WAYS: { title: string; lines: string[] }[] = [
  { title: "方式一：官方代理", lines: [
    "GEN_BASE_URL=https://mobileaitest.com",
    "GEN_API_KEY=<你的 token>",
    "GEN_PROMPT_MODE=server"] },
  { title: "方式二：自带 Key（OpenAI 兼容端点）", lines: [
    "GEN_BASE_URL=<你的 OpenAI 兼容端点>",
    "GEN_API_KEY=<你的 Key>",
    "GEN_MODEL=<语言模型名>",
    "GEN_VL_MODEL=<视觉模型名>"] },
];

function GenConfigWays() {
  return (
    <div style={{ margin: "4px 0 10px 16px" }}>
      {GEN_WAYS.map((w) => (
        <div key={w.title} style={{ marginBottom: 10 }}>
          <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 2 }}>{w.title}</div>
          <div style={{ background: "#f6f8fa", border: "1px solid #f0f0f0", borderRadius: 8,
            padding: "6px 12px", fontSize: 12, lineHeight: "20px" }}>
            {w.lines.map((l) => (
              <div key={l} style={{ fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace" }}>{l}</div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

// 修复动作按钮:kind=open(打开 .env)即点即开;其余(run 装依赖 / wda 启动服务)带
// loading 并在完成后重新检测（发起人 2026-10-12：WDA 未起行加一键「启动」）。
function FixButton({ fix, onDone }: { fix: NonNullable<DoctorCheck["fix"]>; onDone: () => void }) {
  const [loading, setLoading] = useState(false);
  const isOpen = fix.kind === "open";
  const run = async () => {
    setLoading(true);
    if (fix.action === "start-wda") {
      message.info("正在启动 WDA…若本机从未构建过 WDA，将自动下载源码并构建（约 2~5 分钟），请稍候");
    }
    try {
      const r = await doctorFix(fix.action);
      message.success(isOpen ? `已打开配置文件 ${r.path ?? ".env"},改完保存后点「重新检测」` : `${fix.label}完成,已重新检测`);
      onDone();
    } catch (e) { message.error(String(e)); }
    finally { setLoading(false); }
  };
  return (
    <Button size="small" type="link" style={{ flex: "none", padding: 0, height: "auto" }}
      loading={loading} onClick={(e) => { e.stopPropagation(); run(); }}>
      {fix.label}
    </Button>
  );
}

function CheckRow({ c, onRerun, onGoSettings, onManage }: { c: DoctorCheck; onRerun: () => void;
  onGoSettings?: () => void; onManage?: () => void }) {
  const tip = [c.detail, c.hint].filter(Boolean).join("\n");
  const blocked = c.status !== "ok" && !!c.fix;
  return (
    <div>
      <Tooltip title={tip || undefined} overlayStyle={{ maxWidth: 420 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "3px 0",
          cursor: tip ? "help" : "default" }}>
          <span style={{ flex: "none", width: 8, height: 8, borderRadius: "50%",
            background: STATUS_DOT[c.status] }} />
          <span style={{ wordBreak: "break-all", color: c.status === "ok" ? "inherit" : "#595959" }}>{c.label}</span>
          {blocked && c.fix && <FixButton fix={c.fix} onDone={onRerun} />}
          {/* 套餐用尽需要输入激活码,不能一键修复——导航到设置页的激活表单 */}
          {c.id === "quota" && c.status !== "ok" && onGoSettings && (
            <Button size="small" type="link" style={{ flex: "none", padding: 0, height: "auto" }}
              onClick={() => onGoSettings()}>去激活</Button>
          )}
          {/* AI 卡两行各带常驻入口（发起人 2026-10-14）：「AI生成prompt」行→管理浮层；
              「AI模型配置」行→配置（打开 .env，任意状态可进，不只是未配置时） */}
          {c.id === "prompt" && onManage && (
            <Button size="small" type="link" style={{ flex: "none", padding: 0, height: "auto" }}
              onClick={() => onManage()}>管理</Button>
          )}
          {c.id === "gen" && (
            <FixButton fix={{ action: "open-env", kind: "open", label: "配置" }} onDone={onRerun} />
          )}
        </div>
      </Tooltip>
      {c.id === "gen" && !c.ok && <GenConfigWays />}
    </div>
  );
}

// Android:关键项(python/adb/devices/u2)失败即阻塞——与 CLI doctor 退出码同源。
function androidConclusion(checks: DoctorCheck[]): { color: string; text: string } {
  const blocking = checks.some((c) => c.critical && c.status !== "ok");
  return blocking ? { color: "red", text: "有阻塞项" } : { color: "green", text: "就绪,可以开测" };
}

// iOS:整组可选——库没装=灰(用户没进这条赛道);装了库服务没起=红;模拟器没开=橙。
function iosConclusion(checks: DoctorCheck[]): { color: string; text: string } {
  const byId = Object.fromEntries(checks.map((c) => [c.id, c]));
  if (!byId["wda-lib"]?.ok) return { color: "default", text: "未安装(可选)" };
  if (byId["wda-server"]?.status === "fail") return { color: "red", text: "WDA 服务未起" };
  if (!byId["simulators"]?.ok) return { color: "orange", text: "模拟器未启动" };
  return { color: "green", text: "就绪" };
}

function DoctorCard({ title, loading, checks, resp, conclusion, onRerun, busy, onGoSettings, onManage }: {
  title: string; loading: boolean; checks: DoctorCheck[]; resp: DoctorResp | null;
  conclusion: { color: string; text: string }; onRerun: () => void; busy: boolean;
  onGoSettings?: () => void; onManage?: () => void }) {
  const age = resp ? Math.max(0, Math.round((Date.now() - Date.parse(resp.ran_at)) / 1000)) : 0;
  return (
    <Card size="small" loading={loading}
      title={<span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
        {/* 标题可省略、结论 Tag 不可被挤掉（发起人 2026-10-12：窄列下 Android 卡的结论图标容易缺省） */}
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{title}</span>
        <Tag style={{ flex: "none", marginInlineEnd: 0 }} color={conclusion.color}>{conclusion.text}</Tag>
      </span>}
      extra={<Button size="small" onClick={onRerun} loading={busy}>重新检测</Button>}>
      {resp && <div style={{ color: "#999", fontSize: 12, marginBottom: 4 }}>
        {age}s 前跑过 · {(resp.duration_ms / 1000).toFixed(1)}s
      </div>}
      {checks.map((c) => <CheckRow key={c.id} c={c} onRerun={onRerun}
        onGoSettings={onGoSettings} onManage={onManage} />)}
    </Card>
  );
}

export default function EnvDoctor({ state, onGoSettings }: { state: StateResp;
  onGoSettings?: () => void }) {
  const [android, setAndroid] = useState<DoctorResp | null>(null);
  const [ios, setIos] = useState<DoctorResp | null>(null);
  // 单卡独立重跑（发起人 2026-10-12：点一个「重新检测」另外两个也响应）——
  // busyKey 记录正在检测的平台，按钮 loading 只落在对应卡上
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [manageOpen, setManageOpen] = useState(false);

  const runOne = useCallback((key: "android" | "ios") => {
    setBusyKey(key); setErr(null);
    getDoctor(key).then((r) => { (key === "android" ? setAndroid : setIos)(r); })
      .catch((e) => setErr(String(e)))
      .finally(() => setBusyKey(null));
  }, []);
  useEffect(() => { runOne("android"); runOne("ios"); }, [runOne]);

  const androidChecks = android?.checks ?? [];
  const envChecks = androidChecks.filter((c) => !isAi(c));
  const aiChecks = androidChecks.filter(isAi);
  const iosChecks = ios?.checks ?? [];
  const genOk = aiChecks.some((c) => c.id === "gen" && c.ok);

  return (
    <Row gutter={16} style={{ marginTop: 16 }}>
      <Col span={8}>
        <DoctorCard title="Android 环境" loading={!android && !err} checks={envChecks} resp={android}
          conclusion={androidConclusion(envChecks)} onRerun={() => runOne("android")}
          busy={busyKey === "android"} />
      </Col>
      <Col span={8}>
        <DoctorCard title="iOS 环境" loading={!ios && !err} checks={iosChecks} resp={ios}
          conclusion={iosConclusion(iosChecks)} onRerun={() => runOne("ios")}
          busy={busyKey === "ios"} />
      </Col>
      <Col span={8}>
        <DoctorCard title="AI 生成" loading={!android && !err} checks={aiChecks} resp={android}
          conclusion={genOk
            ? { color: "green", text: `已配置 · ${state.prompt_mode === "server" ? "server模式" : "自部署"}` }
            : { color: "orange", text: "未配置" }}
          onRerun={() => runOne("android")} busy={busyKey === "android"}
          onGoSettings={onGoSettings}
          onManage={() => setManageOpen(true)} />
      </Col>
      {err && <Col span={24} style={{ marginTop: 8 }}>
        <Card size="small">环境检测失败:{err}</Card>
      </Col>}
      {/* Prompt 文件管理（gen/convert 槽位选择生效文件）；变更后重跑 AI 检测刷新行标签 */}
      <PromptManager open={manageOpen} onClose={() => setManageOpen(false)}
        onChanged={() => runOne("android")} />
    </Row>
  );
}
