"""本地 thinking 模型适配：`GEN_DISABLE_THINKING`。

**起因**：Qwen3 全系（含 14B / 3.5-9B）默认开 thinking。2026-10-28 实测 Qwen3-14B：
16 个 token 全用在 `reasoning` 上，`content` 是 `null` —— MAT 解析 content，必然失败。
服务端（mlx_lm.server）没有关闭开关，只能在请求体里传 `chat_template_kwargs`。

**边界**：只在显式设置 `GEN_DISABLE_THINKING=1` 时才加这个字段。非 Qwen 的
OpenAI 兼容端点不认识它，默认带上会 400——不能为了本地模型破坏现有 BYOK 用户。
"""

from __future__ import annotations

import json

import pytest

from insta_engine.ai_client import GenClient


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers: dict = {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002 - 形参名对齐 requests
        captured.update(json or {})
        return _FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("insta_engine.ai_client.requests.post", fake_post)
    return captured


def _client() -> GenClient:
    return GenClient("http://127.0.0.1:9/v1", "local", "local-model", None, "local")


def test_thinking_disabled_adds_template_kwarg(monkeypatch):
    captured = _capture(monkeypatch)
    monkeypatch.setenv("GEN_DISABLE_THINKING", "1")
    _client().complete_raw_text([{"role": "user", "content": "hi"}])
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


def test_default_does_not_send_template_kwarg(monkeypatch):
    """默认不加：非 Qwen 端点不认识该字段，带了会 400。"""
    captured = _capture(monkeypatch)
    monkeypatch.delenv("GEN_DISABLE_THINKING", raising=False)
    _client().complete_raw_text([{"role": "user", "content": "hi"}])
    assert "chat_template_kwargs" not in captured


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_flag_accepts_common_truthy_spellings(monkeypatch, value):
    captured = _capture(monkeypatch)
    monkeypatch.setenv("GEN_DISABLE_THINKING", value)
    _client().complete_raw_text([{"role": "user", "content": "hi"}])
    assert captured["chat_template_kwargs"]["enable_thinking"] is False
