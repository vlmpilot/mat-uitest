"""同 activity 变体折叠（覆盖率口径修复，2026-10-25）。

实测病灶：某团购 App 首页 activity 130/223、tx-news(资讯 App) 21/44 是
同一逻辑页的内容态变体，覆盖率分母被灌水。判据保守性由构造保证：必须有
锚点（hits≥2 或 depth-0 根），纯内容实例组（全 hits==1 无锚点，如某内容社区
App 的笔记详情×23）永不折叠。
"""

from __future__ import annotations

from insta_engine.graph_repair import collapse_activity_variants


def _page(key, act, *, depth=1, hits=1, controls=1, parent=None, action=None):
    return {"page_key": key, "activity": act, "depth": depth, "hits": hits,
            "controls": [{"text": f"c{i}", "bounds": f"b{key}", "rid": f"r{i}"}
                         for i in range(controls)],
            "tried": [f"t-{key}"], "identities": [f"i-{act}"], "alt_entries": [],
            "parent_key": parent, "parent_action": action, "parent_rid": None,
            "first_seen": "2026-10-25T01:00:00", "last_seen": "2026-10-25T02:00:00"}


def test_home_variants_fold_into_root_keeper():
    pk = {"home": _page("home", "Home", depth=0, hits=3, controls=2)}
    for i in range(6):
        pk[f"v{i}"] = _page(f"v{i}", "Home", parent="home", action=f"tab{i}")
    pk["child"] = _page("child", "Detail", depth=2, parent="v3", action="新闻")

    rep = collapse_activity_variants(pk)

    assert len(rep) == 1 and len(rep[0]["folded"]) == 6
    assert pk["child"]["parent_key"] == "home", "变体的子页必须改挂 keeper"
    assert pk["child"]["depth"] == 1
    assert pk["home"]["hits"] == 3 + 6, "hits 累加（对首页的每次到访都算到访）"
    assert len(pk["home"]["controls"]) == 2 + 6, "控件去重并集"
    assert all(f"v{i}" not in pk for i in range(6))


def test_pure_content_group_without_anchor_never_folds():
    """某内容社区 App 笔记详情×23 同型：全 hits==1、无 depth-0 锚点 ⇒ 不折叠。"""
    pk = {"home": _page("home", "Home", depth=0, hits=2)}
    for i in range(8):
        pk[f"note{i}"] = _page(f"note{i}", "Note", depth=2, parent="home")
    rep = collapse_activity_variants(pk)
    assert rep == []
    assert len(pk) == 9


def test_small_group_below_threshold_not_folded():
    """组 <5 页不碰（NewsDetail×2 可能真是不同内容）。"""
    pk = {k: _page(k, a, parent=par)
          for k, a, par in (("home", "Home", None), ("d1", "Detail", "home"), ("d2", "Detail", "home"))}
    pk["home"]["depth"] = 0
    pk["home"]["hits"] = 2
    assert collapse_activity_variants(pk) == []


# -- 折叠收尾：edges 重映射 / folds 留痕 / 容器二次分簇 ------------------------

def _edges(*triples):
    return [{"from": f, "to": to, "action": a, "count": 1} for f, to, a in triples]


def test_edges_remapped_and_no_dangling(tmp_path):
    """P0-3 ① 验收:折叠后 edges 的 from/to 不得指向已折叠 key(悬空边 = 0)。"""
    pk = {"home": _page("home", "Home", depth=0, hits=3, controls=2)}
    for i in range(6):
        pk[f"v{i}"] = _page(f"v{i}", "Home", parent="home", action=f"tab{i}")
    pk["child"] = _page("child", "Detail", depth=2, parent="v3", action="新闻")
    edges = _edges(("v1", "child", "go"), ("v2", "child", "go"),
                   ("home", "v4", "tab"), ("child", "v5", "back"))

    collapse_activity_variants(pk, edges)

    keys = set(pk)
    for e in edges:
        assert e["from"] in keys and e["to"] in keys, f"悬空边: {e}"
    # v1/v2 → child 两条同 from+to+action 合并为一条 count=2
    merged = [e for e in edges if e["from"] == "home" and e["to"] == "child"]
    assert len(merged) == 1 and merged[0]["count"] == 2
    # home→v4 折叠后变成自环(home→home),必须丢弃
    assert not [e for e in edges if e["from"] == e["to"]]


def test_folds_record_written_with_merged_into():
    """P0-3 ② 验收:折叠报告含 merged_into,被折叠页的 key/title/hits 留痕可审计。"""
    pk = {"home": _page("home", "Home", depth=0, hits=3)}
    for i in range(6):
        pk[f"v{i}"] = _page(f"v{i}", "Home", parent="home", action=f"tab{i}")

    rep = collapse_activity_variants(pk)

    assert len(rep) == 1
    fold = rep[0]
    assert fold["merged_into"] == fold["keeper"] == "home"
    assert len(fold["folded"]) == 6
    assert all("page_key" in f and "hits" in f for f in fold["folded"])


def test_container_activity_clusters_not_whole_group():
    """P0-3 ③ 验收:容器型 activity(Hippy/WebView…)组内按 identities 分簇,
    两种页型(搜索页 vs 详情页)不得并成一种。"""
    pk = {}
    # 簇 A:搜索页族(identities 带 search 信号)
    # 簇内 identities 全同(含 root——root 必须和自己的变体同簇),跨簇仅共 act:Hippy
    # (jaccard=1/3 < 0.5,两簇不并)——这正是"页型"的分界。
    pk["search_root"] = _page("search_root", "HippyActivity", depth=0, hits=4)
    pk["search_root"]["identities"] = ["act:Hippy", "search"]
    for i in range(4):
        p = _page(f"sv{i}", "HippyActivity", parent="search_root", action=f"s{i}")
        p["identities"] = ["act:Hippy", "search"]
        pk[f"sv{i}"] = p
    pk["detail_root"] = _page("detail_root", "HippyActivity", depth=1, hits=2)
    pk["detail_root"]["identities"] = ["act:Hippy", "detail"]
    for i in range(4):
        p = _page(f"dv{i}", "HippyActivity", parent="detail_root", action=f"d{i}")
        p["identities"] = ["act:Hippy", "detail"]
        pk[f"dv{i}"] = p

    rep = collapse_activity_variants(pk)

    keepers = {r["keeper"] for r in rep}
    assert "search_root" in keepers and "detail_root" in keepers, \
        f"容器组必须分簇保留两个页型 keeper,实际 {keepers}"
    assert "search_root" in pk and "detail_root" in pk
    assert len(pk) == 2, f"折叠后应只剩两个 keeper,实际 {len(pk)} 页"


# -- P0-6：折叠留痕要能事后复核（2026-10-26） ------------------------------

def test_fold_records_identities_and_similarity():
    """`folded[]` 必须带 identities 与 similarity，簇内最勉强的那一刀单列。

    病灶：原先只有 `page_key/title/hits/controls`。某团购 App「112 页并成 1 页」
    这种大合并**无法事后验证**是"同一逻辑页的变体"还是"把不同页型并掉了"——
    留痕里只有一串 page_key，谁也没法复算。
    """
    pk = {"home": _page("home", "Home", depth=0, hits=3)}
    pk["home"]["identities"] = ["a", "b", "c", "d"]
    for i in range(6):
        p = _page(f"v{i}", "Home", parent="home", action=f"tab{i}")
        p["identities"] = ["a", "b", "c", "d"]        # 与 keeper 完全一致
        pk[f"v{i}"] = p
    pk["v0"]["identities"] = ["a", "b", "c", "e"]      # 最勉强的一刀：J = 3/5

    fold = collapse_activity_variants(pk)[0]

    assert fold["min_similarity"] == 0.6, fold["min_similarity"]
    by_key = {f["page_key"]: f for f in fold["folded"]}
    assert by_key["v0"]["similarity"] == 0.6
    assert by_key["v0"]["identities"] == ["a", "b", "c", "e"], "identities 是复核的原始证据，必须留全"
    assert by_key["v1"]["similarity"] == 1.0


def test_similarity_uses_pre_merge_keeper_snapshot():
    """similarity 必须拿**并集之前**的 keeper identities 算。

    循环里 `keeper["identities"]` 会被逐个变体撑大。用撑大后的集合算，后面的变体
    会得到**虚高**的相似度（分母与交集同时变）——而 similarity 正是复核分簇的
    唯一依据，虚高等于把"勉强并的"伪装成"几乎一样"。
    """
    pk = {"home": _page("home", "Home", depth=0, hits=3)}
    pk["home"]["identities"] = ["a"]
    for i in range(6):
        p = _page(f"v{i}", "Home", parent="home", action=f"tab{i}")
        p["identities"] = ["a", f"x{i}"]
        pk[f"v{i}"] = p

    fold = collapse_activity_variants(pk)[0]

    # 每条变体对「只含 a 的 keeper」：交集恒 1、并集恒 2 ⇒ 全部 0.5。
    # 若用了并集后的 keeper，后面的会掉到 1/3（0.333）。
    assert {f["similarity"] for f in fold["folded"]} == {0.5}


def test_children_relinked_counted_per_fold():
    """多簇时 `children_relinked` 必须**按簇**统计。

    病灶：原实现 `relinked if len(report) == 1 else 0` ⇒ 两条 fold 记录时两边都写 0，
    于是"改挂成功"和"根本没改挂"在留痕里长得一模一样——比不记更坏。
    """
    pk = {}
    pk["sr"] = _page("sr", "HippyActivity", depth=0, hits=4)
    pk["sr"]["identities"] = ["act:H", "search"]
    for i in range(4):
        p = _page(f"sv{i}", "HippyActivity", parent="sr", action=f"s{i}")
        p["identities"] = ["act:H", "search"]
        pk[f"sv{i}"] = p
    pk["dr"] = _page("dr", "HippyActivity", depth=1, hits=3)
    pk["dr"]["identities"] = ["act:H", "detail"]
    for i in range(4):
        p = _page(f"dv{i}", "HippyActivity", parent="dr", action=f"d{i}")
        p["identities"] = ["act:H", "detail"]
        pk[f"dv{i}"] = p
    # 两簇各有一个子页挂在本簇**被折叠的变体**上
    pk["childA"] = _page("childA", "DetailA", depth=2, parent="sv1")
    pk["childB"] = _page("childB", "DetailB", depth=2, parent="dv1")

    rep = collapse_activity_variants(pk)

    by_keeper = {r["keeper"]: r for r in rep}
    assert by_keeper["sr"]["children_relinked"] == 1
    assert by_keeper["dr"]["children_relinked"] == 1
    assert pk["childA"]["parent_key"] == "sr" and pk["childB"]["parent_key"] == "dr"
