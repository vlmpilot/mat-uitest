"""启动被测 App **必须校验 + 重试**，且"没起来"要和"跳走了"分开报（T75）。

## 为什么需要它

旧写法是"`app_start` + 睡 2 秒"，**不校验**。而重 App 冷启动可能 **5–8s**：
等待不够时第一帧还是桌面 ⇒ 主循环把它当成"离场"（`left-app`），8 次之后以 `blocked`
终止 —— **整轮 0 步 0 页**。

**实测踩到**（2026-10-07 候选 App 筛选轮）：某 App 一轮 `步数 0 · 页面 0`，
报告写「连续 8 次无法留在被测 App（**离场拉不回**）」——读起来像"App 一直跳走"，
真相是**从没等到它到前台**。同一轮里另一个包（没有可启动界面）表现完全一样。

⚠️ 这两种成因**处置相反**：
- `left-streak`：对付 App 的正常跳转（点到了广告/深链）；
- `never-started`：只要**等久一点 / 换启动方式**，或那个包根本不该拿来跑。

与 `stuck` 的两种成因（`test_explore_stuck_cause.py`）是同一族。
"""

from __future__ import annotations

from pathlib import Path

from insta_engine.explore import (
    BLOCKED_CAUSE_NEVER_STARTED,
    STARTUP_WAITS,
    STOP_BLOCKED,
    explore,
)


class _Raw:
    def __init__(self, pkg_of) -> None:
        self._pkg_of = pkg_of
        self.calls = 0

    def app_current(self) -> dict:
        self.calls += 1
        return {"package": self._pkg_of(self.calls), "activity": ".Fake"}


class _FakeDrv:
    """`app_current()` 由回调决定；dump 永远给一个可点按钮。"""

    serial = None

    def __init__(self, pkg_of) -> None:
        self.raw = _Raw(pkg_of)
        self.starts = 0          # `app_start` 被调了几次（启动重试的证据）

    def foreground_app(self) -> dict:
        return self.raw.app_current() or {}

    def dump(self) -> str:
        return ('<hierarchy><node class="Button" text="去B" resource-id="com.x:id/go"'
                ' clickable="true" bounds="[0,0][10,10]" package="com.x"/></hierarchy>')

    def click(self, x: int, y: int) -> None:
        pass

    def press(self, key: str) -> None:
        pass

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        self.starts += 1


def _run(tmp_path: Path, drv, tag: str, max_steps: int = 40) -> dict:
    return explore(drv, "com.x", minutes=0.05, max_steps=max_steps,
                   artifacts_dir=tmp_path / tag, max_depth=3, sleep_scale=0.001,
                   index_path=tmp_path / "com.x.json")


def test_never_started_is_reported_separately(tmp_path):
    """前台**一直是桌面** ⇒ `blocked` + `never-started`，且**一步都不走**。"""
    drv = _FakeDrv(lambda _n: "com.huawei.android.launcher")
    r = _run(tmp_path, drv, "dead")
    assert r["stop_reason"] == STOP_BLOCKED
    assert r["blocked_cause"] == BLOCKED_CAUSE_NEVER_STARTED, (
        "App 没起来却报成 left-streak ⇒ 报告读起来像「App 一直跳走」，处置方向全错")
    assert r["steps"] == 0 and r["page_count"] == 0
    assert drv.raw.calls == len(STARTUP_WAITS), "必须把等待梯度用满再判定失败"


def test_retry_recovers_a_slow_cold_start(tmp_path):
    """**反例的另一半**：冷启动慢，第 3 次才到前台 ⇒ 必须**重试成功**、正常探索。

    这正是旧写法（只等一次 2s）会误判成 `left-streak` 的场景。
    """
    drv = _FakeDrv(lambda n: "com.huawei.android.launcher" if n <= 2 else "com.x")
    r = _run(tmp_path, drv, "slow")
    assert r["stop_reason"] != STOP_BLOCKED, "重试后起来了，就不该报 blocked"
    assert r["blocked_cause"] is None
    assert r["steps"] > 0, "起来了就该真的走步"


def test_system_dialog_at_startup_is_not_never_started(tmp_path):
    """**关键反例**：启动时被**系统弹窗**盖住 ⇒ **不能**判"App 没起来"。

    App 其实在后面跑着，只是权限框挡着。这时要交给主循环按 T77 处理
    （点掉它 + 记 `system-dialog`）——判成 `never-started` 会让那条路整个走不到。
    """
    drv = _FakeDrv(lambda _n: "com.android.permissioncontroller")
    r = _run(tmp_path, drv, "dlg")
    assert r["blocked_cause"] != BLOCKED_CAUSE_NEVER_STARTED, (
        "系统弹窗挡着 ≠ App 没起来；判成 never-started 会让 T77 的弹窗处理走不到")
    assert "system-dialog" in {f["type"] for f in r["findings"]}, (
        "必须仍然按系统弹窗处理并记下 finding")


def test_unknown_foreground_is_not_a_failure(tmp_path):
    """**反例**：取不到前台包名（驱动不支持）时**按成功处理**，不能误判成"起不来"。

    `max_steps=1` 是为了把主循环压到一轮——否则回退路径里的 `app_start`（分级回退的
    第二级）会混进来，`starts` 就不是"启动重试了几次"了。
    """
    drv = _FakeDrv(lambda _n: "")
    r = _run(tmp_path, drv, "unknown", max_steps=1)
    assert r["stop_reason"] != STOP_BLOCKED
    assert drv.starts == 1, "第一次就按成功放行，不该把等待梯度用满（白等 12 秒）"
