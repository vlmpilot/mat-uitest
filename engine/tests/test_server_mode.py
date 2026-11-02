"""客户端 server 模式（轻云代理）测试：prompt 服务端组装、Key 不外泄、模型服务端决定。

对应「服务端组装 prompt」的保护能力——这些用例是「保护是否真的生效」的回归门禁。
"""

from __future__ import annotations

import io
import json

import pytest
import requests
from PIL import Image

from insta_engine import vision
from insta_engine.ai_client import GenClient, GenError, complete_task_raw, complete_task_text


class FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        calls.append({"url": url, "headers": headers or {}, "json": json})
        return FakeResp({"content": '```json\n{"caseId": "c1", "steps": [{"id": "s1", "action": "tap"}]}\n```'})

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("GEN_PROMPT_MODE", "GEN_MODEL", "GEN_PROMPT", "GEN_VL_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_promptstore_mode_switch(monkeypatch):
    from insta_engine import promptstore

    assert promptstore.is_server() is False
    monkeypatch.setenv("GEN_PROMPT_MODE", "server")
    assert promptstore.is_server() is True
    monkeypatch.setenv("GEN_PROMPT_MODE", "SERVER ")  # 容错大小写与空白
    assert promptstore.is_server() is True


def test_from_env_server_mode_needs_no_model_or_prompt(monkeypatch):
    monkeypatch.setenv("GEN_PROMPT_MODE", "server")
    monkeypatch.setenv("GEN_BASE_URL", "https://cloud.mat.test")
    monkeypatch.setenv("GEN_API_KEY", "user-token")
    client = GenClient.from_env()
    assert client.prompt_mode == "server"
    assert client.prompt_path is None  # 本地无需 prompt 文件
    assert client.model == ""


def test_from_env_local_mode_requires_model(monkeypatch):
    monkeypatch.setenv("GEN_BASE_URL", "https://maas.invalid/v1")
    monkeypatch.setenv("GEN_API_KEY", "sk-user")
    with pytest.raises(GenError, match="GEN_MODEL"):
        GenClient.from_env()


def test_server_mode_posts_to_proxy_and_never_sends_prompt(captured):
    client = GenClient("https://cloud.mat.test", "user-token", "", prompt_mode="server")
    data = complete_task_raw(client, "gen", {"nl": "打开设置", "app_info": {"package": "com.x"}}, "生成")

    assert data["caseId"] == "c1"  # 围栏已剥离 + JSON 解析成功
    call = captured[0]
    assert call["url"] == "https://cloud.mat.test/mat/complete"
    assert call["headers"]["Authorization"] == "Bearer user-token"  # 用户 token，不是 MaaS Key
    assert call["json"]["task"] == "gen"
    assert call["json"]["variables"]["nl"] == "打开设置"
    # 请求体里只有原始材料，没有任何 prompt 文本
    body = json.dumps(call["json"], ensure_ascii=False)
    assert "prompt" not in call["json"]
    assert "打开设置" in body


def test_server_mode_text_entry(captured):
    client = GenClient("https://cloud.mat.test", "user-token", "", prompt_mode="server")
    text = complete_task_text(client, "map", {"headers": ["a"], "rows": [["1"]]}, "列映射建议")
    assert text == '{"caseId": "c1", "steps": [{"id": "s1", "action": "tap"}]}'
    assert captured[0]["json"]["task"] == "map"


def test_server_mode_quota_429_surfaces_detail(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        return FakeResp({"detail": "gen 额度不足（本月已用 10/10）"}, status=429)

    monkeypatch.setattr(requests, "post", fake_post)
    client = GenClient("https://cloud.mat.test", "user-token", "", prompt_mode="server")
    with pytest.raises(GenError, match="额度不足"):
        complete_task_raw(client, "gen", {"nl": "x"}, "生成")


def test_server_mode_generate_returns_case(captured):
    client = GenClient("https://cloud.mat.test", "user-token", "", prompt_mode="server")
    case = client.generate("打开设置")
    assert case.case_id == "c1"


def test_vision_enabled_in_server_mode_without_vl_model(monkeypatch):
    monkeypatch.setenv("GEN_PROMPT_MODE", "server")
    monkeypatch.setenv("GEN_API_KEY", "user-token")
    monkeypatch.delenv("GEN_VL_MODEL", raising=False)
    assert vision.enabled() is True


def test_vision_disabled_without_key(monkeypatch):
    monkeypatch.setenv("GEN_PROMPT_MODE", "server")
    monkeypatch.delenv("GEN_API_KEY", raising=False)
    assert vision.enabled() is False


def test_vision_server_mode_routes_through_proxy(monkeypatch):
    """server 模式下视觉兜底也走代理（本地无 prompts/vision-locate.prompt 也能用）。"""
    calls: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        calls.append({"url": url, "json": json})
        return FakeResp({"content": '{"found": true, "confidence": 0.9, "x": 0.3, "y": 0.4}'})

    monkeypatch.setattr(requests, "post", fake_post)
    buf = io.BytesIO()
    Image.new("RGB", (200, 400)).save(buf, format="PNG")

    client = GenClient("https://cloud.mat.test", "user-token", "", prompt_mode="server")
    vision._CACHE.clear()
    hit = vision.locate(client, buf.getvalue(), "点击我的", "我的", cache_key="srv-1")

    assert hit == (60, 160)
    assert calls[0]["url"] == "https://cloud.mat.test/mat/complete"
    assert calls[0]["json"]["task"] == "vision-locate"
    assert calls[0]["json"]["images"]  # 截图以 base64 透传
    assert calls[0]["json"]["variables"]["target"] == "我的"


def test_local_mode_still_reads_workspace_prompt(tmp_path, monkeypatch):
    """回归：local 模式行为不变（从工作区读 prompt 并本地拼装）。"""
    ws = tmp_path / "prompts"
    ws.mkdir()
    (ws / "map.prompt").write_text("LOCAL-MAP {headers}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    captured_payload: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        captured_payload.append(json)
        return FakeResp({"choices": [{"message": {"content": '{"title": 0}'}}]})

    monkeypatch.setattr(requests, "post", fake_post)
    client = GenClient("https://maas.invalid/v1", "sk-local", "qwen-plus", prompt_mode="local")
    data = complete_task_raw(client, "map", {"headers": ["a"]}, "列映射建议")

    assert data == {"title": 0}
    sent = json.dumps(captured_payload[0], ensure_ascii=False)
    assert "LOCAL-MAP" in sent  # 本地 prompt 被拼进 messages


# -- 路由偏好（MAT_ROUTE，2026-11-02）--------------------------------------
#
# 默认自部署；只在**显式**设过时才带 `X-Mat-Route`——不带 = 按服务端策略。
# 老服务端不认识这个头，忽略即可（退化成本地优先，不是报错），所以不带也是兼容策略。


def _one_call(captured):
    client = GenClient("https://cloud.mat.test", "user-token", "", prompt_mode="server")
    complete_task_raw(client, "gen", {"nl": "打开设置", "app_info": {"package": "com.x"}}, "生成")
    return captured[0]


def test_no_route_header_by_default(captured, monkeypatch):
    monkeypatch.delenv("MAT_ROUTE", raising=False)
    assert "X-Mat-Route" not in _one_call(captured)["headers"]


def test_route_cloud_header_sent(captured, monkeypatch):
    monkeypatch.setenv("MAT_ROUTE", "cloud")
    assert _one_call(captured)["headers"]["X-Mat-Route"] == "cloud"


def test_route_local_header_sent_and_normalized(captured, monkeypatch):
    monkeypatch.setenv("MAT_ROUTE", " LOCAL ")
    assert _one_call(captured)["headers"]["X-Mat-Route"] == "local"


def test_unknown_route_value_sends_nothing(captured, monkeypatch):
    """拼错（clod）按 auto 处理：不带头发的是安全方向（服务端本地优先），不猜成 cloud。"""
    monkeypatch.setenv("MAT_ROUTE", "clod")
    assert "X-Mat-Route" not in _one_call(captured)["headers"]


# -- 失败要说人话（2026-11-02）----------------------------------------------
#
# 服务端在 503 里写了「本地不可用、不会改走云端 + 三条出路」，客户端原来只记
# `HTTP 503` —— 这段可行动信息等于没写。用户看到的必须是完整那一句。


def test_5xx_detail_reaches_the_user(monkeypatch):
    import insta_engine.ai_client as ai_mod

    hits = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        hits["n"] += 1
        return FakeResp({"detail": "本站自部署的文本模型当前不可用（不会自动改走云端）"}, 503)

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(ai_mod.time, "sleep", lambda *_: None)  # 别真等 2s+4s

    client = GenClient("https://cloud.mat.test", "user-token", "", prompt_mode="server")
    with pytest.raises(GenError) as exc:
        complete_task_raw(client, "gen", {"nl": "打开设置", "app_info": {"package": "com.x"}},
                          "生成")
    assert "不会自动改走云端" in str(exc.value)
    assert hits["n"] == 3, "5xx 应重试 3 次后再失败"


def test_5xx_without_json_falls_back_to_status(monkeypatch):
    import insta_engine.ai_client as ai_mod

    class Plain:
        status_code = 502
        text = "bad gateway"

        def json(self):
            raise ValueError("not json")

        def raise_for_status(self):
            raise requests.HTTPError("502")

    monkeypatch.setattr(requests, "post", lambda *a, **k: Plain())
    monkeypatch.setattr(ai_mod.time, "sleep", lambda *_: None)
    client = GenClient("https://cloud.mat.test", "user-token", "", prompt_mode="server")
    with pytest.raises(GenError, match="bad gateway"):
        complete_task_raw(client, "gen", {"nl": "x", "app_info": {"package": "com.x"}}, "生成")


def test_chat_urls_keep_plain_path_as_primary():
    """默认路径不能改：厂商 API 可能自带版本段（智谱 /api/paas/v4）。"""
    from insta_engine.ai_client import _chat_urls

    primary, fallback = _chat_urls("https://open.bigmodel.cn/api/paas/v4")
    assert primary == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert fallback.endswith("/api/paas/v4/v1/chat/completions")  # 只在 404 时才用


def test_chat_urls_no_fallback_when_base_already_v1():
    from insta_engine.ai_client import _chat_urls

    primary, fallback = _chat_urls("https://api.example.com/v1/")
    assert primary == "https://api.example.com/v1/chat/completions"
    assert fallback == ""


def test_chat_urls_offer_v1_for_bare_local_endpoint():
    """本机服务（vlm_judge_server）只认 /v1，靠这条回退才连得上。"""
    from insta_engine.ai_client import _chat_urls

    primary, fallback = _chat_urls("http://127.0.0.1:8915")
    assert primary == "http://127.0.0.1:8915/chat/completions"
    assert fallback == "http://127.0.0.1:8915/v1/chat/completions"


def test_404_switches_to_v1_and_succeeds(monkeypatch):
    """真实链路：先 404，回退到 /v1 后拿到结果（不吞错、不重复计次）。"""
    import insta_engine.ai_client as ai_mod

    seen: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        seen.append(url)
        if url.endswith("/v1/chat/completions"):
            return FakeResp({"choices": [{"message": {"content": "ok"}}]})
        return FakeResp({"error": "未知路径"}, 404)

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(ai_mod.time, "sleep", lambda *_: None)

    client = GenClient("http://127.0.0.1:8915", "k", "m", prompt_mode="local",
                       prompt_path=None)
    out = complete_task_text(client, "vision-locate", {"intent": "x", "target": "y"}, "定位")
    assert out == "ok"
    assert seen == ["http://127.0.0.1:8915/chat/completions",
                    "http://127.0.0.1:8915/v1/chat/completions"]
