"""任务内省：/api/jobs、/api/job（投影视图）、/api/job/events、/api/job/cancel。"""

from __future__ import annotations

from .runtime import _JOB_LOCK, _JOBS


def api_jobs_list() -> tuple[int, dict]:
    """GET /api/jobs：全部任务轻量列表（监控页 Tabs 数据源；不含 events 大对象）。"""
    with _JOB_LOCK:
        jobs = [{"id": jid, "kind": j["kind"], "status": j["status"],
                 "started_at": j["started_at"], "summary": j["summary"],
                 "link": j["link"], "seq": j.get("seq", 0),
                 "serial": (j.get("meta") or {}).get("serial"),
                 "platform": (j.get("meta") or {}).get("platform"),
                 "operator": (j.get("meta") or {}).get("operator") or ""}
                for jid, j in sorted(_JOBS.items(), key=lambda kv: -kv[1].get("started_ts", 0))]
    return 200, {"jobs": jobs}


def api_job_view(job_id: str) -> tuple[int, dict]:
    """GET /api/job?id=：单任务投影视图。

    原始记录含 cancel Event（不可 JSON 序列化）与 events 大对象 ⇒ 此前整记录直
    dumps **必然 500**（端点自事件契约起就是死的，控制台恰好不用它所以没人发现
    ——T10 覆盖率测试抓到）。status/summary/link/error/seq 才是调用方要的；
    events 走 /api/job/events。
    """
    with _JOB_LOCK:
        job = _JOBS.get(job_id or "")
        if job is None:
            return 200, {"status": "unknown"}
        view = {k: v for k, v in job.items() if k not in ("cancel", "events")}
    return 200, view


def api_job_events(params: dict) -> tuple[int, dict]:
    """GET /api/job/events?id=&since=<seq>：增量步骤事件（前端 1.5s 轮询）。"""
    job = _JOBS.get(str(params.get("id", "")))
    if job is None:
        return 404, {"error": "job 不存在"}
    try:
        since = int(params.get("since") or 0)
    except (TypeError, ValueError):
        since = 0
    events = [e for e in job.get("events", []) if e["seq"] > since]
    return 200, {"seq": job.get("seq", 0), "events": events, "status": job["status"]}


def api_job_cancel(body: dict) -> tuple[int, dict]:
    """POST /api/job/cancel：置位取消标志立即返回（不打断当前步，语义见 Runner 的取消实现）。"""
    job = _JOBS.get(str(body.get("id", "")))
    if job is None:
        return 404, {"error": "job 不存在"}
    if job["status"] != "running":
        return 200, {"requested": False, "reason": "任务已结束"}
    job["cancel"].set()
    return 200, {"requested": True}
