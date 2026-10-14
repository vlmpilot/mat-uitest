import { ReactNode } from "react";

// 统一页头:标题 + 一句话说明 + 右侧动作区。此前各视图直接裸内容,
// 用户只能靠菜单高亮猜「我在哪」——每个页面先回答这一句。
export default function PageHeader({ title, desc, extra }: {
  title: string; desc?: ReactNode; extra?: ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 12, marginBottom: 20 }}>
      <div style={{ minWidth: 0 }}>
        <h2 style={{ margin: 0, fontSize: 20, fontWeight: 600, lineHeight: "28px" }}>{title}</h2>
        {desc && <div style={{ marginTop: 4, color: "#697080", fontSize: 13, lineHeight: "20px" }}>{desc}</div>}
      </div>
      {extra && <div style={{ marginLeft: "auto", display: "flex", gap: 8, flex: "none", alignItems: "center" }}>{extra}</div>}
    </div>
  );
}
