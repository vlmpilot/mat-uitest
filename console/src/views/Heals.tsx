import { Button, Card, Empty, Table, Tag, Typography, message } from "antd";
import { useEffect, useState } from "react";
import { applyHeal, getHeals, HealRun, StateResp } from "../api";
import PageHeader from "../components/PageHeader";

// 自愈提案(资产组):定位失败被本地自愈救回的步骤 → 人确认后生成修复用例。
// 语义与 CLI `mat heal apply` 一致:生成 `<名>-healed.json`,不覆盖原用例。
export default function Heals({ state }: { state: StateResp }) {
  const [runs, setRuns] = useState<HealRun[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => getHeals().then((d) => setRuns(d.runs)).catch(() => setRuns([]));
  useEffect(() => { load(); }, []);

  const apply = async (run: string) => {
    setBusy(run);
    try {
      const r = await applyHeal(run);
      message.success(`已生成 ${r.out}（${r.notes.length} 处变更）——人工比对后再替换原用例`);
      load();
    } catch (e) { message.error(String(e)); } finally { setBusy(null); }
  };

  return (
    <>
      <PageHeader title="自愈提案" desc={<>定位失败被本地自愈救回的步骤会出现在这里——提案**不会自动改用例**，人工确认后一键生成 `&lt;名&gt;-healed.json` 修复版。</>} />
      {runs === null ? <Card loading style={{ minHeight: 200 }} /> : runs.length === 0 ? (
        <Card><Empty description="暂无自愈提案——运行用例时若发生定位失败并被自愈救回，会自动出现在这里" /></Card>
      ) : (
        <Table size="small" rowKey="run" dataSource={runs} scroll={{ x: 860 }}
          expandable={{
            expandedRowRender: (r: HealRun) => (
              <div style={{ padding: "4px 8px" }}>
                {r.proposals.map((p, i) => (
                  <div key={i} style={{ marginBottom: 6, fontSize: 12 }}>
                    <Tag bordered={false} color="purple">{p.via}</Tag>
                    <Typography.Text code style={{ fontSize: 12 }}>{p.original}</Typography.Text>
                    {" → "}
                    <Typography.Text code style={{ fontSize: 12 }}>{p.match}</Typography.Text>
                  </div>
                ))}
              </div>
            ),
          }}
          columns={[
            { title: "执行", dataIndex: "run", ellipsis: true,
              render: (d: string) => <Typography.Text code style={{ fontSize: 12 }}>{d}</Typography.Text> },
            { title: "用例", dataIndex: "case", ellipsis: true },
            { title: "结果", dataIndex: "status", width: 90,
              render: (s: string) => (s === "passed" ? <Tag color="green">通过（自愈）</Tag> : <Tag>{s}</Tag>) },
            { title: "提案数", width: 90,
              render: (_: unknown, r: HealRun) => <Tag color="purple" bordered={false}>{r.proposals.length}</Tag> },
            { title: "", width: 150,
              render: (_: unknown, r: HealRun) => (
                <Button size="small" type="primary" ghost loading={busy === r.run}
                  onClick={() => apply(r.run)}>生成修复用例</Button> ) },
          ]} />
      )}
    </>
  );
}
