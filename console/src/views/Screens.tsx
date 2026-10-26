import { Alert, Button, Card, Empty, Input, Modal, Select, Space, Spin, Table, Tag, Tooltip, message } from "antd";
import { CameraOutlined, CloudUploadOutlined, DeleteOutlined, HomeOutlined } from "@ant-design/icons";
import { useCallback, useEffect, useRef, useState } from "react";
import { bootIosSim, captureScreenshot, deviceKey, getJobs, IosSim, JobInfo, listIosSims, StateResp, uninstallApp } from "../api";
import DeviceScreen from "../components/DeviceScreen";
import InstallModal from "../components/InstallModal";
import LogPanel from "../components/LogPanel";
import PageHeader from "../components/PageHeader";

// 设备投屏(发起人定稿布局):从左到右=设备列表 | 投屏区域 | 日志区域。
// **只有被选中的设备进行投屏**(未选中设备零轮询);投屏区固定屏宽按手机比例;
// 日志区只显示所选设备的任务(任务记录带 serial/platform)。
// 2026-10-11:各入口下发任务后自动跳转本页,深链 #/screens/<jobId> ——按任务选设备、
// 执行日志定位到该任务(执行用例/套件/探索统一走 App.onLaunched)。
type Sel = { serial: string | null; platform: "android" | "ios"; label: string;
  sub?: string; udid?: string; booted?: boolean };
// 候选身份键：安卓=serial、iOS 模拟器=udid、退化 WDA 入口=wda（同为 ios/serial=null
// 的多条模拟器候选必须能区分选中态）
const keyOf = (c: Sel): string => `${c.platform}:${c.serial ?? c.udid ?? "wda"}`;

export const KIND_LABEL: Record<string, string> = {
  run: "执行", gen: "生成", import: "导入", explore: "探索",
  index: "建索引", describe: "补标题", diagnose: "归因", export: "导出",
};

// 近期任务日志的状态色（antd Tag 语义色）
const STATUS_TAG: Record<string, string> = {
  running: "processing", done: "success", error: "error",
  queued: "default", cancelled: "warning",
};

function deviceJobs(jobs: JobInfo[], sel: Sel): JobInfo[] {
  return jobs.filter((j) => {
    if ((j.platform || "android") !== sel.platform) return false;
    // iOS（2026-10-19）：真机按 udid 对齐；模拟器（serial=null）只看没有 serial 的
    // 任务——否则真机跑的任务会混进模拟器日志，反之亦然
    if (sel.platform === "ios") return sel.serial ? j.serial === sel.serial : !j.serial;
    if (sel.serial && j.serial && j.serial !== sel.serial) return false;
    return true;
  });
}

export default function Screens({ state, focusJob }: { state: StateResp; focusJob?: string }) {
  const [jobs, setJobs] = useState<JobInfo[]>([]);
  const [taskRunning, setTaskRunning] = useState(false);
  useEffect(() => {
    const check = () => getJobs().then((d) => {
      setJobs(d.jobs);
      setTaskRunning(d.jobs.some((j) => j.status === "running"));
    }).catch(() => {});
    check();
    const t = setInterval(check, 3000);
    return () => clearInterval(t);
  }, []);

  // 真机 = Android(adb) + iOS USB 真机；iOS 模拟器来自 simctl 清单，不在 state.devices 里
  // iOS 仅 macOS 可用（Xcode 工具链）：Windows/Linux 不拉模拟器清单、不进入设备候选
  const iosSupported = state.ios_supported !== false;
  const androidOnline = state.devices.filter(
    (d) => d.state === "device" && (d.platform || "android") === "android");
  const iosOnline = iosSupported ? state.devices.filter(
    (d) => d.state === "device" && d.platform === "ios") : [];
  const notReady = state.devices.filter(
    (d) => d.state !== "device" && (d.platform || "android") === "android");

  // iOS 模拟器清单（发起人 2026-10-12：支持切换模拟器）——15s 轮询保持启动状态新鲜；
  // xcrun 不可用（未装 Xcode）返回空清单，退化为单条 WDA 入口（行为与旧版一致）
  const [sims, setSims] = useState<IosSim[] | null>(null);
  // 正在启动的模拟器（udid + 名称）——设备列表按钮 loading 与投屏区「启动中」占位共用
  const [booting, setBooting] = useState<{ udid: string; label: string } | null>(null);
  // 启动已等待秒数——boot 一条龙是同步长请求（首次构建分钟级），没有计时看起来像卡死
  const [bootSecs, setBootSecs] = useState(0);
  useEffect(() => {
    if (!booting) return;
    setBootSecs(0);
    const t = setInterval(() => setBootSecs((s) => s + 1), 1000);
    return () => clearInterval(t);
  }, [booting]);
  const loadSims = useCallback(() => {
    listIosSims().then((d) => setSims(d.simulators)).catch(() => setSims([]));
  }, []);
  useEffect(() => {
    if (!iosSupported) { setSims([]); return; }
    loadSims();
    const t = setInterval(loadSims, 15000);
    return () => clearInterval(t);
  }, [loadSims, iosSupported]);

  // 候选设备:在线 Android(带系统版本) + iOS USB 真机(serial=udid，直接透传驱动层)
  // + iOS 模拟器(booted 直接投屏,未启动点击即启动)
  const candidates: Sel[] = [
    ...androidOnline.map((d) => ({ serial: d.serial, platform: "android" as const,
      label: d.model || d.serial,
      sub: d.os ? `Android ${d.os} · ${d.serial}` : d.serial })),
    ...iosOnline.map((d) => ({ serial: d.serial, platform: "ios" as const,
      label: d.model || d.serial,
      sub: `iOS ${d.os || "?"} · USB · ${d.serial}` })),
    ...(iosSupported ? (sims === null || sims.length === 0
      ? [{ serial: null, platform: "ios" as const, label: "iOS 模拟器 (WDA)" }]
      : sims.map((s) => ({ serial: null, platform: "ios" as const, label: s.name,
          sub: `${s.os} · ${s.state === "booted" ? "已启动" : "未启动"}`,
          udid: s.udid, booted: s.state === "booted" }))) : []),
  ];
  const [sel, setSel] = useState<Sel | null>(null);
  const selected = sel ?? candidates[0] ?? null;

  // 点选设备：iOS 未启动的模拟器先弹确认再启动（发起人 2026-10-12）——后端
  // /api/ios/sim/boot 一条龙（boot + 拉起 WDA），启动期间投屏区域显示「启动中」
  // 占位，完成后选中该模拟器自动恢复投屏。其余设备直接选中。
  const bootFlow = (c: Sel) => {
    setBooting({ udid: c.udid!, label: c.label });
    bootIosSim({ udid: c.udid! })
      .then((r) => {
        loadSims();
        setSel({ ...c, booted: true, sub: `${(c.sub || "").split(" · ")[0]} · 已启动` });
        if (r.wda?.started) message.success(`已启动「${c.label}」——WDA 就绪，开始投屏`);
        else message.warning(`模拟器已启动，但 WDA 未就绪：${r.wda?.detail || "可到概览页 iOS 环境卡点「启动」"}`);
      })
      .catch((e) => message.error(String(e)))
      .finally(() => setBooting(null));
  };
  const pick = (c: Sel) => {
    if (c.platform === "ios" && c.udid && !c.booted) {
      Modal.confirm({
        title: `启动模拟器「${c.label}」？`,
        content: "启动后自动拉起 WDA 并开始投屏；其他已启动的模拟器会先关闭（单活切换）。装过 WDA 约 20~40 秒；全新模拟器首次需构建安装 WDA，可能需数分钟，请耐心等待。",
        okText: "启动", cancelText: "取消",
        onOk: () => bootFlow(c),
      });
      return;
    }
    setSel(c);
  };

  // 投屏卡头四操作（发起人 2026-10-12：icon+tooltip，顺序 回到桌面-截屏-安装包-卸载包）。
  // 均不抢设备锁——人的干预是救场通道，后果由任务执行中的警示条讲清。
  const [capturing, setCapturing] = useState(false);
  const capture = async () => {
    if (!selected) return;
    setCapturing(true);
    try {
      const r = await captureScreenshot({ serial: selected.serial, platform: selected.platform });
      message.success(`已截屏 → ${r.file}`);
    } catch (e) { message.error(String(e)); } finally { setCapturing(false); }
  };
  const goHome = async () => {
    if (!selected) return;
    try { await deviceKey({ action: "home", serial: selected.serial, platform: selected.platform }); }
    catch (e) { message.error(String(e)); }
  };

  const [installOpen, setInstallOpen] = useState(false);
  const [uninstallOpen, setUninstallOpen] = useState(false);
  const [uninstallPkg, setUninstallPkg] = useState("");
  const [uninstalling, setUninstalling] = useState(false);
  const doUninstall = async () => {
    if (!selected) return;
    const pkg = uninstallPkg.trim();
    if (!pkg) { message.error("输入要卸载的包名/bundleId"); return; }
    setUninstalling(true);
    try {
      await uninstallApp({ serial: selected.serial, platform: selected.platform, package: pkg });
      message.success(`已卸载 ${pkg}（数据一并清除）`);
      setUninstallOpen(false);
      setUninstallPkg("");
    } catch (e) { message.error(String(e)); } finally { setUninstalling(false); }
  };

  const [logJob, setLogJob] = useState<string | undefined>(undefined);
  const devJobs = selected ? deviceJobs(jobs, selected) : [];
  const recentJobs = jobs.slice(0, 50);

  // 深链定位(focusJob):任务列表轮询到该任务后,按任务的设备/平台选中设备。
  // 每个 focusJob 只处理一次(handledFocus),不与用户手动切换设备打架。
  const handledFocus = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!focusJob || handledFocus.current === focusJob) return;
    const j = jobs.find((x) => x.id === focusJob);
    if (!j) return;  // 任务列表未就绪,等下一轮轮询
    if ((j.platform || "android") === "ios") {
      // iOS 模拟器清单未就绪时**不消费深链**——此前直接落入 "ios:wda" 兜底选中,
      // 清单加载后 keyOf 与真实模拟器失配,设备列表永远没有选中高亮(发起人 2026-10-13)
      if (sims === null) return;
      // 真机任务（serial=udid）优先定位到该真机；其余（serial=null）落到已启动模拟器
      const dev = j.serial
        ? state.devices.find((d) => d.serial === j.serial && d.platform === "ios") : undefined;
      if (dev) {
        setSel({ serial: dev.serial, platform: "ios", label: dev.model || dev.serial,
          sub: `iOS ${dev.os || "?"} · USB · ${dev.serial}` });
      } else {
        const b = sims.find((s) => s.state === "booted");
        setSel(b ? { serial: null, platform: "ios", label: b.name,
          sub: `${b.os} · 已启动`, udid: b.udid, booted: true }
          : { serial: null, platform: "ios", label: "iOS 模拟器 (WDA)" });
      }
    } else if (j.serial) {
      const d = state.devices.find((x) => x.serial === j.serial);
      setSel({ serial: j.serial, platform: "android", label: `${d?.model || j.serial} · ${j.serial}` });
    }
    // 直接定位日志:设备若本来就是选中态,下面「设备切换」effect 不会重跑
    setLogJob(focusJob);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusJob, jobs, sims]);

  // 近期任务日志行点击 → 设备列表联动选中该任务的设备并发定位日志
  // (发起人 2026-10-13:点下方任务要能"切"到它的设备,展开日志仍走行首 +)
  const pickDeviceForJob = (j: JobInfo) => {
    if ((j.platform || "android") === "ios") {
      const dev = j.serial
        ? state.devices.find((d) => d.serial === j.serial && d.platform === "ios") : undefined;
      if (dev) {
        setSel({ serial: dev.serial, platform: "ios", label: dev.model || dev.serial,
          sub: `iOS ${dev.os || "?"} · USB · ${dev.serial}` });
      } else {
        const b = sims?.find((s) => s.state === "booted");
        if (b) setSel({ serial: null, platform: "ios", label: b.name,
          sub: `${b.os} · 已启动`, udid: b.udid, booted: true });
      }
    } else if (j.serial) {
      const d = state.devices.find((x) => x.serial === j.serial);
      setSel({ serial: j.serial, platform: "android", label: `${d?.model || j.serial} · ${j.serial}` });
    }
    setLogJob(j.id);
  };

  useEffect(() => {  // 设备切换/深链 ⇒ 日志优先定位深链任务,否则选最近的运行中任务
    setLogJob(devJobs.find((j) => j.id === focusJob)?.id
      || devJobs.find((j) => j.status === "running")?.id || devJobs[0]?.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.serial, selected?.platform, focusJob]);

  return (
    <>
      <PageHeader title="设备/执行日志" />
      {taskRunning && (
        <Alert style={{ marginBottom: 12 }} type="warning" showIcon
          message="任务执行中，手动操作可能干扰用例（操控不抢设备锁，救场通道保持畅通）" />
      )}
      <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
        {/* ① 设备列表（发起人 2026-10-12：真机/模拟器分组展示） */}
        <Card size="small" title="设备列表" style={{ width: 210, flex: "none" }}>
          {candidates.length === 0 && <span style={{ color: "#999" }}>无在线设备——手机开启 USB 调试并插上</span>}
          {([["真机", candidates.filter((c) => c.platform === "android" || c.serial != null)],
             ["模拟器", candidates.filter((c) => c.platform === "ios" && c.serial == null)]] as const)
            .filter(([, list]) => list.length > 0)
            .map(([title, list]) => (
              <div key={title} style={{ marginBottom: 8 }}>
                <div style={{ fontSize: 11, color: "#8a919f", margin: "0 0 4px 2px" }}>{title}</div>
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  {list.map((c) => {
                    const isSel = selected != null && keyOf(selected) === keyOf(c);
                    const isNotReady = c.platform === "android"
                      && state.devices.find((d) => d.serial === c.serial)?.state !== "device";
                    return (
                      <Button key={keyOf(c)} type={isSel ? "primary" : "default"}
                        ghost={isSel} block
                        style={{ textAlign: "left", justifyContent: "flex-start",
                                 whiteSpace: "normal", height: "auto", minHeight: 32, padding: "4px 10px" }}
                        disabled={isNotReady} loading={booting != null && booting.udid === c.udid}
                        onClick={() => pick(c)}>
                        <span style={{ display: "block", minWidth: 0 }}>
                          {c.platform === "ios" ? "📱 " : "🤖 "}{c.label}
                          {c.sub && <span style={{ display: "block", fontSize: 11,
                            /* ghost 选中态是白底蓝字，白色副文本会隐形（发起人 2026-10-12） */
                            color: isSel ? "rgba(79,91,255,0.75)" : "#8a919f" }}>{c.sub}</span>}
                        </span>
                        {isNotReady && <Tag style={{ marginLeft: 6 }}>未就绪</Tag>}
                      </Button>
                    );
                  })}
                  {title === "真机" && notReady.map((d) => (
                    <Tag key={d.serial} style={{ width: "100%" }}>{d.serial} · {d.state}</Tag>
                  ))}
                </Space>
              </div>
            ))}
        </Card>
        {/* ② 投屏区域（固定屏宽，按比例；只有选中设备轮询） */}
        <div style={{ width: 296, flex: "none" }}>
          {booting ? (
            <Card size="small" title="投屏">
              <div style={{ width: 227, height: 400, background: "#fafafa", borderRadius: 8,
                border: "1px solid #f0f0f0", display: "flex", flexDirection: "column",
                alignItems: "center", justifyContent: "center", gap: 8, color: "#595959" }}>
                <Spin />
                <div>「{booting.label}」启动中… 已等 {bootSecs} 秒</div>
                <div style={{ fontSize: 12, color: "#8a919f" }}>
                  模拟器先开（十几秒），随后自动部署 WDA：装过约 20~40 秒；
                  全新模拟器首次构建可能数分钟——完成后自动开始投屏，无需刷新
                </div>
              </div>
            </Card>
          ) : selected ? <DeviceScreen serial={selected.serial} platform={selected.platform}
              extra={<Space size={4}>
                <Tooltip title="回到桌面">
                  <Button size="small" icon={<HomeOutlined />} onClick={goHome} />
                </Tooltip>
                <Tooltip title="截屏（保存到 screenshots/）">
                  <Button size="small" icon={<CameraOutlined />} loading={capturing} onClick={capture} />
                </Tooltip>
                <Tooltip title="安装测试包">
                  <Button size="small" icon={<CloudUploadOutlined />} onClick={() => setInstallOpen(true)} />
                </Tooltip>
                <Tooltip title="卸载包（清除应用数据）">
                  <Button size="small" danger icon={<DeleteOutlined />}
                    onClick={() => setUninstallOpen(true)} />
                </Tooltip>
              </Space>} />
            : <Card size="small"><Empty description="无可选设备" /></Card>}
        </div>
        {/* ③ 日志区域（发起人 2026-10-12：第三列纵向堆叠两张卡——所选设备执行日志在上，
            近期任务日志在下，不再单独占整行） */}
        <div style={{ flex: 1, minWidth: 380, display: "flex", flexDirection: "column", gap: 16 }}>
          <Card size="small" title="所选设备执行日志">
            {devJobs.length === 0 ? (
              <Empty description="该设备暂无任务——从「用例管理/执行」或「遍历探索」启动" />
            ) : (
              <Space direction="vertical" style={{ width: "100%" }} size={10}>
                <Select style={{ minWidth: 260 }} value={logJob} onChange={setLogJob}
                  placeholder="选择任务"
                  options={devJobs.slice(0, 20).map((j) => ({
                    value: j.id,
                    label: `${KIND_LABEL[j.kind] || j.kind} · ${j.id.slice(0, 6)} · ${j.status}`,
                  }))} />
                {logJob && <LogPanel jobId={logJob} />}
              </Space>
            )}
          </Card>
          {/* 近期任务日志：跨设备全量任务列表（最近 50 条），点击行首展开事件日志——
              「所选设备执行日志」只看得到当前设备的任务，历史任务（含已换设备的）统一来这里翻。
              任务记录存内存，服务重启即清空——刚重启后为空属正常，非故障。 */}
          <Card size="small" title="近期任务日志"
            extra={<span style={{ color: "#8a919f", fontSize: 12 }}>
              最近 {recentJobs.length} 条 · 点行选中该任务设备 · 点行首 + 展开事件日志</span>}>
            {recentJobs.length === 0 ? (
              <Empty description="暂无任务记录——任务记录存内存，服务重启后清空（刚重启为空属正常）；跑一次用例/生成/探索后会出现在这里。" />
            ) : (
              <div style={{ maxHeight: 380, overflowY: "auto" }}>
                <Table size="small" rowKey="id" dataSource={recentJobs} pagination={false}
                  onRow={(j: JobInfo) => ({ onClick: () => pickDeviceForJob(j), style: { cursor: "pointer" } })}
                  expandable={{
                    expandedRowRender: (j: JobInfo) => <LogPanel jobId={j.id} />,
                    rowExpandable: () => true,
                  }}
                  columns={[
                    { title: "时间", dataIndex: "started_at", width: 96,
                      render: (t: string) => (t || "").slice(11, 19) || "—" },
                    { title: "类型", dataIndex: "kind", width: 84,
                      render: (k: string) => KIND_LABEL[k] || k },
                    { title: "摘要", dataIndex: "summary", ellipsis: true,
                      render: (s: string | null, j: JobInfo) => s
                        || <span style={{ color: "#b6bac3" }}>{j.id.slice(0, 8)}</span> },
                    { title: "状态", dataIndex: "status", width: 92,
                      render: (s: string) =>
                        <Tag color={STATUS_TAG[s] || "default"} bordered={false}>{s}</Tag> },
                    { title: "设备", dataIndex: "serial", width: 160, ellipsis: true,
                      render: (s: string | null, j: JobInfo) =>
                        s || (j.platform === "ios" ? "iOS 模拟器" : "默认设备") },
                    { title: "执行人", dataIndex: "operator", width: 100, ellipsis: true,
                      render: (o: string) => o || "—" },
                    { title: "操作", width: 78,
                      render: (_: unknown, j: JobInfo) => j.link
                        ? <a href={j.link} target="_blank" rel="noreferrer">报告 ↗</a> : "—" },
                  ]} />
              </div>
            )}
          </Card>
        </div>
      </div>
      <InstallModal open={installOpen} onClose={() => setInstallOpen(false)}
        serial={selected?.serial ?? null} platform={selected?.platform ?? "android"} />
      {/* 卸载包（发起人 2026-10-12）：独立入口，双平台（Android=adb uninstall /
          iOS 模拟器=simctl uninstall booted），清数据语义——重装即全新状态 */}
      <Modal title={`卸载应用（${selected?.platform === "ios"
        ? "iOS 模拟器 · simctl uninstall" : "Android · adb uninstall"}）`}
        open={uninstallOpen} onCancel={() => setUninstallOpen(false)} width={460}
        footer={<Space>
          <Button onClick={() => setUninstallOpen(false)}>取消</Button>
          <Button danger type="primary" loading={uninstalling}
            disabled={!uninstallPkg.trim()} onClick={doUninstall}>卸载</Button>
        </Space>}>
        <Input value={uninstallPkg} onChange={(e) => setUninstallPkg(e.target.value)}
          onPressEnter={doUninstall}
          placeholder={selected?.platform === "ios"
            ? "bundleId，如 com.example.app" : "包名，如 com.example.app"} />
        <div style={{ marginTop: 8, color: "#8a919f", fontSize: 12 }}>
          卸载会清除该应用的全部数据，不可恢复；iOS 真机卸载请用 mat run --fresh-install（配置台暂不支持）。
        </div>
      </Modal>
    </>
  );
}
