"""P1.5 /api/polish 离线测试：同步短调用的错误口径。

锁死点（**润色免费**——不管有没有用润色，AI 生成统一
1 次额度/任务）：
1. 成功返回润色文本，**不写 usage.json**（免费，无预检/核销）；
2. 模型失败（空输出/未预期异常）一律 502；
3. 空 nl 400。
"""

from __future__ import annotations

from insta_engine import web_server
from insta_engine.ai_client import GenClient


def _polish_stub(text: str) -> GenClient:
    """complete_task_text 桩：真 GenClient 实例，只挡网络层（同 test_gen_split 模式）。"""
    client = GenClient.__new__(GenClient)
    client.complete_raw_text = lambda messages, context="", task=None: text
    return client


def test_polish_success_free(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(GenClient, "from_env",
                        staticmethod(lambda: _polish_stub("已登录首页后，点击底部「我的」入口")))
    code, obj = web_server.api_polish(tmp_path / "ws", {"nl": "去我的页面"})
    assert code == 200 and obj["text"].startswith("已登录首页")
    assert not (tmp_path / "home" / "usage.json").exists(), "润色免费：不写配额计数"


def test_polish_empty_output(tmp_path, monkeypatch):
    """空输出 → GenError → 502。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(GenClient, "from_env", staticmethod(lambda: _polish_stub("  ")))
    code, obj = web_server.api_polish(tmp_path / "ws", {"nl": "x"})
    assert code == 502 and "润色失败" in obj["error"], obj


def test_polish_unexpected_error(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(GenClient, "from_env", staticmethod(boom))
    code, obj = web_server.api_polish(tmp_path / "ws", {"nl": "x"})
    assert code == 502 and "润色失败" in obj["error"]


def test_polish_requires_nl(tmp_path):
    code, obj = web_server.api_polish(tmp_path / "ws", {})
    assert code == 400 and "nl" in obj["error"]
