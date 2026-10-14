"""录屏单测：Recorder 平台分派/生命周期 + Runner 集成 + 报告内嵌。

不打真 adb/真模拟器：Popen 与 subprocess.run 全 monkeypatch；
keep/discard 语义用真文件验证（磁盘纪律是本功能的核心承诺）。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from insta_engine.recording import Recorder, make_recorder
from insta_engine.report import render_html
from insta_engine.runner import Runner, RunResult


class _FakePopen:
    procs: list[_FakePopen] = []

    def __init__(self, args, **kwargs):
        self.args = args
        self.terminated = False
        self.signaled = False
        _FakePopen.procs.append(self)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def send_signal(self, sig):
        self.signaled = True

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return None


@pytest.fixture
def fake_popen(monkeypatch):
    _FakePopen.procs = []
    run_cmds: list[list[str]] = []

    def fake_run(*args, **kwargs):
        run_cmds.append(args[0] if args else kwargs.get("args", []))
        # stdout 给具体值（pidof 场景返回 pid 形态；其余场景调用方自取所需字段）
        return mock.Mock(returncode=0, stdout="12345")

    monkeypatch.setattr("insta_engine.recording.subprocess.Popen", _FakePopen)
    monkeypatch.setattr("insta_engine.recording.subprocess.run", fake_run)
    return _FakePopen.procs, run_cmds


def test_android_start_uses_screenrecord_with_serial(fake_popen):
    procs, _ = fake_popen
    rec = Recorder("android", "SER1", Path("art/rec"), rotate_watch=False)
    rec.start()
    args = procs[0].args
    assert "screenrecord" in args and "--time-limit" in args
    assert args.index("-s") >= 0 and args[args.index("-s") + 1] == "SER1", "多设备必须 -s serial"


def test_android_stop_signals_device_side_and_pulls(fake_popen, tmp_path):
    """stop 语义（2026-10-11 实测修正）：kill 本地 adb 客户端不会停设备端 screenrecord——
    必须 pidof 找设备侧进程发 SIGINT；本地客户端随设备端退出；然后 pull + 清理设备端。"""
    procs, run_cmds = fake_popen
    rec = Recorder("android", "SER1", tmp_path / "rec", rotate_watch=False)
    rec.start()
    files = rec.stop()
    flat = [x for c in run_cmds for x in (c or [])]
    assert "pidof" in flat and "kill" in flat and "-INT" in flat, "必须对设备侧进程发 SIGINT"
    assert "pull" in flat, "段文件必须 pull 回本机"
    assert "rm" in flat, "设备端段文件必须清理（防 /sdcard 堆积）"
    assert files == []


def test_android_stop_terminates_local_when_device_pid_missing(fake_popen, tmp_path):
    """pidof 找不到设备侧进程（可能已到 180s 自退）⇒ 本地客户端卡住时回退 kill。"""
    import subprocess as _sp

    import insta_engine.recording as rec_mod
    procs, run_cmds = fake_popen
    rec = Recorder("android", "SER1", tmp_path / "rec", rotate_watch=False)
    rec.start()
    # pidof 返回空：让 fake_run 对 pidof 返回空 stdout
    orig_run = rec_mod.subprocess.run
    def pidof_empty_run(*a, **k):
        # 仅对 pidof 返回空；其余命令（pull/rm）照常记录并返回成功
        if a and a[0] and "pidof" in a[0]:
            return mock.Mock(returncode=0, stdout="")
        run_cmds.append(a[0] if a else [])
        return mock.Mock(returncode=0)
    rec_mod.subprocess.run = pidof_empty_run
    # 本地客户端的 wait 超时（设备端自退但客户端卡住的形态）⇒ 必须回退 kill
    procs[0].wait = lambda timeout=None: (_ for _ in ()).throw(
        _sp.TimeoutExpired(cmd="adb shell", timeout=15))
    try:
        rec.stop()
    finally:
        rec_mod.subprocess.run = orig_run
    assert procs[0].terminated, "pidof 空且本地卡住 ⇒ 回退 kill"
    flat = [x for c in run_cmds for x in (c or [])]
    assert "pull" in flat and "rm" in flat, "pidof 空 ⇒ 跳过信号直接 pull 已知段"


def test_ios_sim_uses_simctl_and_sigint(fake_popen):
    procs, _ = fake_popen
    rec = Recorder("ios", None, Path("art/rec"))
    rec.start()
    args = procs[0].args
    assert args[:4] == ["xcrun", "simctl", "io", "booted"], "模拟器走 simctl io booted"
    rec.stop()
    assert procs[0].signaled, "simctl 收 SIGINT 才会封装出完整文件"


def test_make_recorder_android_prefers_scrcpy_then_screenrecord(tmp_path, monkeypatch):
    # scrcpy 在 ⇒ scrcpy 模式（任意真机可用）
    monkeypatch.setattr("insta_engine.recording.shutil.which", lambda _: "/usr/local/bin/scrcpy")
    rec, reason = make_recorder("android", None, tmp_path)  # make_recorder 无轮转
    assert isinstance(rec, Recorder) and rec.mode == "scrcpy" and reason is None
    # scrcpy 无 + 设备有 screenrecord ⇒ screenrecord 模式
    monkeypatch.setattr("insta_engine.recording.shutil.which", lambda _: None)
    monkeypatch.setattr("insta_engine.recording.subprocess.run",
                        lambda *a, **k: mock.Mock(stdout="/system/bin/screenrecord"))
    rec, reason = make_recorder("android", None, tmp_path)  # make_recorder 无轮转
    assert isinstance(rec, Recorder) and rec.mode == "screenrecord"
    # 两者皆无 ⇒ None + 精确原因（华为 EMUI 实测裁剪了 screenrecord）
    monkeypatch.setattr("insta_engine.recording.subprocess.run",
                        lambda *a, **k: mock.Mock(stdout=""))
    rec, reason = make_recorder("android", None, tmp_path)  # make_recorder 无轮转
    assert rec is None and "scrcpy" in (reason or "") and "screenrecord" in (reason or "")


def test_make_recorder_ios_sim_only(tmp_path):
    rec, reason = make_recorder("ios", "simulator", tmp_path)
    assert isinstance(rec, Recorder) and reason is None
    rec, reason = make_recorder("ios", "00008101-XXXX", tmp_path)
    assert rec is None and "P2" in (reason or ""), "iOS 真机录屏属 P2（诚实降级）"


def test_make_recorder_unknown_platform():
    rec, reason = make_recorder("unknown", None, Path("a"))
    assert rec is None and reason


def test_keep_moves_into_video_dir_and_discard_deletes(tmp_path):
    rec = Recorder("ios", None, tmp_path / "rec")
    rec.staging.mkdir(parents=True)
    seg = rec.staging / "rec_01.mp4"
    seg.write_bytes(b"x")
    kept = rec.keep([seg], tmp_path / "art")
    assert kept[0] == tmp_path / "art" / "video" / "rec_01.mp4"
    assert kept[0].exists() and not seg.exists(), "keep=挪进 video/（报告相对引用）"
    other = tmp_path / "b.mp4"
    other.write_bytes(b"y")
    rec.discard([other])
    assert not other.exists(), "discard=删除（通过的录屏不留）"


# -- Runner 集成：假录制器验证生命周期与 keep/discard 决策 ------------------------------

class _FakeDrv:
    serial = None

    def dump(self):
        return "<hierarchy/>"

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def app_start(self, package, activity=None):
        pass

    def press(self, key):
        pass

    def wake(self):
        pass

    def unlock(self):
        pass


class _FakeRecorder:
    calls: list = []

    def start(self):
        _FakeRecorder.calls.append("start")

    def stop(self):
        _FakeRecorder.calls.append("stop")
        return [Path("rec_01.mp4")]  # 哨兵：驱动 keep/discard 决策分支

    def keep(self, files, artifacts_dir):
        _FakeRecorder.calls.append("keep")

    def discard(self, files):
        _FakeRecorder.calls.append("discard")


def _case(steps: list[dict]) -> str:
    return json.dumps({
        "caseId": "case_rec", "name": "录屏集成",
        "target": {"package": "com.x"}, "steps": steps,
    }, ensure_ascii=False)


def test_runner_passed_discards_recording(tmp_path):
    _FakeRecorder.calls = []
    case = json.loads(_case([
        {"id": "s1", "intent": "启动", "action": "launch", "params": {"package": "com.x"}},
        {"id": "s2", "intent": "等待", "action": "wait", "params": {"ms": 5}},
    ]))
    from insta_engine.dsl import TestCase
    Runner(_FakeDrv(), tmp_path, recorder=_FakeRecorder()).run(TestCase.model_validate(case))
    assert _FakeRecorder.calls[:2] == ["start", "stop"]
    assert "discard" in _FakeRecorder.calls and "keep" not in _FakeRecorder.calls, (
        "通过的用例：录屏必须删除（磁盘纪律）")


def test_runner_failed_keeps_recording(tmp_path):
    _FakeRecorder.calls = []
    case = json.loads(_case([
        {"id": "s1", "intent": "启动", "action": "launch", "params": {"package": "com.x"}},
        {"id": "s2", "intent": "断言不存在的元素", "action": "wait", "params": {"ms": 5},
         "assertions": [{"type": "exists", "target": {"strategy": "text", "value": "不存在"}}]},
    ]))
    from insta_engine.dsl import TestCase
    run = Runner(_FakeDrv(), tmp_path, recorder=_FakeRecorder()).run(TestCase.model_validate(case))
    assert run.status == "failed"
    assert "keep" in _FakeRecorder.calls and "discard" not in _FakeRecorder.calls, (
        "失败的用例：录屏必须保留进 artifacts/video/")


def test_report_embeds_video_player(tmp_path):
    (tmp_path / "video").mkdir()
    (tmp_path / "video" / "rec_01.mp4").write_bytes(b"\x00\x00")
    run = RunResult(case_id="c", case_name="n", status="failed",
                    step_success_rate=0.0, duration_ms=10, steps=[])
    html = render_html(run, tmp_path).read_text(encoding="utf-8")
    assert '<video controls preload="metadata" src="video/rec_01.mp4"' in html, (
        "有录屏 ⇒ 报告必须相对引用播放器（不内联，不破坏 20MB 单文件预算）")
    # 无录屏 ⇒ 无 video 节
    (tmp_path / "no-video").mkdir(exist_ok=True)
    html2 = render_html(run, tmp_path / "no-video").read_text(encoding="utf-8")
    assert "video/" not in html2
