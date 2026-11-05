"""C3：GET 不得写盘（2026-11-05）。

旧实现 `case_meta()` 在 GET 路径发现新用例就 `write_text(artifacts/.case_no.json)`：
只读挂载/只读权限的工作区一访问 `/api/state` 就 PermissionError → 500。
这里用**真实 chmod** 验证（不 mock 权限），并回归写接口仍能落盘。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from insta_engine.web_api import device, state, workspace


def _mk_case(cases: Path, name: str, package: str = "com.demo") -> None:
    (cases / name).write_text(json.dumps({
        "caseId": f"c-{name}", "name": name,
        "target": {"package": package},
        "steps": [{"id": "s1", "action": "launch"}],
    }, ensure_ascii=False), encoding="utf-8")


def _readonly(path: Path) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)


def _writable(path: Path) -> None:
    os.chmod(path, stat.S_IRWXU)


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """隔离用户目录 + 桩掉真机探测（只关心文件副作用，不关心插没插手机）。"""
    monkeypatch.setenv("MAT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(device, "_probe_devices", lambda: [])
    device.reset_devices_snapshot()
    (tmp_path / "cases").mkdir()
    (tmp_path / "artifacts").mkdir()
    yield tmp_path
    device.reset_devices_snapshot()


def test_api_state_readonly_workspace_returns_200_and_writes_nothing(ws):
    """只读 artifacts 下 GET /api/state：200，且目录内**不产生任何新文件**。"""
    _mk_case(ws / "cases", "a.json")
    _mk_case(ws / "cases", "b.json")
    art = ws / "artifacts"
    before = sorted(os.listdir(art))
    _readonly(art)
    try:
        code, data = state.api_state(ws)
    finally:
        _writable(art)
    assert code == 200, "只读工作区不应让整个 /api/state 500"
    assert sorted(data["caseMeta"]) == ["a.json", "b.json"]
    assert sorted(os.listdir(art)) == before, "GET 不得写盘（.case_no.json 也不许出现）"


def test_readonly_case_meta_derives_numbers_in_memory_and_keeps_them(ws):
    """读不到注册表也要有编号：内存推导、删除不回收、新增续号，全程不落盘。"""
    _mk_case(ws / "cases", "a.json")
    _mk_case(ws / "cases", "b.json")
    _readonly(ws / "artifacts")
    try:
        meta1 = workspace.case_meta(ws)
        assert meta1["a.json"]["no"] == 1
        assert meta1["b.json"]["no"] == 2
        (ws / "cases" / "a.json").unlink()
        _mk_case(ws / "cases", "c.json")
        meta2 = workspace.case_meta(ws)
        assert meta2["b.json"]["no"] == 2
        assert meta2["c.json"]["no"] == 3, "编号在进程内必须稳定，且删除不回收"
        assert not (ws / "artifacts" / ".case_no.json").exists()
    finally:
        _writable(ws / "artifacts")


def test_write_endpoint_persists_numbers(ws):
    """可写目录：POST 保存用例后编号表落盘（GET 只管读）。"""
    from insta_engine.web_api.cases import api_case_save

    body = {"name": "one.json", "content": json.dumps({
        "caseId": "c-one", "name": "one", "target": {"package": "com.demo"},
        "steps": [{"id": "s1", "action": "launch"}],
    }, ensure_ascii=False)}
    code, payload = api_case_save(ws, body)
    assert code == 200 and payload["ok"] is True
    reg = ws / "artifacts" / ".case_no.json"
    assert reg.exists(), "写接口必须落盘编号表"
    assert json.loads(reg.read_text(encoding="utf-8"))["one.json"] == 1
    assert workspace.case_meta(ws)["one.json"]["no"] == 1
    # 无新编号时不重复写盘（只读部署因此不会制造无意义的失败）
    assert workspace.persist_case_numbers(ws) is True


def test_persist_is_best_effort_on_readonly(ws):
    """只读目录下写接口的落盘辅助不抛异常（保存本身已成功，编号晚一轮持久化即可）。"""
    _mk_case(ws / "cases", "a.json")
    _readonly(ws / "artifacts")
    try:
        assert workspace.persist_case_numbers(ws) is False
        assert not (ws / "artifacts" / ".case_no.json").exists()
    finally:
        _writable(ws / "artifacts")
