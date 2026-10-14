"""拆分生成（一次 NL → N 个用例文件）的离线测试（设计见 MAT-用例拆分功能详细设计-2026-10-13）。

锁死点：
1. `_validate_multi` 三形态兼容 + salvage（单条坏不整批死，全坏才回喂重试）；
2. `_validate_single` 兼容 cases 数组取首条（校准/CLI 单用例语义不被拆分 prompt 打挂）；
3. `generate_cases` 逐条过质量闸门（R8）——无 launch（A5）必须被抓到；
4. api_gen 落盘命名 / split_batch 溯源 / caseId 去重 / 配额 1 次/任务（不随条数涨）。
"""

from __future__ import annotations

import json
import time

import pytest

from insta_engine import web_server
from insta_engine.ai_client import GenClient, GenError, _validate_multi, _validate_single
from insta_engine.dsl import TestCase as DslTestCase  # 别名避开 pytest 收集 Test* 类


def _case(cid: str = "c1", launch: bool = True) -> dict:
    steps = []
    if launch:
        steps.append({"id": "s0", "intent": "启动", "action": "launch",
                      "params": {"package": "com.mat.demo"}})
    steps.append({"id": "s1", "intent": "点击登录", "action": "tap",
                  "target": {"strategy": "text", "value": "登录"}})
    return {"caseId": cid, "name": f"入口-{cid}", "target": {"package": "com.mat.demo"},
            "steps": steps}


class _StubClient:
    """complete_task_raw 桩（带 task 形参，走按命令选模型分支）。"""

    def __init__(self, payload):
        self._payload = payload

    def complete_raw_text(self, messages, context="", task=None):
        return json.dumps(self._payload, ensure_ascii=False)


def _gen_client_stub(payload) -> GenClient:
    """from_env 替身：真 GenClient 实例（generate_cases/闸门是真链路），只挡网络层。"""
    client = GenClient.__new__(GenClient)
    client.complete_raw_text = lambda messages, context="", task=None: \
        json.dumps(payload, ensure_ascii=False)
    return client


# -- validator -------------------------------------------------------------------

def test_validate_multi_accepts_three_shapes():
    """{"cases":[...]} / [...] / 单对象 {...} 都要能解析（模型跟不上格式时兜住）。"""
    for data in ({"cases": [_case(), _case("c2")]}, [_case(), _case("c2")], _case()):
        items, warns = _validate_multi(data)
        assert len(items) >= 1 and warns == []
        assert all(isinstance(c, DslTestCase) for c in items)


def test_validate_multi_salvage_keeps_valid_ones():
    """salvage：1 好 1 坏 → 合法条目保留 + 坏条目进 warns，不整批作废（T53c 口径）。"""
    items, warns = _validate_multi({"cases": [_case(), {"caseId": "bad"}]})
    assert len(items) == 1
    assert len(warns) == 1 and "第 2 条" in warns[0]


def test_validate_multi_all_invalid_raises():
    """全坏才视为内容级失败（回喂重试由 complete_task_raw 承接）。"""
    with pytest.raises(GenError, match="没有任何合法用例"):
        _validate_multi({"cases": [{"caseId": "bad"}, {"caseId": "worse"}]})


def test_validate_multi_empty_cases_raises():
    with pytest.raises(GenError, match="cases 数组为空"):
        _validate_multi({"cases": []})


def test_validate_single_takes_first_of_cases():
    """单用例语义的调用方（generate_calibrated / mat gen CLI）取首条，不因数组输出硬报错。"""
    assert _validate_single({"cases": [_case("first"), _case("second")]}).case_id == "first"
    assert _validate_single(_case("solo")).case_id == "solo"


# -- generate_cases：闸门接线（R8） -----------------------------------------

def test_generate_cases_applies_quality_gate():
    payload = {"cases": [_case("a"), _case("b", launch=False)]}
    client = _StubClient(payload)
    cases, warns = GenClient.generate_cases(client, "测主菜单", {"package": "com.mat.demo"})
    assert [c.case_id for c in cases] == ["a", "b"]
    no_launch = next(c for c in cases if c.case_id == "b")
    issues = no_launch.meta.get("quality_issues") or []
    assert issues, "无 launch 用例必须被闸门抓住（A5：没有切到被测 App）"
    assert warns == []


# -- api_gen：落盘 / 溯源 / 去重 / 配额 ---------------------------------------------

def _wait_job(jid: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = web_server._JOBS[jid]
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    return web_server._JOBS[jid]


def test_api_gen_splits_bills_once_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))  # 隔离配额与 license
    root = tmp_path / "ws"
    payload = {"cases": [_case("c1"), _case("c2"), _case("c1")]}  # 第三条重复 caseId
    monkeypatch.setattr(GenClient, "from_env", staticmethod(lambda: _gen_client_stub(payload)))

    code, obj = web_server.api_gen(root, {"nl": "测主菜单", "package": "com.mat.demo"})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "done", job

    files = sorted((root / "cases").glob("gen-*.json"))
    assert len(files) == 3, f"每个拆分用例独立落盘: {files}"
    assert files[0].name.endswith("-1.json") and files[2].name.endswith("-3.json"), files
    docs = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    ids = [d["caseId"] for d in docs]
    assert len(set(ids)) == 3, f"重复 caseId 必须去重: {ids}"
    batch = {d["meta"].get("split_batch") for d in docs}
    assert len(batch) == 1 and None not in batch, "同一次生成的 split_batch 必须一致"
    assert "3 个用例" in job["summary"] and "本月生成用量" in job["summary"], job["summary"]
    # 文件名后追加 #展示编号+标题（发起人 2026-10-13）：与用例列表同源编号
    assert "(#1 入口-c1)" in job["summary"] and "(#" in job["summary"], job["summary"]

    usage = json.loads((tmp_path / "home" / "usage.json").read_text(encoding="utf-8"))
    assert usage["gen_count"] == 1.0, "配额 1 次/任务：不随拆分条数增长"


def test_api_gen_truncates_over_max_split(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    root = tmp_path / "ws"
    payload = {"cases": [_case(f"c{i}") for i in range(10)]}
    monkeypatch.setattr(GenClient, "from_env", staticmethod(lambda: _gen_client_stub(payload)))

    code, obj = web_server.api_gen(root, {"nl": "x", "package": "com.mat.demo"})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "done", job
    assert len(list((root / "cases").glob("gen-*.json"))) == 8, "MAX_SPLIT=8 兜底截断"
    assert "截断 2 条" in job["summary"], job["summary"]


def test_api_gen_quota_precheck_fails_job(tmp_path, monkeypatch):
    """额度预检失败 → job error（模型不该被调用）；消息要给可行动提示。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    root = tmp_path / "ws"

    def exhausted(self, amount=1.0):
        from insta_engine.quota import QuotaExceeded
        raise QuotaExceeded("本月免费生成额度不足（剩 0 次，本次需 1 次）。")

    monkeypatch.setattr("insta_engine.quota.UsageStore.ensure_capacity", exhausted)
    monkeypatch.setattr(GenClient, "from_env",
                        staticmethod(lambda: pytest.fail("预检失败不许调模型")))

    code, obj = web_server.api_gen(root, {"nl": "x", "package": "com.mat.demo"})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "error", job
    assert "额度不足" in job["error"], job["error"]


def test_api_gen_emits_quality_events(tmp_path, monkeypatch):
    """质量问题逐条 emit 进事件流（快速查看入口，用户反馈 2026-10-13：不用翻 JSON meta）。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    root = tmp_path / "ws"
    payload = {"cases": [_case("ok"), _case("bad", launch=False)]}
    monkeypatch.setattr(GenClient, "from_env", staticmethod(lambda: _gen_client_stub(payload)))

    code, obj = web_server.api_gen(root, {"nl": "x", "package": "com.mat.demo"})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "done", job
    quality = [e for e in job["events"] if e["step_id"] == "quality"]
    assert quality and all(e["status"] == "warning" for e in quality), job["events"]
    assert any("bad" in (e.get("detail") or "") for e in quality)
    assert "详情见下方日志" in job["summary"], job["summary"]
