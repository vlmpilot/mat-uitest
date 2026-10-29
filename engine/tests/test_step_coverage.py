"""步骤覆盖自检（R28）：需求里的动作，生成结果是否都有步骤承载。

**为什么需要它**（2026-10-28 实测）：few-shot 把示例的步骤范围带了过来，实测用例
丢了「评分排序 / 进店」两步——定位对了、用例却少做了一半，跑完还会报通过。
定位错会失败，漏步骤会假绿，后者更危险。

**为什么用字符覆盖而不是动词表**：中文表达太散（"进店" / "点击店铺" / "进入评分最高
的一家店"），词表一定漏；字符覆盖对改写更稳。
"""

from __future__ import annotations

from types import SimpleNamespace

from insta_engine import ai_client, step_coverage
from insta_engine.ai_client import _attach_coverage

NL = "打开外卖应用，搜索火锅，按评分排序，进入评分最高的店"

FULL_STEPS = [
    {"action": "launch", "intent": "启动外卖应用"},
    {"action": "tap", "intent": "点击搜索框", "target": {"value": "搜索商家、商品、品类"}},
    {"action": "input", "intent": "输入关键词火锅", "target": {"value": "火锅"}},
    {"action": "tap", "intent": "点击搜索按钮", "target": {"value": "搜索"}},
    {"action": "tap", "intent": "点击评分排序", "target": {"value": "评分排序"}},
    {"action": "tap", "intent": "点击评分最高的店铺", "target": {"value": "店铺"}},
]


def test_full_coverage_has_no_issue():
    assert step_coverage.missing(NL, {"steps": FULL_STEPS}) == []


def test_missing_enter_step_is_reported():
    """少了「进店」这一步——正是 few-shot 实测中丢的那一步。"""
    issues = step_coverage.missing(NL, {"steps": FULL_STEPS[:-1]})
    assert len(issues) == 1
    assert "进入评分最高的店" in issues[0]
    assert issues[0].startswith("[R28]")


def test_connector_clauses_are_ignored():
    """「然后」「接着」这类碎片不该单独算一个动作。"""
    steps = [{"action": "tap", "intent": "点击搜索", "target": {"value": "搜索"}}]
    issues = step_coverage.missing("然后，点击搜索", {"steps": steps})
    assert issues == []


def test_accepts_testcase_like_object():
    case = SimpleNamespace(steps=[SimpleNamespace(intent="点击评分排序", target=None)])
    issues = step_coverage.missing("按评分排序", case)
    assert issues == []


def test_attach_coverage_writes_quality_issues():
    case = SimpleNamespace(meta={}, steps=[SimpleNamespace(intent="点击搜索", target=None)])
    _attach_coverage(case, "点击搜索，进入详情页")
    issues = case.meta.get("quality_issues") or []
    assert issues and issues[0].startswith("[R28]")


def test_book_quotes_do_not_dilute_coverage():
    """需求里带书名号时不能误报（实测「搜索「火锅」」曾因分母变大被判 33%）。"""
    steps = [
        {"action": "input", "intent": "输入关键词火锅", "target": {"value": "火锅"}},
        {"action": "tap", "intent": "触发搜索", "target": {"value": "搜索"}},
    ]
    assert step_coverage.missing("搜索「火锅」", {"steps": steps}) == []


def test_retry_switch_defaults_off(monkeypatch):
    monkeypatch.delenv("MAT_COVERAGE_RETRY", raising=False)
    assert step_coverage.retry_enabled() is False


def test_retry_switch_accepts_truthy(monkeypatch):
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("MAT_COVERAGE_RETRY", value)
        assert step_coverage.retry_enabled() is True


def test_coverage_rounds_carry_existing_case():
    """回喂必须带原产物并要求「只补不删」——整条重生成会把已经对的步骤改坏。"""
    from types import SimpleNamespace

    from insta_engine.ai_client import _coverage_rounds

    fake = SimpleNamespace(model_dump_json=lambda by_alias=True: '{"caseId":"c1"}')
    rounds = _coverage_rounds(fake, ["[R28] 需求片段「进店」没有对应步骤"])
    assert len(rounds) == 2
    assert rounds[0]["role"] == "assistant" and "c1" in rounds[0]["content"]
    assert "进店" in rounds[1]["content"] and "保持已有步骤不变" in rounds[1]["content"]


NL_FULL = "打开外卖应用，搜索火锅，按评分排序，进入评分最高的店"


def _raw(steps):
    """构造真实 DSL 对象：`apply_quality_gate` 要求 `TestCase`，不是 dict。"""
    from insta_engine.dsl import TestCase as DslCase

    return DslCase.model_validate({
        "caseId": "c1", "name": "外卖应用-搜索并进店",
        "target": {"package": "com.demo.app"},
        "steps": [
            # launch 必须显式给 intent：否则「打开外卖应用」这个子句覆盖不到，测试会假失败
            {"id": "s0", "intent": "启动外卖应用", "action": "launch"},
            *[{"id": f"s{i}", "intent": intent, "action": "tap",
               "target": {"strategy": "text", "value": value}}
              for i, (intent, value) in enumerate(steps, 1)],
        ],
    })


MISSING = _raw([("搜索火锅", "搜索"), ("点击评分排序", "评分排序")])
COMPLETE = _raw([("搜索火锅", "搜索"), ("点击评分排序", "评分排序"),
                 ("进入评分最高的店铺", "店铺")])


def test_finalize_retries_once_when_steps_missing(monkeypatch):
    """缺步骤时：带原用例回喂一次，补齐后 R28 消失，且只多调一次模型。"""
    calls = []

    def fake_raw(client, task, variables, context, validator=None, rounds=None, images=None):
        calls.append(rounds)
        return COMPLETE if rounds else MISSING

    monkeypatch.setattr(ai_client, "complete_task_raw", fake_raw)
    monkeypatch.setenv("MAT_COVERAGE_RETRY", "1")

    result = ai_client._finalize_gen(object(), NL_FULL, {"package": "com.demo.app"},
                                     None, None, MISSING)
    # 初次生成在 generate 里已完成，_finalize_gen 只负责「补一次」⇒ 恰好 1 次调用
    assert len(calls) == 1, "应恰好补一次"
    assert calls[0] and calls[0][0]["role"] == "assistant"
    assert step_coverage.missing(NL_FULL, result) == []


def test_finalize_does_not_retry_by_default(monkeypatch):
    calls = []

    def fake_raw(*args, **kwargs):
        calls.append(1)
        return COMPLETE

    monkeypatch.setattr(ai_client, "complete_task_raw", fake_raw)
    monkeypatch.delenv("MAT_COVERAGE_RETRY", raising=False)

    result = ai_client._finalize_gen(object(), NL_FULL, {"package": "com.demo.app"},
                                     None, None, MISSING)
    assert calls == [], "默认只报不改，不该多调模型"
    assert step_coverage.missing(NL_FULL, result)  # 缺失仍如实报出


def test_finalize_keeps_original_when_retry_fails(monkeypatch):
    """补步骤失败不能把整条生成带崩——保留原产物并由 R28 报出来。"""
    def fake_raw(*args, **kwargs):
        raise ai_client.GenError("模型抽风")

    monkeypatch.setattr(ai_client, "complete_task_raw", fake_raw)
    monkeypatch.setenv("MAT_COVERAGE_RETRY", "1")

    result = ai_client._finalize_gen(object(), NL_FULL, {"package": "com.demo.app"},
                                     None, None, MISSING)
    assert step_coverage.missing(NL_FULL, result)
