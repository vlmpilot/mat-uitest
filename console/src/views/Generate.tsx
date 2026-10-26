import { Alert, AutoComplete, Badge, Button, Card, Input, Modal, Space, Steps, Tabs, Tag, Upload, message } from "antd";
import { EditOutlined, FileTextOutlined, InboxOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { useState, useEffect } from "react";
import { StateResp, postJson, getReadiness, getJob, startIndex, getAppNote, saveAppNote } from "../api";
import JobMonitor from "../components/JobMonitor";
import PageHeader from "../components/PageHeader";

// 生成与导入(CASE-07):NL 一句话生成 + 手工用例批量转换。
// 交互收敛:包名此前「下拉选 + 手动输」两个控件绑定同一值,语义含混——统一为一个
// AutoComplete(可选历史、可自由输入);导入流程用 Steps 显式表达「上传→预览→转换」,
// 按钮不再靠 ①② 前缀自我解释。任务视图复用 JobMonitor(时间线 + 中止,与执行页同款)。
function b64(file: File): Promise<string> {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1] || "");
    r.onerror = () => rej(new Error("读取失败"));
    r.readAsDataURL(file);
  });
}

// AI 接入 = 自带 API Key（local）时不设上限：入口的「消耗 AI 额度」提示只对**官方代理**
// 有意义，其余隐藏（2026-10-24：这是接入维度，与「使用方式」是两回事）
const unlimitedOf = (state: StateResp) =>
  state.ai_mode === "local" || state.usage.gen_limit == null;

function PackagePicker({ state, value, onChange, placeholder }: {
  state: StateResp; value: string; onChange: (v: string) => void; placeholder: string }) {
  const history = Array.from(new Set(Object.values(state.caseMeta).map((m) => m.app).filter(Boolean)));
  return (
    <AutoComplete value={value} onChange={onChange} style={{ width: 300 }}
      placeholder={placeholder} allowClear
      options={history.map((a) => ({ value: a as string, label: a as string }))} />
  );
}

// 应用说明（app_notes/<包名>.md）：负责人对被测应用的补充——黑/白名单、禁止操作、
// 账号等；生成与润色时后端自动注入 prompt。按钮上的小圆点 = 该应用已有说明在生效。
function AppNoteButton({ pkg }: { pkg: string }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [hasNote, setHasNote] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!pkg) { setHasNote(false); return; }
    let alive = true;
    getAppNote(pkg)
      .then((d) => { if (alive) setHasNote(!!d.notes.trim()); })
      .catch(() => {});
    return () => { alive = false; };
  }, [pkg]);
  const openEditor = () => {
    setOpen(true);
    getAppNote(pkg).then((d) => setText(d.notes)).catch(() => setText(""));
  };
  const save = async () => {
    setSaving(true);
    try {
      await saveAppNote(pkg, text);
      setHasNote(!!text.trim());
      message.success("已保存;此后生成/润色将自动带上这份说明");
      setOpen(false);
    } catch (e) { message.error(String(e)); }
    finally { setSaving(false); }
  };
  return (
    <>
      <Badge dot={hasNote} offset={[-2, 2]}>
        <Button icon={<FileTextOutlined />} disabled={!pkg} onClick={openEditor}>应用说明</Button>
      </Badge>
      <Modal title={`应用说明 — ${pkg || ""}`} open={open} onCancel={() => setOpen(false)}
        onOk={save} okText="保存" cancelText="取消" confirmLoading={saving} width={560}
        okButtonProps={{ disabled: text.length > 4000 }}>
        <Input.TextArea rows={8} value={text} onChange={(e) => setText(e.target.value)}
          showCount maxLength={4000}
          placeholder={"对该应用的测试约束与补充,生成用例时 AI 会看到并遵守,例如:\n黑名单:不要点「提交订单/支付」,会真实下单\n白名单:搜索、浏览、加购物车都可以\n账号:138xxxx / 密码见凭据「测试号」"} />
        <div style={{ color: "#8a919f", fontSize: 12, marginTop: 8 }}>
          保存在工作区 app_notes/{pkg || "<包名>"}.md,可直接用编辑器改;清空并保存即删除。
        </div>
      </Modal>
    </>
  );
}

function GenTab({ state, onCasesChanged }: { state: StateResp; onCasesChanged: () => void }) {
  const [nl, setNl] = useState("");
  const [pkg, setPkg] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // AI 润色(P1.5,设计 §7):可选增强——结果回填输入框由用户确认,不静默替换也不自动接续
  // 生成;prevNl 保留润色前原文,支持一键撤销
  const [polishing, setPolishing] = useState(false);
  const [prevNl, setPrevNl] = useState<string | null>(null);
  const unlimited = unlimitedOf(state);
  // 图谱准备度（2026-10-17）：只在**选了包**之后取一次——没包谈不上准备度。
  // 软提示而非闸门：生成照常可点，用户自己决定先补图谱还是先跑。
  const [ready, setReady] = useState<{ text: string; hint: string | null; has_index: boolean } | null>(null);
  const refreshReady = (target: string) => {
    getReadiness(target)
      .then((r) => setReady({ text: r.text, hint: r.hint, has_index: r.has_index }))
      .catch(() => setReady(null));   // 取不到就静默不显示（别用错误打断生成）
  };
  useEffect(() => {
    if (!pkg) { setReady(null); return; }
    refreshReady(pkg);
  }, [pkg]);   // eslint-disable-line react-hooks/exhaustive-deps
  // 就地构建图谱（2026-10-25）：无图谱的提示不再把人支去「页面复核」页找按钮——
  // action 直接放提示条上，一键发起 index job、就地轮询到完成再刷新准备度。
  const [building, setBuilding] = useState(false);
  const buildIndex = async () => {
    if (!pkg) return;
    setBuilding(true);
    try {
      const { job_id } = await startIndex({ package: pkg, refresh: true });
      message.info("正在构建页面图谱(需要一台在线设备,约 1-2 分钟)…");
      for (let i = 0; i < 400; i++) {           // 上限 ~10 分钟（与看门狗一致）
        const job = await getJob(job_id);
        if (job.status !== "running") {
          if (job.status === "done") { message.success(job.summary || "页面图谱构建完成"); refreshReady(pkg); }
          else message.error(job.error || `任务${job.status}`);
          break;
        }
        await new Promise((r) => setTimeout(r, 1500));
      }
    } catch (e) { message.error(String(e)); }
    finally { setBuilding(false); }
  };

  const launch = async () => {
    setBusy(true);
    try {
      // 必须走 postJson(内部注入 operator)——裸 fetch 不带名字,人数闸按匿名 400
      const d = await postJson<{ job_id: string }>("/api/gen", { nl, package: pkg });
      setJobId(d.job_id);
    } catch (e) { message.error(String(e)); setBusy(false); }
  };

  const polish = async () => {
    setPolishing(true);
    try {
      const d = await postJson<{ text: string }>("/api/polish", { nl, package: pkg });
      setPrevNl(nl);
      setNl(d.text);
      message.success(unlimited ? "已润色并回填,确认无误后再生成" : "已润色并回填(免费),确认无误后再生成");
    } catch (e) { message.error(String(e)); }
    finally { setPolishing(false); }
  };

  return (
    <>
      {!state.gen_ready && <Alert type="warning" showIcon style={{ marginBottom: 12 }}
        message="AI 未配置" description="在设置页按指引配置 .env(官方代理 3 行或自带 API Key)后即可生成。" />}
      <Input.TextArea rows={3} value={nl} onChange={(e) => setNl(e.target.value)}
        placeholder='一句话描述场景,如:登录后搜索"蓝牙耳机",验证结果页出现商品' />
      <Space style={{ margin: "12px 0" }} wrap>
        <PackagePicker state={state} value={pkg} onChange={setPkg}
          placeholder="被测应用包名(可从历史选择或直接输入)" />
        <AppNoteButton pkg={pkg} />
        <Button icon={<EditOutlined />} disabled={!nl} loading={polishing} onClick={polish}>AI 润色</Button>
        <Button type="primary" icon={<ThunderboltOutlined />} loading={busy}
          disabled={!nl || !pkg} onClick={launch}>生成用例</Button>
        {prevNl !== null && (
          <Button size="small" onClick={() => { setNl(prevNl); setPrevNl(null); }}>撤销润色</Button>
        )}
      </Space>
      {jobId && <JobMonitor jobId={jobId} onFinished={() => { setBusy(false); onCasesChanged(); }} />}
      {ready?.hint && (
        <Alert type={ready.has_index ? "warning" : "info"} showIcon style={{ margin: "12px 0" }}
          message={ready.text} description={ready.hint}
          action={!ready.has_index && (
            <Button size="small" type="primary" loading={building} onClick={buildIndex}>
              构建页面图谱
            </Button>
          )} />
      )}
      {ready && !ready.hint && (
        <div style={{ color: "#8c8c8c", fontSize: 12, margin: "10px 0" }}>{ready.text}（全部已复核）</div>
      )}
      <Alert type="info" style={{ marginBottom: 12 }}
        message="同页多个入口会自动拆分为多个用例文件;完成自动写入 cases/,可到「用例」页编辑后运行。" />
    </>
  );
}

function ImportTab({ state, onCasesChanged }: { state: StateResp; onCasesChanged: () => void }) {
  const [upload, setUpload] = useState<{ name: string; content_b64: string } | null>(null);
  const [pkg, setPkg] = useState("");
  const [preview, setPreview] = useState<{ count: number; titles: string[]; template_label?: string } | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const step = jobId ? 2 : preview ? 1 : upload ? 0 : 0;

  return (
    <>
      <Steps size="small" style={{ marginBottom: 16 }} current={step} items={[
        { title: "上传文件", description: upload ? upload.name : "Excel / CSV / Markdown / XMind / TXT,≤5MB" },
        { title: "预览解析", description: preview ? `解析到 ${preview.count} 条` : "确认用例标题与模板" },
        { title: "转换为用例", description: "AI 转换并写入 cases/ 草稿" },
      ]} />
      <Upload.Dragger maxCount={1} accept=".csv,.xlsx,.xlsm,.md,.markdown,.txt,.xmind"
        showUploadList={false}
        beforeUpload={async (file) => {
          try { setUpload({ name: file.name, content_b64: await b64(file) }); setPreview(null); setJobId(null); }
          catch (e) { message.error(String(e)); }
          return false;  // 手动控制,不自动上传
        }}>
        <p style={{ margin: 8, color: "#697080" }}><InboxOutlined style={{ fontSize: 28, color: "#4F5BFF" }} /></p>
        <p style={{ margin: 8 }}>点击或拖入用例文件</p>
        <p style={{ margin: 8, color: "#8a919f", fontSize: 12 }}>Excel / CSV / Markdown / XMind / TXT,≤5MB</p>
      </Upload.Dragger>
      <Space style={{ margin: "12px 0" }} wrap>
        <PackagePicker state={state} value={pkg} onChange={setPkg} placeholder="被测 App 包名" />
        <Button disabled={!upload || !pkg} onClick={async () => {
          try { setPreview(await postJson("/api/import/preview", { ...upload, package: pkg })); }
          catch (e) { message.error(String(e)); }
        }}>预览解析</Button>
        <Button type="primary" disabled={!upload || !pkg || !preview} loading={busy} onClick={async () => {
          setBusy(true);
          try { const d = await postJson<{ job_id: string }>("/api/import/convert", { ...upload, package: pkg }); setJobId(d.job_id); }
          catch (e) { message.error(String(e)); setBusy(false); }
        }}>转换为用例</Button>
      </Space>
      {jobId && <JobMonitor jobId={jobId} onFinished={() => { setBusy(false); onCasesChanged(); }} />}
      {preview && (
        <Card size="small" title={`解析到 ${preview.count} 条(模板:${preview.template_label || "-"})`} style={{ marginBottom: 12 }}>
          {preview.titles.map((t2, i) => <Tag key={i}>{t2}</Tag>)}
        </Card>
      )}
      <Alert type="info" style={{ marginBottom: 12 }}
        message="产物按 A/B/C 置信度分级写入 cases/ 草稿;批次报告在 artifacts/imports/。" />
    </>
  );
}

export default function Generate({ state, onCasesChanged }: {
  state: StateResp; onCasesChanged: () => void }) {
  return (
    <>
      <PageHeader title="生成 & 导入"
        desc="两条产线:一句话让 AI 生成结构化用例;或把既有 Excel/XMind 用例批量转换。" />
      <Card>
        <Tabs items={[
          { key: "gen", label: "AI 生成", children: <GenTab state={state} onCasesChanged={onCasesChanged} /> },
          { key: "import", label: "批量导入", children: <ImportTab state={state} onCasesChanged={onCasesChanged} /> },
        ]} />
      </Card>
    </>
  );
}
