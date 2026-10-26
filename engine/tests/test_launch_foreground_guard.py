"""launch 前台守卫（P0 导航链守卫，2026-10-25 真机实测病灶）。

病灶原始证据：同一用例 ×15 轮真机连跑，**3 轮设备跑到了系统「设置」**，而 `s1 launch`
全部判 passed：

- 第 1 例：s4 tap 之后前台 = 系统设置，s5 的 `text=综合` 定位失败，
  最终归因 **locate-fail**（"定位失败"）—— 与真因（设备在别的 App 里）完全无关；
- 第 2 例：launch 后 2s，`WindowManager` 里 `Splash Screen <系统设置> EXITING`，
  被测 App 的窗口随即 `destroySurfaces appStopped=true`，s2 的 res-id 定位失败。

根因：Android 分支只发启动命令，**从不校验前台**（`_wait_landed` 原先只在 iOS 分支用）。
所以"设备在别的 App"这件事没有任何一步会报出来，只会以"定位失败"的形式出现在下游。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from insta_engine import runner as runner_mod
from insta_engine.dsl import TestCase
from insta_engine.runner import Runner

PKG = "com.x"
OTHER = "com.android.settings"


class _Driver:
    """最小驱动面：只够跑通 launch 步（含启动噪声清理与前台守卫）。"""

    platform = "android"
    serial = "SERIAL-1"

    def __init__(self, fg=PKG):
        self._fg = fg

    # -- 证据/生命周期 --------------------------------------------------------
    def dump(self):
        return "<hierarchy/>"

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def wake_and_unlock(self):
        return None

    def start_keepalive(self):
        return None

    def stop_keepalive(self):
        return None

    def start_evidence(self):
        return None

    def reconnect(self):
        return None

    # -- 关键：前台可注入 -----------------------------------------------------
    def foreground_app(self):
        if isinstance(self._fg, Exception):
            raise self._fg
        return {} if self._fg is None else {"package": self._fg}

    def app_start(self, package, activity=None):
        return None

    def find(self, by, value, timeout=0):
        class _Absent:
            exists = False

            def click(self):
                return None
        return _Absent()

    def press(self, key):
        return None


def _launch_case() -> TestCase:
    return TestCase.model_validate({
        "caseId": "fg-guard",
        "name": "launch 前台守卫",
        "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch", "params": {"package": PKG}}],
    })


@pytest.fixture(autouse=True)
def _stub_am_start(monkeypatch):
    """别真跑 adb：am start -W 假装成功并给出启动耗时。"""
    monkeypatch.setattr(runner_mod, "am_start_w",
                        lambda serial, package, activity=None: {"launch_ms": 1234})


def test_launch_fails_when_another_app_is_in_front(tmp_path):
    """设备在别的 App ⇒ launch 当场失败，且分类是 env-failure，不是 locate-fail。"""
    run = Runner(_Driver(fg=OTHER), tmp_path).run(_launch_case())
    s1 = run.steps[0]

    assert s1.status == "failed", "前台不是目标 App 时 launch 不许判 passed"
    assert s1.category == "env-failure", "这是设备被别的界面占着，不是控件找不到"
    assert "前台不是目标" in (s1.error or "")
    assert OTHER in (s1.error or "") and PKG in (s1.error or ""), "错误里要带实际前台包名"
    # 下游步骤不该被执行（launch 失败即 abort，避免把错误转嫁成"定位失败"）
    assert all(s.status == "skipped" for s in run.steps[1:]) or len(run.steps) == 1


def test_launch_passes_when_target_is_in_front(tmp_path):
    run = Runner(_Driver(fg=PKG), tmp_path).run(_launch_case())
    s1 = run.steps[0]
    assert s1.status == "passed"
    assert s1.category is None


def test_unknown_foreground_does_not_fail_launch(tmp_path):
    """前台查不出来（驱动报错）⇒ 不判死：宁可漏报，也不制造假失败。"""
    run = Runner(_Driver(fg=RuntimeError("dumpsys 不可用")), tmp_path).run(_launch_case())
    assert run.steps[0].status == "passed"


def test_empty_foreground_does_not_fail_launch(tmp_path):
    """前台返回空 dict（部分机型/驱动）⇒ 同样不判死。"""
    run = Runner(_Driver(fg=None), tmp_path).run(_launch_case())
    assert run.steps[0].status == "passed"


def test_guard_does_not_apply_to_api_steps(tmp_path):
    """纯 api 步骤不碰设备，守卫不能拦（无设备也能跑）。"""
    case = TestCase.model_validate({
        "caseId": "api-only",
        "name": "api",
        "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "api",
                   "params": {"method": "GET", "url": "http://127.0.0.1:9/"}}],
    })
    run = Runner(_Driver(fg=OTHER), tmp_path).run(case)
    assert "前台不是目标" not in (run.steps[0].error or "")
