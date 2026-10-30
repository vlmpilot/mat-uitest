"""UI 骨架信号（`chrome_signals`）与它在碎片合并里的**通道选择**。

## 为什么需要这个文件（2026-10-29，某内容社区 App 真机图谱）

`fragment_merge` 原来的判据只有 rid 通道。某内容社区 App **全图只有一个非退化 rid**，
而且是根容器 `index_store`（每页都有）⇒ 16 个页面（首页 / 市集 / 消息 / 我的）的
rid 集合都是 `{index_store}`，Jaccard 恒为 1.0 ⇒ dry-run 把「首页 + 消息 + 我的」
并成一页（52 页 → 8 页）。**1 元集的 Jaccard 只有 0/1 两档**，不能当相似度用。

⇒ 两条规则：① 单 rid 不构成证据（`MIN_RID_SIGNALS`）；② rid 稀薄时改比骨架信号。
本文件锁住这两条，以及"跨页型不许并"。
"""

from __future__ import annotations

from insta_engine.fragment_merge import CHROME_SIM, MIN_RID_SIGNALS, merge_fragments
from insta_engine.page_index import chrome_signals, keyed_pages, new_index

# -- chrome_signals 本体 -------------------------------------------------------


def test_collects_navigation_and_drops_content():
    """tab / 底栏 / 搜索 / 菜单 进信号；推荐流标题这类**内容**不进。"""
    controls = [
        {"class": "android.app.ActionBar$Tab", "desc": "发现"},
        {"class": "android.widget.TextView", "text": "关注"},
        {"class": "android.widget.TextView", "text": "搜索"},
        {"class": "android.view.ViewGroup", "desc": "首页"},
        {"class": "android.widget.TextView", "text": "刚刚发生的最新消息来了"},
        {"class": "android.widget.TextView", "text": "霍启刚又幸福了"},
    ]
    assert chrome_signals(controls) == frozenset(
        {"tab:发现", "txt:关注", "txt:搜索", "txt:首页"})


def test_dynamic_segment_uses_stable_prefix():
    """`消息，3条未读` → `消息`（未读数一变就换指纹，是碎片化的经典成因）。"""
    assert chrome_signals([{"class": "android.view.ViewGroup",
                            "desc": "消息，3条未读"}]) == frozenset({"txt:消息"})
    # 纯动态整串直接丢（`317` 赞数）
    assert chrome_signals([{"class": "android.widget.TextView", "text": "317"}]) == frozenset()


def test_degenerate_rid_falls_back_to_label():
    """退化 rid（`0_resource_name_obfuscated`）当没有 rid，改看文案。"""
    got = chrome_signals([{"class": "android.widget.TextView",
                           "res-id": "com.x:id/0_resource_name_obfuscated",
                           "text": "我"}])
    assert got == frozenset({"txt:我"})


def test_real_rid_is_kept():
    got = chrome_signals([{"class": "android.view.ViewGroup",
                           "res-id": "com.x:id/index_store"}])
    assert got == frozenset({"rid:com.x:id/index_store"})


def test_iconfont_labels_are_dropped():
    """iconfont 私有区字符没有语义（重放按文案匹配不到）。"""
    assert chrome_signals([{"class": "android.widget.TextView",
                            "text": "\ue90f"}]) == frozenset()


# -- 通道选择 ------------------------------------------------------------------


def _page(key, *, activity="com.x/.Main", rids=(), labels=(), parent="root", hits=1):
    """构造一页：`rids` 进 identities，`labels` 进 controls 的短文案。

    `parent` 默认挂在 `root` 下——`merge_fragments` 要求**链根相同**才比较，
    没有共同根页时它一律不并（那是另一个保护，不该混进这些用例）。
    """
    controls = [{"class": "android.view.ViewGroup", "res-id": r} for r in rids]
    controls += [{"class": "android.widget.TextView", "text": label} for label in labels]
    page = {"page_key": key, "activity": activity, "depth": 0 if not parent else 1,
            "hits": hits, "identities": [f"rid:{r}" for r in rids], "controls": controls}
    if parent:
        page["parent_key"] = parent
        page["parent_action"] = "去页"
    return page


def _index(pages):
    idx = new_index("com.x")
    # 根页：无 rid、无文案 ⇒ 两个通道都够不上候选（只提供共同的链根）
    idx["pages"] = [_page("root", parent=None)] + pages
    idx["edges"] = []
    return idx


#: 首页骨架（真实形态：tab 三连 + 底栏五连 + 搜索/菜单）。
_HOME = ("关注", "发现", "天津", "首页", "市集", "发布", "消息", "我", "搜索", "菜单")


def test_single_shared_rid_is_not_evidence():
    """单 rid（根容器）不足以判同页：首页与消息页**不许**并。"""
    pages = [
        _page("home", rids=["com.x:id/index_store"], labels=_HOME + ("刚刚发生",)),
        _page("msg", rids=["com.x:id/index_store"],
              labels=("首页", "市集", "发布", "消息", "我", "系统消息", "活动消息")),
    ]
    report = merge_fragments(_index(pages))
    assert MIN_RID_SIGNALS == 2
    assert report["merged_away"] == 0, report["groups"]


def test_chrome_channel_merges_same_page_type_fragments():
    """rid 稀薄时，同页型碎片（首页 3 份观测，各带 2 条轮换文案）并成 1 页。"""
    pages = [
        _page("home_a", rids=["com.x:id/index_store"], labels=_HOME + ("懒懒", "晴小天sd")),
        _page("home_b", rids=["com.x:id/index_store"], labels=_HOME + ("南星指南", "天津周末"), hits=2),
        _page("home_c", rids=["com.x:id/index_store"], labels=_HOME + ("八零徐姥姥", "蒙曼")),
    ]
    report = merge_fragments(_index(pages))
    assert report["merged_away"] == 2, report["groups"]
    assert len(report["groups"]) == 1
    worst = min(m["j"] for m in report["groups"][0]["members"])
    assert worst >= CHROME_SIM


def test_chrome_channel_keeps_different_categories_apart():
    """同 activity 但不同类目（市集-运动 vs 市集-家居）**不许**并。

    真实形态：两页共享顶栏 + 底栏 + 一级类目条（`食饮/运动/潮玩…`），
    但二级网格完全不同（`运动鞋/瑜伽/露营灯…` vs `家居/客厅/卧室…`）。
    实测这类对 J 在 0.30~0.55，低于 0.6 —— 这也是阈值不取 0.5 的原因。
    """
    shared = ("首页", "市集", "发布", "消息", "我", "搜索", "ai购物",
              "食饮", "运动", "潮玩", "生鲜", "文玩", "数码", "亲子")
    sports = shared + ("运动鞋", "瑜伽", "露营灯", "护腕", "骑行手套", "越野跑", "网球鞋", "泳裤")
    home_living = shared + ("家居", "客厅", "卧室", "地毯", "浴室", "玄关", "阳台", "香薰")
    pages = [
        _page("mall_sport", rids=["com.x:id/index_store"], labels=sports),
        _page("mall_home", rids=["com.x:id/index_store"], labels=home_living),
    ]
    report = merge_fragments(_index(pages))
    assert report["merged_away"] == 0, report["groups"]


def test_rid_channel_still_wins_when_evidence_is_rich():
    """rid 证据充分（≥2）时仍走 rid 通道：骨架不同也照样并（历史口径不变）。"""
    pages = [
        _page("d1", rids=["com.x:id/title", "com.x:id/body"], labels=("分享", "评论")),
        _page("d2", rids=["com.x:id/title", "com.x:id/body"], labels=("赞", "收藏")),
    ]
    report = merge_fragments(_index(pages))
    assert report["merged_away"] == 1
    assert report["groups"][0]["members"][0]["j"] == 1.0


def test_channels_are_not_compared_across_each_other():
    """一页 rid 充分、一页稀薄 ⇒ 通道不同，不比（宁漏勿错）。"""
    pages = [
        _page("rich", rids=["com.x:id/title", "com.x:id/body"], labels=("分享", "评论")),
        _page("thin", rids=["com.x:id/index_store"], labels=("分享", "评论")),
    ]
    assert merge_fragments(_index(pages))["merged_away"] == 0


def test_no_empty_groups_in_report():
    """代表页与组内其余页都够不上阈值时**不报空组**（旧实现会留 `← [0页]` 噪声）。

    构造：A~B、B~C 均达阈值，A~C 不达；A 的 hits 最高 ⇒ A 当代表，C 单独成组。
    """
    pages = [
        _page("A", rids=["com.x:id/1", "com.x:id/2", "com.x:id/3"], hits=9),
        _page("B", rids=["com.x:id/2", "com.x:id/3", "com.x:id/4"]),
        _page("C", rids=["com.x:id/3", "com.x:id/4", "com.x:id/5"]),
    ]
    report = merge_fragments(_index(pages))
    assert report["merged_away"] == 1
    assert len(report["groups"]) == 1
    assert all(g["members"] for g in report["groups"])


# -- apply：控件取并集（合并对候选集召回中性）-----------------------------------


def test_apply_unions_controls_of_merged_pages():
    """被并页独有的控件必须留在代表页上——否则生成侧候选集静默降覆盖。

    实测代价（资讯类 App）：只留代表页控件时，某用例的一个定位值从"在清单里"
    变成"不在"（召回 22/35 → 21/35）。
    """
    a = _page("a", rids=["com.x:id/t1", "com.x:id/b1"])
    b = _page("b", rids=["com.x:id/t1", "com.x:id/b1"], hits=1)
    a["controls"] = [{"class": "T", "res-id": "com.x:id/t1"},
                     {"class": "T", "text": "分享"},
                     {"class": "T", "text": "只有 A 才有的文案"}]
    b["controls"] = [{"class": "T", "res-id": "com.x:id/t1"},
                     {"class": "T", "text": "分享"},
                     {"class": "T", "text": "只有 B 才有的文案"}]
    idx = _index([a, b])
    rep = merge_fragments(idx, apply=True)["groups"][0]["representative"]
    page = next(p for p in idx["pages"] if p["page_key"] == rep)
    labels = [c.get("text") for c in page["controls"] if c.get("text")]
    assert "分享" in labels
    assert len([x for x in labels if x == "分享"]) == 1, "同名骨架控件不许重复（R21 会误报）"
    assert {"只有 A 才有的文案", "只有 B 才有的文案"} <= set(labels), labels
    assert page["node_count"] == len(page["controls"])


def test_keyed_pages_resolves_aliases_to_representative():
    """合并删掉的旧 key 必须仍能反查到代表页。

    否则运行期拿到"活页面的 key 恰好是被删的那个碎片"时，`pages_by_key.get()` 是 None
    ⇒ `pages_equivalent(None, target)` 恒 False ⇒ 置备/走链误判"没到目标页"，
    现象看起来像定位值写错（`arrival_provision` 与 `arrivals._walk_chain` 都吃这个亏）。
    """
    pages = [
        _page("home_a", rids=["com.x:id/index_store"], labels=_HOME + ("懒懒",)),
        _page("home_b", rids=["com.x:id/index_store"], labels=_HOME + ("蒙曼",)),
    ]
    idx = _index([*pages])
    rep = merge_fragments(idx, apply=True)["groups"][0]["representative"]
    gone = next(p for p in ("home_a", "home_b") if p != rep)
    keyed = keyed_pages(idx)
    assert keyed[rep]["page_key"] == rep
    assert keyed[gone] is keyed[rep], "旧 key 应解析到代表页（同一个对象）"
    # 活页 key 不受影响，也不重复
    assert len([k for k in keyed if k in ("home_a", "home_b")]) == 2
