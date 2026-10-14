import {
  Alert, Button, Card, Checkbox, Collapse, Drawer, Empty, Input, Radio, Select, Space, Tag,
  Tooltip, Typography, message,
} from "antd";
import { useEffect, useState } from "react";
import {
  applyCalibrate, CalibrateData, CalibrateItem, CalibrateStep, getCalibrate,
} from "../api";

// 校准抽屉:把 CLI `mat calibrate` 的两层校准搬进界面——
// ①分类改判（可落规则：填「错误里包含/日志里包含」= 下次自动归类）；
// ②断言校准（采纳实际值/放宽包含 → 生成 -calibrated.json，不覆盖原用例）。
// 标记类扩展(发起人 2026-10-13)：失败步骤可标「可忽略」、通过步骤可标「误判」、
// 每步可写「校准原因」——只加标注不改结果统计；成功用例也开放校准入口。
export default function CalibrateDrawer({ run, open, onClose, onApplied }: {
  run: string | null; open: boolean; onClose: () => void; onApplied: () => void }) {
  const [data, setData] = useState<CalibrateData | null>(null);
  const [loading, setLoading] = useState(false);
  const [steps, setSteps] = useState<Record<string, CalibrateStep>>({});
  const [applying, setApplying] = useState(false);

  useEffect(() => {
    if (open && run) {
      setLoading(true); setData(null); setSteps({});
      getCalibrate(run).then(setData).catch((e) => message.error(String(e)))
        .finally(() => setLoading(false));
    }
  }, [open, run]);

  const setStep = (stepId: string, patch: CalibrateStep) =>
    setSteps((prev) => ({ ...prev, [stepId]: { ...prev[stepId], ...patch } }));

  const changedCount = Object.values(steps)
    .filter((s) => s.category || s.error_contains || s.logcat_contains
      || s.assertion_fix || s.note !== undefined
      || s.ignore !== undefined || s.false_positive !== undefined).length;

  const apply = async () => {
    if (!run) return;
    setApplying(true);
    try {
      // steps 里只含用户改过的键（Input 受控才会写入）——后端按「键存在」生效，
      // 空 note = 用户显式清空既有校准原因（撤销语义），照发
      const payload: Record<string, CalibrateStep> = {};
      for (const [k, v] of Object.entries(steps)) {
        if (Object.keys(v).length) payload[k] = v;
      }
      if (!Object.keys(payload).length) { message.warning("还没有任何修改"); return; }
      const r = await applyCalibrate(run, payload);
      const parts = [`改判 ${r.applied} 步`, `判定一致 ${r.unchanged}`];
      if (r.marked) parts.push(`标记 ${r.marked} 步`);
      parts.push(`落规则 ${r.rules_saved} 条`);
      if (r.calibrated) parts.push(`校准用例 ${r.calibrated.file}`);
      message.success(`校准已应用：${parts.join(" · ")}`);
      if (r.warning) message.warning(r.warning);
      if (r.missing.length) message.warning(`找不到 ${r.missing.length} 步（图谱重跑后 key 会变）`);
      onApplied(); onClose();
    } catch (e) { message.error(String(e)); } finally { setApplying(false); }
  };

  const card = (it: CalibrateItem) => {
    const st = steps[it.step_id] || {};
    const diag = it.diagnosis || {};
    const passed = it.status === "passed";
    return (
      <Card key={it.step_id} size="small" style={{ marginBottom: 12 }}>
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <Space wrap size={6}>
            <Typography.Text code>{it.step_id}</Typography.Text>
            <Typography.Text strong>{it.intent}</Typography.Text>
            {passed ? <Tag color="green" bordered={false}>✅ 通过</Tag> : (
              <Tag color="orange" bordered={false}>
                规则判定：{data?.category_labels[it.category] || it.category}
              </Tag>)}
            {it.category_source === "human" && <Tag color="purple">人工</Tag>}
            {it.ignored && <Tag color="default" bordered={false}>已忽略</Tag>}
            {it.false_positive && <Tag color="red" bordered={false}>误判</Tag>}
            {diag.need_human && <Tag color="red">LLM 建议人工确认</Tag>}
          </Space>
          {it.screenshot_url && (
            <img alt={it.step_id} src={it.screenshot_url}
              style={{ maxWidth: 260, borderRadius: 6, border: "1px solid #f0f0f0" }} />
          )}
          {it.calibration_note && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              现有校准原因：{it.calibration_note}
            </Typography.Text>)}
          {!passed && it.error && (
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 4 }}
              ellipsis={{ rows: 3, expandable: true, symbol: "展开" }}>
              {it.error}
            </Typography.Paragraph>)}
          <Space wrap size={8}>
            {!passed && (
              <Select size="small" style={{ minWidth: 200 }}
                placeholder={`改判（当前：${data?.category_labels[it.category] || it.category}）`}
                value={st.category || undefined}
                onChange={(v) => setStep(it.step_id, { ...st, category: v })}
                options={data?.categories.map((c) => ({
                  value: c, label: `${data.category_labels[c]}（${c}）`,
                })) || []} />)}
            {!passed && (
              <Input size="small" style={{ width: 190 }} placeholder="错误里包含（→落规则）"
                value={st.error_contains || ""}
                onChange={(e) => setStep(it.step_id, { ...st, error_contains: e.target.value })} />
            )}
            {!passed && (
              <Input size="small" style={{ width: 190 }} placeholder="日志里包含（→落规则）"
                value={st.logcat_contains || ""}
                onChange={(e) => setStep(it.step_id, { ...st, logcat_contains: e.target.value })} />
            )}
          </Space>
          {passed ? (
            <Checkbox checked={st.false_positive ?? it.false_positive ?? false}
              onChange={(e) => setStep(it.step_id, { ...st, false_positive: e.target.checked })}>
              标记为「误判」——通过但没有真正验证到（侥幸通过/断言过松），只做标注不改结果
            </Checkbox>
          ) : (
            <Checkbox checked={st.ignore ?? it.ignored ?? false}
              onChange={(e) => setStep(it.step_id, { ...st, ignore: e.target.checked })}>
              标记为「可忽略」——误报/不影响产品，报告挂「已忽略」徽标（不改成功率统计）
            </Checkbox>
          )}
          <Input size="small" placeholder="校准原因（为什么改判/标记，写给后来的人，清空=撤销）"
            value={st.note ?? it.calibration_note ?? ""}
            onChange={(e) => setStep(it.step_id, { ...st, note: e.target.value })} />
          {!passed && it.assertion && (
            <Alert type="info" showIcon={false} style={{ background: "#f7f9ff" }}
              message={<span>断言校准 · {it.assertion.kind === "equals" ? "文本相等" : "文本包含"}</span>}
              description={
                <div>
                  <div style={{ fontSize: 13, marginBottom: 6 }}>
                    期望 <Typography.Text code type="danger">{it.assertion.expected}</Typography.Text>
                    {"  实际 "}
                    <Typography.Text code>{it.assertion.actual}</Typography.Text>
                  </div>
                  <Radio.Group size="small"
                    value={st.assertion_fix || ""}
                    onChange={(e) => setStep(it.step_id, {
                      ...st, assertion_fix: e.target.value,
                      assertion_expected: it.assertion!.expected,
                      assertion_actual: it.assertion!.actual,
                    })}>
                    <Space direction="vertical">
                      <Radio value="">不改（只改上面的分类）</Radio>
                      <Radio value="adopt-actual">
                        采纳实际值「{it.assertion.actual}」——下次真的通过（改用例，出 -calibrated.json）
                      </Radio>
                      {it.assertion.kind === "equals" && (
                        <Radio value="loosen">
                          放宽为「包含」——「{it.assertion.expected}」出现就算过
                        </Radio>)}
                    </Space>
                  </Radio.Group>
                </div>
              } />
          )}
          {(diag.suggestion || diag.category) && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              LLM 归因：{data?.attr_labels[diag.category || ""] || diag.category || "—"}
              {diag.confidence != null && ` · 置信度 ${Math.round(diag.confidence * 100)}%`}
              {diag.suggestion ? ` · ${diag.suggestion}` : ""}
            </Typography.Text>
          )}
        </Space>
      </Card>
    );
  };

  const failedItems = data?.items.filter((it) => it.status !== "passed") || [];
  const passedItems = data?.items.filter((it) => it.status === "passed") || [];

  return (
    <Drawer title={`报告校准 · ${data?.run_label || run || ""}`} width={680} open={open}
      onClose={onClose}
      extra={<Button type="primary" loading={applying} disabled={changedCount === 0}
        onClick={apply}>应用校准（{changedCount} 项）</Button>}>
      {loading && <Card loading style={{ minHeight: 200 }} />}
      {!loading && data && data.items.length === 0 && (
        <Empty description="本次执行没有可校准的步骤" />)}
      {!loading && data && failedItems.map(card)}
      {!loading && data && passedItems.length > 0 && (
        <Collapse
          items={[{
            key: "passed", label:
              <span>通过步骤（{passedItems.length}）——校准是否「误判」
                <Tooltip title="成功用例也可能是假的：断言过松、侥幸通过、没验证到。标记后报告挂「误判」徽标。">
                  <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
                    什么是误判？
                  </Typography.Text>
                </Tooltip></span>,
            children: <div>{passedItems.map(card)}</div>,
          }]} />)}
      {!loading && data && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          已有 {data.rules_count} 条校准规则在生效——同类失败会自动按你的判定归类。
          标记类校准（可忽略/误判）只加标注，不改变通过率统计。
        </Typography.Text>
      )}
    </Drawer>
  );
}
