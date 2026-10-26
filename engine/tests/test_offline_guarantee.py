"""P0-4：离线保证——MAT_OFFLINE=1 时 run 全程零 socket 出网；生成/激活在发请求前被拒。"""

from __future__ import annotations

import socket

import pytest

from insta_engine.dsl import TestCase


class _Boom:
    """任何 socket 构造都让测试失败——零出网断言的核心（比 patch connect 更严）。"""

    def __init__(self, *a, **k):
        raise AssertionError("离线模式下发生了 socket 连接（run 路径出网）")


def test_run_makes_no_network_calls(tmp_path, monkeypatch):
    from test_v1 import FakeDriver  # 复用既有最小设备桩

    from insta_engine.runner import Runner

    monkeypatch.setenv("MAT_OFFLINE", "1")
    monkeypatch.setattr(socket, "socket", _Boom)
    monkeypatch.setattr("insta_engine.adb.logcat_clear", lambda *a, **k: True)
    monkeypatch.setattr("insta_engine.adb.logcat_tail", lambda *a, **k: "")
    case = TestCase.model_validate({
        "caseId": "case_offline", "name": "离线零出网",
        "target": {"package": "com.insta.demo"},
        "steps": [
            {"id": "s1", "intent": "启动", "action": "launch",
             "params": {"package": "com.insta.demo"}},
            {"id": "s2", "intent": "等待", "action": "wait", "params": {"ms": 5}},
        ],
    })
    run = Runner(FakeDriver(), tmp_path).run(case)
    assert run.status == "passed", [(s.step_id, s.status, s.error) for s in run.steps]


def test_offline_blocks_generation_fast(monkeypatch):
    from insta_engine.ai_client import GenClient
    from insta_engine.offline import OfflineError

    monkeypatch.setenv("MAT_OFFLINE", "1")
    monkeypatch.setenv("GEN_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("GEN_API_KEY", "k")
    monkeypatch.setenv("GEN_MODEL", "m")
    client = GenClient.from_env()
    with pytest.raises(OfflineError, match="离线模式"):
        client.complete_raw_text([{"role": "user", "content": "x"}], context="生成")


def test_cloudapi_offline_raises_before_request(monkeypatch):
    from insta_engine import cloudapi

    monkeypatch.setenv("MAT_OFFLINE", "1")
    with pytest.raises(cloudapi.CloudError, match="离线模式"):
        cloudapi.get("/usage")


def test_notify_skipped_offline(monkeypatch):
    import requests

    from insta_engine import notify

    monkeypatch.setenv("MAT_OFFLINE", "1")
    monkeypatch.setenv("MAT_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: pytest.fail("离线模式不许发通知"))
    assert notify.send_run_finished("x") is False
