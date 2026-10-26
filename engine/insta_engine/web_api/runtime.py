"""共享基建：任务队列 / 驱动缓存 / 设备锁 / 源码新鲜度（自 web_server.py 原样搬入）。"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..driver_base import BaseDriver
from ..driver_factory import connect_driver

WEB_DIR = Path(__file__).parent.parent / "web"
CONSOLE_DIST = Path(__file__).parent.parent / "console_dist"  # AntD 构建产物（存在即优先服务）
JOB_TIMEOUT_SECONDS = 15 * 60  # 任务看门狗：**无活动**超时（不是总时长）
#: 超时文案按任务类型分支（2026-10-21）：此前所有任务共用「设备锁屏/离线」一句，
#: 而导入/生成/描述根本不碰设备——纯 AI 转换被套上设备归因，用户按提示去解锁手机毫无意义。
_TIMEOUT_HINTS = {
    "import": "AI 转换耗时过长或上游接口超时。可减少单批条数后重试；"
              "若反复出现请检查 .env 的 AI 配置（GEN_BASE_URL / GEN_API_KEY）",
    "gen": "AI 生成耗时过长或上游接口超时。请检查网络与 .env 的 AI 配置后重试",
    "describe": "AI 页面描述耗时过长或上游接口超时。可减少页数后重试",
    "diagnose": "AI 归因耗时过长或上游接口超时。请稍后重试",
}
_DEVICE_TIMEOUT_HINT = ("常见原因：设备锁屏/离线或 adb 无响应。请点亮并解锁手机、确认 USB 连接后重试；"
                        "若反复出现请重启控制台")

_JOBS: dict[str, dict] = {}
_JOB_LOCK = threading.Lock()
_DRIVERS: dict[str, BaseDriver] = {}
_DRV_LOCK = threading.Lock()

_PROCESS_STARTED = time.time()
_SOURCE_DIR = Path(__file__).resolve().parent.parent


def source_staleness() -> dict:
    """长驻 console 的经典陷阱：源码改了，进程里跑的还是 import 时的旧代码。

    explore 一轮要几分钟、还得真机跑，跑完才发现用的是旧逻辑，代价极高——真实踩过：
    P0/P1 已提交，console 里仍是改前版本，两轮 80 步产出的报告全部作废，且报告
    里缺字段这件事只有比对源码才能看出来，从 UI 上完全看不出异常。
    这里用「源码最新 mtime 是否晚于进程启动」判定，命中就让接口直接拒绝，
    而不是产出一份看起来正常、其实无效的报告。
    """
    newest, newest_name = 0.0, ""
    for path in _SOURCE_DIR.rglob("*.py"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > newest:
            newest, newest_name = mtime, path.name
    if newest <= _PROCESS_STARTED:
        return {"stale": False, "file": "", "behind_seconds": 0}
    return {"stale": True, "file": newest_name,
            "behind_seconds": round(newest - _PROCESS_STARTED, 1)}


def _require_fresh_code() -> None:
    """写盘型任务（explore/run/gen）的开工前检查：跑在旧代码上等于白跑。"""
    if os.environ.get("MAT_ALLOW_STALE_CODE", "").lower() in ("1", "true", "yes"):
        return
    stale = source_staleness()
    if stale["stale"]:
        raise RuntimeError(
            f"console 里是旧代码（{stale['file']} 比进程启动晚 {stale['behind_seconds']}s）。"
            "请重启 console 后再执行，否则结果不可信"
            "（确要强行执行可设 MAT_ALLOW_STALE_CODE=1）")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _driver_key(serial: str | None, platform: str) -> str:
    """缓存键**必须含平台**：否则 Android 与 iOS 的默认驱动会互踩同一条目（评审 I10）。"""
    return f"{platform}:{serial or '__default__'}"


def get_driver(serial: str | None, platform: str = "android") -> BaseDriver:
    """按 (platform, serial) 取/建驱动（平台路由走 `driver_factory.connect_driver` 单源）。

    `platform` 缺省 `"android"`——既有行为不变（console/MCP 不传平台时仍连 Android）。
    """
    key = _driver_key(serial, platform)
    with _DRV_LOCK:
        driver = _DRIVERS.get(key)
        if driver is None:
            driver = connect_driver(serial, platform)
            _DRIVERS[key] = driver
        return driver


def invalidate_driver(serial: str | None, platform: str = "android") -> None:
    """设备重插/USB 复位后旧连接失效，由调用方在异常时调用。"""
    with _DRV_LOCK:
        _DRIVERS.pop(_driver_key(serial, platform), None)


_DEVICE_LOCKS: dict[str, threading.Lock] = {}
_DEVICE_LOCKS_GUARD = threading.Lock()


def device_lock(serial: str | None) -> threading.Lock:
    """每设备一把锁：同 serial 的任务**串行**执行（评审 P1-4）。

    并发跑同一部手机会互抢点击、污染控件树——产出**看着正常、实际无效**的报告，
    且不报错。第二个任务排队等待（事件流显示 queued），而不是静默并发。"""
    key = serial or "__default__"
    with _DEVICE_LOCKS_GUARD:
        lock = _DEVICE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DEVICE_LOCKS[key] = lock
        return lock


def operator_of(body: dict | None) -> str:
    """触发者身份（团队协作，发起人 2026-10-12）：前端所有 POST 自动注入 operator
    （首次进入时填写的名字，存 localStorage）。截断 40 字符防滥用。

    空名兜底为「本机」（2026-10-25 方案 B）：单人模式不再要求设名，run.json/审计
    仍记 operator 字段；共享模式（--share）下 seats.enforce 已保证走到这里的名字非空。"""
    return str((body or {}).get("operator") or "").strip()[:40] or "本机"


def _append_event(job_id: str, step_id: str, status: str, detail: str = "",
                  duration_ms: int | None = None, screenshot: str | None = None) -> None:
    """任务事件追加（持 _JOB_LOCK）。worker 闭包 emit 与异常/看门狗路径共用同一实现。"""
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job["seq"] = job.get("seq", 0) + 1
        # 任何事件都算「任务在推进」——看门狗据此判无活动，而不是按总时长杀（2026-10-21）
        job["last_activity_ts"] = time.time()
        events = job.setdefault("events", [])  # 容错：看门狗/测试手工造的 job 可能无 events 键
        events.append({"seq": job["seq"], "step_id": step_id, "status": status,
                       "ts": _now(), "duration_ms": duration_ms,
                       "detail": detail, "screenshot": screenshot})


def start_job(kind: str, fn, meta: dict | None = None) -> str:
    """启动后台任务。fn(emit, cancel) 接收两个句柄：
    - emit(step_id, status, detail="", duration_ms=None, screenshot=None)：追加步骤事件（seq 单调递增）；
    - cancel：threading.Event，POST /api/job/cancel 置位——fn 在合适的检查点自行优雅退出。

    meta：附加到任务记录的键值（如 serial/platform）——设备投屏页按设备过滤日志用。"""
    job_id = uuid.uuid4().hex[:10]
    cancel = threading.Event()
    with _JOB_LOCK:
        _JOBS[job_id] = {"kind": kind, "status": "running", "started_at": _now(),
                         "started_ts": time.time(), "last_activity_ts": time.time(),
                         "summary": None, "error": None, "link": None,
                         "events": [], "seq": 0, "cancel": cancel, "meta": meta or {}}

    def emit(step_id: str, status: str, detail: str = "",
             duration_ms: int | None = None, screenshot: str | None = None) -> None:
        _append_event(job_id, step_id, status, detail, duration_ms, screenshot)

    def worker():
        try:
            summary, link = fn(emit, cancel)
            with _JOB_LOCK:
                _JOBS[job_id].update(status=("cancelled" if cancel.is_set() else "done"),
                                     summary=summary, link=link)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            with _JOB_LOCK:
                _JOBS[job_id].update(status="error", error=err)
            # 失败原因进事件流（2026-10-13 用户反馈：事件日志只停在最后一条 running，
            # 失败原因只躺在 /api/job 的 error 字段里，前端执行日志区完全看不到）
            _append_event(job_id, "job", "error", err[:300])

    threading.Thread(target=worker, daemon=True).start()
    with _JOB_LOCK:  # 上限淘汰（防长会话内存增长）
        while len(_JOBS) > 60:
            oldest = min(_JOBS, key=lambda j: _JOBS[j].get("started_ts", 0))
            if _JOBS[oldest]["status"] == "running":
                break
            del _JOBS[oldest]
            if len(_JOBS) <= 50:
                break
    return job_id


def sweep_stale_jobs(timeout_seconds: int = JOB_TIMEOUT_SECONDS) -> int:
    """看门狗：**无活动**超过阈值才标记 error（执行线程无法强杀，但 UI 立即解锁并给出指引）。

    2026-10-21 修正：判据从 `started_ts`（总时长）改为 `last_activity_ts`（最后一次事件）。
    旧实现会把"正常推进但整体较长"的批量转换误杀（16 条串行 >15 分钟很常见），
    还给出设备归因——导入任务根本不碰设备。任何 emit 都会刷新活动时间。
    """
    now = time.time()
    swept: list[str] = []
    with _JOB_LOCK:
        for job_id, job in _JOBS.items():
            if job["status"] != "running":
                continue
            last = job.get("last_activity_ts") or job.get("started_ts", now)
            if now - last <= timeout_seconds:
                continue
            job["status"] = "error"
            hint = _TIMEOUT_HINTS.get(str(job.get("kind") or ""), _DEVICE_TIMEOUT_HINT)
            job["error"] = (f"任务超过 {max(1, int(timeout_seconds // 60))} 分钟无活动，已标记超时——"
                            f"{hint}")
            swept.append(job_id)
    for job_id in swept:  # 锁外追加（_append_event 自行拿锁，嵌套会死锁）
        _append_event(job_id, "job", "error", str(_JOBS.get(job_id, {}).get("error") or "")[:300])
    return len(swept)


def watchdog_loop() -> None:
    while True:
        time.sleep(30)
        try:
            sweep_stale_jobs()
        except Exception:
            pass


def safe_join(base: Path, relative: str) -> Path | None:
    """路径安全拼接：用 is_relative_to 判定包含关系。

    不用 startswith：那是字符串前缀匹配，`../artifacts_secret` 这类路径能绕过它。
    返回 None 表示越界。"""
    try:
        target = (base / relative).resolve()
    except (OSError, ValueError):
        return None
    if not target.is_relative_to(base.resolve()):
        return None
    return target


def lan_urls(port: int = 7420) -> list[str]:
    """枚举本机局域网地址（T55 P0：--share 内网分享用）。

    UDP connect 不发包，只查本机路由表取主出口 IP；再补 hostname 解析的其余网卡。
    纯函数式尽力而为：任何失败都静默跳过（无网/纯回环环境返回空表）。
    """
    import socket

    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            ips.append(probe.getsockname()[0])
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return [f"http://{ip}:{port}" for ip in ips]
