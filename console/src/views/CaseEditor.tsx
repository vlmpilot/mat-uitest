import {
  Alert, Button, Checkbox, Drawer, Input, InputNumber, Popconfirm, Segmented, Select, Space, Tag,
  Tooltip, Typography, message,
} from "antd";
import {
  ArrowDownOutlined, ArrowUpOutlined, CopyOutlined, DeleteOutlined, PlusOutlined,
} from "@ant-design/icons";
import { useEffect, useMemo, useState } from "react";
import {
  ControlItem, cancelJob, getControls, getJob, getJobEvents, runCase, saveCase, StateResp,
} from "../api";

// 结构化用例编辑器（发起人 2026-10-12 P0/P1）：
// - 步骤卡片化编辑（意图/动作/定位/参数/断言/超时/onFailure），替代裸 JSON；JSON 模式保留给专家兜底。
// - target 控件选择器：候选来自 page_index 控件清单（res-id 优先），杜绝手打拼写错误；
//   清单里没有的允许手填，保存后端会给「res-id 不在控件清单」提醒（不阻断）。
// - 试跑本条：保存 → 起单条运行任务 → 事件流回显每步状态，修完即验证。
// - 报告「编辑此步」深链 #/cases/<name>.json?step=<id> → 滚动定位 + 高亮。

export type Editing = { name: string; content: string; isNew: boolean };

type LocatorRow = { strategy?: string; value?: string; fallback?: unknown };
type AssertionRow = { type?: string; target?: LocatorRow | null; expected?: string };
type StepRow = {
  id?: string; intent?: string; action?: string; target?: LocatorRow | null;
  params?: Record<string, unknown>; timeoutMs?: number; onFailure?: string;
  assertions?: AssertionRow[];
};

const ACTIONS: [string, string][] = [
  ["launch", "启动 App"], ["tap", "点击"], ["long-press", "长按"], ["input", "输入文本"],
  ["clear", "清空输入框"], ["swipe", "滑动"], ["scroll", "滚动"], ["back", "返回"],
  ["home", "回到主屏"], ["key-event", "系统按键"], ["wait", "等待"],
];
const ACTION_LABELS = Object.fromEntries(ACTIONS);
const TARGET_ACTIONS = ["tap", "long-press", "input", "clear"];
const STRATEGIES = ["res-id", "id", "text", "textContains", "description", "xpath", "point"];
const ASSERT_TYPES = ["exists", "not-exists", "text-equals", "text-contains",
  "enabled", "disabled", "checked", "selected"];
const NEEDS_EXPECTED = new Set(["text-equals", "text-contains"]);

const RUN_COLORS: Record<string, string> = {
  passed: "green", failed: "red", healed: "orange", running: "blue", queued: "default", skipped: "default",
};

export default function CaseEditor({ state, editing, setEditing, onSaved, focusStep }: {
  state: StateResp; editing: Editing | null; setEditing: (e: Editing | null) => void;
  onSaved: () => void; focusStep?: string | null }) {
  const [mode, setMode] = useState<"form" | "json">("form");
  const [saving, setSaving] = useState(false);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [controls, setControls] = useState<ControlItem[]>([]);
  const [highlight, setHighlight] = useState<string | null>(null);
  // 试跑本条：job 轮询 → 每步结果就地回显
  const [runJob, setRunJob] = useState<string | null>(null);
  const [runResults, setRunResults] = useState<Record<string, { status: string; detail?: string; duration_ms?: number }>>({});
  const [runSummary, setRunSummary] = useState("");
  const [runStatus, setRunStatus] = useState("");

  const pkg = useMemo(() => {
    try {
      const o = JSON.parse(editing?.content || "{}") as { target?: { package?: string } };
      return String(o.target?.package || "");
    } catch { return ""; }
  }, [editing?.content]);

  // 打开不同用例时重置派生状态（同组件常驻，编辑对象换人必须清场）
  useEffect(() => {
    setMode("form"); setWarnings([]); setRunJob(null); setRunResults({});
    setRunSummary(""); setRunStatus(""); setHighlight(null);
  }, [editing?.name, editing?.isNew]);

  // 控件清单按包拉取（无索引 → 空数组，前端降级手填）
  useEffect(() => {
    if (!editing || !pkg) { setControls([]); return; }
    let alive = true;
    getControls(pkg).then((r) => { if (alive) setControls(r.controls); })
      .catch(() => { if (alive) setControls([]); });
    return () => { alive = false; };
  }, [editing?.name, pkg, editing !== null]); // eslint-disable-line react-hooks/exhaustive-deps

  // 报告「编辑此步」深链：滚动定位 + 短暂高亮
  useEffect(() => {
    if (!focusStep || mode !== "form" || !editing) return;
    const t = window.setTimeout(() => {
      const el = document.getElementById(`step-card-${focusStep}`);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        setHighlight(focusStep);
        window.setTimeout(() => setHighlight(null), 2500);
      }
    }, 400);
    return () => window.clearTimeout(t);
  }, [focusStep, mode, editing?.name]); // eslint-disable-line react-hooks/exhaustive-deps

  // 试跑轮询：事件流 → step_id 归位到步骤卡片（单条运行无前缀；run/record 是任务级事件）
  useEffect(() => {
    if (!runJob) return;
    let seq = 0; let stop = false; let timer = 0;
    const tick = async () => {
      try {
        const { events, status } = await getJobEvents(runJob, seq);
        if (events.length) seq = events[events.length - 1].seq;
        if (events.length) {
          setRunResults((prev) => {
            const next = { ...prev };
            for (const ev of events) {
              if (ev.step_id === "run" || ev.step_id === "record") continue;
              next[ev.step_id] = { status: ev.status,
                detail: ev.detail || undefined,
                duration_ms: ev.duration_ms ?? undefined };
            }
            return next;
          });
        }
        setRunStatus(status);
        if (status !== "running" && status !== "queued") {
          const j = await getJob(runJob);
          setRunSummary(j.summary || j.error || "");
          setRunJob(null);
          return; // 终态：停止轮询
        }
      } catch { /* 服务瞬时不可达：下一轮重试 */ }
      if (!stop) timer = window.setTimeout(tick, 1500);
    };
    timer = window.setTimeout(tick, 600);
    return () => { stop = true; window.clearTimeout(timer); };
  }, [runJob]);

  // 控件选择器：res-id 候选 + 无 res-id 的 clickable 稳定文案候选（text 定位）。
  // ⚠️ hooks 必须在 early return 之前——editing 由 null 变非 null 时 hooks 数量须一致，
  // 否则 React 抛 "Rendered more hooks than during the previous render" 整树卸载。
  // 下拉展示三原则（发起人 2026-10-12 反馈「控件名长、分不清哪个是哪个」）：
  // ① res-id 去掉「<pkg>:id/」公共前缀只留尾部短名（完整 id 放 option.title 悬停可见）；
  // ② 同页面同 res-id 的多行合并成一行（首页 fdm 容器以不同文案出现 8 次，定位值本就相同），
  //    文案取前 3 个拼进 label、全量进 title——搜索「外卖」也能命中（见 filterControlOption）；
  // ③ 下拉不限宽于输入框（popupMatchSelectWidth={false}），长项不再被省略号截断。
  const controlOptions = useMemo(() => {
    const prefix = pkg ? `${pkg}:id/` : "";
    type Cand = { value: string; full: string; short: string; texts: string[]; isRid: boolean };
    const groups = new Map<string, Map<string, Cand>>();
    controls.forEach((c) => {
      const key = c.page || "（未知页面）";
      if (!groups.has(key)) groups.set(key, new Map());
      const g = groups.get(key)!;
      if (c.res_id) {
        const value = JSON.stringify(["rid", c.res_id]);
        const short = prefix && c.res_id.startsWith(prefix) ? c.res_id.slice(prefix.length) : c.res_id;
        const hit = g.get(value);
        if (hit) {
          if (c.label && !hit.texts.includes(c.label)) hit.texts.push(c.label);
        } else {
          g.set(value, { value, full: c.res_id, short, texts: c.label ? [c.label] : [], isRid: true });
        }
      } else if (c.label) {
        const value = JSON.stringify(["txt", c.label, g.size]);
        g.set(value, { value, full: c.label, short: `文案「${c.label}」`, texts: [], isRid: false });
      }
    });
    return Array.from(groups.entries()).map(([label, g]) => ({
      label,
      options: Array.from(g.values()).map((o) => o.isRid
        ? {
            value: o.value,
            // 文案多时 label 只展示前 3 个（完整清单在 title 悬停可见、搜索可达）
            label: o.texts.length
              ? `${o.short}｜${o.texts.slice(0, 3).join("/")}${o.texts.length > 3 ? ` 等${o.texts.length}处` : ""}`
              : o.short,
            title: o.texts.length ? `${o.full}｜${o.texts.join("、")}` : o.full,
          }
        : { value: o.value, label: o.short, title: `text 定位：${o.full}` }),
    }));
  }, [controls, pkg]);

  // 生成/导入产物自带的质量闸门 issues(「只报不改」):编辑前先看一眼,
  // 不用翻 JSON meta——修正后保存,提醒随 meta 更新消失
  const qualityIssues = useMemo(() => {
    try {
      const o = JSON.parse(editing?.content || "{}") as { meta?: { quality_issues?: unknown } };
      const v = o.meta?.quality_issues;
      return Array.isArray(v) ? (v as string[]) : [];
    } catch { return []; }
  }, [editing?.content]);

  if (!editing) return null;

  const tryParse = (): Record<string, unknown> | string => {
    try { return JSON.parse(editing.content) as Record<string, unknown>; }
    catch (e) { return `JSON 语法错误:${String(e).split("\n")[0]}`; }
  };
  const parsed = mode === "form" ? tryParse() : null;
  const steps: StepRow[] = (parsed && typeof parsed === "object"
    && Array.isArray((parsed as { steps?: unknown }).steps)
    ? (parsed as { steps: StepRow[] }).steps : []);

  // 结构化模式唯一写入口：parse → mutate → 序列化回 content（JSON 模式永远看到真实状态）
  const update = (fn: (obj: Record<string, unknown>) => void) => {
    const obj = tryParse();
    if (typeof obj === "string") { message.error(obj); setMode("json"); return; }
    fn(obj);
    setEditing({ ...editing, content: JSON.stringify(obj, null, 2) });
  };
  const stepsRef = (fn: (steps: StepRow[]) => void) =>
    update((obj) => { fn(obj.steps as StepRow[]); });
  const updateStep = (idx: number, patch: (s: StepRow) => void) =>
    stepsRef((arr) => { if (arr[idx]) patch(arr[idx]); });

  const nextId = (arr: StepRow[]): string => {
    let max = 0;
    for (const s of arr) {
      const m = /^s(\d+)$/.exec(s.id || "");
      if (m) max = Math.max(max, Number(m[1]));
    }
    let n = max + 1;
    while (arr.some((s) => s.id === `s${n}`)) n += 1;
    return `s${n}`;
  };
  const addStep = () => stepsRef((arr) => {
    arr.push({ id: nextId(arr), intent: "", action: "tap", params: {}, assertions: [] });
  });
  const moveStep = (idx: number, delta: number) => stepsRef((arr) => {
    const to = idx + delta;
    if (to < 0 || to >= arr.length) return;
    [arr[idx], arr[to]] = [arr[to], arr[idx]];
  });
  const dupStep = (idx: number) => stepsRef((arr) => {
    const copy = JSON.parse(JSON.stringify(arr[idx])) as StepRow;
    copy.id = nextId(arr);
    arr.splice(idx + 1, 0, copy);
  });
  const delStep = (idx: number) => stepsRef((arr) => { arr.splice(idx, 1); });
  const setParam = (idx: number, key: string, v: unknown) => updateStep(idx, (s) => {
    const p = { ...(s.params || {}) };
    if (v === undefined || v === "") delete p[key]; else p[key] = v;
    s.params = p;
  });

  // 控件候选 value→Locator 的还原（控件选择器 onChange 用）
  const pickLocator = (v: string): LocatorRow | undefined => {
    try {
      // value 编码：["rid", res_id] / ["txt", 文案, 序号]（序号仅为 Select 去重），
      // 文案必须取 rest[0]——曾误取 rest[1] 把序号填进定位值（发起人 2026-10-12 截图反馈）
      const [kind, ...rest] = JSON.parse(v) as [string, ...unknown[]];
      return kind === "rid" ? { strategy: "res-id", value: String(rest[0] ?? "") }
        : { strategy: "text", value: String(rest[0] ?? "") };
    } catch { return undefined; }
  };

  // 搜索过滤：label 之外连 title（完整 res-id + 合并的全部文案）一起匹配，
  // 这样「外卖」能搜到文案被折叠进 title 的合并项
  const filterControlOption = (input: string, option?: unknown): boolean => {
    const o = (option ?? {}) as { label?: unknown; title?: unknown };
    const hay = `${String(o.label ?? "")} ${String(o.title ?? "")}`.toLowerCase();
    return hay.includes(input.trim().toLowerCase());
  };

  const switchMode = (m: string | number) => {
    if (m === "form") {
      const obj = tryParse();
      if (typeof obj === "string") { message.error(obj); return; }
    }
    setMode(m as "form" | "json");
  };

  const formatJson = () => {
    const obj = tryParse();
    if (typeof obj === "string") { message.error(obj); return; }
    setEditing({ ...editing, content: JSON.stringify(obj, null, 2) });
  };

  const doSave = async (): Promise<boolean> => {
    const name = editing.name.trim();
    if (!name.endsWith(".json") || name.includes("/") || name.includes("..")) {
      message.error("用例名必须以 .json 结尾,且不含路径分隔符"); return false;
    }
    const obj = tryParse();
    if (typeof obj === "string") { message.error(obj); setMode("json"); return false; }
    if (!Array.isArray(obj.steps) || obj.steps.length === 0) {
      message.error("至少需要一个步骤（steps）"); return false;
    }
    const ids = (obj.steps as StepRow[]).map((s) => String(s.id || ""));
    if (new Set(ids).size !== ids.length) { message.error("步骤 id 有重复，请修正"); return false; }
    if (editing.isNew && state.cases.includes(name)) {
      message.error(`已存在同名用例 ${name}`); return false;
    }
    setSaving(true);
    try {
      const r = await saveCase(name, JSON.stringify(obj, null, 2));
      setWarnings(r.warnings || []);
      message.success(`已保存 ${name}` + (r.warnings?.length ? `（${r.warnings.length} 条提醒）` : ""));
      setEditing({ ...editing, name, content: JSON.stringify(obj, null, 2), isNew: false });
      onSaved();
      return true;
    } catch (e) { message.error(String(e)); return false; }
    finally { setSaving(false); }
  };

  // 试跑本条：先保存（试跑的是落盘内容），再起单条任务；结果回显在步骤卡片上
  const tryRun = async () => {
    if (!(await doSave())) return;
    try {
      const { job_id } = await runCase({ case: editing.name.trim(), platform: "android", record: false });
      setRunResults({}); setRunSummary(""); setRunStatus("queued");
      setRunJob(job_id);
      message.success("试跑已启动，结果将回显在各步骤卡片");
    } catch (e) { message.error(String(e)); }
  };

  return (
    <Drawer title={editing.isNew ? "新建用例" : `编辑 ${editing.name}`} width={760}
      open onClose={() => setEditing(null)}
      extra={<Space>
        {runJob
          ? <Button onClick={async () => { await cancelJob(runJob).catch(() => undefined); }}>停止试跑</Button>
          : <Button onClick={tryRun} disabled={runStatus === "running"}>▶ 试跑本条</Button>}
        {mode === "json" && <Button onClick={formatJson}>格式化</Button>}
        <Button onClick={() => setEditing(null)}>关闭</Button>
        <Button type="primary" loading={saving} onClick={doSave}>保存</Button>
      </Space>}>
      <Segmented style={{ marginBottom: 12 }}
        options={[{ label: "结构化", value: "form" }, { label: "JSON", value: "json" }]}
        value={mode} onChange={switchMode} />
      {qualityIssues.length > 0 && (
        <Alert type="warning" showIcon style={{ marginBottom: 8 }}
          message={`AI 质量检查:${qualityIssues.length} 个可能影响执行的问题(只提示,不自动修改)`}
          description={<ul style={{ margin: 0, paddingLeft: 18 }}>
            {qualityIssues.map((q, i) => <li key={i} style={{ fontSize: 12 }}>{q}</li>)}
          </ul>} />
      )}
      {warnings.length > 0 && (
        <Alert type="warning" showIcon closable style={{ marginBottom: 8 }}
          message={`${warnings.length} 条提醒（不影响已保存，建议修正后重新保存）`}
          description={<ul style={{ margin: 0, paddingLeft: 18 }}>
            {warnings.map((w) => <li key={w} style={{ fontSize: 12 }}>{w}</li>)}
          </ul>} />
      )}
      {runSummary && (
        <Alert type={runStatus === "done" ? "success" : "error"} showIcon style={{ marginBottom: 8 }}
          message={`试跑结束：${runSummary}`} />
      )}
      {mode === "json" ? (
        <>
          {editing.isNew && <Input addonBefore="文件名" style={{ marginBottom: 8 }}
            value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />}
          <Input.TextArea rows={26} value={editing.content}
            onChange={(e) => setEditing({ ...editing, content: e.target.value })}
            style={{ fontFamily: "ui-monospace, Menlo, Consolas, monospace", fontSize: 12 }} />
          <div style={{ marginTop: 8, color: "#8a919f", fontSize: 12 }}>
            字段参考:caseId / name / target.package / steps[]（id, intent, action, target.strategy/value, params, assertions）
          </div>
        </>
      ) : parsed && typeof parsed === "object" ? (
        <>
          <Space style={{ marginBottom: 8 }} wrap>
            {/* 文件名语义（发起人 2026-10-12 问询）：新建/克隆时可编辑（给新文件起名）；
                编辑已有用例时是资产名（报告目录/套件引用），锁定为只读展示+复制 */}
            {editing.isNew
              ? <Input addonBefore="文件名" style={{ width: 260 }} placeholder="new-case.json"
                  value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
              : <Tooltip title="文件名是资产名（报告目录/套件引用以它为准），保存时不可改">
                  <span style={{ display: "inline-flex", alignItems: "center", height: 32,
                    padding: "0 11px", border: "1px solid #d9d9d9", borderRadius: 6,
                    background: "#fafafa" }}>
                    <Typography.Text code copyable={{ text: editing.name,
                      tooltips: ["复制文件名", "已复制"] }} style={{ fontSize: 13 }}>
                      {editing.name}
                    </Typography.Text>
                  </span>
                </Tooltip>}
            <Input addonBefore="标题" style={{ width: 340 }}
              value={String((parsed as { name?: string }).name || "")}
              onChange={(e) => update((o) => { o.name = e.target.value; })} />
            <Input addonBefore="被测包名" style={{ width: 260 }} placeholder="com.example.app"
              value={String(((parsed as { target?: { package?: string } }).target || {}).package || "")}
              onChange={(e) => update((o) => {
                const t = { ...(o.target as { package?: string } || {}) };
                if (e.target.value) t.package = e.target.value; else delete t.package;
                o.target = t;
              })} />
          </Space>
          {!pkg && <div style={{ color: "#faad14", fontSize: 12, marginBottom: 8 }}>
            未填被测包名：控件选择器不可用（清单按包检索），保存后也无法做 res-id 清单校验。
          </div>}
          {pkg && controls.length === 0 && (
            <div style={{ color: "#8a919f", fontSize: 12, marginBottom: 8 }}>
              该包暂无控件索引——定位需手填；可到「用例生成/导入」页对 {pkg} 建索引后获得候选。
            </div>
          )}
          {steps.map((st, idx) => {
            const rr = runResults[String(st.id || "")];
            const showTarget = TARGET_ACTIONS.includes(String(st.action)) || st.target != null;
            return (
              <div key={idx} id={`step-card-${st.id || idx}`}
                style={{ border: "1px solid #f0f0f0", borderRadius: 8, padding: "8px 10px",
                  marginBottom: 8, background: highlight === st.id ? "#fffbe6" : undefined,
                  boxShadow: highlight === st.id ? "0 0 0 2px #faad14" : undefined,
                  transition: "box-shadow .3s, background .3s" }}>
                <Space style={{ width: "100%", marginBottom: 6 }} wrap>
                  <Tag color="blue" style={{ marginInlineEnd: 0 }}>{idx + 1}</Tag>
                  <Input size="small" style={{ width: 72 }} value={String(st.id || "")}
                    onChange={(e) => updateStep(idx, (s) => { s.id = e.target.value; })} />
                  <Input size="small" style={{ width: 200 }} placeholder="意图（描述这一步做什么）"
                    value={String(st.intent || "")}
                    onChange={(e) => updateStep(idx, (s) => { s.intent = e.target.value; })} />
                  <Select size="small" style={{ width: 130 }} value={String(st.action || "tap")}
                    options={ACTIONS.map(([v, l]) => ({ value: v, label: l }))}
                    onChange={(v) => updateStep(idx, (s) => { s.action = v; })} />
                  <Button size="small" type="text" disabled={idx === 0}
                    onClick={() => moveStep(idx, -1)}><ArrowUpOutlined /></Button>
                  <Button size="small" type="text" disabled={idx === steps.length - 1}
                    onClick={() => moveStep(idx, 1)}><ArrowDownOutlined /></Button>
                  <Button size="small" type="text" title="复制本步"
                    onClick={() => dupStep(idx)}><CopyOutlined /></Button>
                  <Popconfirm title={`删除步骤 ${st.id || idx + 1}？`} onConfirm={() => delStep(idx)}>
                    <Button size="small" type="text" danger><DeleteOutlined /></Button>
                  </Popconfirm>
                  {rr && <Tag color={RUN_COLORS[rr.status] || "default"} style={{ marginInlineEnd: 0 }}>
                    {rr.status}{rr.duration_ms != null ? ` · ${rr.duration_ms}ms` : ""}
                  </Tag>}
                </Space>
                {showTarget && (
                  <Space style={{ marginBottom: 6 }} wrap>
                    <Select size="small" style={{ width: 130 }} placeholder="定位策略"
                      value={st.target?.strategy || "res-id"}
                      options={STRATEGIES.map((s) => ({ value: s, label: s }))}
                      onChange={(v) => updateStep(idx, (s) => {
                        s.target = { ...(s.target || {}), strategy: v } as LocatorRow;
                      })} />
                    <Input size="small" style={{ width: 260 }} placeholder="定位值"
                      value={String(st.target?.value || "")}
                      onChange={(e) => updateStep(idx, (s) => {
                        s.target = { ...(s.target || {}), strategy: s.target?.strategy || "res-id",
                          value: e.target.value } as LocatorRow;
                      })} />
                    <Select size="small" style={{ minWidth: 240 }} allowClear showSearch
                      popupMatchSelectWidth={false} filterOption={filterControlOption}
                      placeholder="从控件清单选择（按页面/文案搜索）"
                      options={controlOptions} value={null}
                      onChange={(v) => {
                        const loc = v ? pickLocator(String(v)) : undefined;
                        if (loc) updateStep(idx, (s) => { s.target = loc; });
                      }} />
                    {st.target && (
                      <Button size="small" type="text" danger title="移除定位"
                        onClick={() => updateStep(idx, (s) => { s.target = null; })}>
                        <DeleteOutlined /></Button>
                    )}
                  </Space>
                )}
                <Space style={{ marginBottom: st.assertions?.length ? 6 : 0 }} wrap>
                  {st.action === "launch" && <>
                    <Input size="small" style={{ width: 200 }} placeholder="包名（空 = 用例包名）"
                      value={String(st.params?.package ?? "")}
                      onChange={(e) => setParam(idx, "package", e.target.value || undefined)} />
                    <Checkbox checked={st.params?.cold === true || st.params?.cold === "true"}
                      onChange={(e) => setParam(idx, "cold", e.target.checked || undefined)}>冷启动</Checkbox>
                  </>}
                  {st.action === "input" && <Input size="small" style={{ width: 220 }}
                    placeholder="输入文本（params.text）" value={String(st.params?.text ?? "")}
                    onChange={(e) => setParam(idx, "text", e.target.value)} />}
                  {(st.action === "swipe" || st.action === "scroll") &&
                    <Select size="small" style={{ width: 100 }} value={String(st.params?.direction ?? "up")}
                      options={["up", "down", "left", "right"].map((d) => ({ value: d, label: d }))}
                      onChange={(v) => setParam(idx, "direction", v)} />}
                  {st.action === "key-event" && <Input size="small" style={{ width: 140 }}
                    placeholder="键名（默认 back）" value={String(st.params?.key ?? "")}
                    onChange={(e) => setParam(idx, "key", e.target.value || undefined)} />}
                  {st.action === "wait" && <InputNumber size="small" style={{ width: 140 }} min={0} step={500}
                    placeholder="毫秒" value={Number(st.params?.ms ?? 1000)}
                    onChange={(v) => setParam(idx, "ms", v ?? 1000)} />}
                  <span style={{ fontSize: 12, color: "#8a919f" }}>失败时</span>
                  <Select size="small" style={{ width: 96 }} value={String(st.onFailure || "abort")}
                    options={[{ value: "abort", label: "中止" }, { value: "retry", label: "重试" },
                      { value: "heal", label: "自愈" }]}
                    onChange={(v) => updateStep(idx, (s) => { s.onFailure = v; })} />
                  <span style={{ fontSize: 12, color: "#8a919f" }}>超时ms</span>
                  <InputNumber size="small" style={{ width: 96 }} min={500} step={500}
                    value={Number(st.timeoutMs ?? 8000)}
                    onChange={(v) => updateStep(idx, (s) => { s.timeoutMs = v ?? 8000; })} />
                </Space>
                {rr && rr.detail && rr.status !== "passed" && (
                  <div style={{ fontSize: 12, color: "#cf1322", marginBottom: 4, whiteSpace: "pre-wrap" }}>
                    {rr.detail}
                  </div>
                )}
                {(st.assertions || []).map((a, ai) => (
                  <div key={ai} style={{ display: "flex", gap: 6, marginTop: 4, alignItems: "center", flexWrap: "wrap" }}>
                    <Tag style={{ marginInlineEnd: 0 }}>断言{ai + 1}</Tag>
                    <Select size="small" style={{ width: 120 }} value={String(a.type || "exists")}
                      options={ASSERT_TYPES.map((t) => ({ value: t, label: t }))}
                      onChange={(v) => updateStep(idx, (s) => {
                        (s.assertions ||= [])[ai].type = v;
                      })} />
                    <Select size="small" style={{ width: 110 }} placeholder="定位策略"
                      value={a.target?.strategy || "res-id"}
                      options={STRATEGIES.map((s) => ({ value: s, label: s }))}
                      onChange={(v) => updateStep(idx, (s) => {
                        const arr = (s.assertions ||= [])[ai];
                        arr.target = { ...(arr.target || {}), strategy: v } as LocatorRow;
                      })} />
                    <Input size="small" style={{ width: 200 }} placeholder="定位值"
                      value={String(a.target?.value || "")}
                      onChange={(e) => updateStep(idx, (s) => {
                        const arr = (s.assertions ||= [])[ai];
                        arr.target = { ...(arr.target || {}), strategy: arr.target?.strategy || "res-id",
                          value: e.target.value } as LocatorRow;
                      })} />
                    <Select size="small" style={{ minWidth: 200 }} allowClear showSearch
                      popupMatchSelectWidth={false} filterOption={filterControlOption}
                      placeholder="从控件清单选择" options={controlOptions} value={null}
                      onChange={(v) => {
                        const loc = v ? pickLocator(String(v)) : undefined;
                        if (loc) updateStep(idx, (s) => { (s.assertions ||= [])[ai].target = loc; });
                      }} />
                    {NEEDS_EXPECTED.has(String(a.type)) && <Input size="small" style={{ width: 160 }}
                      placeholder="expected 期望值" value={String(a.expected ?? "")}
                      onChange={(e) => updateStep(idx, (s) => {
                        (s.assertions ||= [])[ai].expected = e.target.value;
                      })} />}
                    <Button size="small" type="text" danger title="删除断言"
                      onClick={() => updateStep(idx, (s) => { s.assertions?.splice(ai, 1); })}>
                      <DeleteOutlined /></Button>
                  </div>
                ))}
                <Button size="small" type="dashed" icon={<PlusOutlined />}
                  onClick={() => updateStep(idx, (s) => {
                    (s.assertions ||= []).push({ type: "exists", target: { strategy: "res-id", value: "" } });
                  })} style={{ marginTop: 4 }}>断言</Button>
              </div>
            );
          })}
          <Button block type="dashed" icon={<PlusOutlined />} onClick={addStep}>添加步骤</Button>
          <div style={{ marginTop: 8, color: "#8a919f", fontSize: 12 }}>
            试跑 = 保存后在默认 Android 设备上单条执行；报告「编辑此步」会定位到对应步骤卡片。
          </div>
        </>
      ) : (
        <Alert type="error" showIcon message="用例 JSON 无法解析，请切到 JSON 模式修复语法" />
      )}
    </Drawer>
  );
}
