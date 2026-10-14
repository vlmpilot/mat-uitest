import { Button, Card, Form, Input, InputNumber, Select, message } from "antd";
import { useState } from "react";
import { startExplore, StateResp } from "../api";
import PageHeader from "../components/PageHeader";

// 遍历探索(原「探索测试」,发起人 2026-10-12 更名):只保留探索启动器。任务监控模块已移除——
// 下发任务后自动跳「设备投屏」页,任务进度与执行日志在那里定位跟踪(2026-10-11)。
// 历史执行表也已移除——「报告」页有更完备的历史(筛选/分类明细/归因/校准入口)。

// 遍历探索启动器(与 CLI explore 同语义)
function ExploreLauncher({ state, onLaunched }: { state: StateResp; onLaunched: (id: string) => void }) {
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  const devices = state.devices.filter((d) => d.state === "device");
  if (!devices.length) return <Card size="small" title="遍历探索"><span style={{ color: "#999" }}>无在线设备</span></Card>;
  return (
    <Card size="small" title="遍历探索" style={{ marginBottom: 16 }}>
      <Form form={form} layout="inline" initialValues={{ package: "", minutes: 2, steps: 30, max_depth: 3, serial: null, platform: "android" }}
        onFinish={async (v) => {
          setBusy(true);
          try { const { job_id } = await startExplore(v); message.success(`已启动 ${job_id},已跳转「设备/执行日志」页跟踪`); onLaunched(job_id); }
          catch (e) { message.error(String(e)); } finally { setBusy(false); }
        }}>
        <Form.Item name="package" rules={[{ required: true, message: "包名" }]}>
          <Input style={{ width: 220 }} placeholder="被测 App 包名" />
        </Form.Item>
        <Form.Item name="platform">
          <Select style={{ width: 110 }}
            options={[{ value: "android", label: "Android" }, { value: "ios", label: "iOS" }]} />
        </Form.Item>
        <Form.Item name="serial">
          <Select allowClear placeholder="设备(自动)" style={{ width: 200 }}
            options={devices.map((d) => ({ value: d.serial, label: `${d.model || d.serial} · ${d.serial}` }))} />
        </Form.Item>
        <Form.Item name="minutes"><InputNumber min={1} max={15} addonAfter="分钟" /></Form.Item>
        <Form.Item name="steps"><InputNumber min={5} max={200} addonAfter="步上限" /></Form.Item>
        <Form.Item name="max_depth"><InputNumber min={1} max={6} addonAfter="深度 ≤L" /></Form.Item>
        <Button type="primary" htmlType="submit" loading={busy}>启动遍历探索</Button>
      </Form>
    </Card>
  );
}

export default function Runs({ state, onLaunched }: {
  state: StateResp; onLaunched: (id: string) => void }) {
  return (
    <>
      <PageHeader title="遍历探索"
        desc={<>遍历探索让 AI 在真机上按 L0-Ln 层级遍历 App：记录每个页面的全部可点控件并逐个尝试
          （已试过的不再重复），优先点向没访问过的页面、同分轮转保证覆盖面，超过深度上限自动回退；
          在预算步数/时长内收集页面结构、发现异常（崩溃、弹窗、不可点控件等）并产出探索报告——
          适合版本前期快速摸底，也为用例生成提供真实页面素材。
          任务进度与执行日志在「设备/执行日志」页跟踪，运行历史在「报告」页；暂不支持 iOS。</>} />
      <ExploreLauncher state={state} onLaunched={onLaunched} />
    </>
  );
}
