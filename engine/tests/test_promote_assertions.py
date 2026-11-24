"""promote 的到达断言（评审 #13）。

评审原话：探索转成的用例**没有断言**。没有断言的用例可以回放但**不可判定**——
它会占着"通过"却什么也没证明，与本项目"verify 数字要可信"直接冲突。

这里守的是"挑得对不对"，不是"挑得出来"：宁可返回 None 并说明，也不要塞一条
必然假红或恒真的断言。
"""

from __future__ import annotations

from insta_engine.promote import arrival_assertion


def _page(*labels: str, controls: list[dict] | None = None) -> dict:
    return {"structure": {"key_elements": list(labels)}, "controls": controls or []}


def test_picks_label_that_start_page_lacks():
    start = _page("首页", "搜索")
    target = _page("个人主页", "笔记", "收藏")
    a, why = arrival_assertion(target, start)
    assert a is not None and a.target.value == "个人主页"
    assert "起点页没有" in why


def test_skips_labels_also_on_start_page():
    """起点页也有的文案断它恒真——证明不了"到了新页"。"""
    start = _page("首页", "推荐")
    target = _page("推荐", "关注")
    a, _ = arrival_assertion(target, start)
    assert a is not None and a.target.value == "关注"


def test_prefers_res_id_when_text_matches_a_control():
    target = _page("我的订单", controls=[{"text": "我的订单", "res-id": "com.x:id/order_title"}])
    a, why = arrival_assertion(target, _page("首页"))
    assert a.target.strategy.value == "res-id"
    assert "id 比文案稳" in why


def test_obfuscated_res_id_falls_back_to_text():
    """`0_resource_name_obfuscated` 换一次混淆策略就变 ⇒ 不能当断言。"""
    target = _page("个人页头图",
                   controls=[{"desc": "个人页头图",
                              "res-id": "com.x:id/0_resource_name_obfuscated"}])
    a, _ = arrival_assertion(target, _page("首页"))
    assert a.target.strategy.value == "text"
    assert a.target.value == "个人页头图"


def test_structural_and_dynamic_labels_are_skipped():
    target = _page("头图or头视频区容器", "布局占位", "2026-11-24 更新", "真实特征")
    a, _ = arrival_assertion(target, _page("首页"))
    assert a is not None and a.target.value == "真实特征", "容器/动态文案不能当选断言"


def test_too_long_label_is_skipped():
    target = _page("相恋30年刚领证，转眼人没了", "短标题")
    a, _ = arrival_assertion(target, _page("首页"))
    assert a.target.value == "短标题"


def test_returns_none_with_reason_when_nothing_usable():
    a, why = arrival_assertion(_page("首页", "搜索"), _page("首页", "搜索"))
    assert a is None
    assert "证明不了" in why


def test_missing_target_page_reports_reason():
    a, why = arrival_assertion(None, _page("首页"))
    assert a is None and "结构缺失" in why
