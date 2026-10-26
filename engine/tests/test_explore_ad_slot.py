"""广告位不点 + 离场后学习（2026-10-23 实测 tx-news）。

## 现象

广度优先那轮 `left_app = 18`（跳到 `com.huawei.appmarket`），有一轮甚至只走 6 步就被
`blocked` 打断。每次离场要 2 帧拉回（`back` 常常无效，再 `app_start`），
按 56 步 / 480 秒算，**离场吃掉了约 20% 的帧预算**。

## 证据（不是猜的）

首页 21 条 `tried` 里，两条的 rid 是 `com.tencent.news:id/txt_streamAd_title`
—— **广告位有稳定的 rid 命名**。所以判据按 rid，不按文案（"秒杀"/"好价"会误杀真新闻标题）。

## 两道防线

1. **静态**：`safety.is_ad_slot_rid()` 按 rid 命名拦（窄判据，宁可漏几个）；
2. **动态**：离场时把"刚点的那个入口的 rid"拉黑，本轮不再点——兜住命名不规范的广告位。
"""

from __future__ import annotations

from pathlib import Path

HOME = (
    '<node class="TextView" text="首页" resource-id="com.x:id/tab_home" '
    'clickable="false" bounds="[0,2000][200,2030]" package="com.x"/>'
    # 广告位：rid 带 streamAd（实测 tx-news 就是这个命名）
    '<node class="TextView" text="设计师款夹克" resource-id="com.x:id/txt_streamAd_title" '
    'clickable="true" bounds="[0,0][1000,200]" package="com.x"/>'
    '<node class="TextView" text="一条真新闻" resource-id="com.x:id/title_text" '
    'clickable="true" bounds="[0,220][1000,400]" package="com.x"/>'
    # 命名不规范、点了会离场的入口（靠"学习"兜住）
    '<node class="TextView" text="推荐看看" resource-id="com.x:id/feed_card" '
    'clickable="true" bounds="[0,420][1000,600]" package="com.x"/>'
)

DETAIL = ('<node class="TextView" text="正文" resource-id="com.x:id/body" '
          'clickable="false" bounds="[0,0][1000,100]" package="com.x"/>')


class AdSlotDriver:
    """点 `streamAd` 那条 → 广告页；点 `feed_card` → **跳到别的 App**。"""

    serial = None
    raw = None

    def __init__(self) -> None:
        self.page = "home"
        self.left = 0

    def foreground_app(self) -> dict:
        if self.page == "other":
            return {"package": "com.huawei.appmarket", "activity": ".MarketActivity", "pid": 9}
        return {"package": "com.x", "activity": ".Main", "pid": 1}

    def dump(self) -> str:
        return {"home": HOME, "detail": DETAIL, "ad": DETAIL, "other": DETAIL}[self.page]

    def click(self, x: int, y: int) -> None:
        if self.page != "home":
            return
        if y < 200:
            self.page = "ad"
        elif y < 400:
            self.page = "detail"
        elif y < 600:
            self.page = "other"
            self.left += 1

    def press(self, key: str) -> None:
        if key == "back" and self.page == "other":
            self.page = "home"           # back 有效（最省）

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        self.page = "home"


def _run(tmp_path: Path, steps: int = 10) -> tuple[dict, dict, AdSlotDriver]:
    from insta_engine.explore import explore
    from insta_engine.page_index import load_index

    drv = AdSlotDriver()
    index_path = tmp_path / "com.x.json"
    report = explore(drv, "com.x", minutes=0.05, max_steps=steps,
                     artifacts_dir=tmp_path / "art", max_depth=3,
                     sleep_scale=0.01, index_path=index_path, breadth=1)
    return report, load_index(index_path), drv


def test_ad_slot_rid_is_narrow() -> None:
    """判据只看 rid 且要窄：`streamAd`/`ad_title` 算广告位，普通 rid 不算。"""
    from insta_engine.safety import is_ad_slot_rid

    for rid in ("com.tencent.news:id/txt_streamAd_title",
                "com.x:id/ad_title", "com.x:id/banner_ad", "com.x:id/advert_view"):
        assert is_ad_slot_rid(rid), rid
    # 反向：真页面/真入口不能被误杀
    for rid in ("com.tencent.news:id/title_text", "com.x:id/nav_btn_text",
                "com.x:id/search_edit_text", "com.x:id/feed_card", ""):
        assert not is_ad_slot_rid(rid), rid


def test_ad_slot_is_never_clicked(tmp_path: Path) -> None:
    """广告位候选**一次都不点**，而且这件事要在报告里可见。"""
    report, graph, drv = _run(tmp_path)
    pages = [p for p in graph["pages"] if isinstance(p, dict)]
    clicked = [e for p in pages for e in (p.get("tried") or [])]
    assert not any("streamAd" in e for e in clicked), f"广告位被点了：{clicked}"
    assert report.get("ad_slots_skipped", 0) >= 1, report.get("ad_slots_skipped")
    # 反向：真入口照样点（不能因为过滤把整页点空）
    assert any("title_text" in e for e in clicked), clicked


def test_leaving_the_app_blacklists_that_entry(tmp_path: Path) -> None:
    """离场后**学习**：把"刚点的那个入口"的 rid 拉黑，本轮不再点。"""
    report, _graph, drv = _run(tmp_path, steps=12)
    assert drv.left >= 1, "夹具没造出离场"
    assert "com.x:id/feed_card" in (report.get("ad_rid_blacklist") or []), (
        f"离场后没学到该拉黑的 rid：{report.get('ad_rid_blacklist')}")
