"""T56：`gen` 的 schema 容错与重试。

**背景**：实测 18 次生成里 **7 次直接失败（39%）**，根因是模型给 `exists` 断言加了
`{"expected": true}`，而 `expected` 只接受字符串 ⇒ pydantic 校验失败 ⇒ **整次生成作废**。
但 `exists` / `not-exists` 这类断言**本来就不需要 `expected`**。

两个修法各一组用例：
- **宽容**：不使用 `expected` 的断言类型 ⇒ 丢弃该字段；确实使用的 ⇒ 非字符串转 `str`
- **重试**：schema 校验**放进重试回路**（此前在回路外，重试够不到它）

配套的缺陷探针集：`benchmarks/tasks/gen-schema-probes.json` + `scripts/gen_probe.py`。
"""

from __future__ import annotations

import json

import pytest
import requests

from insta_engine.ai_client import GenClient, GenError, complete_task_raw
from insta_engine.dsl import Assertion, AssertionType, TestCase

# ---------------------------------------------------------------- 修法 ①：宽容


def test_exists_drops_meaningless_expected():
    """`exists` 带 `expected: true` ⇒ **丢弃**该字段（它对 exists 没有语义）。"""
    a = Assertion.model_validate({
        "type": "exists", "target": {"strategy": "text", "value": "扫一扫"},
        "expected": True,
    })
    assert a.type == AssertionType.EXISTS
    assert a.expected is None, "exists 不该保留 expected"


@pytest.mark.parametrize("atype", ["not-exists", "enabled", "disabled", "checked", "selected"])
def test_all_expectedless_types_drop_it(atype):
    """所有**不使用** `expected` 的类型都要宽容（不只 exists 一个）。"""
    a = Assertion.model_validate({"type": atype, "target": {"strategy": "text", "value": "x"},
                                  "expected": True})
    assert a.expected is None


def test_text_assertion_coerces_nonstring_expected():
    """`text-contains` 确实用 `expected` ⇒ 非字符串**转成字符串**，而不是让用例作废。

    生成物本来就要人工校对（`ai_client.generate` 的 docstring），
    让用例生成出来再人工看一眼，比整次生成失败有用。
    """
    a = Assertion.model_validate({"type": "text-contains",
                                  "target": {"strategy": "text", "value": "x"},
                                  "expected": True})
    assert a.expected == "True"


def test_normal_expected_untouched():
    """正常字符串不受影响（防止宽容改坏正常路径）。"""
    a = Assertion.model_validate({"type": "text-equals",
                                  "target": {"strategy": "text", "value": "x"},
                                  "expected": "登录"})
    assert a.expected == "登录"
    b = Assertion.model_validate({"type": "text-contains",
                                  "target": {"strategy": "text", "value": "x"}})
    assert b.expected is None, "缺 expected 仍是 None"


# ---------------------------------------------------------------- 修法 ②：重试


def _resp(content: str):
    class _R:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

        def raise_for_status(self):
            pass

    return _R()


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("GEN_PROMPT_MODE", "GEN_VL_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEN_JSON_REPAIRS", "2")   # 3 次尝试，让用例确定
    return monkeypatch


def _client():
    return GenClient("https://maas.invalid/v1", "sk-test", "qwen-test")


def test_expected_true_no_longer_breaks_generation(clean_env, monkeypatch):
    """**T56 原症状回归**：`exists` + `expected: true` 以前让整次生成作废（实测 39%）。

    现在修法 ①（宽容）应让它在**第一次**就通过——**不该触发重试**（`len(sent) == 1`）。
    """
    payload = json.dumps({"caseId": "c1", "steps": [
        {"id": "s1", "action": "wait",
         "assertions": [{"type": "exists", "target": {"strategy": "text", "value": "扫一扫"},
                         "expected": True}]}]})
    sent: list[dict] = []
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: (sent.append(1), _resp(payload))[1])
    case = complete_task_raw(_client(), "gen", {"nl": "x", "app_info": {}}, "生成",
                             validator=TestCase.model_validate)
    assert len(sent) == 1, "宽容修好后不该再走重试"
    assert case.steps[0].assertions[0].expected is None, "无意义的 expected 应被丢弃"


def test_schema_error_is_retried_with_repair_note(clean_env, monkeypatch):
    """**核心回归**：schema 不符 ⇒ 回喂错误让模型改，而不是直接抛。

    ⚠️ 载荷要用**修法 ① 不宽容**的错（这里 `action` 不是合法枚举）——
    否则会被宽容通过，测不到重试（第一版就踩了这个坑）。
    """
    bad = json.dumps({"caseId": "c1", "steps": [{"id": "s1", "action": "not-an-action"}]})
    good = json.dumps({"caseId": "c1", "steps": [{"id": "s1", "action": "tap"}]})
    sent: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        sent.append(json)
        return _resp(bad if len(sent) == 1 else good)

    monkeypatch.setattr(requests, "post", fake_post)
    case = complete_task_raw(_client(), "gen", {"nl": "x", "app_info": {}}, "生成",
                             validator=TestCase.model_validate)
    assert len(sent) == 2, f"应当重试一次，实际发了 {len(sent)} 次"
    assert case.steps[0].action.value == "tap"
    # 第 2 次请求里必须带上**回喂的修复说明**（否则重试等于原样重发）
    body = json.dumps(sent[1], ensure_ascii=False)
    assert "schema" in body, "重试请求里应带 schema 修复说明"


def test_schema_error_gives_up_after_retries(clean_env, monkeypatch):
    """**不能无限重试**：始终 schema 不符 ⇒ 用尽次数后抛错，且错误里带原文。"""
    bad = json.dumps({"caseId": "c1", "steps": [{"id": "s1", "action": "not-an-action"}]})
    calls: list[int] = []

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        calls.append(1)
        return _resp(bad)

    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(GenError):
        complete_task_raw(_client(), "gen", {"nl": "x", "app_info": {}}, "生成",
                          validator=TestCase.model_validate)
    assert len(calls) == 3, f"GEN_JSON_REPAIRS=2 ⇒ 共 3 次尝试，实际 {len(calls)}"


def test_no_validator_keeps_old_behaviour(clean_env, monkeypatch):
    """不传 `validator` 时行为不变（向后兼容——其它调用方不该被影响）。"""
    good = json.dumps({"caseId": "c1", "steps": [{"id": "s1", "action": "tap"}]})
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: _resp(good))
    data = complete_task_raw(_client(), "gen", {"nl": "x", "app_info": {}}, "生成")
    assert isinstance(data, dict) and data["caseId"] == "c1"


# -- 研发侧：T57 优先级 / T58 按命令模型 / 403 分开报错 --------------

def test_gen_max_tokens_env_wins_over_auto_budget(monkeypatch):
    """T57：GEN_MAX_TOKENS 显式设置必须**优先**于 describe 的自动预算（此前 or 短路恒赢）。"""
    import requests as _rq

    from insta_engine.ai_client import GenClient

    monkeypatch.setenv("GEN_MAX_TOKENS", "8192")
    client = GenClient.__new__(GenClient)
    client.model, client.base_url, client.api_key = "m", "http://x", "k"
    client.max_tokens_override = 20960  # describe 按页数自动抬高过
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(_rq, "post", lambda url, **kw: captured.update(kw) or _Resp())
    monkeypatch.setattr("insta_engine.ai_client._JSON_MODE_STATE", {"unsupported": True})
    client.complete_raw_text([{"role": "user", "content": "x"}])
    assert captured["json"]["max_tokens"] == 8192, "环境变量必须赢（T57）"


def test_model_for_task_override(monkeypatch):
    """T58：GEN_MODEL_<TASK> 按命令覆盖模型，未设回落 GEN_MODEL。"""
    from insta_engine.ai_client import GenClient

    monkeypatch.setenv("GEN_MODEL", "base-model")
    monkeypatch.setenv("GEN_MODEL_DESCRIBE", "cheap-describe")
    monkeypatch.setenv("GEN_MODEL_DIAGNOSE", "strong-reason")
    client = GenClient.__new__(GenClient)
    client.model = "base-model"
    assert client._model_for("describe") == "cheap-describe"
    assert client._model_for("diagnose") == "strong-reason"
    assert client._model_for("convert") == "base-model", "未设覆盖的命令回落 GEN_MODEL"
    assert client._model_for(None) == "base-model"


def test_403_reported_as_rate_limit_not_business_failure(monkeypatch):
    """403 与业务失败分开报错：消息必须说明'不是用例问题'且不重试。"""
    import pytest
    import requests as _rq

    from insta_engine.ai_client import GenClient, GenError

    monkeypatch.setenv("GEN_JSON_MODE", "off")
    client = GenClient.__new__(GenClient)
    client.model, client.base_url, client.api_key = "m", "http://x", "k"

    class _Resp:
        status_code = 403

        def json(self):
            return {}

    calls = []

    def _post(url, **kw):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr(_rq, "post", _post)
    with pytest.raises(GenError, match="不是用例问题"):
        client.complete_raw_text([{"role": "user", "content": "x"}])
    assert len(calls) == 1, "403 不应重试（重试火上浇油）"


def test_describe_pages_sets_timeout_budget(tmp_path, monkeypatch):
    """T59：大批量自动抬高请求超时（131 页实测 60s 默认必超时）；GEN_TIMEOUT 显式仍优先。"""
    import pytest

    from insta_engine.ai_client import GenClient, GenError

    class _Stop:
        """一被调用就停（描述属性在调用前就已设置完毕）。"""

        def complete_raw_text(self, messages, context="", task=None):
            raise GenError("stop")

    client = GenClient.__new__(GenClient)
    client.complete_raw_text = _Stop().complete_raw_text  # 实例属性遮掉类方法（__new__ 实例仍带类方法）
    pages = [{"page_key": f"p{i}"} for i in range(131)]
    with pytest.raises(GenError, match="stop"):
        GenClient.describe_pages(client, pages)
    assert client.timeout_override == max(90, 131 + 90)
    assert client.max_tokens_override == max(4096, 131 * 160)
