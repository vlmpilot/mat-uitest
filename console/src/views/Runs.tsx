import { Alert, AutoComplete, Button, Card, Form, InputNumber, Select, message } from "antd";
import { useEffect, useState } from "react";
import { listInstalledApps, startExplore, StateResp } from "../api";
import PageHeader from "../components/PageHeader";

// 遍历探索(原「探索测试」,发起人 2026-10-12 更名):只保留探索启动器。任务监控模块已移除——
// 下发任务后自动跳「设备投屏」页,任务进度与执行日志在那里定位跟踪(2026-10-11)。
// 历史执行表也已移除——「报告」页有更完备的历史(筛选/分类明细/归因/校准入口)。

// 遍历探索启动器(与 CLI explore 同语义)
function ExploreLauncher({ state, onLaunched }: { state: StateResp; onLaunched: (id: string) => void }) {
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  // 包名候选（2026-10-22 发起人）：当前设备的**第三方** App（pm list packages -3）。
  // 仍保留手输——设备没连/读取失败时静默回落，不阻断启动。
  const [apps, setApps] = useState<string[]>([]);
  const serial = Form.useWatch<string | null>("serial", form);
  useEffect(() => {
    listInstalledApps(serial).then((r) => setApps(r.packages)).catch(() => setApps([]));
  }, [serial]);
  // 遍历探索仅 Android（iOS 的图谱适配未完成）——iOS 真机不进入这里的设备下拉
  const devices = state.devices.filter((d) => d.state === "device"
    && (d.platform || "android") === "android");
  if (!devices.length) return <Card size="small" title="遍历探索"><span style={{ color: "#999" }}>无在线设备</span></Card>;
  return (
    <Card size="small" title="遍历探索" style={{ marginBottom: 16 }}>
      <Form form={form} layout="inline" initialValues={{ package: "", minutes: 5, steps: 100, max_depth: 3, serial: null, platform: "android" }}
        onFinish={async (v) => {
          setBusy(true);
          try { const { job_id } = await startExplore(v); message.success(`已启动 ${job_id},已跳转「设备/执行日志」页跟踪`); onLaunched(job_id); }
          catch (e) { message.error(String(e)); } finally { setBusy(false); }
        }}>
        <Form.Item name="package" rules={[{ required: true, message: "包名" }]}>
          {/* AutoComplete 而非 Select：列表来自当前设备，但允许手输不在列表里的包 */}
          <AutoComplete style={{ width: 240 }} allowClear
            placeholder={apps.length ? "选择或输入被测 App 包名" : "被测 App 包名（可手输）"}
            options={apps.map((p) => ({ value: p }))}
            filterOption={(input, option) =>
              String(option?.value ?? "").toLowerCase().includes(input.toLowerCase())} />
        </Form.Item>
        {/* iOS 暂不支持（发起人 2026-10-15）：视觉置灰 + 选中即提示回退——antd 的 disabled 选项点击无任何反馈，
            学不到「为什么点不动」，所以不用 disabled 而用 onChange 拦截 */}
        <Form.Item name="platform">
          <Select style={{ width: 110 }}
            onChange={(v) => {
              if (v === "ios") {
                message.warning("遍历探索暂不支持 iOS");
                form.setFieldValue("platform", "android");
              }
            }}
            options={[{ value: "android", label: "Android" },
                      ...(state.ios_supported === false ? []
                        : [{ value: "ios", label: "iOS", className: "explore-ios-disabled" }])]} />
        </Form.Item>
        <style>{".explore-ios-disabled{color:#bfbfbf !important;}"}</style>
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
      {/* 探索建议（判据本体保留，2026-10-26 去掉「甜点判据」术语）：文案唯一来源
          在服务端 readiness.EXPLORE_TIP，前端只负责显示——避免两处文案漂移。 */}
      {state.explore_tip && (
        <Alert type="info" showIcon style={{ marginBottom: 16 }}
          message="探索建议" description={state.explore_tip} />
      )}
      <ExploreLauncher state={state} onLaunched={onLaunched} />
    </>
  );
}
