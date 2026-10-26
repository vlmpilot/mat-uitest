"""归因（diagnose）的测试——此前**一个都没有**（grep 全空），而这正是 立项目由。

⚠️ **为什么要盯"没有 LLM 时的行为"**：归因的调用方（CLI / console / 失败样本回流）都依赖
"这里永远有返回"。一旦它在缺 Key 时抛异常，**整个 run 会崩**，而不是降级成"需要人工确认"。
"""

from __future__ import annotations

import json

import pytest

from insta_engine.diagnose import diagnose_run
from insta_engine.runner import RunResult


def _run(all_passed: bool = False) -> RunResult:
    """最小 RunResult。`all_passed=True` 时**两步都过**（步骤状态要跟着改——
    我第一次只改了 run.status、忘了改步骤，被自检抓出来）。"""
    second = ({"step_id": "s2", "intent": "断言首页", "action": "wait", "status": "passed",
               "duration_ms": 1, "category": None}
              if all_passed else
              {"step_id": "s2", "intent": "断言首页", "action": "wait", "status": "failed",
               "duration_ms": 1, "category": "assert-fail",
               "error": "AssertionError: 断言失败: 控件应存在: xxx"})
    return RunResult.model_validate({
        "case_id": "case_t", "case_name": "t", "status": "passed" if all_passed else "failed",
        "step_success_rate": 1.0 if all_passed else 0.5,
        "duration_ms": 10, "failed_categories": {}, "perf": {},
        "steps": [
            {"step_id": "s1", "intent": "ok", "action": "launch", "status": "passed",
             "duration_ms": 1, "category": None},
            second,
        ],
    })


@pytest.fixture
def no_llm(monkeypatch):
    """把 LLM 环境变量清干净——模拟"用户没配 Key"的真实机器（BYOK 是默认形态）。"""
    for k in ("GEN_BASE_URL", "GEN_API_KEY", "GEN_MODEL", "GEN_MODE", "GEN_SERVER_URL"):
        monkeypatch.delenv(k, raising=False)


def test_skipped_steps_are_not_diagnosed(no_llm, tmp_path):
    """`skipped` 是前序失败后**未执行**的步骤，不能算进失败样本。

    2026-10-25 真机评测：1 failed + 2 skipped 被报成「3 个失败步骤」，
    归因与失败率都会被虚增。
    """
    data = _run().model_dump()
    data["steps"].append({"step_id": "s3", "intent": "后续步骤", "action": "tap",
                          "status": "skipped", "duration_ms": 0})
    cards = diagnose_run(RunResult.model_validate(data), tmp_path)
    assert set(cards) == {"s2"}, f"只该归因真失败步，实际 {set(cards)}"


def test_no_llm_returns_rule_cards_instead_of_raising(no_llm, tmp_path):
    """**没配 LLM 时必须返回规则卡，不能抛异常。**

    实测 bug（2026-10-03）：`GenClient.from_env()` 抛的 `GenError` **没被 try 包住**
    （只有后面的 `complete_task_raw` 被包了）⇒ 缺 `GEN_BASE_URL/GEN_API_KEY/GEN_MODEL` 时
    **归因直接崩**，文档承诺的"规则兜底"根本够不到。
    """
    cards = diagnose_run(_run(), tmp_path)
    assert set(cards) == {"s2"}, f"应只给失败步骤出卡，实际 {set(cards)}"
    card = cards["s2"]
    assert card["source"] == "rules", f"没有 LLM ⇒ 必须标 source=rules，实际 {card['source']}"
    assert card["need_human"] is True, "规则兜底必须标 need_human（错误成本不对称）"
    assert card["category"] == "assert-fail", "规则卡要带上 runner 已判定的类别"
    assert card["confidence"] is None, "规则卡不该假装有置信度"


def test_no_failed_steps_returns_empty(no_llm, tmp_path):
    """没有失败步骤 ⇒ 空表（不调 LLM、也不给卡）。"""
    assert diagnose_run(_run(all_passed=True), tmp_path) == {}


def test_rule_card_is_json_serializable(no_llm, tmp_path):
    """归因卡要能直接进 JSON（回流样本 / 报告都要序列化它）。"""
    cards = diagnose_run(_run(), tmp_path)
    assert json.loads(json.dumps(cards, ensure_ascii=False))["s2"]["source"] == "rules"


def test_parse_accepts_dict_from_complete_task_raw():
    """`_parse` 必须接住 **dict**——`complete_task_raw()` 的契约就是返回解析后的 JSON。

    实测 bug（2026-10-03，T28 第一次真跑）：
    `AttributeError: 'dict' object has no attribute 'strip'`，
    而且崩点在 `diagnose_run` 的 try **外面** ⇒ **整个 run 崩**，不是降级成规则兜底。
    根因是类型契约错配（一方给 dict、一方按 str 解），而 `diagnose` 此前**零测试**。
    """
    from insta_engine.diagnose import _parse

    run = _run()
    llm_dict = {"results": [{"step_id": "s2", "category": "app-bug", "confidence": 0.9,
                             "evidence": ["x"], "suggestion": "y", "need_human": False}]}
    parsed = _parse(llm_dict, run)          # ← 这就是正常路径（不是测试桩）
    assert parsed and parsed["s2"]["category"] == "app-bug", f"dict 输入没被接住：{parsed}"


def test_parse_still_accepts_string_and_rejects_garbage():
    """字符串（含代码围栏）仍要能解；**非法输出返回 None** 而不是抛异常。"""
    from insta_engine.diagnose import _parse

    run = _run()
    good = '```json\n{"results": [{"step_id": "s2", "category": "ui-change"}]}\n```'
    assert _parse(good, run)["s2"]["category"] == "ui-change", "带围栏的字符串没解出来"
    assert _parse("这不是 JSON", run) is None, "非法输出应返回 None（走规则回退），不能抛"
    assert _parse({"nope": 1}, run) is None, "缺 results 字段应返回 None"


# -- 页面图谱证据（graph_evidence，2026-10-15） ----------------------------------
# 发起人拍板的增强点①：diagnose 注入图谱证据，把 ui-change 从"猜"变"证"。
# ⚠️ 硬约束与归因本体一致：**证据缺失绝不能让它崩**（永远有返回）。

def _case(res_id: str = "com.t:id/login_btn"):
    """最小 TestCase：s2 用 res-id 定位（失败步骤），s1 通过（不参与证据）。"""
    from insta_engine.dsl import AssertionType, LocatorStrategy, TestCase

    return TestCase.model_validate({
        "caseId": "case_t", "name": "t", "target": {"package": "com.t"},
        "steps": [
            {"id": "s1", "intent": "ok", "action": "launch"},
            {"id": "s2", "intent": "断言首页", "action": "wait",
             "target": {"strategy": LocatorStrategy.ID.value, "value": res_id},
             "assertions": [{"type": AssertionType.EXISTS.value,
                             "target": {"strategy": LocatorStrategy.ID.value,
                                        "value": res_id}}]},
        ],
    })


def _write_index(tmp_path, labels=("登录",), updated="2026-10-14T08:00:00"):
    """伪造一份页面图谱：<page_index_dir>/com.t.json。"""
    d = tmp_path / "page_index"
    d.mkdir(exist_ok=True)
    (d / "com.t.json").write_text(json.dumps({
        "updated_at": updated,
        "pages": [{"controls": [{"res-id": "com.t:id/login_btn",
                                 "text": labels[0], "clickable": True}]}],
    }, ensure_ascii=False), encoding="utf-8")
    return d


def test_graph_evidence_injects_labels_and_snapshot_time(tmp_path):
    """失败步骤的 res-id 在图谱里 ⇒ 注入历史文案 + 快照时间（ui-change 的硬证据）。"""
    from insta_engine.diagnose import build_variables

    idx = _write_index(tmp_path)
    v = build_variables(_run(), tmp_path, case=_case(), page_index_dir=idx)
    ev = v["graph_evidence"]
    assert "「登录」" in ev, f"历史文案没进证据：{ev}"
    assert "2026-10-14" in ev, "快照时间必须一起注入（否则模型无从判断时效）"
    assert "存在" in ev and "出现于" in ev, f"存在性表述缺失：{ev}"


def test_graph_evidence_degrades_quietly(tmp_path):
    """无 case / 无包名 / 无 res-id / 图谱缺失 —— 四种降级都给占位文本，不抛异常。"""
    from insta_engine.diagnose import build_variables

    no_case = build_variables(_run(), tmp_path)
    assert "未注入" in no_case["graph_evidence"]
    idx = _write_index(tmp_path)
    no_rid = build_variables(_run(), tmp_path, case=_case("  "), page_index_dir=idx)
    assert "res-id 定位" in no_rid["graph_evidence"]
    missing = build_variables(_run(), tmp_path, case=_case(),
                              page_index_dir=tmp_path / "nope")
    assert "页面图谱不可用" in missing["graph_evidence"]
    # res-id 在图谱里查无 ⇒ 也要有明确表述（这正是 ui-change 判定的另一半证据）
    absent = build_variables(_run(), tmp_path, case=_case("com.t:id/other"),
                             page_index_dir=idx)
    assert "未出现在图谱中" in absent["graph_evidence"]


def test_rid_hints_delegates_to_res_evidence(tmp_path):
    """rid_hints 改为 res_evidence 的薄包装后，行为必须不变（回归盯防）。"""
    from insta_engine.page_index import res_evidence, rid_hints

    idx = _write_index(tmp_path)
    assert rid_hints(idx / "com.t.json", "com.t:id/login_btn") == ["登录"]
    assert rid_hints(idx / "com.t.json", "com.t:id/nope") == []
    ev = res_evidence(idx / "com.t.json", ["com.t:id/login_btn", "com.t:id/nope"])
    assert ev["updated_at"] == "2026-10-14T08:00:00"
    assert ev["hits"]["com.t:id/login_btn"]["exists"] is True
    assert ev["hits"]["com.t:id/nope"]["exists"] is False


def test_old_prompt_without_placeholder_still_renders():
    """旧工作区的 diagnose.prompt 没有 {graph_evidence} 占位符 ⇒ 渲染不能崩。

    ai_client 的替换是逐 key `text.replace("{key}", …)`，模板缺占位符只是收不到
    该字段——这里锁死这个契约，防止有人改成 `str.format` 让旧模板 KeyError。
    """
    from insta_engine.ai_client import substitute

    old_template = "规则先验: {rule_prior}\nlogcat:\n{logcat}"  # 没有 {graph_evidence}
    out = substitute(old_template, {"rule_prior": "R", "logcat": "L",
                                    "graph_evidence": "图谱证据……"})
    assert out == "规则先验: R\nlogcat:\nL", f"多余字段不该出现在输出里：{out!r}"
    # 反向：新模板 + 旧变量集（缺 graph_evidence 键）也不能崩
    assert substitute("图谱:\n{graph_evidence}", {"rule_prior": "R"}) == "图谱:\n{graph_evidence}"

