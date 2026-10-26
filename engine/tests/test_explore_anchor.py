"""候选生成认可「可点容器 + 不可点文字」的入口（`clickable_anchor`）。

为什么（2026-10-21 真机实测）：某资讯类 App 的 7 个频道 tab 都是 `clickable=false` 的 TextView
（`res-id=channelbar_text`），点击事件在父容器上。旧 `_candidate` 要求**节点自己** clickable
⇒ 这些入口**永远进不了候选** ⇒ 探索只能在信息流与广告里打转
（实测首页快照 81 个控件里没有一个频道 tab，`parent_action` 全是广告落地页），
图谱于是缺页，下游的 R20/R21/闸门与执行期置备同时失效。
"""

from __future__ import annotations

from insta_engine.explore import _candidate
from insta_engine.ui_tree import clickable_anchor, parse_nodes

XML = (
    '<hierarchy>'
    '<node class="android.widget.FrameLayout" clickable="true" bounds="[0,200][1080,320]"'
    ' package="com.x">'
    '<node class="android.widget.TextView" text="要闻" clickable="false"'
    ' resource-id="com.x:id/channelbar_text" bounds="[153,221][291,318]" package="com.x"/>'
    '</node>'
    '<node class="android.widget.TextView" text="无容器包裹" clickable="false"'
    ' bounds="[0,900][300,950]" package="com.x"/>'
    '</hierarchy>'
)


def test_clickable_anchor_finds_container():
    nodes = parse_nodes(XML)
    clickables = [n for n in nodes if n.clickable]
    text_node = next(n for n in nodes if n.text == "要闻")
    anchor = clickable_anchor(text_node, clickables)
    assert anchor is not None and anchor.bounds == "[0,200][1080,320]"


def test_candidate_includes_labeled_non_clickable_entry():
    cands = _candidate(XML, "com.x")
    labels = {c["label"]: c for c in cands}
    assert "要闻" in labels, cands
    assert labels["要闻"]["bounds"] == "[0,200][1080,320]", "落点应是可点容器的中心"
    assert labels["要闻"]["rid"] == "com.x:id/channelbar_text", "rid 取文字节点自己的（更精确）"


def test_candidate_skips_labeled_node_without_clickable_ancestor():
    """没有可点祖先的文字节点仍然不能点（不能凭空造出落点）。"""
    cands = _candidate(XML, "com.x")
    assert all(c["label"] != "无容器包裹" for c in cands), cands
