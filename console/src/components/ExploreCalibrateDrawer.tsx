import {
  Button, Card, Drawer, Empty, Input, Radio, Space, Tag, Typography, message,
} from "antd";
import { useEffect, useState } from "react";
import { applyExploreCalibrate, getExplore } from "../api";

// 探索异常校准（发起人 2026-10-13）：逐条把遍历发现的异常标记为「确认」（真异常）
// 或「忽略」（误报），可写校准原因——回写 explore.json 的 findings[i].calibration
// 并重渲染 report.html（报告挂「已确认/已忽略」徽标）；列表「发现 N 处」计数口径不变。
type Verdict = "confirmed" | "ignored" | "";
type Mark = { verdict?: Verdict; note?: string };

export default function ExploreCalibrateDrawer({ exploreId, open, onClose, onApplied }: {
  exploreId: string | null; open: boolean; onClose: () => void; onApplied: () => void }) {
  const [findings, setFindings] = useState<import("../api").ExploreFinding[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [marks, setMarks] = useState<Record<number, Mark>>({});
  const [applying, setApplying] = useState(false);

  useEffect(() => {
    if (open && exploreId) {
      setLoading(true); setFindings(null); setMarks({});
      getExplore(exploreId)
        .then((d) => setFindings(d.findings || []))
        .catch((e) => message.error(String(e)))
        .finally(() => setLoading(false));
    }
  }, [open, exploreId]);

  const setMark = (idx: number, patch: Mark) =>
    setMarks((prev) => ({ ...prev, [idx]: { ...prev[idx], ...patch } }));

  const changedCount = Object.values(marks)
    .filter((m) => m.verdict !== undefined || m.note !== undefined).length;

  const apply = async () => {
    if (!exploreId) return;
    setApplying(true);
    try {
      const payload: Record<number, Mark> = {};
      for (const [k, v] of Object.entries(marks)) {
        const clean: Mark = { ...v };
        if (!clean.note) delete clean.note;   // 空 note = 只改 verdict
        if (!Object.keys(clean).length) continue;
        payload[Number(k)] = clean;
      }
      if (!Object.keys(payload).length) { message.warning("还没有任何修改"); return; }
      const r = await applyExploreCalibrate(exploreId, payload);
      const parts = [`确认 ${r.confirmed} 处`, `忽略 ${r.ignored} 处`];
      if (r.cleared) parts.push(`撤销 ${r.cleared} 处`);
      message.success(`探索异常校准已应用：${parts.join(" · ")}`);
      if (r.warning) message.warning(r.warning);
      if (r.missing.length) message.warning(`${r.missing.length} 条下标无效，已跳过`);
      onApplied(); onClose();
    } catch (e) { message.error(String(e)); } finally { setApplying(false); }
  };

  return (
    <Drawer title={`探索异常校准 · ${exploreId || ""}`} width={680} open={open}
      onClose={onClose}
      extra={<Button type="primary" loading={applying} disabled={changedCount === 0}
        onClick={apply}>应用校准（{changedCount} 项）</Button>}>
      {loading && <Card loading style={{ minHeight: 200 }} />}
      {!loading && findings && findings.length === 0 && (
        <Empty description="本轮探索未发现异常——无需校准" />)}
      {!loading && findings?.map((f, idx) => {
        const st = marks[idx] || {};
        const cur = f.calibration?.verdict || "";
        return (
          <Card key={idx} size="small" style={{ marginBottom: 12 }}>
            <Space direction="vertical" size={6} style={{ width: "100%" }}>
              <Space wrap size={6}>
                <Tag color="volcano">{f.type}</Tag>
                {cur === "ignored" && <Tag color="default" bordered={false}>已忽略</Tag>}
                {cur === "confirmed" && <Tag color="red" bordered={false}>已确认</Tag>}
                {f.depth != null && <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  L{f.depth}</Typography.Text>}
              </Space>
              <Typography.Text strong>{f.detail}</Typography.Text>
              {f.path && (
                <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}
                  ellipsis={{ rows: 2, expandable: true, symbol: "展开" }}>
                  复现路径：{f.path}
                </Typography.Paragraph>)}
              {f.screenshot && (
                <img alt={`finding-${idx}`} src={`/artifacts/explore/${exploreId}/${f.screenshot}`}
                  style={{ maxWidth: 260, borderRadius: 6, border: "1px solid #f0f0f0" }} />
              )}
              {f.calibration?.note && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  现有校准原因：{f.calibration.note}
                </Typography.Text>)}
              <Radio.Group size="small" value={st.verdict ?? cur}
                onChange={(e) => setMark(idx, { ...st, verdict: e.target.value as Verdict })}>
                <Space direction="vertical">
                  <Radio value="">不标记（清除已有标注）</Radio>
                  <Radio value="confirmed">确认异常——真问题，需要跟进</Radio>
                  <Radio value="ignored">忽略（误报）——不影响产品，报告挂「已忽略」徽标</Radio>
                </Space>
              </Radio.Group>
              <Input size="small" placeholder="校准原因（为什么确认/忽略，写给后来的人）"
                value={st.note ?? f.calibration?.note ?? ""}
                onChange={(e) => setMark(idx, { ...st, note: e.target.value })} />
            </Space>
          </Card>
        );
      })}
      {!loading && findings && findings.length > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          标记只加标注：探索报告的重现路径/截图证据保留，列表「发现 N 处」计数不变。
        </Typography.Text>
      )}
    </Drawer>
  );
}
