import { Alert, Button, Card, Space, Table, Typography } from "antd";
import { useEffect, useState } from "react";
import { AuditItem, getAudit } from "../api";
import PageHeader from "../components/PageHeader";

// 操作记录（团队协作 P1，发起人 2026-10-12）：写操作审计的展示端——回答「谁改的、
// 谁下发的」。数据是 artifacts/audit.jsonl（POST 全量落盘），倒序取最近 50 条。
// 2026-10-12 二次调整：从设置页拆出独立成菜单（与「设置」并列于「更多」组）。
const ACTION_LABEL: Record<string, string> = {
  "/api/case": "保存用例", "/api/case/delete": "删除用例", "/api/gen": "生成用例",
  "/api/import/preview": "导入预览", "/api/import/convert": "导入用例",
  "/api/explore": "启动探索", "/api/run": "执行用例/套件", "/api/job/cancel": "取消任务",
  "/api/index": "建索引", "/api/describe": "补标题", "/api/diagnose": "失败归因",
  "/api/export": "数据导出", "/api/apps/install": "安装应用", "/api/apps/uninstall": "卸载应用",
  "/api/heals/apply": "应用自愈", "/api/calibrate/apply": "应用校准", "/api/activate": "激活",
  "/api/ios/sim/boot": "启动 iOS 模拟器", "/api/review": "复核页面标题",
  "/api/doctor/fix": "环境修复", "/api/workspace/switch": "更换工作区",
  "/api/device/key": "获取设备操控密钥", "/api/device/scrcpy": "发起投屏",
  "/api/screenshots/capture": "截屏", "/api/fs/open": "打开目录",
  "/api/seats/check": "成员登记", "/api/seats/remove": "移除成员",
};

export default function Audit() {
  const [items, setItems] = useState<AuditItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const load = () => getAudit(50)
    .then((d) => { setItems(d.items); setTotal(d.total); }).catch(() => setItems([]));
  useEffect(() => { load(); }, []);
  return (
    <>
      <PageHeader title="操作记录"
        desc="谁在何时保存/删除/执行了什么——写操作自动落盘为台账。" />
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
        message="本工作区全部写操作（保存/删除用例、执行、导入等）自动记录。任务下发的「谁」同时显示在任务条与执行日志；此处是完整台账（最近 50 条，落盘于 artifacts/audit.jsonl，随工作区备份迁移）。" />
      <Space style={{ marginBottom: 10 }}>
        <Button size="small" onClick={load}>刷新</Button>
        {total > 50 && <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          共 {total} 条，仅显示最近 50 条（全量见 audit.jsonl）</Typography.Text>}
      </Space>
      {items === null ? <Card loading style={{ minHeight: 120 }} /> : (
        <Table size="small" rowKey={(r, i) => `${i}`}
          dataSource={items} pagination={false}
          columns={[
            { title: "时间", dataIndex: "ts", width: 170,
              render: (v: string) => <span style={{ fontFamily: "monospace", fontSize: 12 }}>{v?.replace("T", " ")}</span> },
            { title: "操作者", dataIndex: "operator", width: 110,
              render: (v: string) => v || <Typography.Text type="secondary">匿名</Typography.Text> },
            { title: "动作", dataIndex: "action", width: 130,
              render: (v: string) => ACTION_LABEL[v] || v },
            { title: "对象", dataIndex: "target", ellipsis: true,
              render: (v: string) => <span style={{ fontFamily: "monospace", fontSize: 12 }}>{v || "—"}</span> },
            { title: "来源 IP", dataIndex: "ip", width: 120,
              render: (v: string) => <span style={{ fontFamily: "monospace", fontSize: 12 }}>{v || "—"}</span> },
          ]} />
      )}
    </>
  );
}
