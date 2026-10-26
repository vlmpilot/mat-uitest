"""广度优先：**每页最多点 N 个入口**，然后去"最浅的、还有配额的页"（2026-10-23）。

## 背景（tx-news 真机，75 步建图）

整轮 25 页里 **24 页是同一篇文章的周边**（评论/回复/字体/分享/畅听/作者主页），
首页 85 个可点控件**只试了 4 个**，`L1` 只有 1 页。

`--max-descent 2` 确实触发过 6 次，但它触发后只退**一层**，退完立刻又往下钻：

    L1 → L2(cap) → back 到 L1 → 点另一个 → L2 → 点 → L3(cap) → back 到 L2 → … → L4

⇒ **"限制连续下钻" ≠ "保证广度"**：预算是被"在同一棵子树里反复换分支"吃掉的。

## 判据

`breadth=1`（默认）：**优先去"最浅的、还有未试入口的页"**；`breadth=0` 是旧行为（对照臂）。

⚠️ 试过并**放弃**的方案：给每页加"最多点 N 个入口"的配额。它和广度**互相打架**——
配额用尽后栈里就找不到"可去的更浅页"，遍历又退化成深度优先（夹具里同一页仍被点 3 次）。
"""

from __future__ import annotations

from pathlib import Path

#: 首页 5 个入口；**每个子页也各有 6 个入口** —— 这一点很关键：
#: 真实 App 的一页有 30+ 个可点控件，所以深度优先**不会**因为"候选耗尽"被迫回头，
#: 而是一路钻下去（实测 tx-news 就是 24/25 页挂在同一篇文章下）。
#: 夹具若只给 2 个入口，DFS 很快就耗尽了、自动回到首页 ⇒ **测不出病**（踩过）。
HOME_ENTRIES = ("去A", "去B", "去C", "去D", "去E")
CHILD_ENTRIES = 6


def _page_xml(title: str, entries: tuple[str, ...]) -> str:
    """一页 = 一个标题（页面指纹）+ 若干可点入口。

    ⚠️ **每页的 res-id 必须不同**：`identities_from_controls` 优先用 res-id 当页面信号，
    所有页共用 `com.x:id/e0` 的话，包含度恒为 1.0 ⇒ 引擎会把所有页**并成一页**
    （实测踩过：跑完 `page_count=1`，测试全红但根因在夹具）。
    """
    slug = title.replace("页面", "p")
    head = (f'<node class="TextView" text="{title}" resource-id="com.x:id/title_{slug}" '
            f'clickable="false" bounds="[0,1500][1000,1600]" package="com.x"/>')
    items = "".join(
        f'<node class="TextView" text="{t}" resource-id="com.x:id/{slug}_e{i}" '
        f'clickable="true" bounds="[0,{i * 200}][1000,{i * 200 + 150}]" package="com.x"/>'
        for i, t in enumerate(entries)
    )
    return head + items


class WideDriver:
    """首页 3 个入口，每个入口后面又是 2 个入口 —— 结构无限可下钻。"""

    serial = None
    raw = None

    def __init__(self) -> None:
        self.path: list[str] = ["home"]
        self.backs = 0

    # -- 页面 -----------------------------------------------------------------
    @property
    def page(self) -> str:
        return self.path[-1]

    def _entries(self, page: str) -> tuple[str, ...]:
        if page == "home":
            return HOME_ENTRIES
        return tuple(f"{page}-{i}" for i in range(CHILD_ENTRIES))

    def dump(self) -> str:
        return _page_xml(f"页面{self.page}", self._entries(self.page))

    def foreground_app(self) -> dict:
        return {"package": "com.x", "activity": ".Main", "pid": 1}

    # -- 动作 -----------------------------------------------------------------
    def click(self, x: int, y: int) -> None:
        entries = self._entries(self.page)
        idx = y // 200
        if idx >= len(entries):
            return
        label = entries[idx]
        self.path.append(label[1:] if label.startswith("去") else label)

    def press(self, key: str) -> None:
        if key == "back" and len(self.path) > 1:
            self.path.pop()
            self.backs += 1

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        self.path = ["home"]


def _run(tmp_path: Path, tag: str, breadth: int, steps: int = 26) -> tuple[dict, dict, WideDriver]:
    from insta_engine.explore import explore
    from insta_engine.page_index import load_index

    drv = WideDriver()
    index_path = tmp_path / f"{tag}.json"
    report = explore(drv, "com.x", minutes=0.05, max_steps=steps,
                     artifacts_dir=tmp_path / f"art-{tag}", max_depth=5,
                     sleep_scale=0.01, index_path=index_path, breadth=breadth)
    return report, load_index(index_path), drv


def test_breadth_spreads_over_home_entries(tmp_path: Path) -> None:
    """广度优先 ⇒ 首页的多个入口都会被点到，而不是一头钻进第一个入口。"""
    report, graph, drv = _run(tmp_path, "wide", breadth=1)
    pages = [p for p in graph["pages"] if isinstance(p, dict)]
    l1 = [p for p in pages if p.get("depth") == 1]
    assert len(l1) >= 2, (
        f"只有 {len(l1)} 个 L1 页——广度优先没生效（应当至少试 2 个首页入口），"
        f"路径 {drv.path}")
    assert report["breadth"] is True
    assert report["breadth_widens"] >= 1, "一次都没因广度主动回退，策略没被触发"


def test_breadth_off_reproduces_the_old_narrow_behaviour(tmp_path: Path) -> None:
    """对照臂：`breadth=0` 恢复旧行为——钻进第一个入口就不再回头（实测的病）。"""
    report, graph, drv = _run(tmp_path, "narrow", breadth=0, steps=10)
    pages = [p for p in graph["pages"] if isinstance(p, dict)]
    l1 = [p for p in pages if p.get("depth") == 1]
    assert len(l1) == 1, f"对照臂不该铺开（实际 {len(l1)} 个 L1 页）"
    assert report["breadth_widens"] == 0 and report["breadth"] is False
    # 而且会一路往下钻（深度 ≥3 的页存在）
    assert any((p.get("depth") or 0) >= 3 for p in pages), \
        [(p.get("depth"), p.get("page_key")) for p in pages]


def test_breadth_does_not_drill_before_home_is_exhausted(tmp_path: Path) -> None:
    """广度模式下，**首页入口没试完之前不该出现 L2**——这正是"广度"的可观测定义。"""
    _report, graph, _drv = _run(tmp_path, "order", breadth=1, steps=4)
    pages = [p for p in graph["pages"] if isinstance(p, dict)]
    l1 = [p for p in pages if p.get("depth") == 1]
    l2 = [p for p in pages if p.get("depth") == 2]
    # 不断言精确条数：`_pick` 是按启发式挑入口（不是顺序挑），4 步里可能有一次点击
    # 落在"没导航"的入口上。要锁的是**顺序性质**：首页还有入口 ⇒ 不下钻。
    assert len(l1) >= 3, f"4 步里应当点出多个 L1 页，实际 {len(l1)}"
    assert l2 == [], f"首页还有没试的入口就下钻了（L2={len(l2)}）——广度优先没生效"


def test_breadth_still_descends_once_home_is_exhausted(tmp_path: Path) -> None:
    """反向：广度优先**不是"永远不下钻"**——首页入口试完后照样往深处走。

    没有这条的话，一个"永远停在 L1"的实现也能让上面几条测试全绿。
    """
    _report, graph, _drv = _run(tmp_path, "deep", breadth=1, steps=40)
    pages = [p for p in graph["pages"] if isinstance(p, dict)]
    assert len([p for p in pages if p.get("depth") == 1]) >= 5, "首页 5 个入口应当都试过"
    assert any((p.get("depth") or 0) >= 2 for p in pages), \
        f"首页试完后应当继续下钻，实际深度 {[p.get('depth') for p in pages]}"
