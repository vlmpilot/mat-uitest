"""页面索引域：/api/index（真机爬取/刷新页面图谱，长任务 job 化）。"""

from __future__ import annotations

from pathlib import Path

from .runtime import _require_fresh_code, device_lock, get_driver, operator_of, start_job


def api_index(root: Path, body: dict) -> tuple[int, dict]:
    """POST /api/index：与 CLI `mat index` 同语义（Review 页闭环）。

    - 空图谱（0 控件）在 CLI 是响亮报错——job 里同理（raise ⇒ error + 事件流可查），
      不能产出"看着正常、实际防幻觉底座已废"的图谱（B2）。
    - iOS 与 CLI 同口径诚实拦截（WDA 适配在 P3）。
    """
    package = str(body.get("package", "")).strip()
    if not package:
        return 400, {"error": "需要 package"}
    refresh = bool(body.get("refresh"))
    steps = min(max(int(body.get("steps", 30)), 5), 120)
    pages_limit = min(max(int(body.get("pages", 15)), 5), 60)
    serial = body.get("serial") or None
    platform = str(body.get("platform") or "android")
    if platform == "ios":
        return 400, {"error": "iOS 平台暂不支持 index（页面图谱的 WDA 适配在 P3）"
                              "——当前 iOS 可用：mat run --platform ios"}

    def job(emit, cancel):
        from ..page_index import load_or_crawl

        _require_fresh_code()
        lock = device_lock(serial)
        if not lock.acquire(blocking=False):
            emit("index", "queued", f"设备忙（{serial or '默认设备'}），排队等待")
            # 排队上限 60s（2026-10-12 用户报「重建一直转圈」）：先下发的任务占着
            # 设备锁时，无限阻塞会让本任务 running 数分钟、前端转圈到 10 分钟上限。
            # 超时快速失败并给去向，比无限排队诚实。
            if not lock.acquire(timeout=60):
                raise RuntimeError("设备忙（另一任务执行中，等待 60s 未释放）——请在执行中心"
                                   "确认当前任务完成后再重建，或稍后重试")
        try:
            emit("index", "running", f"{package}（refresh={refresh}）")
            index_path = root / "artifacts" / "page_index" / f"{package}.json"
            index, created = load_or_crawl(get_driver(serial, platform), package, index_path,
                                           refresh=refresh, max_steps=steps, max_pages=pages_limit)
            pages = index.get("pages", [])
            controls = sum(p.get("node_count", 0) for p in pages)
            if controls == 0:
                # **空图谱是静默失败的典型**（B2）：必须响亮报错而非落一份废底座。
                # crawl 现已自动拉起被测 App（page_index._ensure_foreground），走到这里
                # 说明拉起后仍收不到该包控件——多半是设备锁屏、App 崩溃秒退或包名错。
                raise RuntimeError(
                    "图谱为空（0 控件）：已自动尝试拉起被测 App 仍失败——常见原因："
                    "1) 设备锁屏或弹窗遮挡（解锁后重试）；2) App 启动即崩溃/秒退"
                    "（先手动打开确认可用）；3) 包名写错（用 adb shell pm list packages 核对）")
            return (f"{'已构建' if created else '已更新'}图谱: 页面 {len(pages)} · 控件 {controls}"), None
        finally:
            lock.release()

    return 200, {"job_id": start_job("index", job, meta={"serial": serial, "platform": platform,
                                                         "operator": operator_of(body)})}
