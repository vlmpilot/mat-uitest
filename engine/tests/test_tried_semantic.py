"""`tried` 带语义（2026-10-22）：**别用屏幕坐标当"这个分支是什么"**。

## 背景（真机实测）

`tried` 原来只记 `(page_key, bounds)`。信息流/列表页的卡片按坐标重排，同一坐标每次是
不同内容 ⇒ 旧条目会**误跳新入口**（静默漏页）。实测：某内容社区 App 54 条 `tried` 里 28 条
bounds 已失效、26 条"坐标还在但可能已换内容"；某资讯 App 78 条里 8 条失效。

## 现在的判据

`tried` 条目 = `<bounds>\\t<label>\\t<rid>`，匹配要求 **bounds 相同 且 label/rid 至少一个相同**：

| 情形 | 结果 | 为什么 |
|---|---|---|
| 同坐标、同文案（rid 抖动）| **跳过** | RN/混淆包的 rid 每帧变，靠文案认 |
| 同坐标、文案变了、rid 也变 | **不跳过** | 这是另一个控件（就是原来漏页的情形）|
| 同坐标、只有 rid 稳定（图标入口）| **跳过** | 靠 rid 认 |
| 老格式条目（只有坐标）| **丢弃且不跳过** | 不可验证 ⇒ 宁可重探一次，也不误跳 |

最后一条是**有意的行为变更**：旧图谱里的 `tried` 会作废一次（`legacy_tried_dropped` 计数），
重跑一轮后恢复成语义条目。用一次重探换"不再静默漏页"。
"""

from __future__ import annotations

import json
from pathlib import Path

from insta_engine.page_index import (
    digest_page,
    identities_from_controls,
    is_legacy_tried,
    tried_entry,
    tried_matches,
)

BOUNDS = "[0,0][10,10]"


# -- 纯函数：判据矩阵 ---------------------------------------------------------------

def test_same_bounds_and_label_matches_even_when_rid_churns():
    old = {"bounds": BOUNDS, "label": "去B", "rid": "com.x:id/churn5"}
    now = {"bounds": BOUNDS, "label": "去B", "rid": "com.x:id/churn9"}
    assert tried_matches(tried_entry(old), now), "rid 抖动但文案相同 ⇒ 仍是同一分支"


def test_same_bounds_but_different_control_does_not_match():
    """**这条就是漏页的根因**：坐标撞了、但那是另一个控件。"""
    old = {"bounds": BOUNDS, "label": "笔记A", "rid": "com.x:id/0_resource_name_obfuscated"}
    now = {"bounds": BOUNDS, "label": "笔记B", "rid": "com.x:id/0_resource_name_obfuscated"}
    assert not tried_matches(tried_entry(old), now), (
        "同坐标、不同文案、rid 又都是混淆占位 ⇒ 不能当成已试过（否则新入口被静默跳过）")


def test_icon_entry_matches_by_rid_when_label_is_empty():
    old = {"bounds": BOUNDS, "label": "", "rid": "com.x:id/scan"}
    now = {"bounds": BOUNDS, "label": "", "rid": "com.x:id/scan"}
    assert tried_matches(tried_entry(old), now)


def test_legacy_entry_is_not_trusted():
    """老格式（只有坐标）⇒ 不匹配，且能被识别出来以便丢弃计数。"""
    now = {"bounds": BOUNDS, "label": "去B", "rid": "com.x:id/x"}
    assert is_legacy_tried(BOUNDS)
    assert not tried_matches(BOUNDS, now)


# -- 端到端：老格式图谱 + 撞坐标 -------------------------------------------

class _OnePage:
    """单页两个按钮，**坐标完全相同**（模拟"撞坐标"），文案不同。"""

    serial = None
    raw = None

    def __init__(self, labels: tuple[str, ...]) -> None:
        self.labels = labels
        self.clicks: list[str] = []

    def foreground_app(self) -> dict:
        return {"package": "com.x", "activity": ".Main", "pid": 1}

    def dump(self) -> str:
        return "".join(
            f'<node class="Button" text="{t}" resource-id="com.x:id/b{i}" '
            f'clickable="true" bounds="{BOUNDS}" package="com.x"/>'
            for i, t in enumerate(self.labels)
        )

    def click(self, x: int, y: int) -> None:
        self.clicks.append(f"{x},{y}")

    def press(self, key: str) -> None:
        return None

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        return None


def _seed_index(path: Path, xml: str, tried: list[str]) -> None:
    """手写一份图谱：单页，identities 与真机帧一致，`tried` 由调用方指定。"""
    digest = digest_page(xml, "com.x")
    ids = sorted(identities_from_controls(digest["controls"]))
    path.write_text(json.dumps({
        "schema": 2, "package": "com.x",
        "generated_at": "2026-10-01T00:00:00+00:00", "updated_at": "2026-10-01T00:00:00+00:00",
        "sources": ["explore"], "edges": [],
        "pages": [{"page_key": "seedpage", "depth": 0, "parent_key": None,
                   "parent_action": None, "activity": ".Main", "identities": ids,
                   "tried": tried, "hits": 1, **digest}],
    }, ensure_ascii=False), encoding="utf-8")


def test_legacy_tried_is_dropped_and_the_branch_is_retried(tmp_path: Path) -> None:
    """老图谱的裸坐标条目 ⇒ 丢弃（计数），并且**该候选会被重新点**。"""
    from insta_engine.explore import explore

    drv = _OnePage(("去B",))
    index = tmp_path / "com.x.json"
    _seed_index(index, drv.dump(), [BOUNDS])          # 老格式

    report = explore(drv, "com.x", minutes=0.02, max_steps=3,
                     artifacts_dir=tmp_path / "art", max_depth=2,
                     sleep_scale=0.01, index_path=index)

    assert report["legacy_tried_dropped"] == 1, report["legacy_tried_dropped"]
    assert drv.clicks, "老格式条目不可验证 ⇒ 必须重新点，不能当成已试过"


def test_semantic_tried_skips_only_the_same_control(tmp_path: Path) -> None:
    """撞坐标的两个控件：只跳过**真正试过的那个**，另一个照点。"""
    from insta_engine.explore import explore

    drv = _OnePage(("去B", "去C"))                     # 两个控件同坐标、不同文案
    index = tmp_path / "com.x.json"
    _seed_index(index, drv.dump(), [tried_entry({"bounds": BOUNDS, "label": "去B",
                                                 "rid": "com.x:id/b0"})])

    report = explore(drv, "com.x", minutes=0.02, max_steps=3,
                     artifacts_dir=tmp_path / "art", max_depth=2,
                     sleep_scale=0.01, index_path=index)

    # `skipped_by_history` 是**按迭代**累加的（同一页停留多帧会重复计数，见 explore 注释），
    # 所以这里只断言"确有跳过"；真正要锁的是**去C 被点过**——看图谱里的 tried。
    assert report["skipped_by_history"] >= 1, report["skipped_by_history"]
    pages = json.loads(index.read_text())["pages"]
    clicked = [e.split("\t")[1] for p in pages for e in (p.get("tried") or [])]
    assert "去C" in clicked, f"同坐标的「去C」必须被点（不该连坐），实际点过 {clicked}"
    # ⚠️ 不能用"图谱里没有去B"来判"没重复点"——去B 的条目是**种进去的**（合并是并集）。
    # 只能数真实点击次数：去B 被跳过、去C 点一次 ⇒ 恰好 1 次。
    assert len(drv.clicks) == 1, f"只该点「去C」一次，实际 {len(drv.clicks)} 次"


def test_merge_drops_legacy_entries_so_the_graph_self_heals(tmp_path: Path) -> None:
    """合并时丢掉老格式条目：否则每轮都会"丢弃 54 条"一次，噪声永远不消失。

    合并是唯一同时看得到「旧条目 + 新观测」的地方，让新观测覆盖旧格式是这里的责任。
    """
    from insta_engine.page_index import merge_pages

    legacy, semantic = BOUNDS, tried_entry({"bounds": BOUNDS, "label": "去B", "rid": "com.x:id/b"})
    index = {"package": "com.x", "pages": [{
        "page_key": "p", "depth": 0, "parent_key": None, "parent_action": None,
        "controls": [{"text": "去B", "bounds": BOUNDS}], "identities": ["text:去B"],
        "tried": [legacy], "hits": 1}]}
    merge_pages(index, [{
        "page_key": "p", "depth": 0, "parent_key": None, "parent_action": None,
        "controls": [{"text": "去B", "bounds": BOUNDS}], "identities": ["text:去B"],
        "tried": [semantic], "hits": 1}])
    assert index["pages"][0]["tried"] == [semantic], index["pages"][0]["tried"]


# -- 数字归一化（2026-10-22 实测后补）------------------------------------------------
#
# 真机上"同一个控件"的文案常带**会变的数字**：`已收藏10004` / `消息，1条未读` / `5475`。
# 拿原文案比 ⇒ 永远跳不过（每次重探，白烧预算）；"含数字就丢掉" ⇒ 退化成只比坐标，
# 把刚修好的误跳又放回来。正确做法是**把数字段归一化成 `#` 再比**。

def test_changing_count_still_matches():
    old = {"bounds": BOUNDS, "label": "已收藏10004", "rid": ""}
    now = {"bounds": BOUNDS, "label": "已收藏10005", "rid": ""}
    assert tried_matches(tried_entry(old), now), "计数变了还是同一个控件"


def test_same_title_with_different_count_matches():
    old = {"bounds": BOUNDS, "label": "笔记 169人在评论区晒图", "rid": ""}
    now = {"bounds": BOUNDS, "label": "笔记 892人在评论区晒图", "rid": ""}
    assert tried_matches(tried_entry(old), now)


def test_different_content_with_same_shape_does_not_match():
    """反向：**内容真的不同**时不能因为"结构一样"就当成同一控件（否则又漏页）。"""
    old = {"bounds": BOUNDS, "label": "笔记 169人在评论区晒图", "rid": ""}
    now = {"bounds": BOUNDS, "label": "笔记 我在草原上看到日落", "rid": ""}
    assert not tried_matches(tried_entry(old), now)


def test_legacy_raw_label_in_store_is_normalised_on_read():
    """存量条目里存的是**未归一化**的原文案，读的时候也要归一化（否则升级后全跳不过）。"""
    now = {"bounds": BOUNDS, "label": "已收藏10005", "rid": ""}
    assert tried_matches(f"{BOUNDS}\t已收藏10004\t", now)


def test_shadow_counters_expose_the_gap(tmp_path: Path) -> None:
    """影子统计：同一批候选上"纯坐标"会跳多少、"语义"会跳多少。

    为什么需要它（2026-10-23 实测教训）：两臂**独立跑**会被路径分叉污染——同参数跑
    语义/纯坐标两轮，两臂发现的新页**交集为 0**，噪声比信号大。影子统计在一轮里
    同时记两个数，才可比。
    """
    from insta_engine.explore import explore

    drv = _OnePage(("去B", "去C"))                 # 两个控件同坐标、不同文案
    index = tmp_path / "com.x.json"
    # 种一条**语义**条目：指向"去B"。纯坐标判据会把"去C"也跳掉，语义不会。
    _seed_index(index, drv.dump(), [tried_entry({"bounds": BOUNDS, "label": "去B",
                                                 "rid": "com.x:id/b0"})])
    report = explore(drv, "com.x", minutes=0.02, max_steps=3,
                     artifacts_dir=tmp_path / "art", max_depth=2,
                     sleep_scale=0.01, index_path=index)

    assert report["tried_match"] == "semantic"
    assert report["shadow_skip_bounds"] >= report["shadow_skip_semantic"], (
        f"纯坐标判据不可能比语义判据跳得少（实际 bounds={report['shadow_skip_bounds']} "
        f"semantic={report['shadow_skip_semantic']}）")
    assert report["shadow_skip_bounds"] > report["shadow_skip_semantic"], (
        "该夹具里「去C」与「去B」同坐标但不同文案：纯坐标会把它也跳掉，语义不该跳")
