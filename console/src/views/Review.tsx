import {
  Alert, Badge, Button, Card, Empty, Input, Modal, Select, Space, Table, Tag, Tooltip, Typography, message,
} from "antd";
import { CheckOutlined, ReloadOutlined, SaveOutlined, UndoOutlined } from "@ant-design/icons";
import { useEffect, useMemo, useRef, useState } from "react";
import { getJob, getReview, ReviewPage, ReviewResp, saveReview, startDescribe, startIndex, StateResp } from "../api";
import PageHeader from "../components/PageHeader";

// 页面复核:把图谱里的**页面标题**交给用户过一遍。
//
// 为什么要这个页:图谱标题原先只有 AI 一个来源(`mat describe`),而 AI 起名的
// **粒度不可控**——同一 Activity 下的一批同类页会被按各自的内容词分别命名
// (一批搜索结果页被叫成「A搜索页」「B搜索页」…),而用户比 AI 更懂自己的 App。
// 此前没有任何入口让用户把这件事说出来。
//
// 三个刻意的设计:
//  · 改过的标题标 `manual`,此后 `describe` **不会覆盖**;
//  · 保存是**幂等**的(重复保存不会重复改);
//  · 找不到的页面标识会**逐条列出来**——图谱重跑后标识会变,静默丢弃等于用户白复核。

// 自动 AI 填充的每包最大批数（1 批 ≤50 页 ⇒ 最多自动补 200 页，与后端 limit 上限对齐）
const AUTO_BATCHES = 4;

export default function Review({ state }: { state: StateResp }) {
  const indexes = state.indexes ?? [];
  const [pkg, setPkg] = useState<string>(indexes[0]?.package ?? "");
  const [data, setData] = useState<ReviewResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});   // page_key → 用户改成的标题
  const [draftSum, setDraftSum] = useState<Record<string, string>>({}); // page_key → 用户改成的描述
  const [missing, setMissing] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const [graphBusy, setGraphBusy] = useState<string | null>(null);
  // 批量复核（2026-10-25）：选中页按**当前值**落章成 manual——认可 AI 的标题/描述
  // 不必挨个进输入框改一个字。走既有保存接口（幂等；结论同样落 review/<包名>.json，
  // 重建图谱后可重放恢复）。空标题页跳过：没起名谈不上认可，升级了 describe 会永不补名。
  const [selected, setSelected] = useState<string[]>([]);   // page_key 集
  const [confirming, setConfirming] = useState(false);
  const confirmSelected = () => {
    if (!data || !selected.length) return;
    const corrections: Record<string, string> = {};
    const summaryCorrections: Record<string, string> = {};
    let skipped = 0;
    for (const key of selected) {
      const p = pages.find((x) => x.page_key === key);
      if (!p) continue;
      const title = (draft[key] ?? p.title).trim();
      if (!title) { skipped += 1; continue; }   // 还没起名的页确认无意义
      corrections[key] = title;
      const summary = (draftSum[key] ?? p.summary ?? "").trim();
      if (summary) summaryCorrections[key] = summary;
    }
    if (!Object.keys(corrections).length) {
      message.info("选中的页都还没有标题——先起名（或让 AI 补全）再确认。");
      return;
    }
    setConfirming(true);
    saveReview(data.package, corrections, Object.keys(summaryCorrections).length ? summaryCorrections : undefined)
      .then((r) => {
        setMissing(r.missing ?? []);
        message.success(`已确认 ${Object.keys(corrections).length} 页（新认可 ${r.promoted ?? 0} 页）`
          + (skipped ? ` · 跳过 ${skipped} 页（还没有标题）` : "")
          + "——此后 AI 不再覆盖这些标题与描述");
        setSelected([]);
        setDraft({}); setDraftSum({});
        load(data.package);
      })
      .catch((e) => message.error(String(e)))
      .finally(() => setConfirming(false));
  };
  // 自动 AI 填充（2026-10-12）：打开复核页/切包时，对还没有 AI 描述且非人工命名的
  // 页面自动跑「AI 补全」——免去单独点按钮。**按包记已自动跑的批数**（Map），
  // 一批 ≤50 页；一批补完刷新后若仍有空缺会自动续批，最多 AUTO_BATCHES 批
  // （4×50=200 页覆盖最大图谱）——防「永远补不满」导致无限循环扣额度。
  // 每批是 1 次 AI 调用（口径见概览页「AI 生成」卡；后端 T53b：失败不扣）；
  // 人工命名过的页永不覆盖。超出自动批数的空缺可点「刷新」手动续补。
  const autoDescribed = useRef<Map<string, number>>(new Map());

  // 图谱维护:重建索引(mat index)/AI 补全标题(mat describe)的界面入口。
  // 任务在执行中心也有 tab;这里就地轮询到结束并刷新复核数据——不强迫用户跳页。
  const runGraphJob = async (kind: "index" | "describe", batch = 1) => {
    if (!pkg) return;
    setGraphBusy(kind);
    try {
      let job_id: string;
      if (kind === "index") {
        job_id = (await startIndex({ package: pkg, refresh: true })).job_id;
        message.info("正在重建页面索引…");
      } else {
        const r = await startDescribe({ package: pkg });
        // 全部已填充：正常状态（此前 400 误弹红色 Error，2026-10-13 用户反馈）
        if (r.noop) {
          message.info(r.message || "所有页面都已有标题或描述，无需 AI 补全");
          return;
        }
        job_id = r.job_id;
        // 额度口径收敛到概览页「AI 生成」卡（2026-10-26）——此处只报进度
        message.info(batch > 1
          ? `正在 AI 补全（第 ${batch} 批）…`
          : "正在 AI 补全页面标题与功能描述…");
      }
      for (let i = 0; i < 400; i++) {           // 上限 ~10 分钟（与看门狗一致）
        const job = await getJob(job_id);
        if (job.status !== "running") {
          if (job.status === "done") { message.success(job.summary || "完成"); load(pkg); }
          else message.error(job.error || `任务${job.status}`);
          break;
        }
        await new Promise((r) => setTimeout(r, 1500));
      }
    } catch (e) {
      // 兜底（竞态：job 排队期间图谱被改空）：正常状态不弹红色 error
      const msg = String(e);
      if (!msg.includes("没有可描述")) message.error(msg);
    } finally { setGraphBusy(null); }
  };

  const load = (target: string) => {
    if (!target) { setData(null); return; }
    setLoading(true);
    setDraft({}); setDraftSum({}); setMissing([]); setSelected([]);
    getReview(target)
      .then((d) => {
        setData(d);
        // 默认 AI 填一遍：触发口径与后端 _targets 一致——跳过人工标题/人工描述页
        // （它们满足 !=="ai" 但同样不许重算，漏判会在全填充时误触发 400）；
        // 一批 ≤50 页，补完刷新后仍有空缺则自动续批（每包最多 4 批防无限扣额度）
        const used = autoDescribed.current.get(target) ?? 0;
        if (used < AUTO_BATCHES && !graphBusy
          && d.pages.some((p) => p.title_source !== "manual"
            && p.summary_source !== "ai" && p.summary_source !== "manual")) {
          autoDescribed.current.set(target, used + 1);
          void runGraphJob("describe", used + 1);
        }
      })
      .catch((e) => { setData(null); message.error(String(e)); })
      .finally(() => setLoading(false));
  };
  useEffect(() => { if (pkg) load(pkg); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [pkg]);

  const pages = data?.pages ?? [];
  const originalOf = (p: ReviewPage) => p.title;
  const originalSumOf = (p: ReviewPage) => p.summary ?? "";
  const changed = useMemo(
    () => pages.filter((p) => (draft[p.page_key] !== undefined
      && draft[p.page_key].trim() !== originalOf(p).trim())
      || (draftSum[p.page_key] !== undefined
      && draftSum[p.page_key].trim() !== originalSumOf(p).trim())),
    // originalOf/originalSumOf 是纯函数,依赖 pages/draft/draftSum 即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pages, draft, draftSum]);

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return pages;
    return pages.filter((p) =>
      (draft[p.page_key] ?? p.title).toLowerCase().includes(needle)
      || (draftSum[p.page_key] ?? p.summary).toLowerCase().includes(needle)
      || p.activity.toLowerCase().includes(needle));
  }, [pages, q, draft, draftSum]);

  const doSave = () => {
    if (!data || !changed.length) { message.info("还没有修改。"); return; }
    // 标题与描述分流：只提交真正有改的那部分（后端对空描述写回即清空，不可误传）
    const corrections: Record<string, string> = {};
    const summaryCorrections: Record<string, string> = {};
    changed.forEach((p) => {
      if (draft[p.page_key] !== undefined
        && draft[p.page_key].trim() !== originalOf(p).trim())
        corrections[p.page_key] = draft[p.page_key];
      if (draftSum[p.page_key] !== undefined
        && draftSum[p.page_key].trim() !== originalSumOf(p).trim())
        summaryCorrections[p.page_key] = draftSum[p.page_key];
    });
    setSaving(true);
    saveReview(data.package, corrections,
        Object.keys(summaryCorrections).length ? summaryCorrections : undefined)
      .then((r) => {
        setMissing(r.missing ?? []);
        message.success(`已保存:改标题 ${r.applied} 页 · 改描述 ${r.summary_applied ?? 0} 页`
          + ` · 已是目标值 ${(r.unchanged ?? 0) + (r.summary_unchanged ?? 0)} 页`
          + (r.missing?.length ? ` · 找不到 ${r.missing.length} 页` : ""));
        load(data.package);
      })
      .catch((e) => message.error(String(e)))
      .finally(() => setSaving(false));
  };

  return (
    <>
      <PageHeader title="页面复核"
        desc={<>页面标题与功能描述由 <b>AI 自动补全</b>（打开本页时对未命名的页面分批跑，
          {"每批 ≤50 页，最多自动 4 批；更大的图谱点「刷新」续补）"}，
          两者都<b>可人工修改</b>——人工写的永不被 AI 覆盖，并注入 AI 生成/转换的 prompt。
          AI 起名的粒度可能不对——同一类页被拆成很多名字;
          这里可以逐页改名,<b>你比 AI 更懂你的 App</b>,把同类页改成同一个名字价值最大。</>}
        extra={pkg ? (
          <Space>
            <Button size="small" loading={graphBusy === "index"}
              onClick={() => Modal.confirm({
                title: "重建页面索引？",
                content: "将重新遍历 App 页面并重建页面图谱：AI 自动生成的页面标题会更新，人工复核过的标题（manual）保留不变。"
                  + "需要一台在线 Android 设备（默认自动选择，不要求指定执行配置）；执行期间会占用该设备并打开被测 App。"
                  + "耗时视页面数量而定，任务进度可在执行中心跟踪。",
                okText: "重建", cancelText: "取消",
                // 不把轮询 Promise 交给 onOk（2026-10-12 用户报「重建按钮一直转圈」）：
                // job 可能因设备锁排队/长爬取 running 数分钟，onOk 挂着 Promise ⇒ 弹窗
                // 转圈到 10 分钟上限。改为弹窗立即关闭，进度交给页面按钮 loading
                // （graphBusy）与执行中心跟踪——与「AI 补全标题」按钮同款交互。
                onOk: () => { void runGraphJob("index"); },
              })}>重建页面索引</Button>
            <Button size="small" icon={<ReloadOutlined />} onClick={() => load(pkg)}>刷新</Button>
          </Space>
        ) : undefined} />

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap size={12}>
          <span style={{ color: "#697080", fontSize: 13 }}>被测应用</span>
          <Select style={{ minWidth: 280 }} value={pkg || undefined} onChange={setPkg}
            placeholder={indexes.length ? "选择有页面图谱的应用" : "还没有页面图谱"}
            options={indexes.map((i) => ({
              value: i.package,
              label: `${i.package}（${i.pages} 页）`,
            }))} />
          <Button icon={<ReloadOutlined />} onClick={() => load(pkg)} disabled={!pkg || loading}>重新载入</Button>
          <Input.Search allowClear placeholder="按标题 / Activity / 描述筛选" style={{ width: 260 }}
            value={q} onChange={(e) => setQ(e.target.value)} />
          <Badge count={changed.length} showZero
            style={{ backgroundColor: changed.length ? "#4F5BFF" : "#d9d9d9" }}>
            <Button type="primary" icon={<SaveOutlined />} loading={saving}
              disabled={!changed.length} onClick={doSave}>保存修改</Button>
          </Badge>
          <Tooltip title="认可选中页的当前标题与描述,标为已复核——此后 AI 补全与重建索引都不会覆盖它们">
            <Button icon={<CheckOutlined />} loading={confirming}
              disabled={!selected.length} onClick={confirmSelected}>
              批量复核{selected.length ? `（${selected.length}）` : ""}
            </Button>
          </Tooltip>
          <Tooltip title="把所有输入框恢复成图谱里的当前值(不写盘)">
            <Button icon={<UndoOutlined />} disabled={!changed.length}
              onClick={() => { setDraft({}); setDraftSum({}); }}>撤销</Button>
          </Tooltip>
        </Space>
        {data && (
          <div style={{ marginTop: 10, color: "#697080", fontSize: 13 }}>
            共 <b>{pages.length}</b> 页 · 人工改过标题 <b>{data.manual}</b> 页 ·
            人工写过描述 <b>{pages.filter((p) => p.summary_source === "manual").length}</b> 页 ·
            本次待保存 <b>{changed.length}</b> 页
            <Typography.Text type="secondary" style={{ marginLeft: 12, fontSize: 12 }}>
              复核结论存在 <Typography.Text code style={{ fontSize: 12 }}>{data.review_path}</Typography.Text>
              （与图谱分开——图谱重跑会重建,结论不会丢）
            </Typography.Text>
          </div>
        )}
      </Card>

      {missing.length > 0 && (
        <Alert type="warning" showIcon style={{ marginBottom: 16 }}
          message={`有 ${missing.length} 个页面标识在图谱里找不到（图谱可能被重跑过）`}
          description={<Typography.Text style={{ fontSize: 12 }}>
            {missing.slice(0, 8).join("、")}{missing.length > 8 ? ` …另有 ${missing.length - 8} 个` : ""}
          </Typography.Text>} />
      )}

      {!pkg && <Card><Empty description="还没有页面图谱——选择应用后点右上角「重建页面索引」,或到「遍历探索」页真机遍历(还能发现崩溃/白屏)" /></Card>}
      {pkg && !loading && pages.length === 0 && <Card><Empty description="这个应用的图谱里还没有页面" /></Card>}

      {pkg && pages.length > 0 && (
        <Table size="small" rowKey="page_key" loading={loading} dataSource={rows}
          pagination={{ pageSize: 50, showSizeChanger: true, showTotal: (t) => `共 ${t} 页` }}
          rowSelection={{ selectedRowKeys: selected,
            onChange: (keys) => setSelected(keys as string[]) }}
          columns={[
            { title: "截图", dataIndex: "screenshot_url", width: 96,
              render: (u: string) => (u
                ? <a href={u} target="_blank" rel="noreferrer">
                    <img src={u} alt="" style={{ width: 76, borderRadius: 4, border: "1px solid #f0f0f0" }} />
                  </a>
                : <span style={{ color: "#b6bac3", fontSize: 12 }}>无</span>) },
            { title: "页面标题（可改）", dataIndex: "title_key", width: 320,
              render: (_: unknown, p: ReviewPage) => {
                const val = draft[p.page_key] ?? p.title;
                const dirty = val.trim() !== p.title.trim();
                return (
                  <Space direction="vertical" size={2} style={{ width: "100%" }}>
                    <Input value={val} placeholder="（还没有标题，可在这里起名）"
                      status={dirty ? "warning" : undefined}
                      onChange={(e) => setDraft({ ...draft, [p.page_key]: e.target.value })} />
                    <Space size={4}>
                      {p.title_source === "manual"
                        ? <Tag color="green" bordered={false}>人工</Tag>
                        : p.title_source === "ai"
                          ? <Tag color="blue" bordered={false}>AI</Tag>
                          : <Tag bordered={false}>未命名</Tag>}
                      {dirty && <Tag color="orange" bordered={false}>已修改</Tag>}
                    </Space>
                  </Space>
                );
              } },
            { title: "Activity / 层级", dataIndex: "activity", width: 200,
              render: (a: string, p: ReviewPage) => (
                <Typography.Text style={{ fontSize: 12 }}>
                  {a}<span style={{ color: "#b6bac3" }}> · depth {p.depth ?? "—"}</span>
                </Typography.Text>) },
            { title: "功能描述（可改）", dataIndex: "summary", width: 360,
              render: (_: unknown, p: ReviewPage) => {
                const val = draftSum[p.page_key] ?? p.summary;
                const dirty = val.trim() !== originalSumOf(p).trim();
                return (
                  <Space direction="vertical" size={2} style={{ width: "100%" }}>
                    <Input value={val} placeholder="（还没有描述，可补一句这页是干什么的）"
                      status={dirty ? "warning" : undefined}
                      onChange={(e) => setDraftSum({ ...draftSum, [p.page_key]: e.target.value })} />
                    <Space size={4}>
                      {p.summary_source === "manual"
                        ? <Tag color="green" bordered={false}>人工</Tag>
                        : p.summary_source === "ai"
                          ? <Tag color="blue" bordered={false}>AI</Tag>
                          : <Tag bordered={false}>无描述</Tag>}
                      {dirty && <Tag color="orange" bordered={false}>已修改</Tag>}
                    </Space>
                  </Space>
                );
              } },
          ]} />
      )}
    </>
  );
}
