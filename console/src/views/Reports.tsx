import { Button, Card, Empty, Input, message, Progress, Select, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { MenuProps } from "antd";
import { useEffect, useMemo, useState } from "react";
import { getJob, getShare, RunInfo, ShareResp, startDiagnose, StateResp } from "../api";
import CalibrateDrawer from "../components/CalibrateDrawer";
import ExploreCalibrateDrawer from "../components/ExploreCalibrateDrawer";
import PageHeader from "../components/PageHeader";

// 报告页重构(「内容完善合理、分类清晰、数据准确完备」):
// - 统计区:总数/通过率 + 失败五分类分布(此前只有通过/失败二值);
// - 筛选:状态 / 应用 / 关键字;
// - 表格:失败分类明细列 + 标记列(已归因/已校准/人工改判)+ 行内操作(AI 归因/校准);
// - 数据口径如实标注「最近 50 次」(后端 list_runs 的保留上限)。
const STATUS_LABEL: Record<string, { text: string; color: string }> = {
  passed: { text: "通过", color: "green" },
  failed: { text: "失败", color: "red" },
  cancelled: { text: "已中止", color: "orange" },
};

const CAT_LABEL: Record<string, { text: string; color: string }> = {
  "app-crash": { text: "产品bug", color: "volcano" },
  "assert-fail": { text: "断言失败", color: "gold" },
  "locate-fail": { text: "定位失败", color: "geekblue" },
  "env-failure": { text: "环境问题", color: "cyan" },
  unknown: { text: "待归因", color: "default" },
};

// 套件批次归组行（发起人 2026-10-12 报告聚合）：同批条目折叠为一行汇总，展开看各条明细；
// 单条运行无 batch 字段，照旧平铺——旧产物（无 batch）天然兼容。
type BatchRow = {
  key: string; isBatch: true; suite: string; items: RunInfo[]; children: RunInfo[];
  passedN: number; cancelledN: number; failed_categories: Record<string, number>;
  rate?: number; finished_at?: string;
};
// 探索报告行（发起人 2026-10-12：报告列表一直看不到探索任务）——
// state.explores（后端保留最近 10 次）混入同一张表：带「探索」标签、链到探索报告
type ExploreRow = { key: string; isExplore: true; id: string; package: string;
  steps: number; findings: number; calibrated?: boolean; finished_at?: string };
type ReportRow = RunInfo | BatchRow | ExploreRow;
const isBatchRow = (r: ReportRow): r is BatchRow => (r as BatchRow).isBatch === true;
const isExploreRow = (r: ReportRow): r is ExploreRow => (r as ExploreRow).isExplore === true;

function ShareCard() {
  const [share, setShare] = useState<ShareResp | null>(null);
  useEffect(() => { getShare().then(setShare).catch(() => {}); }, []);
  if (!share) return null;
  return (
    <Card size="small" style={{ marginBottom: 16 }}
      title="报告分享（局域网 · 数据不出内网）">
      {share.shared ? (
        <div style={{ fontSize: 13 }}>
          {share.hint}
          <div style={{ marginTop: 8 }}>
            {share.urls.map((u) => (
              <Typography.Text key={u} code copyable style={{ display: "block", fontSize: 12 }}>{u}</Typography.Text>))}
          </div>
        </div>
      ) : (
        <div style={{ fontSize: 13, color: "#697080" }}>
          {share.hint}
          <div style={{ marginTop: 6 }}>
            <Typography.Text code style={{ fontSize: 12 }}>mat console --share</Typography.Text>
          </div>
        </div>
      )}
    </Card>
  );
}

export default function Reports({ state }: { state: StateResp }) {
  const runs = state.runs;
  const [diagnosing, setDiagnosing] = useState<string | null>(null);
  const [calibRun, setCalibRun] = useState<string | null>(null);
  const [calibExplore, setCalibExplore] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string | null>(null);
  const [appFilter, setAppFilter] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");

  const total = runs.length;
  const passed = runs.filter((r) => r.status === "passed").length;
  const cancelled = runs.filter((r) => r.status === "cancelled").length;
  const failed = total - passed - cancelled;
  const avgRate = total ? Math.round((runs.reduce((a, r) => a + (r.rate ?? 0), 0) / total) * 100) : 0;
  const catTotal: Record<string, number> = {};
  for (const r of runs)
    for (const [k, v] of Object.entries(r.failed_categories || {}))
      catTotal[k] = (catTotal[k] || 0) + v;

  const apps = Array.from(new Set(
    [...runs.map((r) => r.package), ...state.explores.map((e) => e.package)].filter(Boolean),
  )) as string[];
  const filtered = useMemo(() => runs.filter((r) => {
    if (statusFilter && r.status !== statusFilter) return false;
    if (appFilter && r.package !== appFilter) return false;
    if (keyword.trim()) {
      const kw = keyword.trim().toLowerCase();
      const hay = `${r.case_name || ""} ${r.case_id || ""} ${r.package || ""} ${r.dir}`.toLowerCase();
      if (!hay.includes(kw)) return false;
    }
    return true;
  }), [runs, statusFilter, appFilter, keyword]);

  // 探索报告行：同套筛选（无状态语义——选了状态筛选即隐藏）；完成后端记 generated_at
  const exploreRows: ExploreRow[] = useMemo(() => state.explores
    .filter((e) => e.id)
    .map((e) => ({ key: `explore:${e.id}`, isExplore: true as const, id: e.id!,
      package: e.package || "", steps: e.steps || 0, findings: e.findings || 0,
      calibrated: e.calibrated, finished_at: e.finished_at })), [state.explores]);
  // 报告口径总数（发起人 2026-10-13）：套件批次算 1 份报告，组内单条不计数
  const totalReports = useMemo(() => {
    const bids = new Set(runs.filter((r) => r.batch).map((r) => r.batch!.id));
    return runs.filter((r) => !r.batch).length + bids.size + exploreRows.length;
  }, [runs, exploreRows]);
  const filteredExplores = useMemo(() => exploreRows.filter((e) => {
    if (statusFilter) return false;
    if (appFilter && e.package !== appFilter) return false;
    if (keyword.trim()) {
      const kw = keyword.trim().toLowerCase();
      if (!`${e.package} ${e.id}`.toLowerCase().includes(kw)) return false;
    }
    return true;
  }), [exploreRows, statusFilter, appFilter, keyword]);

  // 批次归组（发起人 2026-10-12）：有 batch 的条目按批次 id 折叠为汇总行（children 展开），
  // 筛选条件先作用于条目再重建组——组内全部被筛掉时该组不出现
  const rows: ReportRow[] = useMemo(() => {
    const singles: RunInfo[] = [];
    const batches = new Map<string, RunInfo[]>();
    for (const r of filtered) {
      if (r.batch) {
        const arr = batches.get(r.batch.id);
        if (arr) arr.push(r); else batches.set(r.batch.id, [r]);
      } else singles.push(r);
    }
    const out: ReportRow[] = singles;
    for (const [id, items] of batches) {
      items.sort((a, b) => (b.finished_at || "").localeCompare(a.finished_at || ""));
      const cats: Record<string, number> = {};
      for (const it of items)
        for (const [k, v] of Object.entries(it.failed_categories || {}))
          cats[k] = (cats[k] || 0) + v;
      const rates = items.filter((x) => x.rate != null);
      out.push({
        key: id, isBatch: true, suite: items[0].batch!.suite, items,
        passedN: items.filter((x) => x.status === "passed").length,
        cancelledN: items.filter((x) => x.status === "cancelled").length,
        failed_categories: cats,
        rate: rates.length
          ? rates.reduce((a, x) => a + (x.rate ?? 0), 0) / rates.length : undefined,
        finished_at: items.map((x) => x.finished_at || "").sort().at(-1) || undefined,
        children: items,
      });
    }
    return [...out, ...filteredExplores];
  }, [filtered, filteredExplores]);

  const diagnose = async (dir: string) => {
    setDiagnosing(dir);
    try {
      const { job_id } = await startDiagnose(dir);
      message.info("正在 LLM 归因…完成后报告会挂上归因卡");
      for (let i = 0; i < 200; i++) {
        const job = await getJob(job_id);
        if (job.status !== "running") {
          if (job.status === "done") {
            // 效果直达（发起人 2026-10-12 反馈「只多了个标签看不到效果」）：归因结果
            // 落在报告的归因卡里——完成提示直接给报告链接，不用再回列表找
            message.success(
              <span>归因完成——
                <a href={job.link || `/artifacts/${dir}/report.html#attr`} target="_blank" rel="noreferrer">
                  打开报告查看归因卡 ↗
                </a>
              </span>);
          } else message.error(job.error || `任务${job.status}`);
          break;
        }
        await new Promise((r) => setTimeout(r, 1500));
      }
    } catch (e) { message.error(String(e)); } finally { setDiagnosing(null); }
  };

  return (
    <>
      <PageHeader title="报告" desc="每次执行的自包含 HTML 报告（证据链 + 失败分类 + LLM 归因），行内可归因与校准。" />
      <ShareCard />
      <Card size="small" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 40, alignItems: "center", flexWrap: "wrap" }}>
          <Progress type="circle" size={96} percent={avgRate}
            strokeColor={avgRate >= 80 ? "#52c41a" : avgRate >= 50 ? "#faad14" : "#ff4d4f"}
            format={(p) => <span style={{ fontSize: 18, fontWeight: 600 }}>{p}%</span>} />
          <div>
            <div style={{ fontWeight: 600, marginBottom: 6 }}>整体步骤成功率（最近 {total} 次执行）</div>
            <div style={{ color: "#697080", fontSize: 13 }}>
              <Tag color="green" bordered={false}>通过 {passed}</Tag>
              <Tag color="red" bordered={false}>失败 {failed}</Tag>
              <Tag color="orange" bordered={false}>已中止 {cancelled}</Tag>
            </div>
          </div>
          <div>
            <div style={{ color: "#697080", fontSize: 12, marginBottom: 6 }}>失败分类分布（累计）</div>
            {Object.keys(catTotal).length === 0
              ? <Tag bordered={false} color="green">无失败分类</Tag>
              : Object.entries(catTotal).map(([k, v]) => {
                  const m = CAT_LABEL[k] || { text: k, color: "default" };
                  return <Tag key={k} color={m.color} bordered={false}>{m.text} ×{v}</Tag>;
                })}
          </div>
          <Typography.Text type="secondary" style={{ fontSize: 12, maxWidth: 320 }}>
            失败先看「AI 归因」，判错就用「校准」改判并落成规则——同类失败下次自动归对；
            断言期望过期可直接采纳实际值；失败可标「可忽略」、成功用例可校准「误判」；
            探索报告的异常也能逐条确认/忽略。
          </Typography.Text>
        </div>
      </Card>
      <Card size="small" style={{ marginBottom: 12 }}>
        <Space wrap>
          <Select allowClear placeholder="全部状态" style={{ width: 140 }} value={statusFilter}
            onChange={setStatusFilter}
            options={Object.entries(STATUS_LABEL).map(([k, m]) => ({ value: k, label: m.text }))} />
          <Select allowClear placeholder="全部应用" style={{ width: 220 }} value={appFilter}
            onChange={setAppFilter}
            options={apps.map((a) => ({ value: a, label: a }))} />
          <Input allowClear placeholder="搜用例名 / 产物目录" style={{ width: 220 }}
            value={keyword} onChange={(e) => setKeyword(e.target.value)} />
          <span style={{ color: "#8a919f", fontSize: 12 }}>
            显示 {rows.length}/{totalReports} 份报告
            （执行保留最近 50 份，套件批次算 1 份 · 探索保留最近 10 次）
          </span>
        </Space>
      </Card>
      {total + exploreRows.length === 0 ? (
        <Card><Empty description="暂无执行记录——从「用例管理/执行」页运行第一条用例" /></Card>
      ) : (
        <Table size="small" rowKey={(r: ReportRow) => (isBatchRow(r) || isExploreRow(r) ? r.key : r.dir)}
          dataSource={rows} scroll={{ x: 1360 }} tableLayout="fixed"
          expandable={{ defaultExpandAllRows: false }}
          columns={[
            { title: "用例", dataIndex: "case_name", width: 200, ellipsis: true,
              // 用例列是树形展开按钮所在列，antd 不给它 ellipsis 类（实测 nowrap 缺失、
              // 窄容器下折行，发起人 2026-10-13 反馈）⇒ 自定义截断（block+nowrap+ellipsis）
              render: (n: string | undefined, r: ReportRow) => {
                const clip = { display: "block", whiteSpace: "nowrap",
                               overflow: "hidden", textOverflow: "ellipsis" } as const;
                return isExploreRow(r) ? (
                  <span style={clip}>
                    <Tag color="cyan" bordered={false}>探索</Tag>
                    {r.package || r.id}
                  </span>
                ) : isBatchRow(r) ? (
                  <span style={clip}>
                    <Tag color="geekblue" bordered={false}>套件</Tag>
                    {r.suite}（{r.items.length} 条 · 通过 {r.passedN}）
                  </span>
                ) : (
                  <div style={clip}>
                    <Space size={4}>
                      <span>{n || r.case_id}</span>
                      {r.human_marked && <Tooltip title="有步骤分类经过人工校准"><Tag color="purple" style={{ marginLeft: 0 }}>人工</Tag></Tooltip>}
                    </Space>
                  </div>
                );
              } },
            { title: "应用", dataIndex: "package", width: 170, ellipsis: true,
              render: (p2: string | undefined, r: ReportRow) => {
                if (isExploreRow(r)) return r.package ? <Tag color="blue">{r.package}</Tag> : "—";
                if (isBatchRow(r)) {
                  const pkgs = Array.from(new Set(r.items.map((x) => x.package).filter(Boolean)));
                  return pkgs.length === 1 ? <Tag color="blue">{pkgs[0]}</Tag>
                    : pkgs.length ? <Tooltip title={pkgs.join("、")}><Tag color="blue">{pkgs.length} 个应用</Tag></Tooltip> : "—";
                }
                return p2 ? <Tag color="blue">{p2}</Tag> : "—";
              } },
            { title: "执行人", dataIndex: "operator", width: 96, ellipsis: true,
              render: (_: unknown, r: ReportRow) => {
                if (isExploreRow(r)) return "—";   // 探索暂不记执行人（job 内存态，explore.json 未落）
                if (isBatchRow(r)) {
                  const ops = Array.from(new Set(r.items.map((x) => x.operator).filter(Boolean)));
                  return ops.length === 1 ? ops[0]
                    : ops.length ? <Tooltip title={ops.join("、")}>{ops.length} 人</Tooltip> : "—";
                }
                return r.operator || "—";
              } },
            { title: "结果", dataIndex: "status", width: 130,
              render: (s: string | undefined, r: ReportRow) => {
                if (isExploreRow(r)) {
                  // 块级两行：Tag + 步数竖排——Space 横排在 90px 列里把「发现 N 个异常」
                  // 截断、步数挤成竖字（发起人 2026-10-13 反馈换行）
                  return (
                    <div>
                      {r.findings > 0
                        ? <Tooltip title="遍历中发现的异常事件（崩溃 / 白屏 / 风控页 / 卡住等）">
                            <Tag color="red">发现 {r.findings} 个异常</Tag></Tooltip>
                        : <Tag color="green">无异常</Tag>}
                      <div style={{ color: "#697080", fontSize: 12 }}>{r.steps} 步</div>
                    </div>
                  );
                }
                if (isBatchRow(r)) {
                  // 与「成功率」列同口径（发起人 2026-10-13）：continue 可选步骤会让
                  // 用例全过但步骤成功率<100%——此时不能再标「全部通过」，改显通过比
                  if (r.rate != null && r.rate < 1) {
                    return <Tooltip title={`用例通过 ${r.passedN}/${r.items.length}，部分步骤被跳过或失败（步骤成功率 ${Math.round(r.rate * 100)}%）`}>
                      <Tag color={r.rate >= 0.5 ? "orange" : "red"}>通过 {r.passedN}/{r.items.length}</Tag>
                    </Tooltip>;
                  }
                  if (r.passedN === r.items.length) return <Tag color="green">全部通过</Tag>;
                  if (r.passedN + r.cancelledN === r.items.length)
                    return <Tag color="orange">通过 {r.passedN}/{r.items.length}</Tag>;
                  return <Tag color="red">通过 {r.passedN}/{r.items.length}</Tag>;
                }
                const m = STATUS_LABEL[s || ""];
                return m ? <Tag color={m.color}>{m.text}</Tag> : <Tag>{s}</Tag>;
              } },
            { title: "失败分类", dataIndex: "failed_categories", width: 180,
              render: (_: unknown, r: ReportRow) => {
                if (isExploreRow(r)) return "—";
                const cats = Object.entries(r.failed_categories || {});
                if (!cats.length) return "—";
                return <Space size={2} wrap>{cats.map(([k, v]) => {
                  const m = CAT_LABEL[k] || { text: k, color: "default" };
                  return <Tag key={k} color={m.color} style={{ fontSize: 11 }}>{m.text} ×{v}</Tag>;
                })}</Space>;
              } },
            { title: "成功率", dataIndex: "rate", width: 90,
              sorter: (a: ReportRow, b: ReportRow) =>
                ((a as { rate?: number }).rate ?? -1) - ((b as { rate?: number }).rate ?? -1),
              render: (r?: number) => (r != null
                ? <span style={{ color: r >= 0.8 ? "#389e0d" : r >= 0.5 ? "#d48806" : "#cf1322", fontWeight: 600 }}>
                    {Math.round(r * 100)}%
                  </span> : "—") },
            { title: "标记", key: "marks", width: 130,
              render: (_: unknown, r: ReportRow) => {
                if (isExploreRow(r)) {
                  return r.calibrated
                    ? <Tooltip title="探索异常经过人工校准（确认/忽略）——点「探索报告 ↗」看标注">
                        <Tag color="geekblue" style={{ fontSize: 11 }}>已校准</Tag></Tooltip>
                    : "—";
                }
                if (isBatchRow(r)) {
                  const marked = r.items.some((x) => x.diagnosed);
                  const calib = r.items.some((x) => x.calibrated);
                  return <Space size={2} wrap>
                    {marked && <Tooltip title="批内至少一条用例已 LLM 归因——点该条「报告 ↗」看归因卡">
                      <Tag color="purple" style={{ fontSize: 11 }}>已归因</Tag></Tooltip>}
                    {calib && <Tag color="geekblue" style={{ fontSize: 11 }}>已校准</Tag>}
                    {!marked && !calib && "—"}
                  </Space>;
                }
                return <Space size={2} wrap>
                  {r.diagnosed && <Tooltip title="LLM 已把该次失败步骤归入四分类并写入报告「归因卡」——点操作列「报告 ↗」查看；分类不对可在「校准」里改判">
                    <Tag color="purple" style={{ fontSize: 11 }}>已归因</Tag></Tooltip>}
                  {r.calibrated && <Tag color="geekblue" style={{ fontSize: 11 }}>已校准</Tag>}
                  {!r.diagnosed && !r.calibrated && "—"}
                </Space>;
              } },
            { title: "完成时间", dataIndex: "finished_at", width: 170,
              // 默认按完成时间倒序（发起人 2026-10-12：最新报告在最前）
              defaultSortOrder: "descend",
              sorter: (a: ReportRow, b: ReportRow) => (a.finished_at || "").localeCompare(b.finished_at || ""),
              // nowrap（发起人 2026-10-13 反馈换行）：19 字符的日期时间在 150px 里
              // 会被空格拆成「日期/时间」两行——加宽 + 禁折行
              render: (f: string | undefined) =>
                <span style={{ whiteSpace: "nowrap" }}>{f?.replace("T", " ").slice(0, 19)}</span> },
            { title: "操作", width: 210,
              render: (_: unknown, r: ReportRow) => isExploreRow(r) ? (
                <Space size={4} wrap>
                  {r.findings > 0 && (
                    <Button size="small" onClick={() => setCalibExplore(r.id)}>校准</Button>)}
                  <a href={`/artifacts/explore/${r.id}/report.html`} target="_blank" rel="noreferrer">
                    探索报告 ↗
                  </a>
                </Space>
              ) : isBatchRow(r) ? (
                <a href={`/artifacts/${r.suite}/index.html`} target="_blank" rel="noreferrer">
                  套件聚合报告 ↗
                </a>
              ) : (
                <Space size={4} wrap>
                  {r.status === "failed" && (
                    <Button size="small" type="primary" ghost loading={diagnosing === r.dir}
                      onClick={() => diagnose(r.dir)}>AI 归因</Button>)}
                  {/* 校准对全部结果开放（发起人 2026-10-13）：成功用例也要能校准「误判」 */}
                  <Button size="small" onClick={() => setCalibRun(r.dir)}>校准</Button>
                  <a href={`/artifacts/${r.dir}/report.html`} target="_blank" rel="noreferrer">报告 ↗</a>
                </Space>) },
          ]} />
      )}
      <CalibrateDrawer run={calibRun} open={!!calibRun}
        onClose={() => setCalibRun(null)}
        onApplied={() => { window.dispatchEvent(new Event("hashchange")); }} />
      <ExploreCalibrateDrawer exploreId={calibExplore} open={!!calibExplore}
        onClose={() => setCalibExplore(null)}
        onApplied={() => { window.dispatchEvent(new Event("hashchange")); }} />
    </>
  );
}
