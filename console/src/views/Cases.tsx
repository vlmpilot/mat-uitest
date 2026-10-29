import {
  Alert, Button, Card, Checkbox, Drawer, Dropdown, Empty, Input, Modal, Popconfirm, Select, Space, Table, Tabs, Tag, Tooltip, Typography, message,
} from "antd";
import {
  DeleteOutlined, DownOutlined, PlusOutlined, ThunderboltOutlined, UpOutlined,
} from "@ant-design/icons";
import { useEffect, useMemo, useState } from "react";
import {
  deleteCase, deleteSuite, getAppLabels, getCase, getSuite, getSuites, renameCase, runCase, saveSuite,
  StateResp, SuiteInfo,
} from "../api";
import { Nav } from "../components/CommandPalette";
import PageHeader from "../components/PageHeader";
import CaseEditor, { Editing } from "./CaseEditor";

// 用例管理(+):用例/套件 双 Tab。
// 用例:按被测应用分组(树形表格) + 源码编辑 + 标题编辑(JSON name 同步) + 确认运行。
// 套件:*.suite.json 的管理——多选建套件/排序/勾选运行(运行链已就绪,聚合报告进报告页)。
// 命名(发起人 2026-10-11 定稿):资产名=「套件」。
// 交互(发起人 2026-10-12 定稿):筛选行(应用+名称)与运行行(平台/设备/录屏/冷启动+
// 运行+批量操作)分离;用例与套件均靠列表勾选驱动运行/删除,行内不再放运行/删除按钮。

const TEMPLATE = JSON.stringify({
  caseId: "case_new",
  name: "新建用例",
  target: { package: "com.example.app" },
  steps: [
    { id: "s1", intent: "启动应用", action: "launch", params: { package: "com.example.app" } },
    { id: "s2", intent: "示例:点击某个控件", action: "click", params: { rid: "com.example.app:id/submit" } },
  ],
}, null, 2);

// ---------- 共享：运行参数条（平台/设备/录屏） ----------

function RunParams({ state, runPlatform, setRunPlatform, runSerial, setRunSerial,
                     record, setRecord }: {
  state: StateResp; runPlatform: "android" | "ios" | "harmony"; setRunPlatform: (v: "android" | "ios" | "harmony") => void;
  runSerial: string | null; setRunSerial: (v: string | null) => void;
  record: boolean; setRecord: (v: boolean) => void }) {
  // 显式 flex（替代 antd Space）：筛选栏各控件高度不一（Select 32 / Checkbox 24），
  // Space 的对齐在 wrap 场景下表现不稳定（发起人 2026-10-12 截图再反馈），flex + alignItems
  // 行为完全确定——所有元素垂直中心线一致。
  // ⚠️ 不能带 marginBottom：作为外层筛选栏 flex 的子项，margin 会参与居中计算，
  // 整条运行参数比同行其他元素中心偏移 6px（实测 top 243 vs 249，2026-10-12）
  // iOS 仅 macOS 可用（Xcode 工具链）：Windows/Linux 不提供 iOS 运行平台选项
  const iosSupported = state.ios_supported !== false;
  return (
    <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
      <Tooltip title={iosSupported
        ? "iOS：真机需在右侧选设备（USB 连接，serial=udid）；留空走模拟器（WDA 常驻 localhost:8100）"
        : "运行平台：仅 Android（iOS 需要 macOS + Xcode）"}>
        <Select style={{ width: 120 }} value={runPlatform}
          onChange={(v) => { setRunPlatform(v); setRunSerial(null); }}
          options={[{ value: "android", label: "Android" },
                    ...(iosSupported ? [{ value: "ios", label: "iOS" }] : []),
                    { value: "harmony", label: "鸿蒙" }]} />
      </Tooltip>
      <Tooltip title={runPlatform === "ios"
        ? "选 USB 真机；留空 = 模拟器（WDA 常驻 localhost:8100）"
        : "留空 = 自动使用首台在线设备"}>
        <Select placeholder={runPlatform === "ios" ? "iOS 模拟器（留空）" : "运行设备(自动)"}
          style={{ width: 240 }} allowClear
          value={runSerial ?? undefined}
          onChange={setRunSerial}
          options={state.devices
            .filter((d) => d.state === "device" && (runPlatform === "ios"
              ? d.platform === "ios" : (d.platform || "android") === "android"))
            .map((d) => ({ value: d.serial, label: `${d.model || d.serial} · ${d.serial}` }))} />
      </Tooltip>
      <Tooltip title="全程录屏；通过的自动删除、失败的保留进 artifacts/video/ 并内嵌报告（iOS 真机暂不支持）">
        <Checkbox checked={record} onChange={(e) => setRecord(e.target.checked)}>录屏</Checkbox>
      </Tooltip>
    </div>
  );
}

function useRunTarget(state: StateResp, runPlatform: "android" | "ios" | "harmony", runSerial: string | null) {
  const online = state.devices.filter((d) => d.state === "device");
  const androidOnline = online.filter((d) => (d.platform || "android") === "android");
  const runDisabled = runPlatform === "android" && androidOnline.length === 0;
  const runTarget = runSerial
    ? (online.find((d) => d.serial === runSerial)?.model || runSerial)
    : runPlatform === "ios" ? "iOS 模拟器"
    : (androidOnline.length ? androidOnline[0].serial : "默认设备");
  return { runDisabled, runTarget };
}

// ---------- 用例 Tab ----------

function CasesTab({ state, onLaunched, openCaseName, onOpenCase, onNav, onCasesChanged,
                    runPlatform, setRunPlatform, runSerial, setRunSerial,
                    record, setRecord, reset, setReset, focusStep, appLabels }: {
  state: StateResp; onLaunched: (id: string) => void; openCaseName?: string;
  onOpenCase?: (name: string) => void; onNav?: (nav: Nav) => void; onCasesChanged: () => void;
  runPlatform: "android" | "ios" | "harmony"; setRunPlatform: (v: "android" | "ios" | "harmony") => void;
  runSerial: string | null; setRunSerial: (v: string | null) => void;
  record: boolean; setRecord: (v: boolean) => void;
  reset: "off" | "cold"; setReset: (v: "off" | "cold") => void;
  focusStep?: string | null; appLabels: Record<string, string> }) {
  const [filter, setFilter] = useState<string | null>(null);
  const [nameQ, setNameQ] = useState("");                 // 名称筛选（标题/文件名）
  const [selKeys, setSelKeys] = useState<string[]>([]);   // 勾选用例 → 运行/批量操作
  const [editing, setEditing] = useState<Editing | null>(null);
  const [focusStepId, setFocusStepId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [rename, setRename] = useState<{ from: string; to: string } | null>(null);

  const apps = Array.from(new Set(Object.values(state.caseMeta).map((m) => m.app).filter(Boolean))) as string[];
  const rows = state.cases.map((name) => ({
    name, app: state.caseMeta[name]?.app || "",
    title: state.caseMeta[name]?.title || "",
    cid: state.caseMeta[name]?.caseId || "",
    no: state.caseMeta[name]?.no, key: name,
  }));
  // 双维筛选：被测应用 + 名称（标题/文件名，大小写不敏感；发起人 2026-10-12 交互定稿）
  const needle = nameQ.trim().toLowerCase();
  const filtered = rows.filter((r) =>
    (!filter || r.app === filter)
    && (!needle || (r.title || "").toLowerCase().includes(needle)
        || r.name.toLowerCase().includes(needle)));
  // 勾选 → 用例名：只认当前筛选可见的行（隐藏行不参与运行/删除，避免"看不见却被执行"）
  const selNames = filtered
    .filter((r) => selKeys.includes(r.key) && !r.key.startsWith("group:"))
    .map((r) => r.name);
  // 按被测应用分组（树形表格，默认展开）——平铺文件不便管理，App 是天然的组织维度。
  // 分组行只是组织维度不可勾选（发起人 2026-10-12），包名只出现一次 + displayName + 条数
  const grouped = Object.entries(
    filtered.reduce<Record<string, typeof filtered>>((acc, r) => {
      (acc[r.app || "未指定"] = acc[r.app || "未指定"] || []).push(r);
      return acc;
    }, {})
  ).map(([app, children]) => ({
    key: `group:${app}`, app, isGroup: true,
    count: children.length, children,
  }));

  // 深链文件名解析：报告「编辑此步」对套件条目产物带 NN_ 序号前缀（如 01_xxx.json），
  // 工作区里没有该文件 → 剥掉前缀再试（编辑器深链发起人 2026-10-12 P1）
  const resolveCaseName = (raw: string): string | null => {
    if (state.cases.includes(raw)) return raw;
    const stripped = raw.replace(/^\d{2}_/, "");
    return state.cases.includes(stripped) ? stripped : null;
  };

  useEffect(() => {  // ⌘K / 深链 #/cases/<name>?step=<id> 自动打开编辑器
    if (!openCaseName) return;
    const name = resolveCaseName(openCaseName);
    if (name) openEditor(name);
    else message.warning(`工作区没有用例 ${openCaseName}（可能已被重命名或删除）`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openCaseName]);

  const openEditor = async (name: string, step?: string | null) => {
    onOpenCase?.(name);
    try {
      const content = await getCase(name);
      setFocusStepId(step || null);
      setEditing({ name, content: JSON.stringify(content, null, 2), isNew: false });
    } catch (e) { message.error(String(e)); }
  };

  // 克隆用例（发起人 2026-10-12 P1）：从相似用例复制后改——caseId/标题加副本标记，文件名 <stem>-copy.json
  const cloneCase = async (name: string) => {
    try {
      const content = await getCase(name);
      const obj = { ...(content as Record<string, unknown>) };
      obj.caseId = `${String(obj.caseId || "case")}_copy`;
      obj.name = `${String(obj.name || "")}（副本）`;
      setEditing({ name: name.replace(/\.json$/, "") + "-copy.json",
        content: JSON.stringify(obj, null, 2), isNew: true });
    } catch (e) { message.error(String(e)); }
  };

  const newCase = () => setEditing({ name: "new-case.json", content: TEMPLATE, isNew: true });

  // 长文案缺省显示（发起人 2026-10-12 反馈「用例较长时，列表会空白显示，可缺省末尾文案」）：
  // 渲染层直接截短，内容恒非空——不依赖 CSS ellipsis 的布局收缩（极端列宽下 flex 子项
  // 会被压缩到 0 宽 → 整行空白，宽窄窗口表现不同，难以根除），JS 截断与布局完全解耦。
  const shortText = (t: string, head: number, tail: number) =>
    t.length > head + tail + 1 ? `${t.slice(0, head)}…${t.slice(-tail)}` : t;

  const { runDisabled, runTarget } = useRunTarget(state, runPlatform, runSerial);

  // 运行选中用例（发起人 2026-10-12：列表多选替代单行运行按钮）——后端把 cases 列表
  // 组成内存临时套件串行执行（与套件同链路，聚合报告按批次归组）
  const runSelected = async () => {
    if (!selNames.length) return;
    // iOS：选了 USB 真机就传 udid（WDA 走 usbmux）；留空 = 模拟器（localhost:8100）
    const serial = runSerial || (runPlatform === "ios" ? null
      : state.devices.find((d) => d.state === "device"
          && (d.platform || "android") === "android")?.serial || null);
    setRunning(true);
    try {
      const { job_id } = await runCase({ cases: selNames, serial, platform: runPlatform, record, reset });
      message.success(`已启动 ${selNames.length} 条用例,已跳转「设备/执行日志」页跟踪`);
      onLaunched(job_id);
    } catch (e) { message.error(String(e)); } finally { setRunning(false); }
  };

  // 批量删除：被套件引用的用例后端会拒绝，逐条收集结果诚实汇报（不因单条失败中断整批）
  const delSelected = async () => {
    let ok = 0; const errs: string[] = [];
    for (const n of selNames) {
      try { await deleteCase({ name: n }); ok++; }
      catch (e) { errs.push(`${n}：${String(e).replace(/^Error:\s*/, "")}`); }
    }
    if (ok) { message.success(`已删除 ${ok} 条用例`); setSelKeys([]); onCasesChanged(); }
    if (errs.length) {
      message.warning(`${errs.length} 条删除失败（被套件引用的用例无法删除）：`
        + `${errs[0]}${errs.length > 1 ? " 等" : ""}`);
    }
  };
  const confirmDelSelected = () => Modal.confirm({
    title: `删除选中的 ${selNames.length} 条用例？`,
    content: "被套件引用的用例无法删除；运行产物与历史报告不受影响。",
    okText: "删除", okButtonProps: { danger: true }, cancelText: "取消",
    onOk: delSelected,
  });
  const batchItems = [{
    key: "delete", icon: <DeleteOutlined />, danger: true,
    label: `删除选中（${selNames.length}）`,
  }];

  return (
    <>
      {/* 两行布局（发起人 2026-10-12：筛选与运行参数关注点分离）——
          第一行筛选：应用 + 名称；第二行运行：平台/设备/录屏 + 冷启动 + 运行/批量操作 */}
      <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8, marginBottom: 8 }}>
        <Select allowClear placeholder="按被测应用过滤" style={{ width: 240 }}
          value={filter} onChange={setFilter}
          options={apps.map((a) => ({ value: a,
            label: appLabels[a] ? `${appLabels[a]}（${a}）` : a }))} />
        <Input allowClear placeholder="按名称筛选（标题 / 文件名）" style={{ width: 240 }}
          value={nameQ} onChange={(e) => setNameQ(e.target.value)} />
        <span style={{ color: "#8a919f", fontSize: 12 }}>{filtered.length} 个用例</span>
      </div>
      <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
        <RunParams state={state} runPlatform={runPlatform} setRunPlatform={setRunPlatform}
          runSerial={runSerial} setRunSerial={setRunSerial} record={record} setRecord={setRecord} />
        <Select value={reset} onChange={setReset} style={{ width: 132 }}
          options={[{ value: "cold", label: "用例间冷启动" }, { value: "off", label: "不重置" }]} />
        <Popconfirm title={`运行选中的 ${selNames.length} 条用例？`}
          description={`目标：${runTarget}（串行）。${reset === "cold" ? "每条开始前 force-stop 目标应用，冷启动由用例前置步骤完成，隔离跨用例状态污染。" : "沿用上一条用例的遗留页面状态。"}`}
          okText="运行" cancelText="取消" onConfirm={runSelected}
          disabled={runDisabled || !selNames.length}>
          <Tooltip title={selNames.length
            ? `运行选中的 ${selNames.length} 条用例（串行，聚合报告按批次归组）`
            : "先在下方勾选要运行的用例"}>
            <Button type="primary" loading={running} disabled={runDisabled || !selNames.length}>运行</Button>
          </Tooltip>
        </Popconfirm>
        <Dropdown disabled={!selNames.length}
          menu={{ items: batchItems, onClick: ({ key }) => { if (key === "delete") confirmDelSelected(); } }}>
          <Button icon={<DownOutlined />}>批量操作</Button>
        </Dropdown>
      </div>
      {filtered.length === 0 ? (
        <Card><Empty description={filter || needle ? "当前筛选条件下无用例" : "暂无用例"}>
          <Space>
            <Button type="primary" icon={<ThunderboltOutlined />}
              onClick={() => onNav?.({ view: "generate" })}>AI 生成 / 导入</Button>
            <Button onClick={newCase}>手动新建</Button>
          </Space>
        </Empty></Card>
      ) : (<>
        <Table size="small" rowKey="key" dataSource={grouped} defaultExpandAllRows
          expandable={{ expandIcon: () => <span /> }}
          // 分组行只是组织维度，不应可选（发起人 2026-10-12）：禁用组行 checkbox，
          // 勾选只落在具体用例行上
          rowSelection={{ selectedRowKeys: selKeys, onChange: (keys) => setSelKeys(keys as string[]),
            getCheckboxProps: (r) => ({ disabled: !!(r as { isGroup?: boolean }).isGroup }) }}
          // scroll x max-content：窄窗口不压缩列，横向滚动兜底（发起人 2026-10-12：
          // 窄窗下「用例」列 flex 内容被压到 0 宽 + td overflow:hidden → 整行空白，
          // 宽窗口无法复现；去掉列级 ellipsis（td 不再 hidden）+ 禁止列压缩后，
          // 渲染层 JS 截短的文本任何环境都可见）
          scroll={{ x: "max-content" }}
          columns={[
            { title: "用例", dataIndex: "name",
              render: (n: string | undefined, r: { isGroup?: boolean; app?: string;
                count?: number; title?: string; cid?: string; no?: number }) =>
                r.isGroup
                  // 分组行：包名 tag 只出现一次 + displayName（可解析时）+ 条数（去重）
                  ? <span>
                      <Tag color="blue">{r.app}</Tag>
                      {appLabels[r.app || ""] && <span>{appLabels[r.app || ""]}</span>}
                      <span style={{ color: "#697080" }}>（{r.count ?? 0} 条）</span>
                    </span>
                  // 展示编号 #N（发起人 2026-10-12：caseId 随机字母难沟通且大量重复，
                  // 改用服务端分配的稳定递增数字；对账仍以 JSON caseId 为准，报告不动）。
                  // 文件名降级进 Tooltip 全文（真实资产名，报告目录/套件引用以它为准）。
                  : <Space size={6}>
                      <span style={{ color: "#8a919f", fontSize: 12, fontVariantNumeric: "tabular-nums" }}>
                        #{r.no ?? "—"}
                      </span>
                      <Tooltip title={<div># {r.no ?? "—"} · 文件：{n}</div>}>
                        <a onClick={() => openEditor(n!)}>
                          {shortText(r.title || String(n || ""), 26, 8)}
                        </a>
                      </Tooltip>
                    </Space> },
            { title: "操作", width: 200, align: "right",
              render: (_: unknown, r: { isGroup?: boolean; name?: string }) =>
                r.isGroup ? null : (
                  <Space>
                    <Button size="small" onClick={() => openEditor(r.name!)}>编辑</Button>
                    <Tooltip title="复制一份用例再改（克隆不改动原用例）">
                      <Button size="small" onClick={() => cloneCase(r.name!)}>复制</Button>
                    </Tooltip>
                    {/* 重命名（2026-10-25，目录功能的轻量替代）：整理无语义的生成文件名；
                        套件引用由后端同步替换，编号视为新用例（caseMeta 既有语义） */}
                    <Button size="small" onClick={() => setRename({ from: r.name!, to: "" })}>改名</Button>
                  </Space>) },
          ]} />
      <Modal title={`重命名 ${rename?.from || ""}`} open={!!rename}
        onCancel={() => setRename(null)} okText="重命名" cancelText="取消"
        onOk={async () => {
          if (!rename) return;
          const to = rename.to.trim();
          if (!to || to === rename.from) { setRename(null); return; }
          const finalTo = to.endsWith(".json") ? to : `${to}.json`;
          try {
            const r2 = await renameCase({ from: rename.from, to: finalTo });
            message.success(`已重命名为 ${r2.file}`
              + (r2.suites_updated.length ? `（${r2.suites_updated.length} 个套件引用已同步）` : ""));
            setRename(null);
            onCasesChanged();
          } catch (e) { message.error(String(e)); }
        }}>
        <Input addonBefore="新文件名" placeholder="如 search-bt.json（可省略 .json）"
          value={rename?.to} autoFocus
          onChange={(e) => rename && setRename({ ...rename, to: e.target.value })} />
        <div style={{ color: "#8a919f", fontSize: 12, marginTop: 8 }}>
          只改文件名,用例内容不动;套件里对它的引用会自动更新;历史报告不受影响。
        </div>
      </Modal>
      </>)}
      <CaseEditor state={state} editing={editing} setEditing={setEditing}
        onSaved={onCasesChanged} focusStep={focusStepId || focusStep} />
    </>
  );
}

// ---------- 套件 Tab ----------

type SuiteEditing = { file: string | null; title: string; cases: string[] };

function SuitesTab({ state, onLaunched, runPlatform, setRunPlatform, runSerial, setRunSerial,
                     record, setRecord, reset, setReset, appLabels }: {
  state: StateResp; onLaunched: (id: string) => void;
  runPlatform: "android" | "ios" | "harmony"; setRunPlatform: (v: "android" | "ios" | "harmony") => void;
  runSerial: string | null; setRunSerial: (v: string | null) => void;
  record: boolean; setRecord: (v: boolean) => void;
  reset: "off" | "cold"; setReset: (v: "off" | "cold") => void;
  appLabels: Record<string, string> }) {
  const [suites, setSuites] = useState<SuiteInfo[] | null>(null);
  const [editing, setEditing] = useState<SuiteEditing | null>(null);
  const [q, setQ] = useState("");                     // 名称筛选（标题/文件名）
  const [selKeys, setSelKeys] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const { runDisabled, runTarget } = useRunTarget(state, runPlatform, runSerial);

  const load = () => getSuites().then((d) => setSuites(d.suites)).catch(() => setSuites([]));
  useEffect(() => { load(); }, []);

  const all = suites ?? [];
  const needle = q.trim().toLowerCase();
  const shown = needle
    ? all.filter((s) => (s.title || "").toLowerCase().includes(needle)
        || s.file.toLowerCase().includes(needle))
    : all;
  const selFiles = shown.filter((s) => selKeys.includes(s.file)).map((s) => s.file);

  // 运行选中套件：逐个起 job，同设备由设备锁自动串行排队；聚合报告按套件批次归组
  const runSelected = async () => {
    if (!selFiles.length) return;
    // iOS：选了 USB 真机就传 udid；留空 = 模拟器。Android 未选则回落首台在线设备
    const serial = runSerial || (runPlatform === "ios" ? null
      : state.devices.find((d) => d.state === "device"
          && (d.platform || "android") === "android")?.serial || null);
    setRunning(true);
    try {
      let first = "";
      for (const file of selFiles) {
        const { job_id } = await runCase({ case: file, serial, platform: runPlatform, record, reset });
        if (!first) first = job_id;
      }
      message.success(`已启动 ${selFiles.length} 个套件（同设备串行排队）——聚合报告完成后在「报告」页查看`);
      onLaunched(first);
    } catch (e) { message.error(String(e)); } finally { setRunning(false); }
  };
  const delSelected = async () => {
    let ok = 0; const errs: string[] = [];
    for (const f of selFiles) {
      try { await deleteSuite({ file: f }); ok++; }
      catch (e) { errs.push(`${f}：${String(e).replace(/^Error:\s*/, "")}`); }
    }
    if (ok) { message.success(`已删除 ${ok} 个套件（用例文件不受影响）`); setSelKeys([]); }
    if (errs.length) {
      message.warning(`${errs.length} 个删除失败：${errs[0]}${errs.length > 1 ? " 等" : ""}`);
    }
    load();
  };
  const confirmDelSelected = () => Modal.confirm({
    title: `删除选中的 ${selFiles.length} 个套件？`,
    content: "套件只是用例的引用清单——删除不影响用例文件本身。",
    okText: "删除", okButtonProps: { danger: true }, cancelText: "取消",
    onOk: delSelected,
  });
  const batchItems = [{
    key: "delete", icon: <DeleteOutlined />, danger: true,
    label: `删除选中（${selFiles.length}）`,
  }];

  return (
    <>
      {/* 套件说明（发起人 2026-10-12）：新手不知道套件是什么、怎么用——置顶在搜索栏上方 */}
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message="套件是什么"
        description="套件 = 一组按固定顺序批量执行的用例。用法：①「新建套件」→ 从用例清单勾选并排序（同一设备按顺序串行执行，支持用例间冷启动重置）；②运行参数（平台/设备/录屏/冷启动）在下方运行行选择；③勾选套件点「运行」，跑完后在「报告」页按批次查看聚合报告（通过/失败统计、失败步骤与原因）。套件只引用用例文件——编辑/删除套件不影响用例本身。" />
      {/* 与用例 Tab 同构的两行布局（发起人 2026-10-12）：
          第一行筛选 + 新建套件；第二行运行参数 + 运行/批量操作 */}
      <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8, marginBottom: 8 }}>
        <Input allowClear placeholder="按名称筛选（标题 / 文件名）" style={{ width: 240 }}
          value={q} onChange={(e) => setQ(e.target.value)} />
        <span style={{ color: "#8a919f", fontSize: 12 }}>{shown.length} 个套件</span>
        <Button type="primary" icon={<PlusOutlined />} style={{ marginLeft: "auto" }}
          onClick={() => setEditing({ file: null, title: `套件 ${new Date().toLocaleDateString()}`, cases: [] })}>
          新建套件</Button>
      </div>
      <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
        <RunParams state={state} runPlatform={runPlatform} setRunPlatform={setRunPlatform}
          runSerial={runSerial} setRunSerial={setRunSerial} record={record} setRecord={setRecord} />
        {/* 套件同样需要用例间冷启动（发起人 2026-10-12 确认）：套件=多用例串行执行，
            跨用例状态耦合问题与批量运行完全一致，共用同一开关语义 */}
        <Select value={reset} onChange={setReset} style={{ width: 132 }}
          options={[{ value: "cold", label: "用例间冷启动" }, { value: "off", label: "不重置" }]} />
        <Popconfirm title={`运行选中的 ${selFiles.length} 个套件？`}
          description={`目标：${runTarget}${record ? " · 含录屏" : ""}（逐套件串行排队，聚合报告按套件归组）`}
          okText="运行" cancelText="取消" onConfirm={runSelected}
          disabled={runDisabled || !selFiles.length}>
          <Tooltip title={selFiles.length ? `运行选中的 ${selFiles.length} 个套件` : "先在下方勾选要运行的套件"}>
            <Button type="primary" loading={running} disabled={runDisabled || !selFiles.length}>运行</Button>
          </Tooltip>
        </Popconfirm>
        <Dropdown disabled={!selFiles.length}
          menu={{ items: batchItems, onClick: ({ key }) => { if (key === "delete") confirmDelSelected(); } }}>
          <Button icon={<DownOutlined />}>批量操作</Button>
        </Dropdown>
      </div>
      {suites === null ? <Card loading style={{ minHeight: 160 }} /> : all.length === 0 ? (
        <Card><Empty description="暂无套件——点「新建套件」把多条用例组成一次批量运行" /></Card>
      ) : shown.length === 0 ? (
        <Card><Empty description="当前筛选条件下无套件" /></Card>
      ) : (
        <Table size="small" rowKey="file" dataSource={shown}
          rowSelection={{ selectedRowKeys: selKeys, onChange: (keys) => setSelKeys(keys as string[]) }}
          columns={[
            { title: "套件", dataIndex: "title", ellipsis: true,
              render: (t: string, r: SuiteInfo) => (
                <Space size={6}>
                  <Typography.Text strong>{t || r.file}</Typography.Text>
                  <Typography.Text code style={{ fontSize: 12 }}>{r.file}</Typography.Text>
                </Space>) },
            { title: "用例数", dataIndex: "count", width: 90 },
            { title: "操作", width: 90, align: "right",
              render: (_: unknown, r: SuiteInfo) => (
                <Button size="small" onClick={async () => {
                  try {
                    const d = await getSuite(r.file);
                    setEditing({ file: r.file, title: d.title, cases: d.cases });
                  } catch (e) { message.error(String(e)); }
                }}>编辑</Button>) },
          ]} />
      )}
      <SuiteEditorModal state={state} appLabels={appLabels} editing={editing} setEditing={setEditing}
        onSaved={() => load()} />
    </>
  );
}

// ---------- 套件编辑器（两栏：可选用例 → 已选有序列表） ----------

function SuiteEditorModal({ state, appLabels, editing, setEditing, onSaved }: {
  state: StateResp; appLabels: Record<string, string>; editing: SuiteEditing | null;
  setEditing: (e: SuiteEditing | null) => void; onSaved: () => void }) {
  const [busy, setBusy] = useState(false);
  // 可选用例筛选（2026-10-25）：用例多了之后凭文件名找不到目标——文字按标题/编号/文件名，
  // 包名下拉按被测 App 过滤（多 App 工作区里先把范围收到一个包再挑用例）
  const [q, setQ] = useState("");
  const [appFilter, setAppFilter] = useState<string | null>(null);
  const available = state.cases.filter((c) => !(editing?.cases || []).includes(c));
  const needle = q.trim().toLowerCase();
  const matches = (c: string) => {
    const m = state.caseMeta[c];
    if (appFilter && (m?.app || "") !== appFilter) return false;
    if (!needle) return true;
    return c.toLowerCase().includes(needle)
      || (m?.title || "").toLowerCase().includes(needle)
      || (m?.no != null && String(m.no) === needle.replace("#", ""));
  };
  const shownAvailable = available.filter(matches);
  const appOptions = useMemo(() => {
    const apps = Array.from(new Set(state.cases
      .map((c) => state.caseMeta[c]?.app).filter(Boolean))) as string[];
    return apps.map((a) => ({ value: a, label: appLabels[a] ? `${appLabels[a]}（${a}）` : a }));
  }, [state.cases, state.caseMeta, appLabels]);
  // 显示口径与用例 Tab 对齐：#编号 + 用例标题（文件名降级进 Tooltip——
  // 套件引用与报告目录以文件名为准，但用户认的是标题）
  const caseLabel = (c: string) => {
    const m = state.caseMeta[c];
    return { no: m?.no ?? "—", title: m?.title || c, file: c };
  };

  const move = (idx: number, delta: number) => {
    if (!editing) return;
    const next = [...editing.cases];
    const to = idx + delta;
    if (to < 0 || to >= next.length) return;
    [next[idx], next[to]] = [next[to], next[idx]];
    setEditing({ ...editing, cases: next });
  };

  const save = async () => {
    if (!editing) return;
    if (!editing.title.trim()) { message.error("需要套件标题"); return; }
    if (!editing.cases.length) { message.error("至少选择一条用例"); return; }
    setBusy(true);
    try {
      const r = await saveSuite({ file: editing.file, title: editing.title.trim(), cases: editing.cases });
      message.success(`已保存 ${r.file}`);
      setEditing(null);
      onSaved();
    } catch (e) { message.error(String(e)); } finally { setBusy(false); }
  };

  return (
    <Drawer title={editing?.file ? `编辑套件 ${editing.file}` : "新建套件"}
      width={680} open={!!editing} onClose={() => setEditing(null)}
      extra={<Button type="primary" loading={busy} onClick={save}>保存</Button>}>
      {editing && (
        <>
          <Input addonBefore="套件标题" style={{ marginBottom: 12 }}
            value={editing.title} onChange={(e) => setEditing({ ...editing, title: e.target.value })} />
          <div style={{ display: "flex", gap: 12 }}>
            {/* 筛选行独立于 Card extra（2026-10-25 用户反馈「可...」）：extra 与标题挤在
                一行,小宽度下 antd 会把 Card 标题省略成「可...」——把控件挪到左栏自己的
                行,视觉归属清晰（只作用于可选用例）且标题不再被挤 */}
            <div style={{ flex: 1, minWidth: 0 }}>
              <Space size={4} style={{ marginBottom: 6, width: "100%" }}>
                <Select allowClear showSearch size="small" placeholder="按包筛选" style={{ minWidth: 120 }}
                  value={appFilter} onChange={setAppFilter} options={appOptions}
                  filterOption={(input, opt) => String(opt?.label ?? "").toLowerCase().includes(input.toLowerCase())} />
                <Input allowClear size="small" placeholder="标题/编号/文件名" style={{ flex: 1 }}
                  value={q} onChange={(e) => setQ(e.target.value)} />
              </Space>
              <Card size="small" title="可选用例" style={{ flex: 1 }}>
              <Space direction="vertical" size={4} style={{ width: "100%", maxHeight: 420, overflow: "auto" }}>
                {shownAvailable.map((c) => {
                  const l = caseLabel(c);
                  return (
                    <Tooltip key={c} title={`文件：${l.file}`}>
                      <Button block size="small" style={{ justifyContent: "flex-start", display: "flex" }}
                        onClick={() => setEditing({ ...editing, cases: [...editing.cases, c] })}>
                        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          <span style={{ color: "#8a919f", fontVariantNumeric: "tabular-nums" }}>#{l.no}</span>
                          {" "}{l.title}
                        </span>
                      </Button>
                    </Tooltip>
                  );
                })}
                {available.length === 0 && <span style={{ color: "#999", fontSize: 12 }}>（全部已选）</span>}
                {available.length > 0 && shownAvailable.length === 0
                  && <span style={{ color: "#999", fontSize: 12 }}>（无匹配——换个关键词，或清空筛选）</span>}
              </Space>
              </Card>
            </div>
            <Card size="small" title={`已选（${editing.cases.length}）`} style={{ flex: 1 }}>
              {editing.cases.length === 0 && <span style={{ color: "#999", fontSize: 12 }}>从左侧添加用例</span>}
              <div style={{ maxHeight: 420, overflow: "auto" }}>
                {editing.cases.map((c, idx) => {
                  const l = caseLabel(c);
                  return (
                    <div key={c} style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 4 }}>
                      <Tooltip title={`文件：${l.file}`}>
                        <span style={{ flex: 1, fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          {idx + 1}. <span style={{ color: "#8a919f" }}>#{l.no}</span> {l.title}
                        </span>
                      </Tooltip>
                      <Button size="small" type="text" disabled={idx === 0}
                        onClick={() => move(idx, -1)}><UpOutlined /></Button>
                      <Button size="small" type="text" disabled={idx === editing.cases.length - 1}
                        onClick={() => move(idx, 1)}><DownOutlined /></Button>
                      <Button size="small" type="text" danger
                        onClick={() => setEditing({ ...editing, cases: editing.cases.filter((x) => x !== c) })}>
                        <DeleteOutlined />
                      </Button>
                    </div>
                  );
                })}
              </div>
            </Card>
          </div>
          <div style={{ marginTop: 12, color: "#8a919f", fontSize: 12 }}>
            套件按顺序逐条执行（同一设备串行），产出聚合报告；条目按添加顺序运行，可用 ↑↓ 调整。
          </div>
        </>
      )}
    </Drawer>
  );
}

// ---------- 页面入口：用例/套件 双 Tab ----------

export default function Cases({ state, onLaunched, openCaseName, onOpenCase, onNav, onCasesChanged, focusStep }: {
  state: StateResp; onLaunched: (id: string) => void; openCaseName?: string;
  onOpenCase?: (name: string) => void; onNav?: (nav: Nav) => void; onCasesChanged: () => void;
  focusStep?: string | null }) {
  const [runPlatform, setRunPlatform] = useState<"android" | "ios" | "harmony">("android");
  const [runSerial, setRunSerial] = useState<string | null>(null);
  const [record, setRecord] = useState(true); // 全程录 + 只留失败（发起人拍板）
  const [reset, setReset] = useState<"off" | "cold">("cold"); // 批量默认冷启动重置（跨用例状态隔离）
  // App 显示名（发起人 2026-10-12：包名可读性差）——提升到页级（2026-10-25：套件编辑器
  // 的包名筛选也要用，此前只在用例 Tab 内部拉取，两个 Tab 各拉一遍既重复又传不下去）。
  // 后端 aapt 解析 APK + 缓存（无 aapt/无设备回空，前端回落包名，绝不阻塞列表）。
  const [appLabels, setAppLabels] = useState<Record<string, string>>({});
  const appKey = useMemo(() => Array.from(new Set(Object.values(state.caseMeta)
    .map((m) => m.app).filter(Boolean))).join(","), [state.caseMeta]);
  useEffect(() => {
    if (!appKey) return;
    getAppLabels(appKey.split(",")).then((d) => setAppLabels(d.labels)).catch(() => {});
  }, [appKey]);

  return (
    <>
      <PageHeader title="用例管理/执行" desc="用例与套件的管理、运行；AI 生成的用例也落在这里。" />
      <Card>
        <Tabs items={[
          { key: "cases", label: "用例",
            children: <CasesTab state={state} onLaunched={onLaunched} openCaseName={openCaseName}
              onOpenCase={onOpenCase} onNav={onNav} onCasesChanged={onCasesChanged}
              runPlatform={runPlatform} setRunPlatform={setRunPlatform}
              runSerial={runSerial} setRunSerial={setRunSerial}
              record={record} setRecord={setRecord} appLabels={appLabels}
              reset={reset} setReset={setReset}
              focusStep={focusStep} /> },
          { key: "suites", label: "套件",
            children: <SuitesTab state={state} onLaunched={onLaunched}
              runPlatform={runPlatform} setRunPlatform={setRunPlatform}
              runSerial={runSerial} setRunSerial={setRunSerial}
              record={record} setRecord={setRecord}
              reset={reset} setReset={setReset} appLabels={appLabels} /> },
        ]} />
      </Card>
    </>
  );
}
