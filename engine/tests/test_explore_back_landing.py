"""back 落点**认回本轮已知页**（2026-10-22）：内容型 App 的回退不该造新页。

## 背景（某内容社区 App真机实测）

首页 → 笔记详情 → 返回。返回后信息流**内容轮换了**（另一批笔记），于是：

- `page_key`（精确指纹）必然变；
- 页内去重只与**上一帧**比（那时还在详情页）⇒ 判不出"这是首页"；
- 于是落点被当成新页，深度继承 back 前的层数 ⇒ 落在 `max_depth` 上 ⇒ 立刻"触顶"再回退。

实测后果：一轮 7 步、26 页里 **16 页**是 `depth=3 + parent_action=__system_back`，
12 次回退后 `stuck`，8 分钟预算只用了 2 分钟。

## 判据（真机标定，不是拍的）

| 对比 | 包含度 |
|---|---|
| 回退落回首页的 15 个帧 vs 首页 | **0.654 ~ 0.75**（中位 0.708）|
| 首页 vs **任何非首页** | ≤ **0.083** |

⇒ 阈值取 0.5，两边都留足余量（`explore.BACK_LANDING_MATCH`）。
"""

from __future__ import annotations

from pathlib import Path

from insta_engine.page_index import BACK_HOP, load_index

#: 首页的稳定骨架（导航栏 + 页签）——真机上这部分不变
#: 导航栏**不可点**（只提供稳定信号）：让候选只剩卡片，夹具才走得到 back 分支
CHROME = "".join(
    f'<node class="TextView" text="{t}" resource-id="com.x:id/nav_{i}" '
    f'clickable="false" bounds="[0,{2000 + i * 40}][200,{2030 + i * 40}]" package="com.x"/>'
    for i, t in enumerate(("首页", "市集", "发布", "消息", "我"))
)


def _feed(items: tuple[str, ...]) -> str:
    """信息流卡片：内容会轮换，但**不是**动态文本（长度/格式都像正常标题）。"""
    cards = "".join(
        f'<node class="TextView" text="{t}" resource-id="com.x:id/card_{i}" '
        f'clickable="true" bounds="[0,{i * 300}][1000,{i * 300 + 280}]" package="com.x"/>'
        for i, t in enumerate(items)
    )
    return CHROME + cards


DETAIL = ('<node class="TextView" text="笔记正文" resource-id="com.x:id/body" '
          'clickable="false" bounds="[0,0][1000,100]" package="com.x"/>'
          '<node class="Button" text="关注" resource-id="com.x:id/follow" '
          'clickable="true" bounds="[900,0][1000,80]" package="com.x"/>')


class FeedDriver:
    """首页（内容会轮换）→ 点第一条卡片进详情 → back 回首页（**另一批内容**）。"""

    serial = None
    raw = None
    # ⚠️ 卡片文案里**不能有敏感词**（`_SENSITIVE` 含「分享/发布/订单/评价」…），
    # 否则候选会被过滤掉，夹具根本走不到 back 分支（踩过）。
    HOME_A = _feed(("初秋穿搭记录",))
    HOME_B = _feed(("今日份咖啡",))

    def __init__(self) -> None:
        self.page = "home"
        self.feed = self.HOME_A
        self.backs = 0

    def foreground_app(self) -> dict:
        return {"package": "com.x", "activity": ".Main", "pid": 1}

    def dump(self) -> str:
        return DETAIL if self.page == "detail" else self.feed

    def click(self, x: int, y: int) -> None:
        if self.page == "home" and y < 300:      # 第一条卡片
            self.page = "detail"

    def press(self, key: str) -> None:
        if key == "back" and self.page == "detail":
            self.page = "home"
            self.feed = self.HOME_B             # 回退后内容轮换（真实行为）
            self.backs += 1

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        self.page, self.feed = "home", self.HOME_A


def _run(tmp_path: Path) -> tuple[dict, FeedDriver]:
    from insta_engine.explore import explore

    drv = FeedDriver()
    index_path = tmp_path / "com.x.json"
    explore(drv, "com.x", minutes=0.05, max_steps=8, artifacts_dir=tmp_path / "art",
            max_depth=3, sleep_scale=0.01, index_path=index_path)
    return load_index(index_path), drv


def test_back_landing_is_recognised_as_the_known_page(tmp_path: Path) -> None:
    """回退落回首页（内容轮换过）⇒ 必须**认回**首页，不许新建 `BACK_HOP` 页。"""
    graph, drv = _run(tmp_path)
    pages = [p for p in graph["pages"] if isinstance(p, dict)]
    assert drv.backs >= 1, "夹具没走到 back 分支"
    hops = [p for p in pages if p.get("parent_action") == BACK_HOP]
    assert hops == [], (
        f"回退落点被当成了新页（{len(hops)} 个 BACK_HOP 页）——"
        "包含度认回没生效，真机上这会导致深度爆炸 + stuck")
    # 首页仍是唯一 L0，且没有被"回退链"改深度
    roots = [p for p in pages if p.get("depth") == 0]
    assert len(roots) == 1, [p.get("depth") for p in pages]


def test_containment_of_rotated_feed_still_matches(tmp_path: Path) -> None:
    """夹具自检：轮换后的首页与轮换前的包含度必须 **≥0.5**（否则测的是别的东西）。"""
    from insta_engine.page_index import identities_from_controls
    from insta_engine.ui_tree import parse_nodes

    def ids(xml: str) -> frozenset[str]:
        controls = [{"res-id": n.rid, "text": n.text, "desc": n.desc,
                     "class": n.cls, "clickable": n.clickable}
                    for n in parse_nodes(xml) if n.text or n.desc or n.rid]
        return identities_from_controls(controls)

    a, b = ids(FeedDriver.HOME_A), ids(FeedDriver.HOME_B)
    score = len(a & b) / max(len(a), len(b))
    assert score >= 0.5, f"夹具的两版首页太不像（{score:.2f}），测不出认回这件事"
