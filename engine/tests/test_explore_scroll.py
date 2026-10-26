"""页内滚动探测（2026-10-15）：候选耗尽 ≠ 页面到底。

## 背景

探索此前只有「点击 / back / 冷启动」三个动作，**从不滑动**——长列表、设置二级页
首屏之外的元素永远探不到。本组测试锁住新行为的三条边：

1. **滑出新内容**：候选耗尽 → 向上滑一屏 → 新元素入候选被点（tried 含新 bounds）；
2. **到底判定与预算**：连续 `SCROLL_DRY_LIMIT` 屏无新内容判到底（不再滑满配额）、
   达 `MAX_SCROLLS_PER_PAGE` 也停；单页最多滑配额次，且滑动消耗步数预算；
3. **跨轮复用**：`scroll_done` 随图谱持久化（`_merge_page` 保 True 不回退），
   增量复测对已到底的页**零滑动**。

另锁一个必须堵住的坑：滑动后控件树变化会让 `page_key` 失配、信号包含度跌破阈值
⇒ 首帧被误判成"新页"（深度/父链/tried 去重全乱）——滑动是我们自己的显式动作，
首帧必须强制归并回滑动前那页（场景 1 的 `page_count` 断言）。
"""

from __future__ import annotations

import json
from pathlib import Path

from insta_engine.explore import (
    STOP_STUCK,
    STUCK_CAUSE_CANDIDATES,
    explore,
)


class _Scroller:
    """A 页首屏 1 个按钮「甲」；`reveals=True` 时滑动后多出「乙」。点击不换页。"""

    serial = None
    raw = None

    def __init__(self, reveals: bool = False) -> None:
        self.reveals = reveals
        self.swipes = 0

    def _xml(self) -> str:
        nodes = ('<node class="Button" text="甲" resource-id="com.x:id/a"'
                 ' clickable="true" bounds="[0,0][10,10]" package="com.x"/>')
        if self.reveals and self.swipes > 0:
            nodes += ('<node class="Button" text="乙" resource-id="com.x:id/b"'
                      ' clickable="true" bounds="[0,40][10,50]" package="com.x"/>')
        return f"<hierarchy>{nodes}</hierarchy>"

    def dump(self) -> str:
        return self._xml()

    def click(self, x: int, y: int) -> None:
        pass                                    # 点击不换页：始终停在 A

    def swipe_ext(self, direction: str) -> None:
        self.swipes += 1

    def press(self, key: str) -> None:
        pass                                    # 根页 back 无效

    def app_start(self, package: str) -> None:
        pass

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")


def _run(drv, tmp_path: Path, name: str) -> dict:
    return explore(drv, "com.x", minutes=0.05, max_steps=40,
                   artifacts_dir=tmp_path / name, max_depth=3, sleep_scale=0.01,
                   index_path=tmp_path / "com.x.json")


def test_scroll_uncovers_new_targets(tmp_path):
    """候选耗尽 → 滑动 → 新元素入候选被点：tried 含滑出的 bounds，且**不误判成新页**。"""
    drv = _Scroller(reveals=True)
    r = _run(drv, tmp_path, "reveal")
    assert drv.swipes >= 1, "候选耗尽后必须尝试页内滑动"
    # `tried` 条目格式是 `<bounds>\t<label>\t<rid>`（2026-10-22 起带语义），按首段取坐标
    def _bounds_of(page: dict) -> set[str]:
        return {e.split("\t")[0] for e in (page.get("tried") or [])}

    a = [p for p in json.loads((tmp_path / "com.x.json").read_text())["pages"]
         if "[0,0][10,10]" in _bounds_of(p)]
    assert a, "A 页必须已登记进图谱"
    assert "[0,40][10,50]" in _bounds_of(a[0]), "滑出的「乙」必须被点击并记入 tried"
    # 滑动后控件树变了（甲+乙 vs 甲）：若不做首帧强制归并，会被判成新页——
    # page_count 会 >1 且「乙」记到别的页名下。
    assert r["page_count"] == 1, (
        f"滑动后的帧必须归并回原页，不应误判新页（实际 {r['page_count']} 页）")
    assert r["scrolls"] == drv.swipes, "报告的 scrolls 必须与实际滑动次数一致"


def test_scroll_dry_marks_done_within_quota(tmp_path):
    """滑 2 屏无新内容即判到底（不滑满 3 次配额），scroll_done 落进图谱。"""
    drv = _Scroller(reveals=False)               # 滑动后内容不变
    r = _run(drv, tmp_path, "dry")
    assert r["stop_reason"] == STOP_STUCK
    assert r["stuck_cause"] == STUCK_CAUSE_CANDIDATES
    assert drv.swipes == 2, (
        f"连续 {2} 屏无新内容就应判到底并转回退，不应滑满配额 3 次（实际 {drv.swipes}）")
    pages = json.loads((tmp_path / "com.x.json").read_text())["pages"]
    assert pages and pages[0].get("scroll_done") is True, "到底事实必须持久化"


def test_scroll_done_reused_across_runs(tmp_path):
    """增量复测：上一轮已判到底的页，这一轮**零滑动**（预算只花在新东西上）。"""
    _run(_Scroller(reveals=False), tmp_path, "round1")
    drv2 = _Scroller(reveals=True)               # 即便这轮滑动会有产出，也不该滑
    r2 = _run(drv2, tmp_path, "round2")
    assert drv2.swipes == 0, "上一轮已 scroll_done 的页，增量复测必须跳过滚动探测"
    assert r2["scrolls"] == 0


def test_driver_without_swipe_stays_silent(tmp_path):
    """驱动不支持滑动（旧桩/受限驱动）：静默禁用，不刷 env-issue，行为退回滑动前。"""
    drv = _Scroller(reveals=False)
    drv.swipe_ext = None    # 探索用 try/except 调用：调用即 TypeError ⇒ 走静默禁用路径
    r = _run(drv, tmp_path, "noswipe")
    assert r["stop_reason"] == STOP_STUCK
    assert not [f for f in r["findings"] if f.get("type") == "env-issue"], (
        "滑动能力缺失不该当环境问题刷 finding（测试桩/受限驱动是常态）")
    assert r["scrolls"] == 0
