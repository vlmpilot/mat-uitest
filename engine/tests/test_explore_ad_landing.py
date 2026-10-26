"""广告落地页**不登记为页面**（2026-10-23 实测 tx-news 后补）。

## 背景

信息流 App 里点一条广告，前台**仍是被测 App 自己**（activity 是它的
`WebAdvertActivityV2`）⇒ 既有的"离场检测"（按前台 package 判）抓不到它，
它会作为普通页面被登记，并且继续往里钻。

实测 tx-news：53 页里 **15 条**是广告落地页（商品详情页 / 淘宝精选），
其中一条还挂在首页的父链上：

    真首页 → 点[fakemetenney] → 广告页 → 点[《隐私权政策》] → 广告页
      → 到达=None(又是首页) → 点[广告] → 广告页 → __system_back__ → 又是首页

后果不是"图谱大一点"，而是 **`graph-repair` 借到了「点 天猫双11热卖中」这种链**——
一条**看起来完整、实际不可重放**的链。断链会被 skip，假链会**真去点**。

## 判据

`page_index.is_ad_landing()` 只看 activity（新闻标题含「广告」很常见，按文案判会误杀）。
explore 里：撞到广告页 ⇒ 记一条 `ad-landing` finding、**不登记页面、不往里钻**、直接回退。
"""

from __future__ import annotations

from pathlib import Path

AD_ACT = ".tad.business.ui.activity.WebAdvertActivityV2"

HOME = (
    '<node class="TextView" text="首页" resource-id="com.x:id/tab_home" '
    'clickable="false" bounds="[0,2000][200,2030]" package="com.x"/>'
    # ⚠️ 入口 rid **不能带 ad/promotion 这类词**：那会被"广告位过滤"提前拦掉，
    # 于是根本点不进广告页、测不到"广告落地页不登记"这条（踩过）。
    '<node class="TextView" text="限时特惠" resource-id="com.x:id/feed_entry" '
    'clickable="true" bounds="[0,0][1000,280]" package="com.x"/>'
    '<node class="TextView" text="去B" resource-id="com.x:id/to_b" '
    'clickable="true" bounds="[0,300][1000,560]" package="com.x"/>'
)

AD_PAGE = (
    '<node class="TextView" text="商品详情页" resource-id="com.x:id/ad_title" '
    'clickable="false" bounds="[0,0][1000,100]" package="com.x"/>'
    '<node class="Button" text="立即购买" resource-id="com.x:id/ad_buy" '
    'clickable="true" bounds="[0,120][500,200]" package="com.x"/>'
)

B_PAGE = (
    '<node class="TextView" text="B 页面" resource-id="com.x:id/b_title" '
    'clickable="false" bounds="[0,0][1000,100]" package="com.x"/>'
)


class AdFeedDriver:
    """首页 → 点广告进广告页 → back 回首页 → 点「去B」到 B 页。"""

    serial = None
    raw = None

    def __init__(self) -> None:
        self.page = "home"
        self.ad_visits = 0
        self.backs_from_ad = 0
        self.clicked: list[tuple[int, int]] = []

    def foreground_app(self) -> dict:
        activity = AD_ACT if self.page == "ad" else ".ui.MainActivity"
        return {"package": "com.x", "activity": activity, "pid": 1}

    def dump(self) -> str:
        return {"home": HOME, "ad": AD_PAGE, "b": B_PAGE}[self.page]

    def click(self, x: int, y: int) -> None:
        self.clicked.append((x, y))
        if self.page != "home":
            return
        if y < 300:                     # 广告卡片
            self.page = "ad"
            self.ad_visits += 1
        elif y < 600:                   # 去B
            self.page = "b"

    def press(self, key: str) -> None:
        if key == "back" and self.page == "ad":
            self.page = "home"
            self.backs_from_ad += 1

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        self.page = "home"


def _run(tmp_path: Path) -> tuple[dict, dict, AdFeedDriver]:
    from insta_engine.explore import explore
    from insta_engine.page_index import load_index

    drv = AdFeedDriver()
    index_path = tmp_path / "com.x.json"
    report = explore(drv, "com.x", minutes=0.05, max_steps=10,
                     artifacts_dir=tmp_path / "art", max_depth=3,
                     sleep_scale=0.01, index_path=index_path)
    return report, load_index(index_path), drv


def test_ad_landing_is_not_recorded_as_a_page(tmp_path: Path) -> None:
    """广告页**不进图谱**——否则父链里会出现「点某条广告」这种不可重放的跳。"""
    report, graph, drv = _run(tmp_path)
    assert drv.ad_visits >= 1, "夹具没走到广告页"

    pages = [p for p in graph["pages"] if isinstance(p, dict)]
    ads = [p for p in pages if "advert" in str(p.get("activity") or "").lower()]
    assert ads == [], f"广告落地页被登记成页面了：{[p.get('page_key') for p in ads]}"
    # 报告要如实计数（否则用户看不出"为什么这轮少了几页"）
    assert report.get("ad_landings", 0) >= 1, report.get("ad_landings")


def test_ad_landing_is_reported_as_a_finding(tmp_path: Path) -> None:
    """广告是正常现象，但要**说出来**——它是"离场类"观察，不该静默丢弃。"""
    report, _graph, _drv = _run(tmp_path)
    kinds = [f.get("type") for f in report.get("findings") or []]
    assert "ad-landing" in kinds, kinds


def test_exploration_continues_after_leaving_the_ad(tmp_path: Path) -> None:
    """回退之后必须**继续探**——不能把广告页当成终点卡在那儿。"""
    _report, graph, drv = _run(tmp_path)
    assert drv.backs_from_ad >= 1, "撞到广告页后没有回退"
    pages = [p for p in graph["pages"] if isinstance(p, dict)]
    # 首页之外至少还探到过一页（B），证明回退后还在工作
    assert len(pages) >= 2, [(p.get("depth"), p.get("page_key")) for p in pages]
    assert any("去B" in str(c.get("text") or "")
               for p in pages for c in (p.get("controls") or [])), \
        "首页的候选没被重新枚举（回退后应继续试别的入口）"


def test_mat_index_crawl_also_skips_ad_landing(tmp_path: Path, monkeypatch) -> None:
    """`mat index` 的轻量爬取**同样**不登记广告页——两个生产入口都得拦。

    为什么这条必须单独测：图谱的 `sources` 是 `["index","explore"]`，实测那 15 条
    广告页正是**两个入口都会产出**。只修 explore 等于没修（同"闸门只有一处实现"）。
    """
    from insta_engine import page_index

    monkeypatch.setattr(page_index.time, "sleep", lambda *_: None)

    drv = AdFeedDriver()
    graph = page_index.crawl(drv, "com.x", max_steps=6, max_pages=5)

    ads = [p for p in graph["pages"] if "advert" in str(p.get("activity") or "").lower()]
    assert ads == [], f"crawl 把广告落地页登记进图谱了：{[p.get('page_key') for p in ads]}"
    assert graph["pages"], "首页本身必须被登记（不能连真页一起丢）"
    assert drv.ad_visits >= 1 and drv.backs_from_ad >= 1, "夹具没走到「进广告 → 回退」"
