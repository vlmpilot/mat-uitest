"""目标导向探索（`--goal`）：把有限预算花在"往目标页走"上。

为什么需要（2026-10-21 实测）：某生活服务 App常规探索一轮 61 步就被风控终止，而深层入口
（地址/设置/订单）排在候选队尾，启发式根本走不到 ⇒ 图谱里**没有「我的地址」页**。
后果是连锁的：用例的 `s2` 点地址栏之后，走链接不上 ⇒ R20/页面一致性闸门全部弃权，
执行期置备也报「父链不完整」。**图谱缺页是静态检查与置备的共同天花板。**
"""

from __future__ import annotations

from insta_engine import explore


def _cand(label: str, rid: str = "", bounds: str = "[0,0][10,10]") -> dict:
    return {"label": label, "rid": rid, "bounds": bounds}


def test_pick_prefers_goal_matching_entry():
    """命中目标词的入口即使其它加权更低，也应被选中。"""
    cands = [
        _cand("外卖", "com.x:id/tab_takeout"),      # rid +2、文案 +3 = 5
        _cand("怡景天鹅湖，点击可选择地址"),          # 只有文案 +3
    ]
    assert explore._pick(cands, set(), 0, {}, {})["label"] == "外卖", "无目标时按原加权"
    picked = explore._pick(cands, set(), 0, {}, {}, None, goal_tokens=["地址"])
    assert picked["label"].startswith("怡景"), picked


def test_pick_without_goal_is_unchanged():
    """`goal_tokens=None` 必须与改动前逐字一致（对照组语义不能漂）。"""
    cands = [_cand("A", "com.x:id/a"), _cand("B")]
    assert explore._pick(cands, set(), 0, {}, {}) == explore._pick(
        cands, set(), 0, {}, {}, None, goal_tokens=None)


def test_pick_goal_ignores_non_matching():
    """目标词不命中任何候选时，退回原加权（不因目标而乱点）。"""
    cands = [_cand("外卖", "com.x:id/tab_takeout"), _cand("我的")]
    picked = explore._pick(cands, set(), 0, {}, {}, None, goal_tokens=["不存在的词"])
    assert picked["label"] == "外卖", picked
