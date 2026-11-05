"""评审修复的回归测试（2026-11-04）。

覆盖四组：
1. 产物/截图下载走分块 + Range，内存不随文件大小线性增长；
2. `_JOBS` 上限淘汰会跳过 running（长任务不再让整张表无限增长）；
3. 看门狗判超时后，执行线程晚到的 done 不得覆盖 error；
4. `usage.json` 并发计数不丢、损坏时回退 `.bak` 且保留损坏副本。
"""

from __future__ import annotations

import json
import threading
import time
import tracemalloc
from types import SimpleNamespace

from insta_engine.quota import UsageStore
from insta_engine.web_api import runtime as rt
from insta_engine.web_server import ConsoleHandler

# -- 1. 分块下载 ------------------------------------------------------------

class _Sink:
    def __init__(self) -> None:
        self.total = 0

    def write(self, chunk: bytes) -> None:
        self.total += len(chunk)


def _handler(headers: dict[str, str] | None = None) -> SimpleNamespace:
    recorded: list[tuple[str, str]] = []

    def send_header(name: str, value: str) -> None:
        recorded.append((name, value))

    sink = _Sink()
    handler = SimpleNamespace(
        headers=headers or {},
        wfile=sink,
        send_response=lambda code: recorded.append((":status", str(code))),
        send_header=send_header,
        end_headers=lambda: None,
        _FILE_CHUNK=ConsoleHandler._FILE_CHUNK,
        recorded=recorded,
    )
    return handler


def _sparse_file(path, size: int) -> None:
    with path.open("wb") as fh:
        fh.truncate(size)


def test_send_file_does_not_buffer_whole_file(tmp_path):
    """20MB 文件下峰值内存应远小于文件本身（分块 64KB）。"""
    target = tmp_path / "big.zip"
    _sparse_file(target, 20 * 1024 * 1024)
    handler = _handler()

    tracemalloc.start()
    try:
        ConsoleHandler._send_file(handler, target, "application/zip")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert handler.wfile.total == 20 * 1024 * 1024
    assert peak < 4 * 1024 * 1024, f"峰值 {peak} B —— 仍在整份读入内存"


def test_send_file_supports_range(tmp_path):
    target = tmp_path / "clip.mp4"
    _sparse_file(target, 1000)
    handler = _handler({"Range": "bytes=10-109"})

    ConsoleHandler._send_file(handler, target, "video/mp4")

    status = dict(handler.recorded)[":status"]
    headers = dict(handler.recorded)
    assert status == "206"
    assert headers["Content-Range"] == "bytes 10-109/1000"
    assert headers["Content-Length"] == "100"
    assert headers["Accept-Ranges"] == "bytes"
    assert handler.wfile.total == 100


# -- 2/3. 任务表与看门狗 ----------------------------------------------------

def _wait_status(job_id: str, expected: str, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with rt._JOB_LOCK:
            status = rt._JOBS.get(job_id, {}).get("status")
        if status == expected:
            return True
        time.sleep(0.01)
    return False


def test_job_eviction_skips_running_job(tmp_path):
    """1 个长任务（最旧）+ 100 个短任务：表仍受限，且长任务不被误删。"""
    with rt._JOB_LOCK:
        rt._JOBS.clear()
    release = threading.Event()
    long_id = rt.start_job(
        "run", lambda emit, cancel: (release.wait(10), ("long", None))[1])
    try:
        for _ in range(100):
            short_id = rt.start_job("run", lambda emit, cancel: ("ok", None))
            assert _wait_status(short_id, "done", 2.0)
        with rt._JOB_LOCK:
            size = len(rt._JOBS)
        assert size <= 60, f"任务表 {size} 条 —— running 的长任务把淘汰卡住了"
        assert long_id in rt._JOBS, "还在跑的长任务不得被淘汰"
    finally:
        release.set()
        _wait_status(long_id, "done", 5.0)
        with rt._JOB_LOCK:
            rt._JOBS.clear()


def test_timeout_status_not_overwritten_by_late_done(tmp_path):
    """看门狗置 error 后，worker 收尾不得把状态改回 done，并应置取消位。"""
    with rt._JOB_LOCK:
        rt._JOBS.clear()
    release = threading.Event()
    job_id = rt.start_job(
        "run", lambda emit, cancel: (release.wait(10), ("ok", None))[1])
    try:
        with rt._JOB_LOCK:
            rt._JOBS[job_id]["last_activity_ts"] = time.time() - 3600
        assert rt.sweep_stale_jobs(60) == 1
        with rt._JOB_LOCK:
            job = rt._JOBS[job_id]
            assert job["status"] == "error"
            assert job["cancel"].is_set(), "超时必须置取消位，执行线程才会尽快释放设备锁"
        release.set()
        assert _wait_status(job_id, "error", 3.0), "晚到的 done 覆盖了超时状态"
    finally:
        release.set()
        with rt._JOB_LOCK:
            rt._JOBS.clear()


# -- 4. usage.json ----------------------------------------------------------

def test_usage_consume_is_concurrency_safe(tmp_path):
    store = UsageStore(tmp_path / "usage.json", source="byok")

    def work() -> None:
        for _ in range(50):
            store.consume(1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert store.snapshot()["gen_count"] == 400, "并发记账丢计数"


def test_usage_corrupt_file_falls_back_to_backup(tmp_path):
    path = tmp_path / "usage.json"
    store = UsageStore(path, source="byok")
    store.consume(5)
    store.consume(1)  # 第二次成功写盘会生成 .bak（内容为 5）

    path.write_text("{ 这不是 JSON", encoding="utf-8")

    assert store.snapshot()["gen_count"] == 5, "损坏后归零 —— 应回退到上一次成功写盘"
    backups = list(tmp_path.glob("usage.json.corrupt-*"))
    assert backups, "损坏文件必须留副本，不能被下一次写入覆盖"
    assert backups[0].read_text(encoding="utf-8") == "{ 这不是 JSON"


def test_internal_error_hides_exception_details():
    """500 响应不得回传异常类型/消息（可能含绝对路径与内部参数）。"""
    captured: dict = {}

    def fake_json(code: int, obj: dict) -> None:
        captured.update(code=code, obj=obj)

    handler = SimpleNamespace(_json=fake_json)
    ConsoleHandler._internal_error(handler, ValueError("/Users/secret/boom"))

    assert captured["code"] == 500
    assert captured["obj"]["error"] == "内部错误，请查看控制台日志"
    assert "ValueError" not in json.dumps(captured["obj"], ensure_ascii=False)
    assert "/Users/secret" not in json.dumps(captured["obj"], ensure_ascii=False)
    assert len(captured["obj"]["request_id"]) == 8
