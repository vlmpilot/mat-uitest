"""碎片合并测试：A' 单锚点口径（dry-run 实测定型，见模块 docstring）。"""

from __future__ import annotations

import json

import pytest

from insta_engine.fragment_merge import merge_fragments
from insta_engine.page_index import new_index


def _page(key, *, activity="com.x/.Main", rids=("rid:com.x:id/a",), depth=0,
          parent=None, hits=1, **extra):
    first = rids[0].split(":", 1)[1] if rids else ""
    p = {"page_key": key, "activity": activity, "depth": depth,
         "hits": hits,
         "identities": [*rids]}
    if first:
        p["controls"] = [{"res-id": first}]
    if parent:
        p["parent_key"] = parent[0]
        p["parent_action"] = parent[1]
    p.update(extra)
    return p


def _index(pages, edges=None):
    idx = new_index("com.x")
    idx["pages"] = pages
    idx["edges"] = edges or []
    return idx


# -- 判据 ----------------------------------------------------------------------


def test_merge_same_root_activity_and_structure(tmp_path):
    """同链根 + 同 activity + rid J≥0.5 → 并入代表；dry-run 不改图谱。"""
    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("frag1", depth=1, parent=("root", "去页"), rids=("rid:com.x:id/a", "rid:com.x:id/b")),
        _page("frag2", depth=1, parent=("root", "去页"), rids=("rid:com.x:id/a", "rid:com.x:id/b"), hits=3),
    ])
    report = merge_fragments(idx, apply=False)
    assert report["merged_away"] == 1 and report["groups"][0]["representative"] == "frag2"
    assert len(idx["pages"]) == 3              # dry-run 不动图谱


def test_merge_skips_when_root_or_activity_or_structure_differs():
    """链根不同 / activity 不同 / rid J<0.5，三者任一 → 不并。"""
    base = _page("root", rids=("rid:com.x:id/root",))
    frag = _page("frag", depth=1, parent=("root", "去页"),
                 rids=("rid:com.x:id/a", "rid:com.x:id/b"))
    other_root = _page("frag2", depth=1, parent=("ghost", "别入口"),
                       rids=("rid:com.x:id/a", "rid:com.x:id/b"))
    other_act = _page("frag3", depth=1, parent=("root", "去页"), activity="com.x/.Other",
                      rids=("rid:com.x:id/a", "rid:com.x:id/b"))
    weak = _page("frag4", depth=1, parent=("root", "去页"), rids=("rid:com.x:id/z",))
    report = merge_fragments(_index([base, frag, other_root, other_act, weak]))
    assert report["merged_away"] == 0


def test_single_anchor_breaks_transitive_chain():
    """链式稀释防护：A~B J=0.5、B~C J=0.5、A~C J=0.2 → 闭包成组，但只并与代表 J≥0.5 的。"""
    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("A", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/1", "rid:com.x:id/2", "rid:com.x:id/3")),
        _page("B", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/2", "rid:com.x:id/3", "rid:com.x:id/4")),
        _page("C", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/3", "rid:com.x:id/4", "rid:com.x:id/5")),
    ])
    report = merge_fragments(idx, apply=True)
    # 代表（hits 全同 → key 字典序）= C；与 C J≥0.5 的只有 B（A J=0.2）→ A 不并
    assert report["merged_away"] == 1
    keys = {p["page_key"] for p in idx["pages"]}
    assert keys == {"root", "A", "C"}
    assert idx["aliases"]["B"]["of"] == "C"


# -- apply 落盘 ----------------------------------------------------------------


def test_apply_redirects_parent_and_merges_evidence():
    """被并页的 parent 引用重定向到代表；hits 归并、absorbed 留档；aliases 记录。"""
    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("rep", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b"), hits=7),
        _page("dup", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b"), hits=3),
        _page("child", depth=2, parent=("dup", "进子页"),
              rids=("rid:com.x:id/c",)),
    ])
    report = merge_fragments(idx, apply=True)
    assert report["merged_away"] == 1 and report["repointed_parents"] == 1
    pages = {p["page_key"]: p for p in idx["pages"]}
    assert set(pages) == {"root", "rep", "child"}
    assert pages["rep"]["hits"] == 10
    assert pages["rep"]["absorbed"] == [{"page_key": "dup", "j": 1.0}]
    assert pages["child"]["parent_key"] == "rep"               # 引用重定向
    assert idx["aliases"]["dup"]["of"] == "rep"


def test_representative_prefers_stable_over_hits():
    """代表 rank 与 infer_target_page 同款：stable 优先于 hits（已验证路径优先）。"""
    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("busy", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b"), hits=9),
        _page("verified", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b"), hits=1,
              arrival={"status": "stable", "rounds": 3, "passed": 3}),
    ])
    report = merge_fragments(idx, apply=True)
    assert report["groups"][0]["representative"] == "verified"


def test_apply_inherits_parent_when_ancestor_absorbed():
    """祖先碎片被后代代表吸收 → 代表沿原链上移继承父链（防自环）。"""
    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("mid", depth=1, parent=("root", "进列表"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b"), hits=1),
        _page("deep", depth=2, parent=("mid", "进详情"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b", "rid:com.x:id/c"), hits=9),
    ])
    merge_fragments(idx, apply=True)
    pages = {p["page_key"]: p for p in idx["pages"]}
    deep = pages["deep"]
    assert deep["parent_key"] == "root"                        # mid 被并 → 继承 mid 的父
    assert deep["parent_action"] == "进列表"
    assert deep["depth"] == 1                                  # depth 重算
    assert pages["root"]["depth"] == 0


def test_apply_no_deadlock_when_ancestor_chain_absorbed_in_order():
    """回归（tx-news apply 卡死实锤）：祖先链 K←M 都被 A 吸收、pages 按 root,K,M,A 排序。

    旧代码边遍历边就地改写 parent_key：M.parent 先被「抄近道」改写成 A，
    轮到 A 做自环继承上移时读到半新半旧的链 → A→M→A 人造环 while 死循环
    （dry-run 秒回、apply 卡死几分钟的元凶）。修复后读-算-写分离：while 读
    原始链 → A 继承 root 父链、到达动作取最后一个被并页（K）的。
    """
    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("K", depth=1, parent=("root", "root进K"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b")),
        _page("M", depth=2, parent=("K", "K进M"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b")),
        _page("A", depth=3, parent=("M", "M进A"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b", "rid:com.x:id/c"), hits=9),
    ], edges=[{"from": "root", "to": "K", "action": "root进K", "count": 1},
              {"from": "M", "to": "A", "action": "M进A", "count": 1}])
    report = merge_fragments(idx, apply=True)                  # 旧代码此处挂死
    pages = {p["page_key"]: p for p in idx["pages"]}
    assert set(pages) == {"root", "A"}
    a = pages["A"]
    assert a["parent_key"] == "root"
    assert a["parent_action"] == "root进K"                     # 最后一个被并页（K）的到达动作
    assert report["self_parent_inherited"] == 2  # M（待删成员）与 A 的落点都在各自子树内
    assert a["hits"] == 11 and a["depth"] == 1
    assert idx["aliases"]["K"]["of"] == "A" and idx["aliases"]["M"]["of"] == "A"
    assert ("root", "A") in {(e["from"], e["to"]) for e in idx["edges"]}


def test_apply_no_cycle_when_sandwich_page_between_merged_fragments():
    """回归（首轮 apply 后 135 页成环的「夹心页」实锤）：链 root←P←Z←Y←X←R，
    P/Y/X 都被 R 吸收而夹在中间的 Z 不是——Z 的 parent(P) 重定向落进自己
    子树内的 R，旧代码 R 继承上移又恰好落在 Z ⇒ R→Z→R 成环。统一规则后
    各自上移到子树外落点，森林无环。"""
    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("P", depth=1, parent=("root", "root进P"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b", "rid:com.x:id/c")),
        _page("Z", depth=2, parent=("P", "P进Z"), rids=("rid:com.x:id/z",)),
        _page("Y", depth=3, parent=("Z", "Z进Y"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b", "rid:com.x:id/c")),
        _page("X", depth=4, parent=("Y", "Y进X"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b", "rid:com.x:id/c", "rid:com.x:id/d")),
        _page("R", depth=5, parent=("X", "X进R"), hits=9,
              rids=("rid:com.x:id/a", "rid:com.x:id/b", "rid:com.x:id/c",
                    "rid:com.x:id/d", "rid:com.x:id/e")),
    ])
    report = merge_fragments(idx, apply=True)
    pages = {p["page_key"]: p for p in idx["pages"]}
    assert set(pages) == {"root", "Z", "R"}
    assert pages["R"]["parent_key"] == "Z"          # R 继承上移停在夹心页 Z（Y/X 被并）
    assert pages["R"]["parent_action"] == "Z进Y"    # 最后被跳过节点（Y）的到达动作
    assert pages["Z"]["parent_key"] == "root"       # Z 的父 P 被并 → 继承到 root
    assert pages["Z"]["parent_action"] == "root进P"
    assert report["self_parent_inherited"] == 3     # Z/X/R 三处落点都在自身子树内
    assert pages["R"]["hits"] == 12                 # 9+P+Y+X
    assert pages["Z"]["depth"] == 1 and pages["R"]["depth"] == 2
    for p in idx["pages"]:                          # 全图 parent 链无环
        seen, cur = set(), p
        while cur.get("parent_key"):
            assert cur["page_key"] not in seen
            seen.add(cur["page_key"])
            cur = next(q for q in idx["pages"] if q["page_key"] == cur["parent_key"])


def test_apply_breaks_cross_branch_mutual_cycle():
    """跨枝互并回归（tx-news 残留 2 环实锤）：两代表的 parent 互为对方页的碎片
    ——b.parent=pF（f 代表的碎片）、f.parent=pB（b 代表的碎片），两落点都不在
    对方原始子树内（跨枝），②a 子树规则管不到 ⇒ ②b 终图破环：环上自身
    hits 最弱的一侧（b，hits 6 < f 的 10）升根断链，保住强链。"""
    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("pF", depth=1, parent=("root", "root进pF"),
              rids=("rid:com.x:id/f1", "rid:com.x:id/f2")),
        _page("pB", depth=1, parent=("root", "root进pB"),
              rids=("rid:com.x:id/b1", "rid:com.x:id/b2")),
        _page("b", depth=2, parent=("pF", "pF进b"), hits=5,
              rids=("rid:com.x:id/b1", "rid:com.x:id/b2", "rid:com.x:id/b3")),
        _page("f", depth=2, parent=("pB", "pB进f"), hits=9,
              rids=("rid:com.x:id/f1", "rid:com.x:id/f2", "rid:com.x:id/f3")),
    ])
    report = merge_fragments(idx, apply=True)
    pages = {p["page_key"]: p for p in idx["pages"]}
    assert set(pages) == {"root", "b", "f"}
    assert "parent_key" not in pages["b"]           # 弱侧升根
    assert pages["f"]["parent_key"] == "b"          # 强链保留（pB 被并 → 落到 b）
    assert report["cycle_breaks"] == ["b"]
    for p in idx["pages"]:                          # 全图 parent 链无环
        seen, cur = set(), p
        while cur.get("parent_key"):
            assert cur["page_key"] not in seen
            seen.add(cur["page_key"])
            cur = next(q for q in idx["pages"] if q["page_key"] == cur["parent_key"])


def test_apply_rep_becomes_root_when_whole_parent_chain_absorbed():
    """父链整条被吸收（被并页本身是链根）→ 代表升为根，不残留空父字段。"""
    idx = _index([
        _page("m", rids=("rid:com.x:id/a", "rid:com.x:id/b")),          # 根页碎片
        _page("deep", depth=1, parent=("m", "进详情"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b", "rid:com.x:id/c"), hits=9),
    ])
    report = merge_fragments(idx, apply=True)
    pages = {p["page_key"]: p for p in idx["pages"]}
    assert set(pages) == {"deep"}
    assert "parent_key" not in pages["deep"]
    assert "parent_action" not in pages["deep"]     # 升根后不得残留 parent_action=""
    assert pages["deep"]["depth"] == 0
    assert report["self_parent_inherited"] == 0     # 链走到头：升根而非继承


def test_apply_repoints_edges_and_drops_self_loops():
    """edges 端点重定向；重定向后 from==to 的自环边丢弃。"""
    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("rep", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b")),
        _page("dup", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b")),
    ], edges=[
        {"from": "root", "to": "rep", "action": "去页", "count": 2},
        {"from": "root", "to": "dup", "action": "去页", "count": 1},   # → rep（与首条并语义）
        {"from": "rep", "to": "dup", "action": "换形态", "count": 1},   # → 自环，丢
    ])
    merge_fragments(idx, apply=True)
    edges = {(e["from"], e["to"]) for e in idx["edges"]}
    assert ("root", "rep") in edges
    assert ("rep", "rep") not in edges


def test_depth_recomputed_for_whole_forest():
    """重定向后全图 depth 按 parent 链重算（BFS）。"""
    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("rep", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b")),
        _page("dup", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b")),
        _page("g1", depth=2, parent=("rep", "进"), rids=("rid:com.x:id/g",)),
        _page("g2", depth=2, parent=("dup", "进"), rids=("rid:com.x:id/h",)),
    ])
    merge_fragments(idx, apply=True)
    pages = {p["page_key"]: p for p in idx["pages"]}
    assert pages["g1"]["depth"] == 2 and pages["g2"]["depth"] == 2  # parent 都指向 rep


def test_no_candidates_or_empty_groups_is_noop():
    """无 rid / 无 activity 的页不参与；空图谱不炸。"""
    idx = _index([_page("p1", rids=()), _page("p2", activity="", rids=("rid:com.x:id/a",))])
    report = merge_fragments(idx, apply=True)
    assert report["merged_away"] == 0 and len(idx["pages"]) == 2
    assert merge_fragments(_index([]))["merged_away"] == 0


def test_merge_is_deterministic():
    """同一图谱两次合并结果一致（代表选取/成员排序确定性）。"""
    pages = [_page("root", rids=("rid:com.x:id/root",))] + [
        _page(f"f{i:02d}", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b"), hits=(i % 3) + 1)
        for i in range(6)]
    r1 = merge_fragments(_index(json.loads(json.dumps(pages))))
    r2 = merge_fragments(_index(json.loads(json.dumps(pages))))
    assert r1["groups"] == r2["groups"]


def test_exclude_activity_keeps_pages_out_of_merge():
    """排除 WebView 壳类：activity 含子串的页不当候选（不当代表也不被并）。"""
    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("web1", depth=1, parent=("root", "进网页"),
              activity="com.x/.WebAdvertActivity",
              rids=("rid:com.x:id/w", "rid:com.x:id/x")),
        _page("web2", depth=1, parent=("root", "进网页"),
              activity="com.x/.WebAdvertActivity",
              rids=("rid:com.x:id/w", "rid:com.x:id/x")),
    ])
    report = merge_fragments(idx, exclude_activity=("WebAdvert",))
    assert report["merged_away"] == 0            # 两个 WebView 页结构完全同也不并
    # 对照组：不排除时同图谱会并（J=1.0 → 并掉 1 页）
    assert merge_fragments(_index(json.loads(json.dumps(idx["pages"]))))["merged_away"] == 1


def test_excluded_page_parent_reference_still_repoints():
    """被排除页不被合并，但其 parent 指向被并页时仍须重定向（引用不能断）。"""
    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("rep", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b"), hits=7),
        _page("dup", depth=1, parent=("root", "去页"),
              rids=("rid:com.x:id/a", "rid:com.x:id/b"), hits=3),
        _page("web", depth=2, parent=("dup", "网页里进"),
              activity="com.x/.WebAdvertActivity",
              rids=("rid:com.x:id/w",)),
    ])
    report = merge_fragments(idx, apply=True, exclude_activity=("WebAdvert",))
    pages = {p["page_key"]: p for p in idx["pages"]}
    assert set(pages) == {"root", "rep", "web"}
    assert pages["web"]["parent_key"] == "rep"   # 引用跟走，不断链
    assert report["repointed_parents"] == 1


def test_report_serializable(tmp_path):
    """报告 JSON 可落盘（CLI 落审计文件的前置）。"""
    import json

    from insta_engine.fragment_merge import save_report

    idx = _index([
        _page("root", rids=("rid:com.x:id/root",)),
        _page("a", depth=1, parent=("root", "去页"), rids=("rid:com.x:id/a", "rid:com.x:id/b")),
        _page("b", depth=1, parent=("root", "去页"), rids=("rid:com.x:id/a", "rid:com.x:id/b")),
    ])
    report = merge_fragments(idx, apply=True)
    out = save_report(report, tmp_path, "com.x")
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["applied"] is True and loaded["merged_away"] == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
