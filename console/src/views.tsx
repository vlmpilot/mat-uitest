import {
  BarChartOutlined, DashboardOutlined, FileTextOutlined, HistoryOutlined, MobileOutlined,
  PlayCircleOutlined, SettingOutlined, SolutionOutlined, ThunderboltOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { ReactNode } from "react";

// 视图清单单一事实来源。
// 此前视图名在 App.tsx VIEWS / VIEW_META / CommandPalette PALETTE_VIEWS 三处手工
// 同步，「页面复核」「生成导入」先后漏过——新视图只许在这里加一行。
export type ViewKey =
  | "overview" | "cases" | "generate" | "runs" | "screens"
  | "reports" | "heals" | "review"
  | "settings" | "audit";

export type ViewDef = {
  key: ViewKey;
  label: string;
  /** "" = 顶部无分组项（概览） */
  group: "" | "用例管理与执行" | "资产" | "更多";
  hash: string;
  icon: ReactNode;
  desc: string;
};

// 「设置」组更名「更多」（发起人 2026-10-12）：操作记录从设置页拆出并列成菜单
export const VIEW_GROUPS: ViewDef["group"][] = ["", "用例管理与执行", "资产", "更多"];

export const VIEWS: ViewDef[] = [
  { key: "overview", label: "概览", group: "", hash: "overview",
    icon: <DashboardOutlined />, desc: "环境、设备与最近活动的快照。" },
  // 组内顺序按真实依赖链(发起人 2026-10-13 调整):连设备 → 认识 App(探索/图谱) →
  // 生成用例 → 管理执行——与概览「下一步建议」卡的推断链一致,新用户从上往下走即完成首次闭环。
  { key: "screens", label: "设备/执行日志", group: "用例管理与执行", hash: "screens",
    icon: <MobileOutlined />, desc: "设备实时画面、快捷操控、执行日志与近期任务。" },
  { key: "runs", label: "遍历探索", group: "用例管理与执行", hash: "runs",
    icon: <PlayCircleOutlined />,
    desc: "自动遍历 App 发现异常,同时沉淀页面图谱供 AI 生成用例;进度在「设备/执行日志」页跟踪;暂不支持 iOS。" },
  { key: "generate", label: "用例生成/导入", group: "用例管理与执行", hash: "generate",
    icon: <ThunderboltOutlined />, desc: "一句话生成用例,或导入 Excel/Markdown 手工用例。" },
  { key: "cases", label: "用例管理/执行", group: "用例管理与执行", hash: "cases",
    icon: <FileTextOutlined />, desc: "用例的编辑、运行与批量执行。" },
  { key: "reports", label: "报告", group: "资产", hash: "reports",
    icon: <BarChartOutlined />, desc: "每次执行的证据链报告与整体成功率。" },
  { key: "heals", label: "自愈提案", group: "资产", hash: "heals",
    icon: <ToolOutlined />, desc: "定位失败被自愈救回的步骤，确认后生成修复用例。" },
  { key: "review", label: "页面复核", group: "资产", hash: "review",
    icon: <SolutionOutlined />, desc: "人工复核 AI 起的页面标题（manual 不被覆盖）。" },
  { key: "settings", label: "设置", group: "更多", hash: "settings",
    icon: <SettingOutlined />, desc: "账号、凭据、团队成员、数据导出与关于。" },
  { key: "audit", label: "操作记录", group: "更多", hash: "audit",
    icon: <HistoryOutlined />, desc: "全部写操作台账：谁在何时保存/删除/执行了什么。" },
];

export const VIEW_BY_KEY = Object.fromEntries(VIEWS.map((v) => [v.key, v]));
export const VIEW_BY_HASH = Object.fromEntries(VIEWS.map((v) => [v.hash, v.key]));
export const viewLabel = (key: ViewKey): string => VIEW_BY_KEY[key]?.label ?? key;
