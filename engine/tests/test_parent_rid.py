"""parent_rid 双通道链测试：icon-only 控件（无文案有 rid）的记录与重放。

背景（2026-10-16 真机四轮验证，第 4 层）：tx-news 新鲜页 25/34 的 parent_action
是私有区 iconfont 字符或空串——重放按文案匹配必死（真机 0/25）。修复：explore
把点击控件的 resource-id 随父链记为 parent_rid，重放时按 rid → 文案双通道匹配。
"""

from __future__ import annotations

from insta_engine.arrivals import _chain_of, _find_clickable, _walk_chain
from insta_engine.explore import _candidate
from insta_engine.graph_repair import repair
from insta_engine.page_index import (
    _dedup_entries,
    _merge_page,
    identities_from_controls,
    page_key,
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


# -- explore：候选带 rid、死链排除 ----------------------------------------------------------------


def test_candidate_carries_rid_and_drops_dead_controls():
    xml = (
        '<node class="Button" text="搜索" resource-id="com.x:id/search" '
        'clickable="true" bounds="[0,0][10,10]" package="com.x"/>'
        '<node class="ImageView" resource-id="com.x:id/iv_avatar" clickable="true" '
        'bounds="[0,10][20,20]" package="com.x"/>'  # icon-only 有 rid：保留
        '<node class="ImageView" clickable="true" '
        'bounds="[0,20][30,30]" package="com.x"/>'  # 无文案无 rid：注定死链
    )
    out = _candidate(xml, PKG)
    assert {"bounds": "[0,0][10,10]", "label": "搜索",
            "rid": "com.x:id/search", "toggle": False} in out
    assert {"bounds": "[0,10][20,20]", "label": "",
            "rid": "com.x:id/iv_avatar", "toggle": False} in out
    assert len(out) == 2  # 死链没进来，不浪费探索步数


# -- _chain_of：rid 通道可成链 --------------------------------------------------------------------


def test_chain_of_accepts_rid_only_hop():
    target = {"page_key": "t", "parent_key": "root", "parent_action": "",
              "parent_rid": "com.x:id/iv_enter"}
    root = {"page_key": "root", "parent_key": None}
    chain = _chain_of(target, {"t": target, "root": root})
    assert chain == [{"expected_key": "t", "action": "",
                      "rid": "com.x:id/iv_enter"}]


def test_chain_of_rejects_when_both_channels_empty():
    target = {"page_key": "t", "parent_key": "root", "parent_action": None,
              "parent_rid": None}
    root = {"page_key": "root", "parent_key": None}
    assert _chain_of(target, {"t": target, "root": root}) is None


# -- _find_clickable：rid → 文案 双通道匹配 --------------------------------------------------------


_XML_DUP_LABEL = (
    '<node class="Button" text="更多" resource-id="com.x:id/menu_a" clickable="true" '
    'bounds="[0,0][10,10]" package="com.x"/>'
    '<node class="Button" text="更多" resource-id="com.x:id/menu_b" clickable="true" '
    'bounds="[0,10][20,20]" package="com.x"/>'
)


def test_find_clickable_rid_wins_over_duplicate_labels():
    node = _find_clickable(_XML_DUP_LABEL, PKG, "更多", "com.x:id/menu_b")
    assert node.rid == "com.x:id/menu_b"  # 同文案两条控件，rid 精确锁定


def test_find_clickable_icon_only_by_rid():
    xml = ('<node class="ImageView" resource-id="com.x:id/iv_enter" clickable="true" '
           'bounds="[0,0][10,10]" package="com.x"/>')
    node = _find_clickable(xml, PKG, "", "com.x:id/iv_enter")
    assert node is not None and node.rid == "com.x:id/iv_enter"
    assert _find_clickable(xml, PKG, "", "com.x:id/missing") is None


def test_find_clickable_falls_back_to_label_when_rid_stale():
    xml = ('<node class="Button" text="搜索" resource-id="com.x:id/new_name" '
           'clickable="true" bounds="[0,0][10,10]" package="com.x"/>')
    node = _find_clickable(xml, PKG, "搜索", "com.x:id/old_name")
    assert node is not None and node.text == "搜索"  # rid 改名 → 文案兜底


# -- _walk_chain：icon 跳真机重放语义（桩驱动）------------------------------------------------------


class _IconDriver:
    """A --(icon: com.x:id/iv_enter)--> B。"""

    serial = None
    XML_A = ('<node class="ImageView" resource-id="com.x:id/iv_enter" clickable="true" '
             'bounds="[0,0][10,10]" package="com.x"/>'
             '<node class="TextView" text="A页" package="com.x"/>')
    XML_B = '<node class="TextView" text="B页" package="com.x"/>'

    def __init__(self):
        self.stack = ["A"]

    def dump(self):
        return {"A": self.XML_A, "B": self.XML_B}[self.stack[-1]]

    def click(self, x, y):
        if self.stack[-1] == "A" and x == 5:
            self.stack.append("B")

    def app_start(self, package, activity=None):
        self.stack = ["A"]

    def foreground_app(self):
        return {"package": PKG}


def test_walk_chain_replays_icon_hop():
    key_b = page_key(_IconDriver.XML_B, PKG)
    chain = [{"expected_key": key_b, "action": "", "rid": "com.x:id/iv_enter"}]
    ok, reason = _walk_chain(_IconDriver(), PKG, chain, 0.01, {"clicks": 0})
    assert ok is True, reason


def test_walk_chain_missing_rid_reports_resource_id():
    key_b = page_key(_IconDriver.XML_B, PKG)
    chain = [{"expected_key": key_b, "action": "", "rid": "com.x:id/gone"}]
    ok, reason = _walk_chain(_IconDriver(), PKG, chain, 0.01, {"clicks": 0})
    assert ok is False and "资源id「com.x:id/gone」" in reason


# -- 合并止血：rid 通道参与「可重放」判据 -----------------------------------------------------------


def test_merge_page_shallow_icon_arrival_replaces_deeper_labeled_chain():
    old = {"page_key": "k", "depth": 2, "parent_key": "p1", "parent_action": "点我",
           "hits": 1}
    new = {"page_key": "k", "depth": 1, "parent_key": "p0", "parent_action": "",
           "parent_rid": "com.x:id/iv", "hits": 1}
    merged = _merge_page(old, new)
    assert merged["depth"] == 1 and merged["parent_rid"] == "com.x:id/iv"


def test_merge_page_back_arrival_never_wipes_icon_chain():
    """更浅 back/直达（两通道皆空）⇒ 不可重放链不得抹掉 icon 链（止血对 rid 同样生效）。"""
    old = {"page_key": "k", "depth": 2, "parent_key": "p1", "parent_action": "",
           "parent_rid": "com.x:id/iv", "hits": 1}
    new = {"page_key": "k", "depth": 1, "parent_key": "p0", "parent_action": None,
           "hits": 1}
    merged = _merge_page(old, new)
    assert merged["depth"] == 2 and merged["parent_rid"] == "com.x:id/iv"


def test_merge_page_label_chain_replaces_icon_chain_and_clears_rid():
    old = {"page_key": "k", "depth": 2, "parent_key": "p1", "parent_action": "",
           "parent_rid": "com.x:id/iv", "hits": 1}
    new = {"page_key": "k", "depth": 1, "parent_key": "p0", "parent_action": "新",
           "hits": 1}
    merged = _merge_page(old, new)
    assert merged["parent_action"] == "新" and not merged.get("parent_rid")


# -- alt_entries 去重带 rid 通道 ------------------------------------------------------------------


def test_dedup_entries_keeps_rid_channel():
    entries = [
        {"parent_key": "root", "parent_action": "更多"},
        {"parent_key": "root", "parent_action": "", "parent_rid": "com.x:id/menu_a"},
        {"parent_key": "root", "parent_action": "", "parent_rid": "com.x:id/menu_b"},
        {"parent_key": "root", "parent_action": "", "parent_rid": "com.x:id/menu_a"},
    ]
    out = _dedup_entries(entries)
    assert len(out) == 3
    assert out[1]["parent_rid"] == "com.x:id/menu_a"
    assert "parent_rid" not in out[0]  # 旧形态条目不带空 rid 字段


# -- graph_repair：两个修复器都要把 rid 带上 --------------------------------------------------------


def test_repair_alt_entry_with_rid_fixes_icon_chain():
    pages = [
        _page("root", depth=0),
        _page("broken", depth=2, parent_key="ghost", parent_action=None,
              alt_entries=[{"parent_key": "root", "parent_action": "",
                            "parent_rid": "com.x:id/iv_enter"}]),
    ]
    _idx, report = repair(_index(pages))
    assert report["fixed_by_alt"] == 1
    assert pages[1]["parent_rid"] == "com.x:id/iv_enter"


def test_repair_alias_borrows_icon_chain():
    """链完整的兄弟碎片是 icon 链（action 空、rid 有）⇒ 借链必须连 rid 一起借。"""
    shared = ["text:播放", "rid:btn_play"]
    pages = [
        _page("root", depth=0),
        _page("whole", depth=1, parent_key="root", parent_action="",
              parent_rid="com.x:id/iv_podcast",
              activity=f"{PKG}/.S", identities=shared,
              controls=[{"text": "播放", "res-id": "btn_play"}]),
        _page("frag", depth=2, parent_key="ghost", parent_action=None,
              activity=f"{PKG}/.S", identities=shared + ["text:多一条"]),
    ]
    _idx, report = repair(_index(pages))
    assert report["fixed_by_alias"] == 1
    frag = pages[2]
    assert frag["parent_rid"] == "com.x:id/iv_podcast"
    by_key = {p["page_key"]: p for p in pages}
    chain = _chain_of(frag, by_key)
    assert chain is not None and chain[0]["rid"] == "com.x:id/iv_podcast"


# -- iconfont 死链：同深度升级 + 修复语义 ----------------------------------------------------------


def test_is_pua_label():
    from insta_engine.page_index import is_pua_label

    assert is_pua_label("\ue90f") is True            # iconfont 单字符
    assert is_pua_label(" \ue917 ") is True          # 带空白
    assert is_pua_label("") is False                 # 空串不算（另一条死链路径）
    assert is_pua_label("播放") is False
    assert is_pua_label("IP属地：云南 更多") is False  # 混合文案仍有语义


def test_merge_page_same_depth_upgrades_pua_chain_to_rid_chain():
    """同深度：旧链动作是 iconfont 字符（重放必死）⇒ 新 rid 观测替换，深度不变。"""
    old = {"page_key": "k", "depth": 2, "parent_key": "p1", "parent_action": "\ue917",
           "hits": 1}
    new = {"page_key": "k", "depth": 2, "parent_key": "p0", "parent_action": "",
           "parent_rid": "com.x:id/share", "hits": 1}
    merged = _merge_page(old, new)
    assert merged["depth"] == 2
    assert merged["parent_key"] == "p0" and merged["parent_rid"] == "com.x:id/share"


def test_merge_page_same_depth_text_chain_not_churned():
    """同深度：旧链是可重放文案 ⇒ 不被 rid 观测冲掉（升级只救死链，不做等深翻转）。"""
    old = {"page_key": "k", "depth": 2, "parent_key": "p1", "parent_action": "已关注",
           "hits": 1}
    new = {"page_key": "k", "depth": 2, "parent_key": "p0", "parent_action": "",
           "parent_rid": "com.x:id/om_focus", "hits": 1}
    merged = _merge_page(old, new)
    assert merged["parent_key"] == "p1" and merged["parent_action"] == "已关注"


def test_repair_treats_pua_chain_as_broken():
    """iconfont 链结构完整但语义死 ⇒ 视为断链，可被干净碎片借链救活。"""
    shared = ["text:播放", "rid:btn_play"]
    pages = [
        _page("root", depth=0),
        _page("whole", depth=1, parent_key="root", parent_action="进",
              activity=f"{PKG}/.S", identities=shared,
              controls=[{"text": "播放", "res-id": "btn_play"}]),
        _page("iconpage", depth=1, parent_key="root", parent_action="\ue90f",
              activity=f"{PKG}/.S", identities=shared + ["text:多一条"]),
    ]
    _idx, report = repair(_index(pages))
    assert report["broken"] == 1 and report["fixed_by_alias"] == 1
    assert pages[2]["parent_action"] == "进" and not pages[2].get("parent_rid")


def test_repair_alt_with_pua_action_rejected():
    """alt 条目动作是 iconfont 字符且无 rid ⇒ 不可用（宁可不动，不可记死链）。"""
    pages = [
        _page("root", depth=0),
        _page("mid", depth=1, parent_key="root", parent_action="进mid"),
        _page("broken", depth=2, parent_key="ghost", parent_action=None,
              alt_entries=[{"parent_key": "root", "parent_action": "\ue917"}]),
    ]
    _idx, report = repair(_index(pages))
    assert report["fixed_by_alt"] == 0


# -- 瞬态启动帧（splash「跳过」根）判据 ------------------------------------------------------------


def test_is_splash_skip_label_exact_match():
    from insta_engine.safety import is_splash_skip_label

    assert is_splash_skip_label("跳过") is True
    assert is_splash_skip_label(" 跳过广告 ") is True
    assert is_splash_skip_label("Skip") is True
    assert is_splash_skip_label("跳过训练营") is False   # 全等判据，不误伤内容
    assert is_splash_skip_label("") is False
    assert is_splash_skip_label(None) is False


def test_is_transient_frame():
    from insta_engine.page_index import is_transient_frame

    assert is_transient_frame({"controls": [{"text": "跳过"}]}) is True
    assert is_transient_frame({"controls": [{"desc": "跳过"}]}) is True  # desc 同算
    assert is_transient_frame({"controls": [{"text": "跳过广告"}]}) is True
    assert is_transient_frame({"controls": [{"text": "首页"}]}) is False
    assert is_transient_frame({"controls": []}) is False
    assert is_transient_frame({}) is False


def test_root_reachable():
    from insta_engine.page_index import root_reachable

    # 图谱没记录 launch_activity ⇒ 不表态（旧图谱零行为变化："不知道" ≠ "不可达"）
    assert root_reachable({"activity": f"{PKG}/.X"}, "") is True
    assert root_reachable({"activity": f"{PKG}/.X"}, None) is True
    # 根无 activity ⇒ 同样不表态
    assert root_reachable({"activity": ""}, f"{PKG}/.S") is True
    assert root_reachable({}, f"{PKG}/.S") is True
    # 全等 ⇒ 可达；不等 ⇒ 不可达（遗留页根，实测：tx-news 播客详情页根 245 链全灭）
    assert root_reachable({"activity": f"{PKG}/.S"}, f"{PKG}/.S") is True
    assert root_reachable({"activity": f"{PKG}/.Legacy"}, f"{PKG}/.S") is False


def test_hop_replayable_content_label():
    """内容型长文案（无 rid）判不可重放：随内容轮换，重放必挂。

    tx-news 2026-10-17 真机：8 条内容文案链 24 轮 0 通过，失败文案
    「MORIN 法式撞色条纹开叉上衣皮衣半裙套装女秋2」（25 字）、「俄对华递交
    布恰事件报告?中方回应」（15 字）、「较真榜，可选中」（7 字）全 ≥7。
    """
    from insta_engine.page_index import BACK_HOP, hop_replayable

    # 有 rid：文案再长也走资源 id 通道（文案轮换无所谓）
    assert hop_replayable("MORIN 法式撞色条纹开叉上衣皮衣半裙套装女秋2",
                          f"{PKG}:id/t") is True
    # 无 rid 长文案（真实失败样本）：内容型 → 不可重放
    assert hop_replayable("俄对华递交布恰事件报告?中方回应", "") is False
    assert hop_replayable("较真榜，可选中", "") is False
    # 无 rid 短文案（tab/菜单等稳定语义）：保留可重放通道
    assert hop_replayable("我的", "") is True
    assert hop_replayable("播放器", "") is True
    # BACK_HOP 哨兵（15 字符）是确定性系统动作：显式豁免，不按内容文案判
    assert hop_replayable(BACK_HOP, "") is True
    # 空值 / 纯 iconfont 维持原判
    assert hop_replayable("", "") is False
    assert hop_replayable("\ue90f", "") is False
