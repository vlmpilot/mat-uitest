"""A2（断言与导航耦合）的**确定性改写**：`case_audit.rewrite_coupled_assertions`。

判据来源（2026-10-24）：tx-news 12 条历史失败用例里 2 条是 A2，其中 005（`tap 关注` +
`exists 关注`）真机验证过 —— 频道栏 `channelbar_text` **带 `selected` 状态**，且只有当前 tab
为 true ⇒ 断言从"恒真"变成"真的切过去才通过"。

本文件守四件事：
1. 能改的必须改（tab + 落点页还在 + 文案唯一）；
2. **改不了的一律不动**（宁可不改，不可改错）：真跳转 / 文案不唯一 / 走链断了；
3. 状态断言不再被 R2/R23 误报（`tap 我的` + `selected 我的` 是推荐写法）；
4. 改写要**跑在检查之前**（`review(rewrite=...)`），否则同一条用例会同时背"已改好"和"仍报 R2"。
"""

from __future__ import annotations

from insta_engine.dsl import LocatorStrategy, TestCase
from insta_engine.page_index import identities_from_controls, save_index

PKG = "com.x"
TAB_RID = f"{PKG}:id/channelbar_text"


def _page(key: str, **kw) -> dict:
    base = {"page_key": key, "depth": 1, "parent_key": None, "parent_action": None,
            "activity": f"{PKG}/.A", "identities": [], "controls": [],
            "alt_entries": [], "hits": 1}
    base.update(kw)
    if not base["identities"] and base["controls"]:
        base["identities"] = sorted(identities_from_controls(base["controls"]))
    return base


def _tabs(*labels: str) -> list[dict]:
    return [{"text": label, "res-id": TAB_RID, "clickable": True} for label in labels]


def _index(tmp_path, pages: list[dict]):
    save_index(tmp_path / f"{PKG}.json", {"package": PKG, "generated_at": "t",
                                         "sources": ["explore"], "pages": pages, "edges": []})
    return tmp_path


def _case(*, assertion_type: str = "exists", value: str = "关注",
          step_value: str | None = None, strategy=LocatorStrategy.TEXT) -> dict:
    """`launch → tap(X)` + 一条断言（默认与 X 同一目标）。"""
    step_value = value if step_value is None else step_value
    return {
        "caseId": "case_000001", "name": "切频道", "target": {"package": PKG},
        "steps": [
            {"id": "s1", "action": "launch", "params": {"package": PKG}},
            {"id": "s2", "action": "tap",
             "target": {"strategy": strategy.value, "value": step_value},
             "assertions": [{"type": assertion_type,
                             "target": {"strategy": strategy.value, "value": value}}]},
        ],
    }


# -- 1. 能改的必须改 ------------------------------------------------------------


def test_tab_assertion_rewritten_to_selected(tmp_path):
    """tab 点完自己还在 ⇒ `exists` 恒真 ⇒ 改成 `selected`（真机验证过 tx-news 可读）。"""
    from insta_engine.case_audit import rewrite_coupled_assertions

    idx = _index(tmp_path, [
        _page("root", depth=0, controls=_tabs("要闻", "关注")),
        _page("follow", depth=1, parent_key="root", parent_action="关注",
              controls=_tabs("要闻", "关注") + [{"text": "文章标题", "clickable": True}]),
    ])
    raw = _case()
    issues = rewrite_coupled_assertions(raw, idx)
    assertion = raw["steps"][1]["assertions"][0]
    assert assertion["type"] == "selected", assertion
    assert assertion["target"] == {"strategy": "text", "value": "关注"}
    assert len(issues) == 1 and issues[0].startswith("[R25]"), issues


def test_rewrite_goes_through_review_before_checks(tmp_path):
    """改写跑在检查**之前** ⇒ 修好了就不再报 R2（否则一条用例背两个相反的结论）。"""
    from insta_engine.case_audit import rewrite_coupled_assertions
    from insta_engine.case_quality import review

    idx = _index(tmp_path, [
        _page("root", depth=0, controls=_tabs("要闻", "关注")),
        _page("follow", depth=1, parent_key="root", parent_action="关注",
              controls=_tabs("要闻", "关注")),
    ])
    raw = _case()
    issues = review(raw, rewrite=lambda doc: rewrite_coupled_assertions(doc, idx))
    assert any(i.startswith("[R25]") for i in issues), issues
    assert not any(i.startswith("[R2]") for i in issues), issues


def test_rewrite_applied_by_quality_gate(tmp_path):
    """`apply_quality_gate(index_dir=...)` 是产用例的唯一入口 ⇒ 它必须带上这条改写。"""
    from insta_engine.ai_client import apply_quality_gate

    idx = _index(tmp_path, [
        _page("root", depth=0, controls=_tabs("要闻", "关注")),
        _page("follow", depth=1, parent_key="root", parent_action="关注",
              controls=_tabs("要闻", "关注")),
    ])
    fixed = apply_quality_gate(TestCase.model_validate(_case()), index_dir=idx)
    assert fixed.steps[1].assertions[0].type.value == "selected"
    assert any("[R25]" in i for i in fixed.meta.get("quality_issues") or [])


# -- 2. 改不了的一律不动 --------------------------------------------------------


def test_real_navigation_is_left_alone(tmp_path):
    """真跳转（控件不在落点页）⇒ `selected` 会恒假，不能改；留给 R2 报。"""
    from insta_engine.case_audit import rewrite_coupled_assertions

    idx = _index(tmp_path, [
        _page("root", depth=0, controls=_tabs("要闻", "关注")),
        _page("detail", depth=1, parent_key="root", parent_action="关注",
              controls=[{"text": "订阅", "clickable": True}]),
    ])
    raw = _case()
    assert rewrite_coupled_assertions(raw, idx) == []
    assert raw["steps"][1]["assertions"][0]["type"] == "exists"


def test_ambiguous_label_is_left_alone(tmp_path):
    """文案在本页不唯一（`视频` 同时在频道栏和底栏）⇒ `selected` 会断到错的那个。"""
    from insta_engine.case_audit import rewrite_coupled_assertions

    idx = _index(tmp_path, [
        _page("root", depth=0, controls=_tabs("视频", "关注")
              + [{"text": "视频", "res-id": f"{PKG}:id/nav_btn_text", "clickable": True}]),
        _page("video", depth=1, parent_key="root", parent_action="视频",
              controls=_tabs("视频", "关注")),
    ])
    raw = _case(value="视频", step_value="视频")
    assert rewrite_coupled_assertions(raw, idx) == []
    assert raw["steps"][1]["assertions"][0]["type"] == "exists"


def test_unknown_landing_page_is_left_alone(tmp_path):
    """走链断了（图谱没有这条边）⇒ 不知道点完在哪，就不知道控件还在不在 ⇒ 不动。"""
    from insta_engine.case_audit import rewrite_coupled_assertions

    idx = _index(tmp_path, [
        _page("root", depth=0, controls=_tabs("要闻", "关注")),
        _page("other", depth=1, parent_key="root", parent_action="我的",
              controls=_tabs("要闻", "关注")),
    ])
    raw = _case()
    assert rewrite_coupled_assertions(raw, idx) == []


def test_plain_button_is_not_a_tab(tmp_path):
    """共用 rid 的**列表项**不是 tab（`title_text` 每条新闻都共用）：点完会跳走，不能断 selected。"""
    from insta_engine.case_audit import rewrite_coupled_assertions

    item_rid = f"{PKG}:id/title_text"
    idx = _index(tmp_path, [
        _page("root", depth=0, controls=[
            {"text": "新闻一", "res-id": item_rid, "clickable": True},
            {"text": "新闻二", "res-id": item_rid, "clickable": True},
        ]),
        _page("detail", depth=1, parent_key="root", parent_action="新闻一",
              controls=[{"text": "新闻一", "res-id": item_rid}]),
    ])
    raw = _case(value="新闻一", step_value="新闻一")
    assert rewrite_coupled_assertions(raw, idx) == []


def test_no_index_is_a_noop():
    from insta_engine.case_audit import rewrite_coupled_assertions

    raw = _case()
    assert rewrite_coupled_assertions(raw, "/nonexistent") == []
    assert raw["steps"][1]["assertions"][0]["type"] == "exists"


# -- 3. 状态断言不再被误报 ------------------------------------------------------


def test_state_assertion_on_tapped_control_is_not_reported():
    """`tap 我的` + `selected 我的` 是**推荐**写法（tab 一直在，选中态会变）⇒ 不许报 R2/R23。"""
    from insta_engine.case_quality import review

    raw = _case(assertion_type="selected", value="我的", step_value="我的")
    issues = review(raw)
    assert not any(i.startswith("[R2]") for i in issues), issues
    assert not any(i.startswith("[R23]") for i in issues), issues


def test_exists_assertion_on_tapped_control_still_reported():
    """反过来必须还报：`tap X` + `exists X` 的缺陷没被这次豁免带走。"""
    from insta_engine.case_quality import review

    issues = review(_case(assertion_type="exists", value="我的", step_value="我的"))
    assert any(i.startswith("[R2]") for i in issues), issues
