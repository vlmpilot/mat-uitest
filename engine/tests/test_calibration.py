"""报告校准（`mat calibrate`）的离线测试：不碰真机、不碰真产物目录。

为什么这些用例值钱：校准是**人唯一能纠正失败归类的通道**，而它一旦出错**看起来完全正常** ——
改判写不进报告、规则静默失效、或者一条过宽的规则把**同类全部**失败都改掉。
所以下面每条都在锁一个"错了会静默"的点：

1. **规则必须带内容条件**（否则「所有 assert-fail → app-crash」会把真断言失败藏起来）；
2. **规则真的能覆盖内置判定**（这是"下次不再犯"的全部意义）；
3. **不命中时不许改动**（过宽的规则是最危险的失败模式）；
4. **幂等**（同一份校准重复应用结果一致）；
5. **改判留痕**（`category_source="human"` + 改判前的值）；
6. **找不到的 `step_id` 要报出来**（静默丢弃 = 校准白做）。
"""

from __future__ import annotations

import json
from pathlib import Path

from insta_engine import calibration
from insta_engine.runner import classify_failure


def _payload(steps: list[dict]) -> dict:
    return {"run": {"case_id": "c1", "case_name": "冒烟", "status": "failed",
                    "step_success_rate": 0.5, "duration_ms": 100,
                    "failed_categories": {}, "steps": steps}}


def _step(step_id: str, category: str = "assert-fail") -> dict:
    return {"step_id": step_id, "intent": "点导出", "action": "click", "status": "failed",
            "category": category, "error": "AssertionError: 找不到控件 export_btn"}


# -- 规则：安全护栏 ----------------------------------------------------------------

def test_rule_without_content_condition_is_rejected() -> None:
    """🔴 **最重要的那条**：只写 `category` 的规则会静默把**同类全部**失败改判。"""
    for bad in ({"when": {}, "set_category": "app-crash"},
                {"when": {"category": "assert-fail"}, "set_category": "app-crash"}):
        assert calibration.validate_rule(bad) is not None, f"{bad} 必须被拒"
    assert calibration.validate_rule(
        {"when": {"logcat_contains": "安全验证"}, "set_category": "env-failure"}) is None
    assert calibration.validate_rule(
        {"when": {"error_contains": "滑块"}, "set_category": "env-failure"}) is None


def test_illegal_target_category_is_rejected() -> None:
    assert calibration.validate_rule(
        {"when": {"error_contains": "x"}, "set_category": "乱写"}) is not None


def test_illegal_rules_are_skipped_when_matching() -> None:
    """非法规则**不许参与匹配** —— 不能猜它的意图。"""
    cat, hit = calibration.apply_rules(
        "assert-fail", "找不到控件", "日志",
        [{"when": {}, "set_category": "app-crash"}])
    assert cat == "assert-fail" and hit is None


# -- 规则：真的生效（"下次不再犯"的全部意义）----------------------------------------

def test_rule_overrides_builtin_classification() -> None:
    """真实场景：风控页 ⇒ 内置判 assert-fail，人工知道是 env-failure。"""
    err = "AssertionError: 找不到控件 home_channel_search_box"
    log = "I/YodaRouterTransparentActivity: 安全验证 请拖动下方滑块完成验证"
    assert classify_failure(err, log) == "assert-fail", "前提：内置规则判成断言失败"
    rules = [{"when": {"logcat_contains": "安全验证"}, "set_category": "env-failure"}]
    assert classify_failure(err, log, rules) == "env-failure", "规则必须能覆盖内置判定"


def test_rule_does_not_fire_when_condition_absent() -> None:
    """🔴 不命中时**不许改动** —— 过宽规则的失败模式就是把无关失败一起改掉。"""
    rules = [{"when": {"logcat_contains": "安全验证"}, "set_category": "env-failure"}]
    err = "AssertionError: 找不到控件 export_btn"
    assert classify_failure(err, "普通日志，什么都没有", rules) == "assert-fail"


def test_rule_without_rules_is_unchanged_behaviour() -> None:
    """缺省 `rules=None` ⇒ 完全走内置规则（对既有行为零影响）。"""
    assert classify_failure("AssertionError: x", "") == "assert-fail"
    assert classify_failure("AssertionError: x", "", []) == "assert-fail"


def test_rule_only_changes_category_never_the_verdict() -> None:
    """规则**只改分类，不改判定** —— 它不会让失败的步骤变成通过。"""
    rules = [{"when": {"logcat_contains": "安全验证"}, "set_category": "env-failure"}]
    out = classify_failure("AssertionError: x", "安全验证", rules)
    assert out == "env-failure", "分类被改了"
    assert out not in ("passed", "pass"), "但绝不是「通过」"


# -- 应用：改判 / 幂等 / 留痕 / 缺 key ---------------------------------------------

def test_apply_writes_category_and_marks_human() -> None:
    payload = _payload([_step("s1"), _step("s2")])
    stats = calibration.apply_calibration(payload, {"steps": {
        "s1": {"category": "env-failure", "logcat_contains": "安全验证", "note": "风控"}}})
    assert stats["applied"] == ["s1"]
    s1 = payload["run"]["steps"][0]
    assert s1["category"] == "env-failure"
    assert s1["category_source"] == "human", "报告要能标出这是人判的"
    assert s1["category_before_calibration"] == "assert-fail", "改判前的值要留痕"
    assert payload["run"]["steps"][1]["category"] == "assert-fail", "没校准的步骤不许动"
    assert "category_source" not in payload["run"]["steps"][1]


def test_apply_is_idempotent() -> None:
    payload = _payload([_step("s1")])
    cal = {"steps": {"s1": {"category": "env-failure"}}}
    calibration.apply_calibration(payload, cal)
    again = calibration.apply_calibration(payload, cal)
    assert again["applied"] == [] and again["unchanged"] == ["s1"], "重复应用不许再改一遍"


def test_apply_reports_missing_steps() -> None:
    payload = _payload([_step("s1")])
    stats = calibration.apply_calibration(payload, {"steps": {"幽灵": {"category": "env-failure"}}})
    assert stats["missing"] == ["幽灵"], "找不到的 step_id 必须报出来，不能静默丢弃"


def test_apply_rejects_unknown_target_category() -> None:
    payload = _payload([_step("s1")])
    stats = calibration.apply_calibration(payload, {"steps": {"s1": {"category": "乱写"}}})
    assert stats["applied"] == [] and stats["missing"] == ["s1"]
    assert payload["run"]["steps"][0]["category"] == "assert-fail"


def test_apply_collects_rules_only_when_condition_given() -> None:
    payload = _payload([_step("s1"), _step("s2")])
    stats = calibration.apply_calibration(payload, {"run": "artifacts/case_t1", "steps": {
        "s1": {"category": "env-failure", "logcat_contains": "安全验证"},
        "s2": {"category": "env-failure"},                      # 没给条件 ⇒ 只改这一次
    }})
    assert len(stats["rules"]) == 1, "只有填了条件的才落成规则"
    rule = stats["rules"][0]
    assert rule["set_category"] == "env-failure"
    assert rule["when"] == {"logcat_contains": "安全验证"}
    assert rule["from"] == "artifacts/case_t1"
    assert calibration.validate_rule(rule) is None, "生成的规则本身必须合法"


# -- 规则表：落盘 / 去重 / 坏文件不炸 ----------------------------------------------

def test_save_rules_appends_and_dedups(tmp_path: Path) -> None:
    r1 = {"when": {"logcat_contains": "安全验证"}, "set_category": "env-failure"}
    r2 = {"when": {"error_contains": "滑块"}, "set_category": "env-failure"}
    path = calibration.save_rules(tmp_path, [r1], source="第一次")
    calibration.save_rules(tmp_path, [r1, r2], source="第二次")
    rules = calibration.load_rules(tmp_path)
    assert len(rules) == 2, f"重复的规则要去重，实际 {rules}"
    assert path == tmp_path / "calibration" / "rules.json"


def test_save_rules_skips_illegal_ones(tmp_path: Path) -> None:
    calibration.save_rules(tmp_path, [{"when": {}, "set_category": "app-crash"}])
    assert calibration.load_rules(tmp_path) == [], "非法规则不许落盘"


def test_load_rules_survives_broken_file(tmp_path: Path) -> None:
    """规则表坏掉**不该让执行挂掉** —— 退化成纯内置判定。"""
    p = calibration.rules_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    assert calibration.load_rules(tmp_path) == []


def test_load_rules_missing_file_is_empty(tmp_path: Path) -> None:
    assert calibration.load_rules(tmp_path) == []


# -- 校准页 -----------------------------------------------------------------------

def test_build_items_skips_passed_and_reads_logcat(tmp_path: Path) -> None:
    run_dir = tmp_path / "case_t1"
    run_dir.mkdir()
    (run_dir / "logcat_s2.txt").write_text("E AndroidRuntime: FATAL EXCEPTION", encoding="utf-8")
    payload = _payload([
        {"step_id": "s1", "intent": "a", "action": "click", "status": "passed"},
        {**_step("s2"), "logcat_file": "logcat_s2.txt"},
    ])
    items = calibration.build_items(payload, run_dir)
    assert [i["step_id"] for i in items] == ["s2"], "只列失败步骤"
    assert "FATAL EXCEPTION" in items[0]["logcat"], "logcat 摘录要读出来（证据）"
    assert items[0]["category_source"] == "rules"


def test_build_items_attaches_diagnosis(tmp_path: Path) -> None:
    run_dir = tmp_path / "case_t1"
    run_dir.mkdir()
    (run_dir / "diagnose.json").write_text(json.dumps(
        {"s1": {"category": "env-flake", "confidence": 0.9, "evidence": ["滑块验证"],
                "need_human": False}}), encoding="utf-8")
    items = calibration.build_items(_payload([_step("s1")]), run_dir)
    assert items[0]["diagnosis"]["category"] == "env-flake", "LLM 归因要带上（人判断的依据）"


def test_render_html_has_editable_fields_and_rule_inputs() -> None:
    items = [{"step_id": "s1", "intent": "点导出", "action": "click", "status": "failed",
              "category": "assert-fail", "category_source": "rules",
              "error": "AssertionError: x", "logcat": "", "screenshot": "",
              "diagnosis": {}, "assertion": None}]
    text = calibration.render_html("case_t1", items)
    assert 'class="cat"' in text and 'data-step="s1"' in text, "要有改判下拉"
    assert 'class="ec"' in text and 'class="lc"' in text, "要有规则条件输入（下次不再犯）"
    assert "calibration" in text and "--apply" in text, "要给出下一步命令"
    for c in calibration.CATEGORIES:
        assert f'value="{c}"' in text, f"五分类 {c} 要在下拉里"


def test_render_html_shows_assertion_block_when_parsable() -> None:
    """抠得到「期望/实际」时，页面要给出**断言校准**选项（那才是让结果变对的那一层）。"""
    items = [{"step_id": "s1", "intent": "搜索", "action": "input", "status": "failed",
              "category": "assert-fail", "category_source": "rules",
              "error": "断言失败: 期望文本 '热搜'，实际 '热榜'", "logcat": "",
              "screenshot": "", "diagnosis": {},
              "assertion": {"kind": "equals", "expected": "热搜", "actual": "热榜"}}]
    text = calibration.render_html("case_t1", items)
    assert 'value="adopt-actual"' in text, "要有「采纳实际值」"
    assert 'value="loosen"' in text, "text-equals 要有「放宽为包含」"
    assert "热榜" in text and "热搜" in text, "期望/实际都要显示出来"


def test_render_html_no_assertion_block_when_unparsable() -> None:
    items = [{"step_id": "s1", "intent": "x", "action": "click", "status": "failed",
              "category": "assert-fail", "category_source": "rules",
              "error": "AssertionError: 找不到控件", "logcat": "", "screenshot": "",
              "diagnosis": {}, "assertion": None}]
    text = calibration.render_html("case_t1", items)
    assert 'value="adopt-actual"' not in text, "抠不到期望/实际时不该给断言校准选项"


# -- 断言校准（第二层：让结果本身变对）----------------------------------------------

def test_parse_assertion_error() -> None:
    assert calibration.parse_assertion_error("断言失败: 期望文本 '热搜'，实际 '热榜'") == \
        {"kind": "equals", "expected": "热搜", "actual": "热榜"}
    assert calibration.parse_assertion_error("断言失败: 期望包含 '订单'，实际 '我的订单'") == \
        {"kind": "contains", "expected": "订单", "actual": "我的订单"}
    assert calibration.parse_assertion_error("AssertionError: 找不到控件") is None
    assert calibration.parse_assertion_error("") is None


def test_patch_case_adopt_actual_does_not_touch_original() -> None:
    """🔴 **不许改原对象** —— 原用例是用户的源文件，只能写新文件。"""
    case = {"caseId": "c1", "steps": [
        {"id": "s2", "action": "click",
         "assertions": [{"type": "text-equals", "expected": "热搜"}]}]}
    patched, notes = calibration.patch_case(
        case, {"s2": {"action": "adopt-actual", "expected": "热搜", "actual": "热榜"}})
    assert patched["steps"][0]["assertions"][0]["expected"] == "热榜"
    assert case["steps"][0]["assertions"][0]["expected"] == "热搜", "原用例必须原封不动"
    assert notes and "热榜" in notes[0]


def test_patch_case_loosen_only_changes_equals() -> None:
    case = {"caseId": "c1", "steps": [
        {"id": "s2", "assertions": [{"type": "text-equals", "expected": "A"}]}]}
    patched, _ = calibration.patch_case(
        case, {"s2": {"action": "loosen", "expected": "A", "actual": "B"}})
    assert patched["steps"][0]["assertions"][0]["type"] == "text-contains"
    # 已经是 contains 的不该被"放宽"成别的
    case2 = {"caseId": "c1", "steps": [
        {"id": "s2", "assertions": [{"type": "text-contains", "expected": "A"}]}]}
    patched2, notes2 = calibration.patch_case(
        case2, {"s2": {"action": "loosen", "expected": "A", "actual": "B"}})
    assert patched2["steps"][0]["assertions"][0]["type"] == "text-contains"
    assert "无需放宽" in notes2[0]


def test_patch_case_reports_missing_step() -> None:
    case = {"caseId": "c1", "steps": [{"id": "s1", "assertions": []}]}
    _, notes = calibration.patch_case(
        case, {"幽灵": {"action": "adopt-actual", "expected": "A", "actual": "B"}})
    assert notes and "找不到该步骤" in notes[0]


def test_apply_collects_assertion_fixes_separately() -> None:
    """断言校准与分类改判**相互独立** —— 只修断言、不动分类也要能落。"""
    payload = _payload([_step("s1")])
    stats = calibration.apply_calibration(payload, {"steps": {"s1": {
        "category": "assert-fail",                     # 与原来一致 ⇒ unchanged
        "assertion_fix": "adopt-actual",
        "assertion_expected": "热搜", "assertion_actual": "热榜"}}})
    assert stats["unchanged"] == ["s1"], "分类没变"
    assert stats["assertion_fixes"]["s1"]["action"] == "adopt-actual"
    assert stats["assertion_fixes"]["s1"]["actual"] == "热榜"


def test_apply_ignores_unknown_assertion_fix() -> None:
    payload = _payload([_step("s1")])
    stats = calibration.apply_calibration(payload, {"steps": {"s1": {
        "category": "assert-fail", "assertion_fix": "乱写"}}})
    assert stats["assertion_fixes"] == {}, "未知动作不许落"


def test_find_case_file_by_case_id(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "a.json").write_text(json.dumps({"caseId": "other"}), encoding="utf-8")
    (cases / "b.json").write_text(json.dumps({"caseId": "c1"}), encoding="utf-8")
    assert calibration.find_case_file(tmp_path, "c1") == cases / "b.json"
    assert calibration.find_case_file(tmp_path, "没有") is None
    assert calibration.find_case_file(tmp_path, "") is None


# -- 标记类校准（发起人 2026-10-13：可忽略 / 误判 / 校准原因） ------------------------

def test_apply_marks_ignore_false_positive_and_note() -> None:
    """标记类**独立于分类改判**生效：不打分类也要能落；不改 status/category（对账口径不变）。"""
    payload = _payload([_step("s1"), _step("s2", "locate-fail"), _step("s3")])
    stats = calibration.apply_calibration(payload, {"steps": {
        "s1": {"ignore": True, "note": "风控页误报"},
        "s2": {"false_positive": True, "note": "断言过松"},
        "s3": {},   # 空条目 ⇒ 什么都没做 ⇒ missing（不静默吞掉）
    }})
    steps = {s["step_id"]: s for s in payload["run"]["steps"]}
    assert steps["s1"]["ignored"] is True
    assert steps["s1"]["calibration_note"] == "风控页误报"
    assert steps["s2"]["false_positive"] is True
    assert stats["marks"] == ["s1", "s2"], "打了标记的步骤要逐个报出来"
    assert "s3" in stats["missing"], "空条目必须报出来，不能静默丢弃"
    assert steps["s3"]["category"] == "assert-fail", "空条目不许动步骤"
    # 标记不改失败判定与分类——报告只加标注，成功率统计不动
    assert steps["s1"]["status"] == "failed" and steps["s1"]["category"] == "assert-fail"


def test_apply_marks_revoke_with_false_and_empty_note() -> None:
    """显式 False / 清空 note = 撤销标注（前端取消勾选/清空输入要能撤销）。"""
    payload = _payload([_step("s1")])
    calibration.apply_calibration(payload, {"steps": {
        "s1": {"ignore": True, "false_positive": True, "note": "先都标上"}}})
    stats = calibration.apply_calibration(payload, {"steps": {
        "s1": {"ignore": False, "false_positive": False, "note": ""}}})
    s1 = payload["run"]["steps"][0]
    assert "ignored" not in s1 and "false_positive" not in s1
    assert "calibration_note" not in s1
    assert stats["marks"] == [], "撤销后不再算打标"


def test_build_items_include_passed_lists_passed_steps(tmp_path: Path) -> None:
    """成功用例也要能校准「是否误判」（发起人 2026-10-13）：include_passed=True 收 passed。"""
    payload = _payload([
        {"step_id": "s1", "intent": "启动", "action": "launch", "status": "passed"},
        _step("s2"),
    ])
    assert [i["step_id"] for i in calibration.build_items(payload, tmp_path)] == ["s2"], \
        "缺省只列失败步骤（CLI 校准页口径不变）"
    items = calibration.build_items(payload, tmp_path, include_passed=True)
    assert [i["step_id"] for i in items] == ["s1", "s2"]
    assert items[0]["status"] == "passed"
