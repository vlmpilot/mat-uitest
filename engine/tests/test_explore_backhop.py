"""系统 back 到达链全链路：explore 记录 BACK_HOP → arrivals press back 重放。

背景（2026-10-17 tx-news 实测）：回退恢复 back 落到未收录页，此前只能记
parent_action=None（结构性断链）——62/75 新页无链的主因之二。back 是确定性
动作，可重放：走到 back 前的页再按同一动作回放（page_index.BACK_HOP）。
"""

from pathlib import Path

from insta_engine.page_index import BACK_HOP, load_index


class BackReachDriver:
    """A --去B--> B（候选耗尽 → back）→ back 落到未收录页 R。

    R 信号与 B 部分重叠（老覆盖率虚高场景），确保 back 分支绕过渐进归并。
    """

    serial = None
    XML = {
        "A": '<node class="Button" text="去B" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
        "B": '<node class="TextView" text="B页面" package="com.x"/>',
        # R 与 B 仅 1/5 信号重叠（J=0.2<0.5）：确保落错页时 pages_equivalent 判不等价
        "R": ('<node class="TextView" text="B页面" package="com.x"/>'
              '<node class="Button" text="R按钮" package="com.x"/>'
              '<node class="TextView" text="R标题" package="com.x"/>'
              '<node class="TextView" text="R副标题" package="com.x"/>'
              '<node class="TextView" text="R正文" package="com.x"/>'),
    }
    TRANS = {("A", 5): "B"}
    BACKS = {("B", "back"): "R"}          # back 只在 B 上落 R（R 上 back 无处可去）

    def __init__(self):
        self.stack = ["A"]
        self.clicks = []
        self.presses = []

    def dump(self):
        return self.XML[self.stack[-1]]

    def click(self, x, y):
        self.clicks.append((self.stack[-1], x, y))
        target = self.TRANS.get((self.stack[-1], x))
        if target:
            self.stack.append(target)

    def press(self, key):
        self.presses.append((self.stack[-1], key))
        if key == "back":
            target = self.BACKS.get((self.stack[-1], "back"))
            if target:
                self.stack.append(target)     # back 落到新页（压栈语义与真实一致）

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package):
        self.stack = ["A"]

    def foreground_app(self):
        return {"package": "com.x", "activity": "com.x/.MainActivity"}


def _explore(tmp_path, drv):
    from insta_engine.explore import explore
    index_path = tmp_path / "com.x.json"
    explore(drv, "com.x", minutes=0.05, max_steps=5,
            artifacts_dir=tmp_path / "art", max_depth=3, sleep_scale=0.01,
            index_path=index_path)
    return index_path


def test_back_landing_recorded_with_backhop(tmp_path):
    """back 落到未收录页 R：parent=B、action=BACK_HOP；已知页不动父链。"""
    index_path = _explore(tmp_path, BackReachDriver())
    graph = load_index(index_path)
    pages = {p["page_key"]: p for p in graph["pages"] if isinstance(p, dict)}
    r = [p for p in pages.values() if p.get("parent_action") == BACK_HOP]
    assert r, "back 落点页必须记 BACK_HOP 链"
    entry = r[0]
    assert entry["parent_key"] in pages            # 链根在图谱
    assert entry["depth"] == pages[entry["parent_key"]]["depth"]   # 同层（back 不下钻）
    # B 是点击到达：链不被 back 落点污染
    b = [p for p in pages.values() if p.get("parent_action") == "去B"]
    assert b and b[0]["parent_action"] == "去B"


def test_backhop_chain_replay_stable(tmp_path):
    """重放：走到 back 前的页 → press back → 落点核对 → stable。"""
    from insta_engine.arrivals import verify_arrivals
    index_path = _explore(tmp_path, BackReachDriver())
    report = verify_arrivals(BackReachDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01)
    r = next(e for e in report["results"]
             if e["status"] == "stable" or "back" in (e.get("reason") or ""))
    assert r["status"] == "stable", r


def test_backhop_replay_offpage_reports_unstable(tmp_path):
    """back 重放落错页（真机 back 语义漂移）⇒ 落点核对判 unstable，不误判。"""
    from insta_engine.arrivals import verify_arrivals

    class WrongBackDriver(BackReachDriver):
        BACKS = {}                                 # back 没回到 R（落别的页/不动）
        def press(self, key):
            self.presses.append((self.stack[-1], key))

    index_path = _explore(tmp_path, BackReachDriver())
    report = verify_arrivals(WrongBackDriver(), "com.x", index_path,
                             trials=2, sleep_scale=0.01)
    r = [e for e in report["results"]
         if e["status"] == "unstable" and "落点不符" in (e.get("reason") or "")]
    assert r, f"back 落错页必须如实判落点不符: {report['results']}"
