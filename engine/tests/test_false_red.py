"""假红治理(2026-10-25):传输断连重试 + env-failure 前台复核。

实测病灶:gen-184020-3 连跑 7 次,4 次假红全是 `s1 launch` 的 RemoteDisconnected
(u2 到设备 agent 的 HTTP 会话坏了,业务没坏);同型 10 次样本里假红率可达 40%,
足以让任何首跑通过率失去意义。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from insta_engine.dsl import TestCase
from insta_engine.runner import Runner


class _EnvFailDriver:
    """find 抛环境类错误 → 步骤失败分类 env-failure;foreground 可控。"""

    serial = None
    platform = "android"

    def __init__(self, fg_package="com.x"):
        self.fg = fg_package
        self.reconnects = 0

    # -- 最小面 ---------------------------------------------------------------
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
        self.reconnects += 1

    def foreground_app(self):
        return {"package": self.fg}

    def find(self, by, value, timeout):
        return self._absent()

    def _absent(self):
        class _N:
            exists = False

            def wait(self, timeout=0, raise_error=False):
                return False
        return _N()

    def press(self, key):
        # home 动作的错误**不经 LocateFailed 包装**,分类走 env 标记——
        # 这才是 false_red 复核要抓的形态(定位错误会被包装成 locate-fail 抢先分类)。
        raise ConnectionError("device offline: uiautomator 连接断开")


def _case() -> TestCase:
    return TestCase.model_validate({
        "caseId": "false_red", "name": "假红复核",
        "target": {"package": "com.x"},
        "steps": [{"id": "s1", "action": "home"}],
    })


def test_transport_error_is_detected():
    from insta_engine.driver import is_transport_error

    assert is_transport_error(ConnectionError("x"))
    assert is_transport_error(ConnectionResetError("x"))


def test_with_transport_retry_reconnects_once(tmp_path):
    """传输断连 → reconnect → 重试一次成功;reconnect 计数 = 1(不无限重试)。"""
    drv = _EnvFailDriver()
    runner = Runner(drv, tmp_path)
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("RemoteDisconnected")
        return "ok"

    assert runner._with_transport_retry(op) == "ok"
    assert drv.reconnects == 1 and calls["n"] == 2


def test_with_transport_retry_reraises_non_transport(tmp_path):
    """非传输错误原样上抛,不重连(不许把业务失败当假红救)。"""
    drv = _EnvFailDriver()
    runner = Runner(drv, tmp_path)

    def op():
        raise ValueError("业务错")

    with pytest.raises(ValueError):
        runner._with_transport_retry(op)
    assert drv.reconnects == 0


def test_env_failure_with_app_in_foreground_marked_false_red(tmp_path):
    """判为 env-failure 但 App 稳在前台 ⇒ false_red=True(P0-1 验收字段)。"""
    drv = _EnvFailDriver(fg_package="com.x")   # 前台 = 目标包
    run = Runner(drv, tmp_path).run(_case())
    assert run.status == "failed"
    s1 = run.steps[0]
    assert s1.category == "env-failure"
    assert s1.false_red is True, "App 在前台的 env 失败必须带 false_red"
    assert "false_red" in (s1.warning or "")


def test_env_failure_with_app_gone_stays_env(tmp_path):
    """App 真掉出前台 ⇒ 维持 env-failure、无 false_red(复核不误伤真环境失败)。"""
    drv = _EnvFailDriver(fg_package="com.other.app")   # 前台是别的 App
    run = Runner(drv, tmp_path).run(_case())
    s1 = run.steps[0]
    assert s1.category == "env-failure"
    assert s1.false_red is False


class _FlakyDumpDriver(_EnvFailDriver):
    """首帧 dump 抛一次传输错，之后正常——模拟"u2 会话坏了但设备没事"。

    真机实测（2026-10-25 注入）：kill 掉 `com.wetest.uia2.Main` 后 dump 抛
    `RemoteDisconnected`，重连后恢复；连杀 7 次用例仍 100%。
    """

    def __init__(self):
        super().__init__()
        self.fail_dumps = 1

    def dump(self):
        if self.fail_dumps:
            self.fail_dumps -= 1
            raise ConnectionError("RemoteDisconnected: Remote end closed connection")
        return "<hierarchy/>"

    def press(self, key):
        return None


def test_transport_retry_is_recorded_on_step(tmp_path):
    """重连救回必须**留痕**（StepResult.transport_retried）——否则只能翻 stderr。"""
    drv = _FlakyDumpDriver()
    run = Runner(drv, tmp_path).run(_case())
    s1 = run.steps[0]
    assert s1.status == "passed", "重连后应救回，不该判失败"
    assert drv.reconnects == 1
    assert s1.transport_retried is True, "救回过的步骤要能在产物里认出来"
    assert "重连救回" in (s1.warning or "")
    # 落盘后仍可读（run.json 是事后审计的唯一入口）
    import json
    saved = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert saved["run"]["steps"][0]["transport_retried"] is True


def test_normal_step_has_no_transport_flag(tmp_path):
    """没断连的步骤不许带 transport_retried（否则这个字段就失去意义）。"""

    class _Clean(_FlakyDumpDriver):
        def __init__(self):
            super().__init__()
            self.fail_dumps = 0

    drv = _Clean()
    run = Runner(drv, tmp_path).run(_case())
    assert run.steps[0].status == "passed"
    assert run.steps[0].transport_retried is False
    assert drv.reconnects == 0
