"""`stuck` 的**两种成因必须分开报**（T69）。

## 为什么要拆

旧代码把两个条件写在同一分支里：

```python
if not candidates or current_depth >= max_depth:   # ← 两个原因混在一起
    backoffs += 1
    if backoffs >= MAX_CONSECUTIVE_BACKOFFS: stop_reason = STOP_STUCK
```

于是报告只说"**本页/本轮已无路可走**"。而实测 4 个真实 App 里 **3 个以 stuck 结束**时，
**根本看不出该调 `--max-depth` 还是该改选点策略** —— 这两件事的处置**完全相反**：

| 成因 | 含义 | 处置 |
|---|---|---|
| `depth-limit` | 触到 `--max-depth`，不再下钻 | **调大 `--max-depth` 才有用** |
| `no-candidates` | 本页能点的都试过了 | **调大深度也没用**，要改选点策略 |

## 为什么用假驱动

真机跑一轮 15 分钟且**不可复现**（App 状态在变）。假驱动能把两种成因**单独打开**。
"""

from __future__ import annotations

from pathlib import Path

from insta_engine.explore import (
    STOP_STUCK,
    STUCK_CAUSE_CANDIDATES,
    STUCK_CAUSE_DEPTH,
    explore,
)


class _Drv:
    """A 页有一个可点按钮「去B」；B 页**没有可点控件**。

    ⇒ A→B 之后必然进入"候选耗尽"；把 `max_depth` 设成 0 则一开始就"触到深度上限"。
    """

    serial = None
    raw = None

    def __init__(self) -> None:
        self.stack = ["A"]

    def dump(self) -> str:
        if self.stack[-1] == "A":
            return ('<hierarchy><node class="Button" text="去B" resource-id="com.x:id/go"'
                    ' clickable="true" bounds="[0,0][10,10]" package="com.x"/></hierarchy>')
        return ('<hierarchy><node class="TextView" text="B页" resource-id="com.x:id/b"'
                ' clickable="false" bounds="[0,0][10,10]" package="com.x"/></hierarchy>')

    def click(self, x: int, y: int) -> None:
        if self.stack[-1] == "A":
            self.stack.append("B")

    def press(self, key: str) -> None:
        if key == "back" and len(self.stack) > 1:
            self.stack.pop()

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        self.stack = ["A"]


def _run(tmp_path: Path, *, max_depth: int) -> dict:
    return explore(_Drv(), "com.x", minutes=0.05, max_steps=40,
                   artifacts_dir=tmp_path / f"d{max_depth}",
                   max_depth=max_depth, sleep_scale=0.01,
                   index_path=tmp_path / "com.x.json")


def test_candidates_exhausted_is_reported_as_no_candidates(tmp_path):
    """B 页没有可点控件、深度还远没到上限 ⇒ 成因必须是 `no-candidates`。"""
    r = _run(tmp_path, max_depth=3)
    assert r["stop_reason"] == STOP_STUCK
    assert r["stuck_cause"] == STUCK_CAUSE_CANDIDATES, (
        "深度没到上限就卡住 ⇒ 不能报成 depth-limit（那样会误导人去调 --max-depth）")


def test_depth_limit_is_reported_as_depth_limit(tmp_path):
    """`max_depth=0` ⇒ 起始页就已经"触到上限" ⇒ 成因必须是 `depth-limit`。

    这是两种成因**处置相反**的那一侧：调大 `--max-depth` 才有用。
    """
    r = _run(tmp_path, max_depth=0)
    assert r["stop_reason"] == STOP_STUCK
    assert r["stuck_cause"] == STUCK_CAUSE_DEPTH


def test_non_stuck_runs_have_no_cause(tmp_path):
    """**反例**：不是 stuck 结束时，`stuck_cause` 必须是 None（不能残留上次的值）。"""
    r = explore(_Drv(), "com.x", minutes=0.05, max_steps=1,
                artifacts_dir=tmp_path / "one", max_depth=3, sleep_scale=0.01,
                index_path=tmp_path / "com.x.json")
    assert r["stop_reason"] != STOP_STUCK
    assert r["stuck_cause"] is None


def test_stuck_finding_carries_the_cause(tmp_path):
    """报告里的 finding 也要带成因——人是从报告读结论的，不是从返回值。"""
    r = _run(tmp_path, max_depth=0)
    stuck = [f for f in r["findings"] if f.get("type") == "stuck"]
    assert stuck, "必须留下 stuck finding"
    assert stuck[0].get("cause") == STUCK_CAUSE_DEPTH
    assert "触到深度上限" in stuck[0]["detail"]
    assert "--max-depth" in stuck[0]["detail"]
