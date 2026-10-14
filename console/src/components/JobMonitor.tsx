import { Button, Progress, Space, Tag, Timeline, message } from "antd";
import { useEffect, useRef, useState } from "react";
import { cancelJob, getJobEvents, getJobs, JobEvent } from "../api";

const EV_COLOR: Record<string, string> = {
  running: "blue", passed: "green", healed: "purple", failed: "red",
  cancelled: "orange", skipped: "gray", warning: "gold", error: "red",
};

// 任务级标记（只发 running/queued、永无终态），不计入进度步骤。
// "job"：任务级失败事件（worker 异常/看门狗超时时引擎补发，detail=失败原因）。
// "gen"/"quality"：生成任务的过程与质量提示（2026-10-13 发起人实测：gen(running 永不
// 终态)+quality(终态) 被当步骤算出恒定 50%，任务 done 后进度圈仍停 50%）。
// "record"：录屏可用性提示（非执行步骤）。
const JOB_LEVEL = new Set(["run", "explore", "job", "gen", "quality", "record"]);

// 任务实时监控:1.5s 轮询事件流 + [中止]。生成/导入/执行各视图复用。
export default function JobMonitor({ jobId, onFinished }: { jobId: string; onFinished?: () => void }) {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [status, setStatus] = useState("running");
  const [summary, setSummary] = useState<string | null>(null);
  const since = useRef(0);
  const notified = useRef(false);

  useEffect(() => {
    let alive = true;
    // jobId 切换时重置轮询游标与状态：since 是 ref，不重置会拿旧任务 seq 过滤
    // 新任务事件流（新任务 seq 从 1 起），前几条事件被漏掉、状态 Tag 残留旧值。
    since.current = 0;
    notified.current = false;
    setEvents([]);
    setStatus("running");
    const tick = async () => {
      try {
        const d = await getJobEvents(jobId, since.current);
        if (!alive) return;
        since.current = d.seq;
        setEvents((prev) => [...prev, ...d.events]);
        setStatus(d.status);
        const j = (await getJobs()).jobs.find((x) => x.id === jobId);
        setSummary(j?.summary ?? null);
        if (d.status !== "running" && !notified.current) {
          notified.current = true;
          onFinished?.();
        }
      } catch { /* 下一轮重试 */ }
    };
    tick();
    const t = setInterval(tick, 1500);
    return () => { alive = false; clearInterval(t); };
  }, [jobId]);

  // 进度圈：按去重 step_id 的最新状态算——同一步先后发 running/终态两条事件，
  // 按事件条数算会恒停在 50%（此前转换每条 2 条、套件每条 3 条）。
  const latest = new Map<string, string>();
  for (const e of events) latest.set(e.step_id, e.status);
  const stepIds = [...latest.keys()].filter((id) => !JOB_LEVEL.has(id));
  const percent = stepIds.length
    ? Math.round(stepIds.filter((id) => !["running", "queued"].includes(latest.get(id)!)).length
        / stepIds.length * 100)
    : 0;

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Tag color={status === "running" ? "blue" : status === "cancelled" ? "orange"
          : status === "error" ? "red" : "green"}>{status}</Tag>
        {stepIds.length > 0 && <Progress type="circle" size={36} percent={percent}
          status={status === "error" ? "exception" : undefined} />}
        {!["done", "cancelled", "error"].includes(status) && (
          <Button danger size="small" onClick={async () => {
            try { const r = await cancelJob(jobId); message.success(r.requested ? "已请求中止（当前步完成后退出）" : "任务已结束"); }
            catch (e) { message.error(String(e)); }
          }}>中止</Button>)}
        {summary && <span style={{ color: "#57606a" }}>{summary}</span>}
      </Space>
      <Timeline items={events.map((e) => ({
        color: EV_COLOR[e.status] || "gray",
        children: (<>
          <code>{e.step_id}</code> <Tag>{e.status}</Tag>
          {e.duration_ms != null && <span style={{ color: "#57606a" }}>{e.duration_ms}ms</span>}
          {e.detail && <div style={{ color: e.status === "error" ? "#cf1322" : "#57606a", fontSize: 12 }}>{e.detail}</div>}
        </>),
      }))} />
    </div>
  );
}
