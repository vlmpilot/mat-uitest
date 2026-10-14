"""arrival 试跑验证测试：路径重放 N 轮，通过 ≥N-1 记 stable。"""

from __future__ import annotations

import json
from pathlib import Path

from insta_engine.arrivals import verify_arrivals
from insta_engine.page_index import load_index, merge_pages, new_index


class FakeExploreDriver:
    """A --去B--> B --去C--> C 状态机（与 test_v1.FakeExploreDriver 同构）。"""

    serial = None
    XML = {
        "A": '<node class="Button" text="去B" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
        "B": '<node class="Button" text="去C" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
        "C": '<node class="TextView" text="C页面" package="com.x"/>',
    }
    TRANS = {("A", 5): "B", ("B", 5): "C"}

    def __init__(self):
        self.stack = ["A"]
        self.clicks = []

    def dump(self):
        return self.XML[self.stack[-1]]

    def click(self, x, y):
        self.clicks.append((self.stack[-1], x, y))
        target = self.TRANS.get((self.stack[-1], x))
        if target:
            self.stack.append(target)

    def press(self, key):
        if key == "back" and len(self.stack) > 1:
            self.stack.pop()

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package):
        self.stack = ["A"]


def _build_graph(tmp_path: Path) -> Path:
    """先跑一轮 explore 沉淀 A/B/C 三层图谱（与真机流程一致）。"""
    from insta_engine.explore import explore

    index_path = tmp_path / "com.x.json"
    explore(FakeExploreDriver(), "com.x", minutes=0.05, max_steps=6,
            artifacts_dir=tmp_path / "art", max_depth=3, sleep_scale=0.01,
            index_path=index_path)
    return index_path


def test_arrivals_stable_paths(tmp_path):
    index_path = _build_graph(tmp_path)
    report = verify_arrivals(FakeExploreDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01)
    assert report["stable"] == 2 and report["verified"] == 2  # B、C 两条路径都稳定
    graph = load_index(index_path)
    pages = {p["page_key"]: p for p in graph["pages"]}
    arrivals = [p["arrival"] for p in pages.values() if p.get("depth", 0) >= 1]
    for entry in arrivals:
        assert entry["rounds"] == 3 and entry["passed"] == 3
        assert entry["status"] == "stable" and entry["success_rate"] == 1.0
        assert entry["entry"][0]["action"] == "launch"      # 复现路径自述
        assert all(hop["action"] == "tap" for hop in entry["entry"][1:])


def test_arrivals_unstable_when_flaky(tmp_path):
    """只有第一次点击真的换页 → 1/3 < 2（N-1 门槛）→ unstable。"""
    index_path = _build_graph(tmp_path)

    class FlakyDriver(FakeExploreDriver):
        def __init__(self):
            super().__init__()
            self.allowed = 1

        def click(self, x, y):
            self.clicks.append((self.stack[-1], x, y))
            if self.allowed > 0:
                self.allowed -= 1
                target = self.TRANS.get((self.stack[-1], x))
                if target:
                    self.stack.append(target)

    report = verify_arrivals(FlakyDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01)
    b = min((r for r in report["results"] if r.get("status") in ("stable", "unstable")),
            key=lambda r: r["depth"])
    assert b["status"] == "unstable" and b["passed"] == 1 and b["rounds"] == 3
    assert "未达" in b["reason"]
    graph = load_index(index_path)
    b_page = next(p for p in graph["pages"] if p["depth"] == 1)
    assert b_page["arrival"]["status"] == "unstable"


def test_arrivals_label_missing_fails_honestly(tmp_path):
    """图谱记的文案在当前界面找不到（UI 改版）→ 失败并写明原因，不静默当稳定。

    改 A 页按钮文案（A 是起点、不校验 key）——若改 B 页，B 的 page_key 也变，
    第一跳会先报"落点不符"而非"未找到"，测不到目标分支。
    """
    index_path = _build_graph(tmp_path)

    class RenamedDriver(FakeExploreDriver):
        XML = dict(FakeExploreDriver.XML, A=FakeExploreDriver.XML["A"].replace("去B", "进入B"))

    report = verify_arrivals(RenamedDriver(), "com.x", index_path,
                             trials=2, sleep_scale=0.01)
    b = next(r for r in report["results"] if r["depth"] == 1)
    assert b["status"] == "unstable" and "未找到文案「去B」" in b["reason"]


def test_arrivals_broken_chain_skipped(tmp_path):
    index_path = tmp_path / "com.x.json"
    index = new_index("com.x")
    merge_pages(index, [{"page_key": "orphan", "depth": 1, "parent_key": "ghost",
                         "parent_action": "某动作", "controls": []}])
    index_path.write_text(json.dumps(index), encoding="utf-8")

    report = verify_arrivals(FakeExploreDriver(), "com.x", index_path,
                             trials=2, sleep_scale=0.01)
    assert report["verified"] == 0
    assert report["results"][0]["status"] == "skipped" and "父链不完整" in report["results"][0]["reason"]


def test_arrivals_click_budget_stops_early(tmp_path):
    index_path = _build_graph(tmp_path)
    report = verify_arrivals(FakeExploreDriver(), "com.x", index_path,
                             trials=3, sleep_scale=0.01, click_budget=1)
    assert report["stopped_early"] is True
    assert report["clicks"] == 1
    assert report["verified"] == 1  # 第一条路径只跑了 1 轮就被预算拦下
