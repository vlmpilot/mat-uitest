import { Input, List, Modal, Tag } from "antd";
import { SearchOutlined } from "@ant-design/icons";
import { useEffect, useMemo, useRef, useState } from "react";
import { StateResp } from "../api";
import { VIEWS } from "../views";

export type Nav = { view: string; caseName?: string; jobId?: string };

// ⌘K / Ctrl+K 全局搜索:用例 / 报告 / 设备 / 视图跳转。
// 视图清单来自 src/views.ts 单一来源(修"三处手工同步漏项"复发坑)。

export default function CommandPalette({ open, onClose, state, onNav }: {
  open: boolean; onClose: () => void; state: StateResp | null;
  onNav: (nav: Nav) => void;
}) {
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) { setQ(""); setActive(0); setTimeout(() => inputRef.current?.focus(), 50); }
  }, [open]);

  const items = useMemo(() => {
    if (!state) return [];
    const out: { label: string; hint: string; nav: Nav; openUrl?: string }[] = [];
    for (const v of VIEWS)
      out.push({ label: v.label, hint: `视图 · ${v.group}`, nav: { view: v.key } });
    for (const c of state.cases)
      out.push({ label: c, hint: "用例", nav: { view: "cases", caseName: c } });
    for (const r of state.runs.slice(0, 20))
      out.push({ label: `${r.case_name || r.case_id}(${r.status} ${Math.round((r.rate ?? 0) * 100)}%)`,
                 hint: "报告", nav: { view: "reports" },
                 openUrl: `/artifacts/${r.dir}/report.html` });
    for (const d of state.devices)
      out.push({ label: d.serial, hint: `设备 · ${d.state}`, nav: { view: "overview" } });
    const kw = q.trim().toLowerCase();
    return kw ? out.filter((i) => (i.label + i.hint).toLowerCase().includes(kw)) : out;
  }, [q, state]);

  const pick = (idx: number) => {
    const it = items[idx];
    if (!it) return;
    onNav(it.nav);
    if (it.openUrl) window.open(it.openUrl, "_blank");
    onClose();
  };

  return (
    <Modal title={<span>全局搜索 <Tag style={{ marginLeft: 8 }}>⌘K</Tag></span>}
      open={open} onCancel={onClose} footer={null} width={520} destroyOnClose>
      <Input ref={inputRef as never} value={q} prefix={<SearchOutlined style={{ color: "#b6bac3" }} />}
        placeholder="搜用例 / 报告 / 设备 / 视图…"
        onChange={(e) => { setQ(e.target.value); setActive(0); }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, items.length - 1)); }
          if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
          if (e.key === "Enter" && items[active]) pick(active);
        }} />
      <List size="small" style={{ marginTop: 8, maxHeight: 320, overflow: "auto" }}
        dataSource={items.slice(0, 30)}
        renderItem={(it, idx) => (
          <List.Item style={{ cursor: "pointer", padding: "6px 10px", borderRadius: 6,
                              background: idx === active ? "#eef1ff" : undefined }}
            onMouseEnter={() => setActive(idx)} onClick={() => pick(idx)}>
            <span style={{ fontWeight: idx === active ? 600 : 400 }}>{it.label}</span>
            <Tag style={{ marginLeft: "auto", marginRight: 0 }} bordered={false}>{it.hint}</Tag>
          </List.Item>)} />
    </Modal>
  );
}
