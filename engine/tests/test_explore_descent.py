"""`max_descent`（连续下钻上限）的确定性验证：**别把预算花在一条深链上**。

## 要回答的问题

内容型 App（某内容社区 App/某短视频 App/某电商 App）的首页是无限信息流，价值几乎全在 **L0/L1 的入口广度**；
而一条笔记的「详情 → 评论 → 回复」对图谱毫无贡献。旧引擎会一路往下钻，把
`--max-depth` 的预算全花在一条链上——**实测某内容社区 App 7 步就钻到 L3 然后 `stuck`**
（`artifacts/explore/042701-691e`：7 步 / 22 页 / 19 页堆在 L3 / `cause=depth-limit`）。

## 判据

同一张图，只改 `max_descent`：

| 页 | 结构 |
|---|---|
| A（L0）| 两个兄弟入口：`去B`、`去B2` |
| B（L1）| `去C` |
| C（L2）| `去D` |
| D（L3）| 无可点控件（深链终点）|

- `max_descent=0`（旧行为）⇒ **会到达 D**；
- `max_descent=2` ⇒ **不到达 D**，并且回头点 A 上的第二个兄弟入口 `去B2`，
  `descent_capped > 0`。

两条一起才构成结论：不是"少点了"，而是**把深度预算换成了广度**。
"""

from __future__ import annotations

from pathlib import Path

from insta_engine.explore import explore

#: 每个页面的可点入口：页名 → [(文案, bounds, 目标页)]
EDGES = {
    "A": [("去B", "[0,0][10,10]", "B"), ("去B2", "[20,20][30,30]", "B2")],
    "B": [("去C", "[0,0][10,10]", "C")],
    "C": [("去D", "[0,0][10,10]", "D")],
    "B2": [],
    "D": [],
}


def _xml(page: str) -> str:
    nodes = [f'<node class="TextView" text="{page}页" resource-id="com.x:id/title_{page}"'
             f' clickable="false" bounds="[0,100][100,120]" package="com.x"/>']
    for label, bounds, _target in EDGES[page]:
        nodes.append(f'<node class="Button" text="{label}" '
                     f'resource-id="com.x:id/btn_{page}_{label}" clickable="true" '
                     f'bounds="{bounds}" package="com.x"/>')
    return "".join(nodes)


class ChainDriver:
    """A/B/C/D 链 + A 上的第二个兄弟入口 B2。"""

    serial = None
    raw = None

    def __init__(self) -> None:
        self.stack = ["A"]
        self.clicks: list[str] = []

    def foreground_app(self) -> dict:
        return {"package": "com.x", "activity": ".A", "pid": 1}

    def dump(self) -> str:
        return _xml(self.stack[-1])

    def click(self, x: int, y: int) -> None:
        page = self.stack[-1]
        for label, bounds, target in EDGES[page]:
            x1, y1, x2, y2 = (int(g) for g in bounds.strip("[]").replace("][", ",").split(","))
            if x1 <= x <= x2 and y1 <= y <= y2:
                self.clicks.append(label)
                self.stack.append(target)
                return

    def press(self, key: str) -> None:
        if key == "back" and len(self.stack) > 1:
            self.stack.pop()

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        self.stack = ["A"]


def _run(tmp_path: Path, tag: str, max_descent: int) -> tuple[dict, ChainDriver]:
    drv = ChainDriver()
    # ⚠️ `breadth=0`：这两条测的是 **`max_descent` 这一个开关**，必须把另一个开关按住不动。
    # 广度优先（2026-10-23 起默认开）会在"祖先页还有未试入口"时根本不往下钻 ⇒
    # 不按住的话，这里测到的就不是 `max_descent` 的行为，而是两个策略的叠加。
    report = explore(drv, "com.x", minutes=0.05, max_steps=12,
                     artifacts_dir=tmp_path / tag, max_depth=4,
                     sleep_scale=0.01, max_descent=max_descent, breadth=0)
    return report, drv


def test_old_behaviour_reaches_the_deep_chain(tmp_path: Path) -> None:
    """`max_descent=0` = 旧行为：一路下钻到 L3 的 D 页（这条是对照臂）。"""
    report, drv = _run(tmp_path, "off", 0)
    depths = {p["page_key"]: p["depth"] for p in report["pages"]}
    assert 3 in depths.values(), f"旧行为应当钻到 L3，实际深度分布 {sorted(depths.values())}"
    assert report["descent_capped"] == 0, report["descent_capped"]


def test_descent_cap_trades_depth_for_breadth(tmp_path: Path) -> None:
    """`max_descent=2`：不到达 D，改为回头点 A 的第二个兄弟入口。"""
    report, drv = _run(tmp_path, "cap", 2)
    depths = sorted(p["depth"] for p in report["pages"])
    assert max(depths) <= 2, f"连续下钻应被截在 L2，实际深度分布 {depths}"
    assert report["descent_capped"] > 0, "应当至少触发一次主动回退"
    assert report["max_descent"] == 2
    # 广度：A 上的第二个入口必须被点到（这才是"省下来的预算"的去处）
    assert "去B2" in drv.clicks, f"没回头点兄弟入口，实际点击序列 {drv.clicks}"
