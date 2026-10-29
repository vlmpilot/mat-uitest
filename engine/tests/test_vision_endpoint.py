"""视觉兜底的端点选择：`GEN_VL_BASE_URL` 可独立于 `GEN_BASE_URL`。

背景：视觉兜底最典型的本地化形态是「生成走云端、兜底走本地小模型」。
`GEN_BASE_URL` 是全局单值，没有这个开关时只能两个都本地（生成质量掉）或
两个都云端（截图出本机），这个场景在配置层面根本表达不出来。
"""

from __future__ import annotations

from insta_engine import vision

_KEYS = ("GEN_BASE_URL", "GEN_VL_BASE_URL", "GEN_API_KEY", "GEN_VL_MODEL", "GEN_PROMPT_MODE")


def _env(monkeypatch, **values):
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_vision_endpoint_defaults_to_global_base_url(monkeypatch):
    """只配 GEN_BASE_URL 的老配置，行为不变。"""
    _env(monkeypatch, GEN_BASE_URL="https://api.example.com/v1",
         GEN_API_KEY="k", GEN_VL_MODEL="vl", GEN_PROMPT_MODE="local")
    client = vision.default_client()
    assert client.base_url == "https://api.example.com/v1"
    assert client.model == "vl"


def test_vision_endpoint_can_point_elsewhere(monkeypatch):
    """生成走云端、兜底走本地：两个端点分别生效。"""
    _env(monkeypatch, GEN_BASE_URL="https://api.example.com/v1",
         GEN_VL_BASE_URL="http://127.0.0.1:8080/v1",
         GEN_API_KEY="k", GEN_VL_MODEL="qwen3-vl-8b", GEN_PROMPT_MODE="local")
    client = vision.default_client()
    assert client.base_url == "http://127.0.0.1:8080/v1"
    assert client.model == "qwen3-vl-8b"


def test_vision_enabled_needs_model_even_with_own_endpoint(monkeypatch):
    """配了独立端点但没配模型名 ⇒ 仍视为关闭（不猜模型）。"""
    _env(monkeypatch, GEN_BASE_URL="https://api.example.com/v1",
         GEN_VL_BASE_URL="http://127.0.0.1:8080/v1",
         GEN_API_KEY="k", GEN_PROMPT_MODE="local")
    assert vision.enabled() is False


# -- 本机端点强制 OpenAI 协议（2026-10-26）-------------------------------------
# 背景：`GEN_PROMPT_MODE=server` 表达的是「生成 prompt 由服务端下发」。若 visual 客户端
# 的 prompt_mode 跟着变 server，请求会发到 `{base_url}/mat/complete`（MAT 私有代理协议）
# ——本机小模型服务（mlx-vlm / ollama / LM Studio）只有 OpenAI 兼容端点，固定 404。
# 实测：`GEN_PROMPT_MODE=server` + `GEN_VL_BASE_URL=127.0.0.1:8915` 时兜底一次也调不出去。

def test_local_endpoint_forces_openai_protocol(monkeypatch):
    """官方代理模式 + 本机端点 ⇒ 客户端必须是 local 协议（打 /chat/completions）。"""
    _env(monkeypatch, GEN_BASE_URL="https://mobileaitest.com",
         GEN_VL_BASE_URL="http://127.0.0.1:8915/v1",
         GEN_API_KEY="k", GEN_VL_MODEL="qwen3-vl-8b", GEN_PROMPT_MODE="server")
    client = vision.default_client()
    assert client.base_url == "http://127.0.0.1:8915/v1"
    assert client.prompt_mode == "local", "本机端点不能走 /mat/complete 私有协议"


def test_remote_vision_endpoint_keeps_server_protocol(monkeypatch):
    """反向保障：视觉端点在外网 ⇒ 仍走官方代理协议（不能把私有协议改掉）。"""
    _env(monkeypatch, GEN_BASE_URL="https://mobileaitest.com",
         GEN_VL_BASE_URL="https://vl.example.com/v1",
         GEN_API_KEY="k", GEN_VL_MODEL="qwen3-vl-8b", GEN_PROMPT_MODE="server")
    client = vision.default_client()
    assert client.base_url == "https://vl.example.com/v1"
    assert client.prompt_mode == "server"


def test_local_endpoint_requires_model_name(monkeypatch):
    """本机端点必须给模型名：本地服务不会替我们选模型。"""
    _env(monkeypatch, GEN_BASE_URL="https://mobileaitest.com",
         GEN_VL_BASE_URL="http://127.0.0.1:8915/v1",
         GEN_API_KEY="k", GEN_PROMPT_MODE="server")
    assert vision.enabled() is False
