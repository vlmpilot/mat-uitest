"""图谱断链静态修复测试：alt 提升 / 碎片借链 / 止血 / 碎片等价落点。"""

from __future__ import annotations

from pathlib import Path

from insta_engine.arrivals import _walk_chain
from insta_engine.graph_repair import repair
from insta_engine.page_index import (
    identities_from_controls,
    page_key,
    pages_equivalent,
    save_index,
)

PKG = "com.x"


def _page(key: str, **kw) -> dict:
    base = {"page_key": key, "depth": 1, "parent_key": None, "parent_action": None,
            "activity": f"{PKG}/.A", "identities": [], "controls": [],
            "alt_entries": [], "hits": 1}
    base.update(kw)
    if not base["identities"] and base["controls"]:
        base["identities"] = sorted(identities_from_controls(base["controls"]))
    return base


def _index(pages: list[dict]) -> dict:
    return {"package": PKG, "generated_at": "t", "pages": pages}


# -- repair：两个修复器 -------------------------------------------------------------------------


def test_repair_promotes_alt_entry():
    """断链页的 alt_entries 有可用条目（parent 在图谱 + action 非空）→ 提升为父链。"""
    pages = [
        _page("root", depth=0),
        _page("mid", depth=1, parent_key="root", parent_action="进mid"),
        _page("broken", depth=2, parent_key="mid", parent_action=None,
              alt_entries=[{"parent_key": "root", "parent_action": "直达"}]),
    ]
    index, report = repair(_index(pages))
    p = pages[2]
    assert p["parent_key"] == "root" and p["parent_action"] == "直达"
    assert p["depth"] == 1  # 父深+1
    assert report["fixed_by_alt"] == 1 and report["fixed_by_alias"] == 0
    assert report["changes"][0]["via"] == "alt_entries"


def test_repair_falls_to_alias_when_alt_unusable():
    """alt 条目的 parent 不在图谱 ⇒ 不可用，落到碎片借链。"""
    shared = ["text:搜索", "rid:btn_search"]
    pages = [
        _page("root", depth=0),
        _page("whole", depth=1, parent_key="root", parent_action="进",
              activity=f"{PKG}/.S", identities=shared,
              controls=[{"text": "搜索", "res-id": "btn_search"}]),
        _page("broken", depth=2, parent_key="ghost", parent_action=None,
              activity=f"{PKG}/.S", identities=shared + ["text:extra"],
              alt_entries=[{"parent_key": "ghost", "parent_action": "无"}]),
    ]
    _index(pages)
    _idx, report = repair(_index(pages))
    assert report["fixed_by_alt"] == 0 and report["fixed_by_alias"] == 1
    p = pages[2]
    assert p["parent_key"] == "root" and p["parent_action"] == "进"
    assert p["alias_of"] == "whole" and p["alias_sim"] >= 0.5


def test_repair_borrows_chain_from_similar_fragment():
    """同 activity、J≥0.5、链完整的兄弟碎片 → 借父链 + alias_of 留档。"""
    shared = ["text:播放", "rid:btn_play", "rid:btn_pause"]
    pages = [
        _page("root", depth=0),
        _page("whole", depth=2, parent_key="root", parent_action="进播客",
              activity=f"{PKG}/.Podcast", identities=shared,
              controls=[{"text": "播放", "res-id": "btn_play"}]),
        _page("frag", depth=3, parent_key="ghost", parent_action=None,
              activity=f"{PKG}/.Podcast", identities=shared + ["text:多一条"],
              controls=[{"text": "播放"}]),
    ]
    _idx, report = repair(_index(pages))
    assert report["fixed_by_alias"] == 1
    frag = pages[2]
    assert frag["parent_key"] == "root" and frag["parent_action"] == "进播客"
    assert frag["alias_of"] == "whole"
    # 借链后整链可重放（frag 沿 parent 上溯到 root）
    from insta_engine.arrivals import _chain_of
    by_key = {p["page_key"]: p for p in pages}
    assert _chain_of(frag, by_key) is not None


def test_repair_low_similarity_not_borrowed():
    """同 activity 但 J<0.5（不是同逻辑页）⇒ 不借，记 unresolved。"""
    pages = [
        _page("root", depth=0),
        _page("whole", depth=1, parent_key="root", parent_action="进",
              activity=f"{PKG}/.S", identities=["text:甲", "text:乙", "text:丙", "text:丁"]),
        _page("broken", depth=2, parent_key="ghost", parent_action=None,
              activity=f"{PKG}/.S", identities=["text:甲"]),
    ]
    _idx, report = repair(_index(pages))
    assert report["fixed_by_alias"] == 0
    assert report["unresolved"][0]["page_key"] == "broken"


def test_repair_cross_activity_never_borrowed():
    """J 再高，跨 activity 也**不借**（宁可不动，不可走错页）。"""
    ids = ["text:重合", "text:独有"]
    pages = [
        _page("root", depth=0),
        _page("whole", depth=1, parent_key="root", parent_action="进",
              activity=f"{PKG}/.B", identities=ids),
        _page("broken", depth=2, parent_key="ghost", parent_action=None,
              activity=f"{PKG}/.C", identities=ids),
    ]
    _idx, report = repair(_index(pages))
    assert report["fixed_by_alias"] == 0 and report["fixed_by_alt"] == 0
    assert len(report["unresolved"]) == 1


def test_repair_leaves_whole_pages_untouched():
    """链完整的页不是修复对象，也不出现在 changes。"""
    pages = [
        _page("root", depth=0),
        _page("ok", depth=1, parent_key="root", parent_action="进",
              identities=["text:x"], alt_entries=[{"parent_key": "root", "parent_action": "x"}]),
    ]
    _idx, report = repair(_index(pages))
    assert report["broken"] == 0 and report["changes"] == []


def test_repair_picks_best_candidate():
    """多个可借碎片 → 取 Jaccard 最高者。"""
    ids_frag = ["text:a", "text:b", "text:c", "text:d"]
    pages = [
        _page("root", depth=0),
        _page("low", depth=1, parent_key="root", parent_action="进low",
              activity=f"{PKG}/.S", identities=ids_frag + ["text:e", "text:f"]),
        _page("high", depth=1, parent_key="root", parent_action="进high",
              activity=f"{PKG}/.S", identities=ids_frag + ["text:g"]),
        _page("broken", depth=2, parent_key="ghost", parent_action=None,
              activity=f"{PKG}/.S", identities=ids_frag),
    ]
    # J(low)=4/6, J(high)=4/5 ⇒ high 更高
    _idx, report = repair(_index(pages))
    assert pages[3]["alias_of"] == "high"


def test_repair_treats_back_chain_as_broken():
    """parent_action=「返回」的链**结构完整但语义不可重放** ⇒ 视为断链并修复。

    实测：explore 在首页点「返回」到达的页，arrival 重放 0/3 全死在第 1 跳——
    冷启动后导航起点根本没有「返回」控件。
    """
    pages = [
        _page("root", depth=0),
        _page("backpage", depth=1, parent_key="root", parent_action="返回",
              activity=f"{PKG}/.S", identities=["text:a"]),
    ]
    _idx, report = repair(_index(pages))
    assert report["broken"] == 1  # 回退链按断链处理（无法 alt/alias 修复 ⇒ unresolved）
    assert report["unresolved"][0]["page_key"] == "backpage"


def test_repair_never_borrows_back_chain():
    """借链候选自身链含回退动作 ⇒ 排除（不扩散坏链）。"""
    ids = ["text:a", "text:b", "text:c"]
    pages = [
        _page("root", depth=0),
        _page("backsrc", depth=1, parent_key="root", parent_action="返回",
              activity=f"{PKG}/.S", identities=ids),
        _page("whole", depth=1, parent_key="root", parent_action="进",
              activity=f"{PKG}/.T", identities=ids),  # 跨 activity，不可借
        _page("broken", depth=2, parent_key="ghost", parent_action=None,
              activity=f"{PKG}/.S", identities=ids),
    ]
    _idx, report = repair(_index(pages))
    # 同 activity 只有回退链 backsrc ⇒ 不借，unresolved（backsrc 自己也因回退链进 unresolved）
    assert report["fixed_by_alias"] == 0
    assert {"broken", "backsrc"} == {u["page_key"] for u in report["unresolved"]}


def test_repair_alt_entry_with_back_word_rejected():
    """alt 条目的 action 是回退词 ⇒ 不可用（宁可不动，不可记不可重放的链）。"""
    pages = [
        _page("root", depth=0),
        _page("mid", depth=1, parent_key="root", parent_action="进mid"),
        _page("broken", depth=2, parent_key="mid", parent_action=None,
              alt_entries=[{"parent_key": "root", "parent_action": "关闭"}]),
    ]
    _idx, report = repair(_index(pages))
    assert report["fixed_by_alt"] == 0 and report["broken"] == 1


# -- 止血：更浅覆盖不再抹掉可重放链 --------------------------------------------------------------


def test_merge_page_keeps_replayable_chain():
    """_merge_page：新到达更浅但 parent_action 为空（back/直达）⇒ 保旧链不换。"""
    from insta_engine.page_index import _merge_page

    old = {"page_key": "k", "depth": 2, "parent_key": "p1", "parent_action": "点我",
           "hits": 1}
    new = {"page_key": "k", "depth": 1, "parent_key": "p0", "parent_action": None,
           "hits": 1}
    merged = _merge_page(old, new)
    assert merged["depth"] == 2 and merged["parent_key"] == "p1"
    assert merged["parent_action"] == "点我"  # 旧的可重放链保住了


def test_merge_page_still_allows_shallower_replayable():
    """新到达更浅**且带 action** ⇒ 照常替换（止血不误伤真正的更浅点击链）。"""
    from insta_engine.page_index import _merge_page

    old = {"page_key": "k", "depth": 2, "parent_key": "p1", "parent_action": "旧",
           "hits": 1}
    new = {"page_key": "k", "depth": 1, "parent_key": "p0", "parent_action": "新",
           "hits": 1}
    merged = _merge_page(old, new)
    assert merged["depth"] == 1 and merged["parent_action"] == "新"


def test_merge_page_old_without_action_still_updated():
    """旧记录本来就没 action ⇒ 新链（哪怕也空）照常写入，行为不变。"""
    from insta_engine.page_index import _merge_page

    old = {"page_key": "k", "depth": 2, "parent_key": "p1", "parent_action": None,
           "hits": 1}
    new = {"page_key": "k", "depth": 1, "parent_key": "p0", "parent_action": None,
           "hits": 1}
    merged = _merge_page(old, new)
    assert merged["depth"] == 1 and merged["parent_key"] == "p0"


# -- 碎片等价 ------------------------------------------------------------------------------------


def test_pages_equivalent():
    ok = {"activity": f"{PKG}/.S", "identities": ["a", "b", "c"]}
    frag = {"activity": f"{PKG}/.S", "identities": ["a", "b", "d"]}
    assert pages_equivalent(ok, frag) is True  # J=2/4 ≥ 0.5
    far = {"activity": f"{PKG}/.S", "identities": ["x", "y", "z", "w"]}
    assert pages_equivalent(ok, far) is False  # J=0
    other_act = {"activity": f"{PKG}/.T", "identities": ["a", "b", "c"]}
    assert pages_equivalent(ok, other_act) is False  # 跨 activity 一票否决
    assert pages_equivalent(ok, {"activity": f"{PKG}/.S", "identities": []}) is False
    assert pages_equivalent(ok, {}) is False


# -- 走链落点的碎片等价（验证与置备共用判据）------------------------------------------------------


class _FragDriver:
    """A --「去B」--> B2（B 的兄弟碎片：page_key 不同、identities 高重叠）。"""

    serial = None
    XML_A = '<node class="Button" text="去B" clickable="true" bounds="[0,0][10,10]" package="com.x"/>'
    XML_B = ('<node class="Button" text="去B" clickable="true" bounds="[0,0][10,10]" package="com.x"/>'
             '<node class="TextView" text="B页" package="com.x"/>')
    XML_B2 = ('<node class="Button" text="去B" clickable="true" bounds="[0,0][10,10]" package="com.x"/>'
              '<node class="TextView" text="B页" package="com.x"/>'
              '<node class="TextView" text="广告位" package="com.x"/>')  # 多一个节点 → key 不同

    def __init__(self):
        self.stack = ["A"]

    def dump(self):
        return {"A": self.XML_A, "B": self.XML_B, "B2": self.XML_B2}[self.stack[-1]]

    def click(self, x, y):
        if self.stack[-1] == "A" and x == 5:
            self.stack.append("B2")  # 点击后落到**碎片**而非图谱记的 B

    def app_start(self, package, activity=None):
        self.stack = ["A"]

    def foreground_app(self):
        return {"package": PKG}


def _frag_graph(tmp_path: Path) -> tuple[Path, dict[str, dict]]:
    key_b = page_key(_FragDriver.XML_B, PKG)
    key_b2 = page_key(_FragDriver.XML_B2, PKG)
    assert key_b != key_b2
    pages = [
        _page("root", depth=0, activity=f"{PKG}/.A",
              controls=[{"text": "去B", "res-id": "com.x:id/go_b"}]),
        _page(key_b, depth=1, parent_key="root", parent_action="去B",
              activity=f"{PKG}/.B", controls=[{"text": "B页"}],
              arrival={"status": "stable"}),  # stable ⇒ 推断的代表页是 B 而非碎片
        _page(key_b2, depth=1, parent_key=None, parent_action=None,
              activity=f"{PKG}/.B", controls=[{"text": "B页"}]),
    ]
    # 碎片等价：B2 与 B 的 identities 高重叠（"text:B页" 共有）
    for p in pages:
        p["identities"] = sorted(identities_from_controls(p["controls"]))
    index_path = tmp_path / "com.x.json"
    save_index(index_path, _index(pages))
    return index_path, {p["page_key"]: p for p in pages}


def test_walk_chain_accepts_equivalent_fragment(tmp_path):
    """落点是同逻辑页碎片（等价）⇒ 走链通过；不传图谱时保持精确相等（旧语义）。"""
    _path, pages_by_key = _frag_graph(tmp_path)
    key_b = page_key(_FragDriver.XML_B, PKG)
    chain = [{"expected_key": key_b, "action": "去B"}]

    ok, reason = _walk_chain(_FragDriver(), PKG, chain, 0.01, {"clicks": 0},
                             pages_by_key)
    assert ok is True, reason  # 碎片等价：不再误报落点不符

    ok2, reason2 = _walk_chain(_FragDriver(), PKG, chain, 0.01, {"clicks": 0})
    assert ok2 is False and "落点不符" in reason2  # 旧语义（不传图谱）不变


def test_walk_chain_still_rejects_real_mismatch(tmp_path):
    """落到**真不同页**（跨 activity / identities 不重叠）⇒ 照样判不符。"""
    _path, pages_by_key = _frag_graph(tmp_path)
    key_b = page_key(_FragDriver.XML_B, PKG)
    chain = [{"expected_key": key_b, "action": "去B"}]

    class OtherPage(_FragDriver):
        XML_B2 = ('<node class="TextView" text="完全别的页" package="com.x"/>'
                  '<node class="TextView" text="另一个应用" package="com.x"/>'
                  '<node class="Button" text=" unrelated " clickable="true" bounds="[0,0][9,9]" package="com.x"/>')

    ok, reason = _walk_chain(OtherPage(), PKG, chain, 0.01, {"clicks": 0},
                             pages_by_key)
    assert ok is False and "落点不符" in reason


# -- 置备：已在等价碎片页 ⇒ 无事可做 --------------------------------------------------------------


def test_provision_skips_when_on_equivalent_fragment(tmp_path):
    from insta_engine.arrival_provision import provision
    from insta_engine.dsl import TestCase

    path, _by_key = _frag_graph(tmp_path)
    # 目标页 = B（图谱记的形态）；驱动停在 B2（碎片）——等价 ⇒ 不置备
    steps = [{"id": "s0", "action": "launch"},
             {"id": "s1", "action": "tap",
              "target": {"strategy": "text", "value": "B页"}}]
    case = TestCase.model_validate(
        {"caseId": "c", "name": "n", "target": {"package": PKG}, "steps": steps})

    class OnFrag(_FragDriver):
        def __init__(self):
            super().__init__()
            self.stack = ["B2"]

    ev = provision(case, OnFrag(), PKG, path)
    assert ev["needed"] is False and "已在目标页" in ev["reason"]


# -- 瞬态启动帧根（splash「跳过」）：结构性不可重放 ------------------------------------------------


def test_repair_treats_transient_root_chain_as_broken():
    """瞬态根下**非跳过**入口的链 ⇒ 视为断链（语义完整但真机不可重放）。

    经「跳过」到达的 L1 已被提升为根（splash 是环境噪声，见 test_promote_*），
    这里验证别的入口（广告帧下乱点的产物）仍如实标断链。
    """
    pages = [
        _page("splash", depth=0, controls=[{"text": "跳过"}]),
        _page("child", depth=1, parent_key="splash", parent_action="不感兴趣"),
    ]
    _idx, report = repair(_index(pages))
    assert report["broken"] == 1
    assert report["fixed_by_alt"] == 0 and report["fixed_by_alias"] == 0
    assert report["unresolved"][0]["page_key"] == "child"  # 没有可用修复 → 如实记未解


def test_repair_never_borrows_from_transient_root():
    """瞬态根下的完整链不可作借链 donor——它的可重放性是假的（launch 后根帧已翻页）。"""
    shared = ["text:搜索"]
    pages = [
        _page("splash", depth=0, controls=[{"text": "跳过"}]),
        _page("donor", depth=1, parent_key="splash", parent_action="跳过",
              activity=f"{PKG}/.S", identities=shared),
        _page("broken", depth=1, parent_key="ghost", parent_action=None,
              activity=f"{PKG}/.S", identities=shared),
    ]
    _idx, report = repair(_index(pages))
    assert report["fixed_by_alias"] == 0
    assert pages[2].get("alias_of") is None


def test_repair_alt_pointing_to_transient_root_rejected():
    """alt 提升后的根仍是瞬态帧 ⇒ 不采纳（修完还是不可重放链）。"""
    pages = [
        _page("splash", depth=0, controls=[{"text": "跳过"}]),
        _page("broken", depth=1, parent_key="ghost", parent_action=None,
              alt_entries=[{"parent_key": "splash", "parent_action": "跳过"}]),
    ]
    _idx, report = repair(_index(pages))
    assert report["fixed_by_alt"] == 0
    assert pages[1]["parent_key"] == "ghost"  # 原链未被替换


# -- launch 不可达根（遗留页根）：第二类结构性不可重放 ----------------------------------------------


def test_repair_treats_unreachable_root_chain_as_broken():
    """根 activity ≠ launch 落点 ⇒ 视为断链（遗留页根：launch 重放永远不可达）。

    实测（2026-10-16 真机）：tx-news 245 条链挂遗留播客页根，语义完整可重放，
    真机 0/20 全在根核对处拦下——「语义可重放」不等于「launch 可达」。
    """
    pages = [
        _page("legacy", depth=0, activity=f"{PKG}/.LegacyPage"),
        _page("child", depth=1, parent_key="legacy", parent_action="进入",
              activity=f"{PKG}/.LegacyPage"),
    ]
    idx = _index(pages)
    idx["launch_activity"] = f"{PKG}/.HomeActivity"
    _idx, report = repair(idx)
    assert report["broken"] == 1
    assert report["fixed_by_alt"] == 0 and report["fixed_by_alias"] == 0
    assert report["unresolved"][0]["page_key"] == "child"


def test_repair_never_borrows_from_unreachable_root():
    """launch 不可达根下的完整链不可作借链 donor——换链不换根是假修复。

    教训（2026-10-16 真机）：第一版只排瞬态帧，56 条瞬态根链借到了遗留播客根
    的链上，真机 0/20——修复器必须把可达性纳入 donor 资格。
    """
    shared = ["text:搜索"]
    pages = [
        _page("legacy", depth=0, activity=f"{PKG}/.LegacyPage"),
        _page("donor", depth=1, parent_key="legacy", parent_action="进入",
              activity=f"{PKG}/.S", identities=shared),
        _page("broken", depth=1, parent_key="ghost", parent_action=None,
              activity=f"{PKG}/.S", identities=shared),
    ]
    idx = _index(pages)
    idx["launch_activity"] = f"{PKG}/.HomeActivity"
    _idx, report = repair(idx)
    assert report["fixed_by_alias"] == 0
    assert pages[2].get("alias_of") is None


def test_repair_borrows_from_reachable_root():
    """donor 根 activity = launch 落点 ⇒ 正常借链（判据不误伤可达 donor）。"""
    shared = ["text:搜索"]
    pages = [
        _page("home", depth=0, activity=f"{PKG}/.HomeActivity"),
        _page("donor", depth=1, parent_key="home", parent_action="进入",
              activity=f"{PKG}/.S", identities=shared),
        _page("broken", depth=1, parent_key="ghost", parent_action=None,
              activity=f"{PKG}/.S", identities=shared),
    ]
    idx = _index(pages)
    idx["launch_activity"] = f"{PKG}/.HomeActivity"
    _idx, report = repair(idx)
    assert report["fixed_by_alias"] == 1
    assert pages[2]["alias_of"] == "donor"


def test_repair_alt_pointing_to_unreachable_root_rejected():
    """alt 提升后的根 launch 不可达 ⇒ 不采纳（修完还是不可重放链）。"""
    pages = [
        _page("legacy", depth=0, activity=f"{PKG}/.LegacyPage"),
        _page("broken", depth=1, parent_key="ghost", parent_action=None,
              alt_entries=[{"parent_key": "legacy", "parent_action": "进入"}]),
    ]
    idx = _index(pages)
    idx["launch_activity"] = f"{PKG}/.HomeActivity"
    _idx, report = repair(idx)
    assert report["fixed_by_alt"] == 0
    assert pages[1]["parent_key"] == "ghost"  # 原链未被替换


def test_repair_without_launch_activity_keeps_old_semantics():
    """图谱没记录 launch_activity ⇒ 不表态（旧图谱零行为变化）。"""
    pages = [
        _page("legacy", depth=0, activity=f"{PKG}/.LegacyPage"),
        _page("child", depth=1, parent_key="legacy", parent_action="进入",
              activity=f"{PKG}/.LegacyPage"),
    ]
    _idx, report = repair(_index(pages))   # 无 launch_activity 字段
    assert report["broken"] == 0           # 语义完整链不算断链（不知道 ≠ 不可达）


# -- 瞬态根「跳过」子页提升为根（splash 是环境噪声）------------------------------------------------


def test_promote_splash_skip_child_to_root():
    """冷启动必经开屏帧：经「跳过」到达的 L1 真实首页提升为根，后代 depth-1。"""
    pages = [
        _page("splash", depth=0, controls=[{"text": "跳过"}]),
        _page("home", depth=1, parent_key="splash", parent_action="跳过",
              controls=[{"text": "首页"}]),
        _page("channel", depth=2, parent_key="home", parent_action="进频道"),
    ]
    pages_by_key = {p["page_key"]: p for p in pages}
    from insta_engine.graph_repair import _promote_splash_children
    promoted = _promote_splash_children(pages_by_key)
    assert [x["page_key"] for x in promoted] == ["home"]
    assert pages[1]["depth"] == 0 and pages[1]["parent_key"] is None
    assert pages[2]["depth"] == 1 and pages[2]["parent_key"] == "home"  # 子树上移


def test_promote_rejects_non_skip_entry():
    """瞬态根下经「不感兴趣」等别的动作到达的 L1 不提升（不是有效入口）。"""
    pages = [
        _page("splash", depth=0, controls=[{"text": "跳过"}]),
        _page("junk", depth=1, parent_key="splash", parent_action="不感兴趣"),
    ]
    pages_by_key = {p["page_key"]: p for p in pages}
    from insta_engine.graph_repair import _promote_splash_children
    assert _promote_splash_children(pages_by_key) == []
    assert pages[1]["depth"] == 1 and pages[1]["parent_key"] == "splash"


def test_repair_after_promotion_root_reachable():
    """提升后首页根 launch 可达（activity=launch）⇒ 其下链不再 broken。"""
    pages = [
        _page("splash", depth=0, activity=f"{PKG}/.SplashActivity",
              controls=[{"text": "跳过"}]),
        _page("home", depth=1, parent_key="splash", parent_action="跳过",
              activity=f"{PKG}/.SplashActivity"),
        _page("detail", depth=2, parent_key="home", parent_action="看详情",
              activity=f"{PKG}/.Detail"),
    ]
    idx = _index(pages)
    idx["launch_activity"] = f"{PKG}/.SplashActivity"
    _idx, report = repair(idx)
    assert [x["page_key"] for x in report["promoted"]] == ["home"]
    assert pages[1]["depth"] == 0                 # 首页已是根
    assert pages[2]["depth"] == 1                 # 子页上移
    assert report["broken"] == 0                  # 链根可达，不算断链
    assert len(report["unresolved"]) == 0


def test_promote_rejects_ad_redirect_mismatch():
    """「跳过」点击误触广告跳转（activity ≠ 有效首屏）⇒ 不提升（宁可漏不可假根）。

    实测（2026-10-16 真机）：tx-news explore 在开屏帧点「跳过」落到了
    KkVideoDetailDarkModeActivity——提升它就是造一个 launch 不可达的假根。
    """
    pages = [
        _page("splash", depth=0, activity=f"{PKG}/.SplashActivity",
              controls=[{"text": "跳过"}]),
        _page("ad_land", depth=1, parent_key="splash", parent_action="跳过",
              activity=f"{PKG}/.AdLandingActivity"),
    ]
    pages_by_key = {p["page_key"]: p for p in pages}
    from insta_engine.graph_repair import _promote_splash_children
    got = _promote_splash_children(pages_by_key, f"{PKG}/.SplashActivity")
    assert got == []
    assert pages[1]["parent_key"] == "splash"      # 未提升


# -- 借链途经控件可见性（2026-10-17 批量验证 2/8 unstable：懒加载控件不在首屏）--------------------


def test_alias_skips_donor_with_invisible_hop_control():
    """donor 链上的控件不在源页 controls（懒加载）⇒ 不借（重放必死于未找到）。"""
    pages = [
        _page("root", depth=0, activity=f"{PKG}/.MainActivity",
              controls=[{"text": "首页", "res-id": f"{PKG}:id/home"},
                        {"text": "详情", "res-id": f"{PKG}:id/detail"}]),
        _page("donor", depth=1, parent_key="root", parent_action="详情",
              activity=f"{PKG}/.MainActivity",
              controls=[{"text": "详情", "res-id": f"{PKG}:id/detail"}]),
        _page("lazy", depth=1, parent_key="root", parent_action="懒加载",
              activity=f"{PKG}/.MainActivity",
              controls=[{"text": "懒加载", "res-id": f"{PKG}:id/lazy"}]),
        _page("broken", depth=1, parent_key=None, parent_action=None,
              activity=f"{PKG}/.MainActivity",
              controls=[{"text": "详情", "res-id": f"{PKG}:id/detail"}]),
    ]
    pages[3]["parent_key"] = "root"          # 指向无 parent_action 的根链断
    pages[3]["parent_action"] = None
    idx = _index(pages)
    idx["launch_activity"] = f"{PKG}/.MainActivity"
    _idx, report = repair(idx)
    fixed = {c["page_key"] for c in report["changes"]}
    assert "broken" in fixed                        # 只有 broken 页被修
    # broken 与 donor 同 activity 同 J：唯一可借 donor 是 donor 页
    # donor 链首跳「详情」在根页 controls 里可见 ⇒ 借链成立（对照用例见下）
    assert report["fixed_by_alias"] == 1
    assert pages[3]["parent_action"] == "详情"


def test_alias_rejects_donor_hop_not_in_source_controls():
    """donor 首跳控件不在根页 controls ⇒ 拒绝借链，unresolved（不制造必死链）。"""
    pages = [
        _page("root", depth=0, activity=f"{PKG}/.MainActivity",
              controls=[{"text": "首页", "res-id": f"{PKG}:id/home"}]),
        _page("donor", depth=1, parent_key="root", parent_action="幽灵按钮",
              activity=f"{PKG}/.MainActivity",      # 「幽灵按钮」不在根页 controls
              controls=[{"text": "其他", "res-id": f"{PKG}:id/other"}]),
        _page("broken", depth=1, parent_key="root", parent_action=None,
              activity=f"{PKG}/.MainActivity",
              controls=[{"text": "其他", "res-id": f"{PKG}:id/other"}]),
    ]
    idx = _index(pages)
    idx["launch_activity"] = f"{PKG}/.MainActivity"
    _idx, report = repair(idx)
    assert report["fixed_by_alias"] == 0
    assert pages[2]["parent_action"] is None        # 未借
    assert any("可见" in (u.get("reason") or "") for u in report["unresolved"])


def test_alias_donor_without_controls_not_blocked():
    """donor 页没有 controls（旧图谱）⇒ 无法校验，不误杀（放行）。"""
    pages = [
        _page("root", depth=0, activity=f"{PKG}/.MainActivity", controls=[],
              identities=["sig:root"]),
        _page("donor", depth=1, parent_key="root", parent_action="任意",
              activity=f"{PKG}/.MainActivity", controls=[],
              identities=["sig:root"]),
        _page("broken", depth=1, parent_key="root", parent_action=None,
              activity=f"{PKG}/.MainActivity", controls=[],
              identities=["sig:root"]),
    ]
    idx = _index(pages)
    idx["launch_activity"] = f"{PKG}/.MainActivity"
    _idx, report = repair(idx)
    assert report["fixed_by_alias"] == 1


# -- 2026-10-21 补：扁平图谱要出声 / 无障碍标签形态的「返回」不能当前进导航 -------------------


def test_repair_reports_flat_graph_instead_of_zero_broken():
    """`mat index` 建的扁平图谱（所有页 depth 为空）必须报 flat=True。

    实测背景：tx-news 图谱 `sources=["index"]`、16 页全 `depth=None`，
    旧实现 `targets` 为空 ⇒ `broken=0`，终端显示"断链 0 条"，**读起来像很健康**，
    实际是"没有层级、无从修起"——而真机 10 条用例的前置置备全因"父链不完整"失败。
    """
    pages = [_page(f"p{i}", depth=None, parent_key=None) for i in range(4)]
    _idx, report = repair({"package": PKG, "generated_at": "t",
                           "sources": ["index"], "pages": pages})
    assert report["flat"] is True
    assert report["broken"] == 0, "扁平图谱没有 depth≥1 的页，broken 必然为 0"
    assert report["sources"] == ["index"], "报告要带来源，才能说清为什么修不了"


def test_repair_not_flat_when_graph_has_levels():
    pages = [_page("root", depth=0), _page("a", depth=1, parent_key="root", parent_action="进a")]
    _idx, report = repair(_index(pages))
    assert report["flat"] is False


def test_back_word_recognizes_accessibility_label():
    """「返回 按钮」是 accessibility label 形态的回退动作，不能当前进导航。

    实测背景：某生活服务 App图谱里 24 条父链的到达动作是「返回 按钮」，旧的全等判据认不出
    ⇒ 被 graph-repair 当"完整可重放链"借给别的页，真机重放第 1 跳就点「返回」，
    起点页根本没有这个控件，必然失败（假修复）。
    """
    from insta_engine.safety import is_back_word

    assert is_back_word("返回") is True
    assert is_back_word("返回 按钮") is True, "无障碍标签形态"
    assert is_back_word("返回按钮") is True, "无空格形态"
    assert is_back_word("关闭 按钮") is True
    assert is_back_word("返回首页") is False, "复合词不误伤（这是真实的前进入口）"
    assert is_back_word("二手") is False


def test_repair_does_not_borrow_chain_whose_hop_is_back_label():
    """兄弟碎片的链若靠「返回 按钮」到达，不算可重放，不能借出去。"""
    shared = ["text:列表", "rid:btn_list"]
    pages = [
        _page("root", depth=0),
        _page("back_chain", depth=1, parent_key="root", parent_action="返回 按钮",
              activity=f"{PKG}/.S", identities=shared),
        _page("broken", depth=2, parent_key="ghost", parent_action=None,
              activity=f"{PKG}/.S", identities=shared + ["text:extra"]),
    ]
    _idx, report = repair(_index(pages))
    assert report["fixed_by_alias"] == 0, "回退动作的链不能借"
    assert report["fixed_by_alt"] == 0
    assert pages[2]["parent_key"] == "ghost", "断链应保持原样（宁可修不了，也不假修复）"


# -- 广告落地页与扁平页（2026-10-23 实测 tx-news 后补）------------------------------------------
#
# 53 条记录里 15 条是 `WebAdvertActivityV2`（点信息流广告进去的「商品详情页/淘宝精选」）。
# 它们不是被测 App 的功能页，却会被当成"可途经节点"：实测 4 条借链里 3 条借到了
# 「点 天猫双11热卖中」——**把不可重放的路径标成已修复**。另有 16 页 depth 为空
# （`mat index` 建的页），以前既不在 targets 也不在 donor 池 ⇒ 没人管。

AD_ACT = "com.x/.tad.business.ui.activity.WebAdvertActivityV2"


def test_ad_landing_is_detected_by_activity_only():
    """判据**只看 activity**：新闻标题里带「广告」二字很常见，按文案判会误杀真页面。"""
    from insta_engine.page_index import is_ad_landing

    assert is_ad_landing({"activity": AD_ACT})
    assert is_ad_landing({"activity": "com.x/.ui.AdActivity"})
    # 反向：真页面即便文案含「广告」，也不算广告落地页
    assert not is_ad_landing({"activity": "com.x/.ui.NewsDetailActivity",
                              "controls": [{"text": "广告投放请联系我们"}]})
    assert not is_ad_landing({"activity": ""})


def test_chain_is_broken_when_it_passes_through_an_ad_page():
    """途经广告页的链**不算可重放**：重放时要「再点一次那条广告」，不可复现。"""
    from insta_engine.arrivals import _chain_of

    pages = [
        _page("root", depth=0),
        _page("ad", depth=1, parent_key="root", parent_action="天猫双11热卖中",
              activity=AD_ACT),
        _page("deep", depth=2, parent_key="ad", parent_action="进详情"),
    ]
    by = {p["page_key"]: p for p in pages}
    assert _chain_of(pages[2], by) is None, "穿过广告页的链必须判为断链"
    # 对照：同一页挂在真页下就是完整链（证明不是「只要 depth≥2 就返回 None」）
    pages[2]["parent_key"] = "root"
    assert _chain_of(pages[2], by)


def test_repair_never_borrows_a_chain_from_an_ad_page():
    """广告页不能当 donor——否则借来的链第一跳就是「点某条广告」。"""
    shared = ["text:搜索", "rid:btn_search"]
    pages = [
        _page("root", depth=0),
        _page("ad_donor", depth=1, parent_key="root", parent_action="天猫双11热卖中",
              activity=AD_ACT, identities=shared, controls=[{"text": "搜索"}]),
        _page("broken", depth=1, parent_key="root", parent_action=None,
              identities=shared, controls=[{"text": "搜索"}]),
    ]
    index, report = repair(_index(pages))
    assert report["noise_pages"] == 1
    assert report["fixed_by_alias"] == 0, f"居然从广告页借了链: {report['changes']}"
    assert pages[2].get("alias_of") is None
    assert any(u["page_key"] == "broken" for u in report["unresolved"])


def test_repair_fixes_flat_pages_that_mat_index_created():
    """depth 为空的页（`mat index` 只记页面不记到达）也要修——它们以前**没人管**。

    实测 tx-news：53 页里 16 页 depth 为空，执行期置备对它们只会报「父链不完整」。
    """
    shared = ["text:搜索", "rid:btn_search"]
    pages = [
        _page("root", depth=0),
        _page("twin", depth=1, parent_key="root", parent_action="进搜索",
              identities=shared, controls=[{"text": "搜索"}]),
        _page("flat", depth=None, parent_key=None, parent_action=None,
              identities=shared, controls=[{"text": "搜索"}]),
    ]
    index, report = repair(_index(pages))
    assert report["flat_pages"] == 1 and report["broken_flat"] == 1
    assert pages[2]["alias_of"] == "twin", "扁平页应借到孪生页的链"
    assert pages[2]["depth"] == 1 and pages[2]["parent_action"] == "进搜索"
