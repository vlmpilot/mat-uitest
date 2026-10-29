"""断言强度分层：strong / exists / skeleton / tautology / none。

`skeleton`（骨架断言）是 2026-10-28 新加的一层，A1 复核的两条确认假绿都是它：
    input 咖啡 → tap 搜索 → assert exists text=综合       ← 结果页排序栏，任何结果页都有
    input 火锅 → tap 搜索 → assert exists text=综合排序
断言本身不假（`综合` 确实在），但它证明不了"搜到了咖啡"——换词、搜空、输入没生效，
它照样在。这里锁三件事：① 判据是"任何同页状态都成立"；② 同一个控件语义由**动作**决定
（`已选中热点榜` 是效果，`已选中综合` 是骨架）；③ 词表是精确匹配，别把内容词按子串吞进来。
"""

from __future__ import annotations

from insta_engine import assertion_strength as _as


def _tier(steps: list[dict], index: int) -> str:
    return _as.analyze({"name": "t", "steps": steps})["rows"][index]["tier"]


def test_search_result_skeleton_is_flagged():
    """断结果页排序栏 ⇒ 骨架（输入之后的骨架断言，风险最高的一种）。"""
    case = [
        {"id": "s3", "action": "input",
         "target": {"strategy": "text", "value": "搜索框"}, "params": {"text": "火锅"}},
        {"id": "s5", "action": "wait",
         "assertions": [{"type": "exists",
                         "target": {"strategy": "text", "value": "综合排序"}}]},
    ]
    result = _as.analyze({"name": "搜索", "steps": case})
    assert result["rows"][1]["tier"] == "skeleton"
    assert result["rows"][1]["after_input"] is True, "输入之后的骨架断言要能单独标出来"
    assert result["skeleton_rate"] == 0.5


def test_selected_tab_semantics_depend_on_the_action():
    """同一个 `已选中X`：刚点过它 ⇒ 是动作效果（exists）；默认就选中 ⇒ 骨架。"""
    effect = [
        {"id": "s2", "action": "tap", "target": {"strategy": "text", "value": "热点"}},
        {"id": "s3", "action": "wait",
         "assertions": [{"type": "exists",
                         "target": {"strategy": "text", "value": "已选中热点榜"}}]},
    ]
    assert _tier(effect, 1) == "exists"

    default = [
        {"id": "s3", "action": "input",
         "target": {"strategy": "text", "value": "搜索框"}, "params": {"text": "咖啡"}},
        {"id": "s5", "action": "wait",
         "assertions": [{"type": "exists",
                         "target": {"strategy": "text", "value": "已选中综合"}}]},
    ]
    assert _tier(default, 1) == "skeleton"


def test_content_word_is_not_swallowed_by_substring():
    """`新增关注` 含"关注"，但它是消息页的**内容**，不是骨架（词表是精确匹配）。"""
    msg = [{"id": "s3", "action": "wait",
            "assertions": [{"type": "exists",
                            "target": {"strategy": "text", "value": "新增关注"}}]}]
    assert _tier(msg, 0) == "exists"


def test_tautology_is_worse_than_skeleton():
    """断本步刚点的控件（必然通过）仍归 tautology，不被骨架层吞掉。"""
    step = [{"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "首页"},
             "assertions": [{"type": "exists",
                             "target": {"strategy": "text", "value": "首页"}}]}]
    assert _tier(step, 0) == "tautology"


def test_aggregate_keeps_five_tiers_and_total_step_denominator():
    """分母是**总步骤**（含 launch/wait）；骨架层必须进 tiers，否则统计漏一层。"""
    case = {"name": "x", "steps": [
        {"id": "s0", "action": "launch"},
        {"id": "s1", "action": "wait",
         "assertions": [{"type": "exists",
                         "target": {"strategy": "text", "value": "综合"}}]},
        {"id": "s2", "action": "wait",
         "assertions": [{"type": "text-equals",
                         "target": {"strategy": "text", "value": "结果"},
                         "expected": "结果"}]},
    ]}
    agg = _as.aggregate([_as.analyze(case)])
    assert set(agg["tiers"]) == {"strong", "exists", "skeleton", "tautology", "none"}
    assert agg["tiers"]["skeleton"] == 1 and agg["tiers"]["strong"] == 1
    assert agg["tiers"]["none"] == 1, "launch 没有断言 ⇒ 计入 none，分母是 3 步"
    assert abs(agg["b2b_strong"] - 1 / 3) < 1e-9
    assert abs(agg["skeleton_rate"] - 1 / 3) < 1e-9
    assert agg["skeleton_cases"][0]["step"] == "s1"


def test_render_lists_skeleton_with_input_marker():
    """清单要能一眼看出"哪几条在输入之后"——那才是假绿风险。"""
    case = [
        {"id": "s3", "action": "input",
         "target": {"strategy": "text", "value": "搜索框"}, "params": {"text": "咖啡"}},
        {"id": "s5", "action": "wait",
         "assertions": [{"type": "exists",
                         "target": {"strategy": "text", "value": "已选中综合"}}]},
    ]
    text = _as.render(_as.aggregate([_as.analyze({"name": "搜索", "steps": case})]))
    assert "骨架断言清单" in text and "⚠️ 输入之后" in text
    assert "骨架断言占比" in text
