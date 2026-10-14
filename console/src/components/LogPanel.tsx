import { useEffect, useRef, useState } from "react";
import { Button, Tag } from "antd";
import { getJobEvents, JobEvent } from "../api";

// 事件日志面板:任务事件流 → 终端风格的文本日志。
// 数据来自 /api/job/events(引擎 on_step 契约已有),面板只是另一种"读法"——
// Timeline 看状态分布,这里看时间线原文(可复制,贴缺陷单/飞书)。
const STATUS_COLOR: Record<string, string> = {
  passed: "#52c41a", failed: "#ff4d4f", healed: "#722ed1", running: "#1677ff",
  queued: "#faad14", error: "#ff4d4f", cancelled: "#faad14", done: "#52c41a",
};

export default function LogPanel({ jobId }: { jobId: string }) {
  const [lines, setLines] = useState<JobEvent[]>([]);
  const [since, setSince] = useState(0);
  const [autoScroll, setAutoScroll] = useState(true);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => { setLines([]); setSince(0); }, [jobId]);
  useEffect(() => {
    let alive = true;
    const tick = () => getJobEvents(jobId, since)
      .then((d) => {
        if (!alive) return;
        if (d.events.length) {
          setLines((prev) => [...prev, ...d.events]);
          setSince(d.seq);
        }
        setTimeout(tick, 1500);
      })
      .catch(() => { if (alive) setTimeout(tick, 3000); });
    tick();
    return () => { alive = false; };
  }, [jobId, since]);

  useEffect(() => {
    if (autoScroll && boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [lines, autoScroll]);

  const text = lines.map((e) =>
    `[${e.ts?.slice(11, 19) || ""}] ${e.step_id} · ${e.status}${e.detail ? ` — ${e.detail}` : ""}`).join("\n");

  return (
    <div>
      <div style={{ display: "flex", gap: 8, marginBottom: 8, alignItems: "center" }}>
        <Tag color="blue" bordered={false}>事件日志</Tag>
        <span style={{ color: "#8a919f", fontSize: 12 }}>{lines.length} 条 · 实时</span>
        <Button size="small" type="text" onClick={() => navigator.clipboard?.writeText(text)}
          disabled={!lines.length}>复制全部</Button>
        <Button size="small" type="text" onClick={() => setAutoScroll((a) => !a)}>
          {autoScroll ? "跟随中" : "已暂停跟随"}
        </Button>
      </div>
      <div ref={boxRef} style={{
        background: "#0d1117", color: "#c9d1d9", borderRadius: 8, padding: "10px 12px",
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12,
        lineHeight: "20px", maxHeight: 460, overflow: "auto", whiteSpace: "pre-wrap",
      }}>
        {lines.length === 0 && <span style={{ color: "#8b949e" }}>等待事件…</span>}
        {lines.map((e) => (
          <div key={e.seq}>
            <span style={{ color: "#8b949e" }}>[{e.ts?.slice(11, 19) || ""}]</span>{" "}
            <span style={{ color: STATUS_COLOR[e.status] || "#c9d1d9", fontWeight: 600 }}>{e.status}</span>{" "}
            <span style={{ color: "#79c0ff" }}>{e.step_id}</span>
            {e.detail ? <span> — {e.detail}</span> : null}
          </div>
        ))}
      </div>
    </div>
  );
}
