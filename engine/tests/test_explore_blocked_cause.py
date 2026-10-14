"""`blocked` 终止**必须报出真实原因**，不能伪装成"预算耗尽"（T73）。

## 为什么需要它

`explore()` 的 `stop_reason` **默认值是 `budget`**，只有 `stuck` 分支会改写它。
而 `blocked` 的两处 `break`（连续离场拉不回 / 回退失败）**都没有改写** ⇒
报告落回默认值，汇总行印成「结束 预算耗尽」。

**实测踩到**（2026-10-06，`artifacts/explore/134029-1cda`）：某 App 一轮
`duration_seconds=72.9`（预算 **900s**）、`left_app=12/13 步`，
报告却说「预算耗尽」——**用户会去加预算，而真因是 App 一直往别的 App / QQ 跳**。

这与 `stuck` 的两种成因是**同一族**（见 `test_explore_stuck_cause.py`）：
停止原因决定处置方向，**报错方向比不报更坏**。
"""

from __future__ import annotations

from pathlib import Path

from insta_engine.explore import (
    BLOCKED_CAUSE_FALLBACK_FAILED,
    BLOCKED_CAUSE_LEFT_STREAK,
    STOP_BLOCKED,
    STOP_STUCK,
    STUCK_CAUSE_CANDIDATES,
    explore,
)


class _Raw:
    """`_app_current()` 走的是 `drv.raw.app_current()`，不是 dump ⇒ 假驱动必须提供它。

    （第一版漏了这个 ⇒ 前台包名"未知" ⇒ 离场守卫**根本没触发**，测试报的是 `stuck`。
    这也说明：**假驱动少一个入口，就会把被测分支整个跳过**，而失败信息看起来像"逻辑写错了"。）
    """

    def __init__(self, package: str, activity: str = ".Fake") -> None:
        self._pkg = package
        self._act = activity

    def app_current(self) -> dict:
        return {"package": self._pkg, "activity": self._act}


class _RawLeaves(_Raw):
    """**启动校验时先报目标包**，之后才"跳走"。

    ⚠️ 必须这样：若第一次就报别的包，`_ensure_foreground()` 会判成
    `never-started`（App 根本没起来）而**进不到离场分支**——
    两者在"前台不是目标包"这一点上重合，但成因与处置完全不同（T75）。
    `app_current` 第 1 次调用是启动校验，第 2 次起是主循环。
    """

    def __init__(self, target: str, other: str) -> None:
        super().__init__(other)
        self._target = target
        self.calls = 0

    def app_current(self) -> dict:
        self.calls += 1
        return {"package": self._target if self.calls == 1 else self._pkg,
                "activity": ".Fake"}


class _LeavesAppDrv:
    """启动能起来，但**随后前台永远是别的 App**（`com.other.app`）⇒ 每轮都记一次"离场"。

    `press` / `app_start` 都拉不回来（`app_current` 依旧返回别的包）⇒ 必然撞 `MAX_LEFT_STREAK`。
    """

    serial = None

    def __init__(self) -> None:
        self.raw = _RawLeaves("com.x", "com.other.app")

    def foreground_app(self) -> dict:
        return self.raw.app_current() or {}

    def dump(self) -> str:
        return ('<hierarchy><node class="Button" text="别的App" resource-id="com.other:id/x"'
                ' clickable="true" bounds="[0,0][10,10]" package="com.other.app"/></hierarchy>')

    def click(self, x: int, y: int) -> None:
        pass

    def press(self, key: str) -> None:
        pass

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        pass


def test_left_streak_reports_blocked_not_budget(tmp_path):
    """连续离场拉不回 ⇒ `stop_reason` 必须是 `blocked`，**不能是 `budget`**。"""
    r = explore(_LeavesAppDrv(), "com.x", minutes=0.05, max_steps=40,
                artifacts_dir=tmp_path / "left", max_depth=3, sleep_scale=0.01,
                index_path=tmp_path / "com.x.json")
    assert r["stop_reason"] == STOP_BLOCKED, (
        "被 blocked 守卫终止却报 budget ⇒ 用户会去加预算（实测 72.9s 就报过「预算耗尽」）")
    assert r["blocked_cause"] == BLOCKED_CAUSE_LEFT_STREAK
    # 反例：stuck 的成因不能残留（两个字段是互斥的）
    assert r["stuck_cause"] is None


def test_blocked_finding_carries_the_cause(tmp_path):
    """报告里的 finding 也要带成因——人是从报告读结论的。"""
    r = explore(_LeavesAppDrv(), "com.x", minutes=0.05, max_steps=40,
                artifacts_dir=tmp_path / "left2", max_depth=3, sleep_scale=0.01,
                index_path=tmp_path / "com.x.json")
    blocked = [f for f in r["findings"] if f.get("type") == "blocked"]
    assert blocked, "必须留下 blocked finding"
    assert blocked[0].get("cause") == BLOCKED_CAUSE_LEFT_STREAK


class _BackExitsAppDrv:
    """**根 Activity 上 `back` 会退出 App**（真机实测：前台变成桌面）——但**冷启动能救**。

    旧写法在 `back` 失败时**直接终止**（`level` 只在成功时推进 ⇒ 冷启动那一级从没被试过），
    报文却写"back 与冷启动都回不去"。这个驱动锁住"**失败必须升级**"（T79）。
    """

    serial = None

    def __init__(self) -> None:
        self.left = False          # `back` 之后前台变成桌面
        self.starts = 0

    class _Raw:
        def __init__(self, outer) -> None:
            self._o = outer

        def app_current(self) -> dict:
            pkg = "com.android.launcher" if self._o.left else "com.x"
            return {"package": pkg, "activity": ".Fake"}

    @property
    def raw(self):
        return self._Raw(self)

    def dump(self) -> str:
        # 起始页只有一个可点按钮；点完之后是**死路**（无候选）⇒ 走回退分支
        if self.left:
            return '<hierarchy/>'
        return ('<hierarchy><node class="Button" text="去B" resource-id="com.x:id/go"'
                ' clickable="true" bounds="[0,0][10,10]" package="com.x"/></hierarchy>')

    def click(self, x: int, y: int) -> None:
        self.dead = True

    def press(self, key: str) -> None:
        if key == "back":
            self.left = True        # back 退出 App（前台变桌面）

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        self.starts += 1
        self.left = False           # 冷启动能把 App 拉回来


def test_back_exiting_app_escalates_to_cold_start(tmp_path):
    """`back` 退出 App ⇒ **必须升级到冷启动**，不能直接判 `blocked`。

    ⚠️ 旧写法在这里直接终止，而报文却写"back 与冷启动都回不去"——**说了没发生的事**。
    """
    drv = _BackExitsAppDrv()
    r = explore(drv, "com.x", minutes=0.05, max_steps=40,
                artifacts_dir=tmp_path / "esc", max_depth=3, sleep_scale=0.001,
                index_path=tmp_path / "com.x.json")
    assert drv.starts > 0, "`back` 退出 App 后**必须试过冷启动**（旧写法从没试过）"
    assert r["blocked_cause"] != BLOCKED_CAUSE_FALLBACK_FAILED, (
        "冷启动能把 App 拉回来，就不该判「回退失败」")


class _CantFallbackDrv:
    """前台**一直是目标包**（所以不触发离场守卫），但 `back`/冷启动都**抛异常**。

    ⇒ `fallback_once()` 返回 False ⇒ 走**另一处** `break`（回退失败）。
    用来验证那一处也带成因——两处都不带的话，修一处等于没修。
    """

    serial = None
    raw = None

    def dump(self) -> str:
        return ('<hierarchy><node class="TextView" text="死路" resource-id="com.x:id/dead"'
                ' clickable="false" bounds="[0,0][10,10]" package="com.x"/></hierarchy>')

    def click(self, x: int, y: int) -> None:
        pass

    def press(self, key: str) -> None:
        raise RuntimeError("back 不可用")

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        raise RuntimeError("冷启动不可用")


def test_fallback_failure_reports_blocked_not_budget(tmp_path):
    """**另一处 `break`**：回退失败也要报 `blocked` + 对应成因。"""
    r = explore(_CantFallbackDrv(), "com.x", minutes=0.05, max_steps=40,
                artifacts_dir=tmp_path / "fb", max_depth=3, sleep_scale=0.01,
                index_path=tmp_path / "com.x.json")
    assert r["stop_reason"] == STOP_BLOCKED
    assert r["blocked_cause"] == BLOCKED_CAUSE_FALLBACK_FAILED
    assert r["stuck_cause"] is None


class _StuckOnSamePageDrv:
    """A 页有一个可点按钮；点了之后 B 页没有可点控件，但**回退能成功**。

    ⇒ 应当以 `stuck`（候选耗尽）结束，**而不是** blocked ⇒ 用来验证两个字段互斥。
    """

    serial = None
    raw = None

    def __init__(self) -> None:
        self.clicked = False

    def dump(self) -> str:
        if not self.clicked:
            return ('<hierarchy><node class="Button" text="去B" resource-id="com.x:id/go"'
                    ' clickable="true" bounds="[0,0][10,10]" package="com.x"/></hierarchy>')
        return ('<hierarchy><node class="TextView" text="B页" resource-id="com.x:id/b"'
                ' clickable="false" bounds="[0,0][10,10]" package="com.x"/></hierarchy>')

    def click(self, x: int, y: int) -> None:
        self.clicked = True

    def press(self, key: str) -> None:
        pass

    def screenshot(self, path) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package: str) -> None:
        pass


def test_stuck_run_has_no_blocked_cause(tmp_path):
    """**反例**：以 `stuck` 结束时 `blocked_cause` 必须是 None（两个成因字段互斥）。"""
    r = explore(_StuckOnSamePageDrv(), "com.x", minutes=0.05, max_steps=40,
                artifacts_dir=tmp_path / "stuck", max_depth=3, sleep_scale=0.01,
                index_path=tmp_path / "com.x.json")
    assert r["stop_reason"] == STOP_STUCK
    assert r["stuck_cause"] == STUCK_CAUSE_CANDIDATES
    assert r["blocked_cause"] is None
