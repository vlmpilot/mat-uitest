"""explore 深度策略：触顶候选记 tried + stuck(depth) 冷启动重开换入口。

背景（2026-10-17 tx-news 实测）：探索触到 max_depth 后，触顶页「点下去必然
超限」的候选在 backoff/冷启动后原样重现 → 再次触顶 → 循环空转——80 步预算
只走 18 步，12 次回退全耗在重选同一批超限候选上。修法：触顶候选记本轮
tried；stuck(depth) 且预算还剩时冷启动重开一次（回滚触顶 tried + 重置滚动
配额 = 换入口），再 stuck 才如实终止。
"""

from pathlib import Path


class DepthLimitDriver:
    """A --去B--> B --去C--> C（L2 触顶，max_depth=2）→ 重开后 A 多「去E」入口。"""

    serial = None
    XML_A = '<node class="Button" text="去B" clickable="true" bounds="[0,0][10,10]" package="com.x"/>'
    XML_A_R2 = (XML_A
                + '<node class="Button" text="去E" clickable="true" bounds="[0,20][10,30]" package="com.x"/>')
    XML = {
        "A": XML_A,
        "B": '<node class="Button" text="去C" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
        "C": '<node class="Button" text="去D" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
        "E": '<node class="TextView" text="E页面" package="com.x"/>',
    }
    TRANS = {("A", 5, 5): "B", ("B", 5, 5): "C", ("A", 5, 25): "E"}   # (页, 中心x, 中心y)

    def __init__(self):
        self.stack = ["A"]
        self.round = 0

    def dump(self):
        if self.stack[-1] == "A":
            return self.XML_A_R2 if self.round >= 1 else self.XML_A
        return self.XML[self.stack[-1]]

    def click(self, x, y):
        target = self.TRANS.get((self.stack[-1], x, y))
        if target:
            self.stack.append(target)

    def press(self, key):
        if key == "back" and len(self.stack) > 1:
            self.stack.pop()

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package):
        self.stack = ["A"]
        self.round += 1                     # 冷启动 = 重开/回退升级的标记

    def foreground_app(self):
        return {"package": "com.x", "activity": "com.x/.MainActivity"}


def _explore(tmp_path, drv, **kw):
    from insta_engine.explore import explore
    index_path = tmp_path / "com.x.json"
    report = explore(drv, "com.x", minutes=0.05, max_steps=40,
                     artifacts_dir=tmp_path / "art", sleep_scale=0.01,
                     index_path=index_path, **kw)
    return index_path, report


def test_depth_capped_candidates_break_backoff_loop(tmp_path):
    """触顶页候选记 tried：C 的「去D」不会在回退后被反复重选（backoff 有界）。"""
    from insta_engine.explore import MAX_CONSECUTIVE_BACKOFFS
    _idx, report = _explore(tmp_path, DepthLimitDriver(), max_depth=2)
    # 回退次数有界：没陷入「触顶→回退→重选→再触顶」的无穷循环
    assert report["backoffs"] <= MAX_CONSECUTIVE_BACKOFFS + 1, report["backoffs"]


def test_stuck_depth_restarts_once_and_finds_new_entry(tmp_path):
    """stuck(depth) 且预算还剩 ⇒ 冷启动重开一次，换入口探索到新页 E。"""
    drv = DepthLimitDriver()
    # `breadth=0`：这条测的是"**触到深度上限后**重开换入口"这条路，前提是**能触到**上限。
    # 广度优先（默认开）会先横向铺开、根本到不了 L2 ⇒ 必须按住它，否则测的不是这条路径。
    index_path, report = _explore(tmp_path, drv, max_depth=2, breadth=0)
    kinds = [f["type"] for f in report["findings"]]
    assert "restart" in kinds, kinds
    graph = __import__("json").loads(index_path.read_text(encoding="utf-8"))
    texts = {c.get("text") for p in graph["pages"] for c in (p.get("controls") or [])}
    assert "E页面" in texts, "重开换入口后应探索到新页 E"
    assert report["stop_reason"] in {"stuck", "budget", "exhausted"}


def test_stuck_candidates_does_not_restart_twice(tmp_path):
    """无新入口时：重开至多一次，再 stuck 如实终止（防重开循环烧预算）。"""

    class NoEntryDriver(DepthLimitDriver):
        XML_A_R2 = DepthLimitDriver.XML_A          # 重开后 A 没有新入口

    _idx, report = _explore(tmp_path, NoEntryDriver(), max_depth=2)
    kinds = [f["type"] for f in report["findings"]]
    assert kinds.count("restart") <= 1, kinds
    assert report["stop_reason"] == "stuck"


# -- 触顶记录必须能被查重通道命中（MAT 评估 2026-10-24 P0-1） ----------------------

class CapRevisitDriver:
    """双路由重访：A→B→C 在 L2 触顶（去D 记 tried）；回退后 A→C 直达，C 落在 L1。

    去D 在重访帧必须因 tried 被跳过——修复前触顶记录写的是裸坐标
    `(key, bounds)`，两条查重通道（三段式精确键 / 语义匹配）都命中不了它，
    去D 会在 L1 被原样重选点掉、D 页进图谱（= 2026-10-17 实测的空转复活）。
    """

    serial = None
    XML_A0 = '<node class="Button" text="去B" clickable="true" bounds="[0,0][10,10]" package="com.x"/>'
    XML_A1 = (XML_A0
              + '<node class="Button" text="去C" clickable="true" bounds="[0,20][10,30]" package="com.x"/>')
    XML = {
        "B": '<node class="Button" text="去C" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
        "C": '<node class="Button" text="去D" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
        "D": '<node class="Button" text="去E" clickable="true" bounds="[0,0][10,10]" package="com.x"/>'
             '<node class="TextView" text="D页面" package="com.x"/>',
    }
    TRANS = {("A", 5, 5): "B", ("A", 5, 25): "C", ("B", 5, 5): "C", ("C", 5, 5): "D"}

    def __init__(self):
        self.stack = ["A"]
        self.seen_b = False            # B 到访后 A 才露出第二个入口（控制选择顺序）
        self.c_d_clicked = False

    def dump(self):
        if self.stack[-1] == "A":
            return self.XML_A1 if self.seen_b else self.XML_A0
        return self.XML[self.stack[-1]]

    def click(self, x, y):
        if self.stack[-1] == "C" and (x, y) == (5, 5):
            self.c_d_clicked = True
        target = self.TRANS.get((self.stack[-1], x, y))
        if target:
            if target == "B":
                self.seen_b = True
            self.stack.append(target)

    def press(self, key):
        if key == "back" and len(self.stack) > 1:
            self.stack.pop()

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package):
        self.stack = ["A"]

    def foreground_app(self):
        return {"package": "com.x", "activity": "com.x/.MainActivity"}


def test_capped_candidates_are_skipped_on_revisit(tmp_path):
    """直接断言「触顶候选在回退重访后被跳过」（非有界性兜底）。

    触顶记录若用裸坐标，L1 重访帧查重全 miss ⇒ 去D 被重选点掉、D 页进图谱。
    有界性断言（上面的用例）对这一回归永远是绿的——它断言的性质由
    MAX_CONSECUTIVE_BACKOFFS 本身保证。
    """
    import json

    drv = CapRevisitDriver()
    index_path, report = _explore(tmp_path, drv, max_depth=2, breadth=0)
    assert drv.c_d_clicked is False, "触顶候选「去D」在重访帧被重选点掉了（tried 没命中）"
    graph = json.loads(index_path.read_text(encoding="utf-8"))
    texts = {c.get("text") for p in graph["pages"] for c in (p.get("controls") or [])}
    assert "D页面" not in texts, f"触顶放弃的 D 页进了图谱——空转复活的直接证据: {sorted(texts)}"
