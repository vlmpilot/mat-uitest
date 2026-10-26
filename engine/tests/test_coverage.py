"""覆盖缺口分析（`coverage.py`）的测试——重点盯**口径**，不是代码。

⚠️ 为什么口径要写测试：这个模块的代码只有几十行，**价值全在"分母怎么定"**。
口径错了指标就失真（实测：全部控件口径下某资讯 App 覆盖率 5.4%、某本地生活 App 0.3%，
看着像"几乎没测"，其实是分母掺了新闻标题与广告位）⇒ 团队会为了达标去灌水用例。
所以下面每条测试都对应一条**刻意定下的口径**。
"""

from __future__ import annotations

from insta_engine.coverage import (
    analyze,
    case_targets,
    load_cases_for,
    page_coverage,
    read_core_pages,
    render_text,
)


def _case(*targets: tuple[str, str]) -> dict:
    """构造用例：每个 (strategy, value) 一个步骤。"""
    return {"caseId": "c", "name": "n", "target": {"package": "com.t"},
            "steps": [{"id": f"s{i}", "action": "tap",
                       "target": {"strategy": s, "value": v}}
                      for i, (s, v) in enumerate(targets, 1)]}


def _page(title: str, *controls: dict, depth: int = 0, key: str = "k") -> dict:
    return {"page_key": key, "title": title, "depth": depth, "controls": list(controls)}


def _ctrl(text: str = "", clickable: bool = True, **kw) -> dict:
    out = {"text": text, "clickable": clickable}
    out.update(kw)
    return out


# -- 口径 ①：页面级优先 ---------------------------------------------------------

def test_page_coverage_counts_page_as_covered_by_any_control():
    """页面只要被任意用例点到任意控件，就算被碰过（不受内容文案影响）。"""
    pages = [_page("搜索页", _ctrl("搜索框"), _ctrl("2026年诺贝尔物理学奖揭晓"), key="a"),
             _page("图片查看页", _ctrl("图片"), key="b")]
    covered, gap = page_coverage(pages, [_case(("text", "搜索框"))])
    assert [p["title"] for p in covered] == ["搜索页"]
    assert [p["title"] for p in gap] == ["图片查看页"], "没被碰过的页要进盲区"


def test_page_coverage_dedupes_by_title_and_sorts_gap_by_depth():
    """`page_key` 不稳会让同屏记多条 ⇒ 按 title 去重；盲区按层级浅→深排（浅层更该先补）。"""
    pages = [_page("首页", _ctrl("甲"), depth=0, key="k1"),
             _page("首页", _ctrl("甲"), depth=0, key="k2"),      # 同页重复记录
             _page("深页", _ctrl("乙"), depth=3, key="k3"),
             _page("浅页", _ctrl("丙"), depth=1, key="k4")]
    covered, gap = page_coverage(pages, [])
    assert [p["title"] for p in covered] == []
    assert [p["title"] for p in gap] == ["首页", "浅页", "深页"], "按 depth 排序"


# -- 口径 ②：控件级分母的四道过滤 -----------------------------------------------

def test_denominator_excludes_non_clickable_dynamic_snapshot_and_invisible():
    """默认分母 = 可点击 + 非动态 + 非内容快照 + 非不可见字符。

    ⚠️ 内容快照这条是实测补的：某资讯 App 缺口前 12 条里 8 条是新闻标题
    （「2026年诺贝尔物理学奖揭晓」这类），不排除会让指标失真。
    """
    pages = [_page("页", _ctrl("搜索"),                       # 保留
                   _ctrl("3分钟前"),                          # 动态（相对时间，命中 is_dynamic_text）
                   _ctrl("2026年诺贝尔物理学奖揭晓，网友热议"),      # 内容快照（够长 + 标点）
                   _ctrl("不可点的文字", clickable=False),      # 非可点击
                   _ctrl("\ue917"),                          # 私有区图标字形
                   key="p")]
    result = analyze(pages, [])
    values = {g["value"] for g in result["gap"]}
    assert "搜索" in values
    assert not ({"3分钟前", "2026年诺贝尔物理学奖揭晓，网友热议",
                 "不可点的文字", "\ue917"} & values), f"过滤失效：{values}"
    # ⚠️ 已知局限（实测）：`is_dynamic_text` 是**锚定**模式，复合日期串
    #（"2026年10月16日 12:30"）不在其列 ⇒ 控件级清单仍会漏进这类文案。
    # 这也是口径 ① 把页面级放前面的原因之一。


def test_all_controls_switch_uses_full_denominator():
    """`--all-controls` 是诊断口径：分母变大、包含被过滤的控件。"""
    pages = [_page("页", _ctrl("搜索"), _ctrl("3分钟前"), key="p")]
    default = analyze(pages, [])
    full = analyze(pages, [], all_controls=True)
    assert default["denominator"] == 1 and full["denominator"] == 2, (default, full)


# -- 覆盖率计算与其它信号 -------------------------------------------------------

def test_rate_and_covered_extra_and_empty_inputs():
    """覆盖率算法；用例引用了图谱没有的值 ⇒ 记入 `covered_extra`（幻觉/改版候选）。"""
    pages = [_page("页", _ctrl("搜索"), _ctrl("设置"), key="p")]
    result = analyze(pages, [_case(("text", "搜索"), ("text", "编造的入口"))])
    assert result["denominator"] == 2 and result["covered"] == 1
    assert result["rate"] == 0.5
    assert result["covered_extra"] == ["编造的入口"], result["covered_extra"]
    # 空输入不崩、rate 为 None（不是 0——"没有分母"与"覆盖率为 0"是两件事）
    empty = analyze([], [])
    assert empty["denominator"] == 0 and empty["rate"] is None
    assert empty["pages_gap"] == []


def test_case_targets_includes_assertions_and_fallback_chain():
    """断言 target 与 fallback 链都算覆盖（只断不点的用例同样覆盖了那个控件）。"""
    case = {"caseId": "c", "target": {"package": "com.t"}, "steps": [
        {"id": "s1", "action": "tap", "target": {
            "strategy": "res-id", "value": "pkg:id/a",
            "fallback": {"strategy": "text", "value": "兜底文案"}}},
        {"id": "s2", "action": "wait", "assertions": [
            {"type": "exists", "target": {"strategy": "description", "value": "断言目标"}}]},
    ]}
    targets = case_targets([case])
    assert targets["res-id"] == {"pkg:id/a"}
    assert targets["text"] == {"兜底文案"}
    assert targets["desc"] == {"断言目标"}


# -- 主操作覆盖档（页面级第二档，判据已冻结）---------------------------

def test_main_ops_tier_requires_half_of_top_actionable_controls():
    """主操作覆盖 = 前 8 个可操作控件命中 ≥ 半数；命中 1/4 只算「仅碰过」。"""
    pages = [_page("页", _ctrl("按钮0"), _ctrl("按钮1"), _ctrl("按钮2"), _ctrl("按钮3"),
                   key="p")]
    half = analyze(pages, [_case(("text", "按钮0"), ("text", "按钮1"))])
    entry = half["pages_covered"][0]
    assert entry["main_ops_hit"] == 2 and entry["main_ops_total"] == 4
    assert entry["main_ops_covered"] is True
    few = analyze(pages, [_case(("text", "按钮0"))])
    entry = few["pages_covered"][0]
    assert entry["main_ops_hit"] == 1 and entry["main_ops_covered"] is False


def test_main_ops_excludes_non_actionable_and_caps_at_8():
    """主操作分母走口径 ② 过滤（动态文案/不可点击不进），且按记录序只取前 8 个。"""
    controls = [_ctrl(f"按钮{i}") for i in range(10)]
    controls += [_ctrl("3分钟前"), _ctrl("不可点的文字", clickable=False)]
    pages = [_page("页", *controls, key="p")]
    case = _case(*[("text", f"按钮{i}") for i in range(9)])
    entry = analyze(pages, [case])["pages_covered"][0]
    assert entry["main_ops_total"] == 8, "超出前 8 的控件不进主操作分母"
    assert entry["main_ops_hit"] == 8 and entry["main_ops_covered"] is True


def test_main_ops_dedupes_duplicate_page_records():
    """同页重复记录（page_key 不稳）不能重复计入主操作分母。"""
    pages = [_page("首页", _ctrl("甲甲"), key="k1"),
             _page("首页", _ctrl("甲甲"), key="k2")]
    entry = analyze(pages, [])["pages_gap"][0]
    assert entry["main_ops_total"] == 1


def test_main_ops_zero_actionable_means_never_covered():
    """该页没有可操作控件 ⇒ 永远不可能主操作覆盖（哪怕因动态文案被「碰过」）。"""
    pages = [_page("页", _ctrl("3分钟前"), key="p")]
    entry = analyze(pages, [_case(("text", "3分钟前"))])["pages_covered"][0]
    assert entry["matched"] > 0 and entry["main_ops_covered"] is False
    assert entry["main_ops_total"] == 0


def test_render_text_shows_two_tiers_in_core_mode():
    """--core 下输出两档明细：主操作覆盖 / 仅碰过 / 盲区 三态都要可见。"""
    pages = [_page("首页", _ctrl("甲甲"), _ctrl("乙乙"), _ctrl("丙丙"), key="a"),
             _page("搜索页", _ctrl("搜一搜"), _ctrl("扫一扫"), key="b"),
             _page("设置页", _ctrl("丁丁"), key="c")]
    result = analyze(pages, [_case(("text", "甲甲"), ("text", "搜一搜"), ("text", "扫一扫"))],
                     core_pages=["首页", "搜索页", "设置页"])
    text = render_text("com.t", result)
    assert "主操作覆盖 2/2" in text, "搜索页 2/2 过半 ⇒ 主操作覆盖"
    assert "仅碰过（主操作 1/3）" in text, "首页 1/3 不过半 ⇒ 仅碰过"
    assert "[盲区]" in text and "设置页" in text


def test_render_text_flags_core_pages_missing_from_graph():
    """核心清单里图谱不存在的页不得无声消失——点名警告（反灌水）。"""
    pages = [_page("首页", _ctrl("甲甲"), key="a")]
    result = analyze(pages, [], core_pages=["首页", "幽灵页"])
    text = render_text("com.t", result)
    assert "幽灵页" in text and "不存在" in text


# -- 核心页面口径（推荐验收口径）------------------------------------------------

def test_core_pages_filter_narrows_scope():
    """`--core` 只统计清单里的页面 ⇒ 分母可控（推荐作为验收判据的口径）。"""
    pages = [_page("核心页", _ctrl("甲甲"), key="a"), _page("其他页", _ctrl("乙乙"), key="b")]
    result = analyze(pages, [], core_pages=["核心页"])
    assert result["core_pages"] == ["核心页"]
    assert result["denominator"] == 1 and result["pages_total"] == 1
    assert [p["title"] for p in result["pages_gap"]] == ["核心页"]


def test_read_core_pages_ignores_comments_and_blanks(tmp_path):
    path = tmp_path / "core.txt"
    path.write_text("# 核心页面\n搜索页\n\n  首页  # 行尾注释\n", encoding="utf-8")
    assert read_core_pages(path) == ["搜索页", "首页"]


def test_load_cases_filters_package_and_skips_broken_files(tmp_path):
    """`cases/` 是用户可手改目录：坏 JSON 跳过而不是崩；只收本包的用例。"""
    (tmp_path / "a.json").write_text('{"caseId":"a","target":{"package":"com.t"},"steps":[]}',
                                     encoding="utf-8")
    (tmp_path / "b.json").write_text('{"caseId":"b","target":{"package":"com.other"},"steps":[]}',
                                     encoding="utf-8")
    (tmp_path / "broken.json").write_text("{ 这不是 JSON", encoding="utf-8")
    cases, names = load_cases_for("com.t", tmp_path)
    assert names == ["a.json"] and len(cases) == 1


def test_render_text_shows_page_level_first():
    """输出顺序：**页面级盲区在前**（可行动），控件级细节在后。"""
    pages = [_page("盲区页", _ctrl("甲"), key="a")]
    text = render_text("com.t", analyze(pages, []))
    assert "页面级" in text and "盲区 1 页" in text
    assert text.index("页面级") < text.index("控件级"), "页面级必须排在控件级前面"


# -- 用例发现深度（覆盖率口径修复，2026-10-25） ------------------------------

def test_load_cases_finds_subdirectory_cases(tmp_path):
    """用例按 App 分子目录组织时也必须被发现——一层 glob 曾让某内容社区 App 的 6 条显示
    「用例 0 条」，覆盖率口径整体失真（与 export/list_runs 的 rglob 修复同族，C33）。"""
    import json

    from insta_engine.coverage import load_cases_for

    (tmp_path / "cases" / "xhs").mkdir(parents=True)
    (tmp_path / "cases" / "xhs" / "a.json").write_text(json.dumps(
        {"caseId": "a", "target": {"package": "com.xingin.xhs"}, "steps": []}), encoding="utf-8")
    (tmp_path / "cases" / "top.json").write_text(json.dumps(
        {"caseId": "t", "target": {"package": "com.xingin.xhs"}, "steps": []}), encoding="utf-8")
    (tmp_path / "cases" / "xhs" / "other.json").write_text(json.dumps(
        {"caseId": "o", "target": {"package": "com.other"}, "steps": []}), encoding="utf-8")

    cases, names = load_cases_for("com.xingin.xhs", tmp_path / "cases")
    assert sorted(names) == ["a.json", "top.json"], names
