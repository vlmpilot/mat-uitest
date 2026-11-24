"""任务元数据落盘（`web_api/runtime.persist_job`）的契约测试。

评审 #11：任务注册表只在内存里，关掉 IDE 后同一个 job_id 会变成"从未存在过"。
落盘解决的是"跑完没有 / 结论是什么"这两个问题——不解决"能不能继续跑"。
"""

from __future__ import annotations

import pytest

from insta_engine.web_api import runtime


@pytest.fixture()
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_job_dir", lambda: tmp_path / "jobs")
    return tmp_path / "jobs"


def test_persist_and_load_roundtrip(jobs_dir):
    job = {"kind": "explore", "status": "done", "summary": {"pages": 12},
           "link": "/live/abc", "started_at": "2026-11-24T10:00:00+00:00",
           "events": [{"seq": 1}], "cancel": object()}
    runtime.persist_job("abc123", job)

    saved = runtime.load_persisted_job("abc123")
    assert saved is not None
    assert saved["status"] == "done" and saved["summary"] == {"pages": 12}
    assert saved["events_count"] == 1
    assert saved["persisted"] is True
    assert "note" in saved
    assert "cancel" not in saved, "线程句柄不可序列化，不能落盘"
    assert "events" not in saved, "事件明细不落盘（撑大文件且不影响结论）"


def test_load_missing_returns_none(jobs_dir):
    assert runtime.load_persisted_job("nope") is None


def test_load_rejects_path_traversal(jobs_dir):
    """job_id 来自请求体——`../` 不能逃出 jobs 目录。"""
    assert runtime.load_persisted_job("../etc/passwd") is None
    assert runtime.load_persisted_job("a/b") is None


def test_load_corrupt_file_returns_none(jobs_dir):
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "bad.json").write_text("{不是 json", encoding="utf-8")
    assert runtime.load_persisted_job("bad") is None


def test_persist_failure_does_not_raise(jobs_dir, monkeypatch):
    """落盘是观测设施：写不进去不能影响任务本身。"""
    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(runtime, "_job_dir", boom)
    runtime.persist_job("x", {"status": "done"})  # 不抛
