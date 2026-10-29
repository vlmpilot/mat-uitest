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


# -- 本机端点放行（2026-10-26）--------------------------------------------------
# 离线模式承诺的是「数据不出本机」，而 127.0.0.1 / localhost 上的服务不产生出网
# 流量。此前无条件拒绝，导致「私有化交付包（默认写 MAT_OFFLINE=1）+ 本地小模型」
# 这一本该配套的组合直接不可用——同一份数据不出内网，却被自己的闸拦住。

@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:8080/v1",     # mlx-vlm / LM Studio
    "http://localhost:11434/v1",    # Ollama
    "http://[::1]:8080/v1",         # IPv6 本机
    "127.0.0.1:8899/v1",            # 容错：不带 scheme
    "http://127.0.1.1:8080/v1",     # 127.0.0.0/8 整段都是回环
])
def test_loopback_endpoint_is_allowed_offline(monkeypatch, endpoint):
    from insta_engine.offline import require_online

    monkeypatch.setenv("MAT_OFFLINE", "1")
    require_online("视觉兜底（BYOK 端点）", endpoint=endpoint)  # 不抛即通过


def test_remote_endpoint_still_blocked_offline(monkeypatch):
    """放行只针对本机：公网/内网其它主机照旧拒绝。"""
    from insta_engine.offline import OfflineError, require_online

    monkeypatch.setenv("MAT_OFFLINE", "1")
    for endpoint in ("https://api.example.com/v1", "http://192.168.1.9:8080/v1"):
        with pytest.raises(OfflineError, match="离线模式"):
            require_online("视觉兜底（BYOK 端点）", endpoint=endpoint)


def test_endpoint_without_flag_is_still_blocked(monkeypatch):
    """不传端点（官方代理等）行为不变——宁可拦错，不可放错。"""
    from insta_engine.offline import OfflineError, require_online

    monkeypatch.setenv("MAT_OFFLINE", "1")
    with pytest.raises(OfflineError):
        require_online("生成（任务 gen）")


@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1.evil.com/v1",   # 前缀骗术
    "http://127.0.0.1@evil.com/v1",   # userinfo 骗术（真实 host 是 evil.com）
    "https://localhost.evil.com/v1",
    "",
])
def test_loopback_lookalikes_are_not_local(endpoint):
    from insta_engine.offline import is_loopback

    assert is_loopback(endpoint) is False


def test_byok_generation_works_offline_against_loopback(tmp_path, monkeypatch):
    """端到端：MAT_OFFLINE=1 + 本机端点 ⇒ 真能调通（此前必抛 OfflineError）。"""
    from insta_engine.ai_client import GenClient

    class _Resp:
        status_code = 200
        headers: dict = {}

        def json(self):
            return {"model": "local-vl",
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                    "choices": [{"message": {"content": '{"found": true}'}}]}

        def raise_for_status(self):
            return None

    monkeypatch.setenv("INSTA_HOME", str(tmp_path))
    monkeypatch.setenv("MAT_OFFLINE", "1")
    monkeypatch.setenv("GEN_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("GEN_API_KEY", "k")
    monkeypatch.setenv("GEN_MODEL", "local-vl")
    monkeypatch.setenv("GEN_PROMPT_MODE", "local")
    monkeypatch.setattr("insta_engine.ai_client.requests.post", lambda *a, **k: _Resp())

    client = GenClient.from_env()
    assert client.complete_raw_text([{"role": "user", "content": "x"}], context="兜底") \
        == '{"found": true}'


def test_api_step_allows_loopback_mock_server_offline(tmp_path, monkeypatch):
    """api 步骤同理：本机 mock 服务的断言不出网，离线模式应放行。"""
    from insta_engine import api_client

    class _Resp:
        status_code = 200
        headers: dict = {}
        text = '{"ok": true}'

        def json(self):
            return {"ok": True}

    monkeypatch.setenv("MAT_OFFLINE", "1")
    monkeypatch.setattr("insta_engine.api_client.requests.request",
                        lambda *a, **k: _Resp())

    evidence = api_client.run_api(
        {"method": "GET", "url": "${base}/health",
         "expect": [{"desc": "状态码", "status": 200}]},
        {"base": "http://127.0.0.1:8899"},
    )
    assert evidence["passed"] is True, evidence.get("error")
