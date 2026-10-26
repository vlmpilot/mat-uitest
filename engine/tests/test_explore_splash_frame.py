"""explore 开屏帧复现处置：中途回到开屏 → 不记页/不起链，点掉继续。

背景（2026-10-17 tx-news 实测）：探索中 App 被杀重启回到开屏广告帧，这帧被
当页记录后起一条 launch 不可达的瞬态根链——本轮重探 15 页瞬态根的来源。
处置：点掉「跳过」继续，本帧不记页；连续 3 帧翻不过 → blocked 终止。
"""

import json
from pathlib import Path

SPLASH_XML = ('<node class="Button" text="跳过" clickable="true" '
              'bounds="[800,60][880,100]" package="com.x"/>')


class SplashReopenDriver:
    """A --去B--> B --刷新--> App 被杀回开屏帧 → 翻过「跳过」→ 有效首屏 A。"""

    serial = None
    XML = {
        "A": '<node class="Button" text="去B" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
        "B": '<node class="Button" text="刷新" clickable="true" bounds="[0,0][10,10]" package="com.x"/>',
    }

    def __init__(self):
        self.stack = ["A"]
        self.clicks = []
        self.restarted = False                  # B 上点「刷新」后 App 重启
        self.dismissed = 0                      # 「跳过」被点的次数

    def dump(self):
        if self.restarted:
            return XML_A_AFTER_RESTART if self.dismissed >= 1 else SPLASH_XML
        return self.XML[self.stack[-1]]

    def click(self, x, y):
        self.clicks.append((self.stack[-1], x, y))
        if self.restarted:
            self.dismissed += 1                 # 开屏帧上的点击 = _dismiss_splash 点「跳过」
            self.stack = ["A"]                  # 跳过后回有效首屏
            return
        if (self.stack[-1], x, y) == ("A", 5, 5):
            self.stack.append("B")
        elif self.stack[-1] == "B":
            self.restarted = True               # 「刷新」点击 → App 被杀重启

    def press(self, key):
        if key == "back" and len(self.stack) > 1:
            self.stack.pop()

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package):
        self.stack = ["A"]

    def foreground_app(self):
        return {"package": "com.x", "activity": "com.x/.MainActivity"}


XML_A_AFTER_RESTART = SplashReopenDriver.XML["A"]


def _explore(tmp_path, drv, **kw):
    from insta_engine.explore import explore
    index_path = tmp_path / "com.x.json"
    report = explore(drv, "com.x", minutes=0.05, max_steps=20,
                     artifacts_dir=tmp_path / "art", sleep_scale=0.01,
                     index_path=index_path, **kw)
    return index_path, report


def test_midrun_splash_frame_not_recorded(tmp_path):
    """中途开屏帧：不记为页（无瞬态根链）、跳过被点、探索继续。"""
    drv = SplashReopenDriver()
    index_path, report = _explore(tmp_path, drv)
    graph = json.loads(index_path.read_text(encoding="utf-8"))
    texts = {c.get("text") for p in graph["pages"] for c in (p.get("controls") or [])}
    assert "跳过" not in texts, "开屏帧不得被记录为页"
    assert any(f["type"] == "splash-dismissed" for f in report["findings"])
    assert drv.dismissed >= 1


def test_persistent_splash_stops_exploration(tmp_path):
    """连续 3 帧停在开屏（跳过无效）→ blocked 终止，不空转烧预算。"""

    class StuckSplashDriver(SplashReopenDriver):
        def dump(self):
            return SPLASH_XML                   # 一直停在开屏帧（点不动，等超时）

        def click(self, x, y):
            self.clicks.append((self.stack[-1], x, y))

    _index_path, report = _explore(tmp_path, StuckSplashDriver())
    kinds = [f["type"] for f in report["findings"]]
    assert "blocked" in kinds, kinds
