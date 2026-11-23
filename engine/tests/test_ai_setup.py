"""AI 配置引导（ai_setup）单一话术源：缺项判定、报错即引导。

覆盖三层消费方：`GenClient.from_env`（CLI stderr）、MCP 工具预检（见 test_mcp.py）、
skill 文档口径（test_skill_install 守护 front-matter 与 version，文本改动人工评审）。
"""

import pytest

from insta_engine import ai_setup
from insta_engine.ai_client import GenClient, GenError

_ALL_GEN_KEYS = ("GEN_BASE_URL", "GEN_API_KEY", "GEN_MODEL", "GEN_PROMPT_MODE")


@pytest.fixture(autouse=True)
def _clean_gen_env(monkeypatch):
    """测试进程可能带着开发者本机的 GEN_* 配置——本文件全部按「从零开始」判定。"""
    for key in _ALL_GEN_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_missing_local_requires_three():
    """local（默认）模式：BASE_URL / API_KEY / MODEL 三件套缺一不可。"""
    assert ai_setup.missing_env_vars() == ["GEN_BASE_URL", "GEN_API_KEY", "GEN_MODEL"]


def test_missing_server_needs_no_model(monkeypatch):
    """server 模式模型由服务端决定——只差 BASE_URL / API_KEY；配齐即空列表。"""
    monkeypatch.setenv("GEN_PROMPT_MODE", "server")
    assert ai_setup.missing_env_vars() == ["GEN_BASE_URL", "GEN_API_KEY"]
    monkeypatch.setenv("GEN_BASE_URL", ai_setup.SERVER_BASE_URL)
    monkeypatch.setenv("GEN_API_KEY", "t")
    assert ai_setup.missing_env_vars() == []


def test_hint_is_pasteable_and_recheckable():
    """引导必须自带可粘贴片段与复检命令——agent 只看这一段就要能完成配置闭环。"""
    hint = ai_setup.ai_setup_hint(["GEN_BASE_URL", "GEN_API_KEY", "GEN_MODEL"])
    assert hint.startswith("缺少环境变量: "), "前缀是既有 job 错误断言的兼容面，不能改"
    assert "GEN_PROMPT_MODE=server" in hint
    assert ai_setup.SERVER_BASE_URL in hint
    assert "mat doctor" in hint
    assert "本地执行、回放、探索不依赖 AI" in hint, "先告诉 agent 哪些能力不受影响"


def test_hint_includes_env_path_when_caller_knows_it():
    """知道工作区根的调用方（CLI cwd / MCP root）传入 env_path ⇒ 引导直接点名目标文件；
    通用入口（from_env）不传 ⇒ 保持「工作区 .env」的通用表述（猜路径必错，见 docstring）。"""
    hint = ai_setup.ai_setup_hint(["GEN_API_KEY"], env_path="/tmp/ws/.env")
    assert "`/tmp/ws/.env`" in hint and "写入即创建" in hint
    plain = ai_setup.ai_setup_hint(["GEN_API_KEY"])
    assert "/tmp/ws/.env" not in plain and "在工作区 .env 写入" in plain


def test_from_env_error_is_actionable():
    """CLI 侧：未配置时 from_env 抛出的 GenError 就是完整引导，不是裸键名列表。"""
    with pytest.raises(GenError) as exc:
        GenClient.from_env()
    msg = str(exc.value)
    assert msg.startswith("缺少环境变量: ")
    assert "GEN_PROMPT_MODE=server" in msg and "mat doctor" in msg
