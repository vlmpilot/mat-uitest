"""`back` 的**过渡帧**会把源页深度写成 0（2026-10-23 实测 tx-news 后修）。

## 现象

广度优先那一轮建出来的图谱里，7 条 `depth=0` 的记录中 6 条是**不同的频道页**
（7x24 / 国际 / 关注 / 收听…），却都带着 `parent=首页` —— **自相矛盾**：
有父页就不该是 L0。连带 `max_descent` 的连续下钻计数、深度上限判定、`descent_capped`
全部失真（那一轮 `descent_capped=0`，而它本该频繁触发）。

## 机理

`back` 之后 App 往往**还没退完**，这一帧画面仍是"要离开的那一页"，但**内容微轮换**
让 `page_key` 变了（列表项文案变了）。于是它走不到 `key in pages` 那条精确分支，
而是落到**包含度认回**：

    认回源页（包含度 1.0 或 ≥0.5）⇒ 当成"落点" ⇒ depth = src_depth - 1 ⇒ 0

也就是说：**它把"还没走"误判成"已经退回上一层"**。

## 两处修（缺一不可）

1. **落点核对前先问：这一帧是不是还在源页上**（与 `back_from` 的包含度 ≥ 阈值）。
   是 ⇒ 原样返回源页，不做深度/父链更新；且源页自己不能再作为"落点"候选。
   ⇒ 本文件的测试守这一条（修复拿掉会多出一条重复页）。
2. **"认回已知页"那一支绝不改深度**。旧代码 `best_page.depth = min(depth, src_depth - 1)`
   隐含"落点就是父页"，但实测回退会落到**另一个频道页** ⇒ 那一页被写成 `depth=0`
   却仍带 `parent=首页`（自相矛盾），`max_descent` 计数 / 深度上限判定 / `descent_capped`
   全部失真。⚠️ **这一条没有单元测试能复现**（要"回退落到另一个已知页"的驱动行为），
   靠真机验收：改后 tx-news 一轮里 `parent 有值却 depth=0` 的页数 **4 → 0**。
"""

from __future__ import annotations

from pathlib import Path

HOME = ('<node class="TextView" text="推荐" resource-id="com.x:id/tab0" '
        'clickable="true" bounds="[0,0][300,100]" package="com.x"/>'
        '<node class="TextView" text="国际" resource-id="com.x:id/tab1" '
        'clickable="true" bounds="[300,0][600,100]" package="com.x"/>'
        '<node class="TextView" text="首页头条" resource-id="com.x:id/title" '
        'clickable="false" bounds="[0,200][1000,300]" package="com.x"/>')


#: 频道页里**稳定**的列表项（真机上这类条目 rid 固定、文案不变）
_STABLE_ITEMS = ("要闻一", "要闻二", "要闻三", "要闻四")


def _channel(item: str) -> str:
    """频道页：标题 + 4 条稳定条目 + **2 条会轮换的条目**，全部**不可点**；
    另给 1 个可点入口（点了没反应，让候选很快耗尽 ⇒ 走到回退分支）。

    ⚠️ 轮换量必须让重合度落进 **0.5 ~ 0.8** 这个窗口，否则复现不出这个 bug：
      · 重合度 ≥ `PREV_COVERAGE_SIM`(0.8) ⇒ 被"同屏去重"并回上一帧，**根本走不到落点判定**；
      · 重合度 < `BACK_LANDING_MATCH`(0.5) ⇒ 认不出是同一页，只多一条重复页（也是 bug，但不是这个）。
    6 个信号里换掉 2 条 ⇒ 4/6 = **0.67**，正好在窗口内。实测踩过：只换 1 条（0.8）时
    测试永远绿——修复拿掉也不红。
    """
    rows = [item, f"{item}副", *_STABLE_ITEMS]
    body = "".join(
        f'<node class="TextView" text="{t}" clickable="false" '
        f'bounds="[0,{320 + i * 80}][1000,{380 + i * 80}]" package="com.x"/>'
        for i, t in enumerate(rows)
    )
    entry = ('<node class="TextView" text="刷新" resource-id="com.x:id/ch_refresh" '
             'clickable="true" bounds="[900,200][1000,280]" package="com.x"/>')
    return ('<node class="TextView" text="国际频道" resource-id="com.x:id/ch_title" '
            'clickable="false" bounds="[0,200][900,280]" package="com.x"/>' + entry + body)


class RotatingBackDriver:
    """点「国际」进频道页；**第一次 back 被吞掉，且这一帧内容轮换了**。"""

    serial = None
    raw = None

    def __init__(self) -> None:
        self.page = "home"
        self.backs = 0
        self.transition_seen = False

    def foreground_app(self) -> dict:
        return {"package": "com.x", "activity": ".Main", "pid": 1}

    def dump(self) -> str:
        if self.page == "home":
            return HOME
        if self.page == "channel":
            return _channel("频道内一条")
        return _channel("频道内另一条")      # 过渡帧：内容轮换过

    def click(self, x: int, y: int) -> None:
        if self.page == "home" and 300 <= x <= 600 and y < 100:
            self.page = "channel"

    def press(self, key: str) -> None:
        if key != "back" or self.page == "home":
            return
        self.backs += 1
        if self.backs == 1:
            self.page = "channel_rotated"     # 被吞 + 内容轮换（过渡帧）
            self.transition_seen = True
        else:
            self.page = "home"

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        self.page = "home"


def _run(tmp_path: Path, breadth: int = 1) -> tuple[dict, RotatingBackDriver]:
    from insta_engine.explore import explore
    from insta_engine.page_index import load_index

    drv = RotatingBackDriver()
    index_path = tmp_path / "g.json"
    explore(drv, "com.x", minutes=0.05, max_steps=8, artifacts_dir=tmp_path / "art",
            max_depth=3, sleep_scale=0.01, index_path=index_path, breadth=breadth)
    return load_index(index_path), drv


def test_transition_frame_does_not_reset_the_source_page_depth(tmp_path: Path) -> None:
    """被吞掉的 back + 内容轮换 ⇒ 那一帧是**过渡帧**，不许把源页深度写成 0。"""
    graph, drv = _run(tmp_path)
    assert drv.transition_seen, "夹具没造出过渡帧"
    pages = [p for p in graph["pages"] if isinstance(p, dict)]
    channel = [p for p in pages if "ch_title" in str(p.get("controls"))]
    assert channel, [(p.get("depth"), p.get("page_key")) for p in pages]
    assert channel[0]["depth"] >= 1, (
        f"频道页深度被写成 {channel[0]['depth']} —— 过渡帧被当成了落点")
    # 通用判据：**有父页却 depth=0** 一律自相矛盾
    bad = [p for p in pages if p.get("parent_key") and p.get("depth") == 0]
    assert bad == [], f"有父页却是 L0：{[(p['page_key'][:8], p.get('parent_action')) for p in bad]}"


def test_transition_frame_does_not_create_a_second_channel_page(tmp_path: Path) -> None:
    """过渡帧也不能被登记成"另一个频道页"（它只是同一页的另一帧）。"""
    graph, _drv = _run(tmp_path)
    pages = [p for p in graph["pages"] if isinstance(p, dict)]
    channel = [p for p in pages if "ch_title" in str(p.get("controls"))]
    assert len(channel) == 1, (
        f"频道页被记了 {len(channel)} 条（过渡帧被当成新页）："
        f"{[(p.get('depth'), p.get('parent_action')) for p in channel]}")
