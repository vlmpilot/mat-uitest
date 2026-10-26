"""Runner 保活收尾（P0-5，2026-10-19）。

为什么单独一个文件：iOS 真机跑测会起一个「每 20 秒按音量键」的保活线程，而控制台
是长驻进程——run() 不留收尾，任务结束（或中途抛异常）后线程会一直按下去。
这里守的判据只有一条：**无论正常结束还是异常退出，stop_keepalive 都必须被调用**。
"""

from __future__ import annotations

import pytest

from insta_engine.dsl import TestCase
from insta_engine.runner import Runner


class _KeepaliveDriver:
    """最小设备桩：无控件树（断言 not-exists 通过）+ 记录保活调用。"""

    serial = None
    platform = "android"

    def __init__(self) -> None:
        self.keepalive: list[str] = []

    # -- 被记录的两个契约方法 -------------------------------------------------
    def start_keepalive(self, interval: float = 20.0) -> None:
        self.keepalive.append("start")

    def stop_keepalive(self) -> None:
        self.keepalive.append("stop")

    # -- 执行所需的最小面 -----------------------------------------------------
    def dump(self) -> str:
        return "<hierarchy stable/>"

    def screenshot(self, path: str) -> None:
        from pathlib import Path

        Path(path).write_bytes(b"\x89PNG fake")

    def wake_and_unlock(self) -> None:
        return None

    def _absent_node(self):
        class _Node:
            exists = False

            def wait(self, timeout: float = 0, raise_error: bool = False) -> bool:
                return False

            def center(self):
                return (0, 0)

        return _Node()

    def find(self, by: str, value: str, timeout: float):
        return self._absent_node()

    def find_xpath(self, expr: str, timeout: float):
        return self._absent_node()


def _case() -> TestCase:
    return TestCase.model_validate({
        "caseId": "keepalive",
        "name": "保活收尾",
        "target": {"package": "com.insta.demo"},
        "steps": [{
            "id": "s1", "intent": "断言目标不存在", "action": "wait",
            "params": {"ms": 10},
            "assertions": [{"type": "not-exists",
                            "target": {"strategy": "res-id",
                                       "value": "com.insta.demo:id/none"}}],
        }],
    })


def test_runner_stops_keepalive_on_normal_exit(tmp_path):
    """run() 正常结束必须停保活——不停会一直按音量键。"""
    drv = _KeepaliveDriver()
    run = Runner(drv, tmp_path).run(_case())
    assert run.status == "passed"
    assert drv.keepalive == ["start", "stop"]


def test_runner_stops_keepalive_when_run_raises(tmp_path, monkeypatch):
    """run() 中途抛异常也必须经 finally 停保活（否则异常任务会永久按音量键）。"""
    drv = _KeepaliveDriver()
    runner = Runner(drv, tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("persist failed")

    monkeypatch.setattr(runner, "_persist", _boom)
    with pytest.raises(RuntimeError):
        runner.run(_case())
    assert drv.keepalive == ["start", "stop"]


class _ExplodingRecorder:
    """模拟 scrcpy 启动即退（recording.py 的 RuntimeError 路径）。"""

    def start(self) -> None:
        raise RuntimeError("scrcpy 录制启动失败: 进程立即退出")


def test_runner_stops_keepalive_when_recorder_start_raises(tmp_path):
    """P1-2 回归（2026-10-24）：recorder.start() 在主 try/finally 圈外——
    此前抛错会绕过 finally 的 stop_keepalive()，长驻进程（控制台 + 驱动缓存）
    里保活线程持续每 20s 按音量键直到进程退出。异常必须上抛（录屏失败要可见），
    但保活先停。现有两条 keepalive 测试只覆盖 try 内抛错，盖不到这个入口。"""
    drv = _KeepaliveDriver()
    runner = Runner(drv, tmp_path, recorder=_ExplodingRecorder())
    with pytest.raises(RuntimeError, match="scrcpy"):
        runner.run(_case())
    assert drv.keepalive == ["start", "stop"], "start 抛错后保活必须已停"
