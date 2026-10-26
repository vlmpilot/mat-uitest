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
    assert rec is None and "公开的录制 API" in (reason or ""), (
        "iOS 真机录屏明确不做（诚实降级），文案须说清原因")


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


# -- 段轮转竞态（P1-3，2026-10-24）--------------------------------------------

def test_rotate_watch_rechecks_stopping_after_sleep(tmp_path, monkeypatch):
    """P1-3 回归：段到点自退与 stop() 并发——sleep 窗口内停止位已置 ⇒ 不得起新段。

    此前 11 条录屏测试全传 rotate_watch=False，轮转线程零覆盖；实际竞态是
    watch 线程 sleep(0.5) 期间 stop() 完成了 kill+pull，醒来再 _start_segment()
    ⇒ 新段不被 kill、不被 pull（设备端进程/文件泄漏至多 180s）。
    """
    from insta_engine import recording

    rec = Recorder("android", "SER", tmp_path / "rec", rotate_watch=True)
    rec._started = True
    rec._seg = 1

    class _DeadProc:  # 模拟 180s 到点自退：wait() 立即返回
        def wait(self, *a, **k):
            return 0

    rec._proc = _DeadProc()

    def _sleep_raced(_s):
        rec._stopping = True  # 竞态时序：stop() 恰落在 watch 线程的 sleep 窗口内

    monkeypatch.setattr(recording.time, "sleep", _sleep_raced)
    started: list[int] = []
    monkeypatch.setattr(rec, "_start_segment", lambda: started.append(1))

    rec._rotate_watch()  # 主线程直跑（确定性），不再依赖守护线程时序

    assert started == [], "stop() 置位后轮转线程不得再起新段"
    assert rec._stopping is True


def test_start_segment_entry_refuses_after_stop(tmp_path, monkeypatch):
    """P1-3 入口兜底：_stopping 已置位时 _start_segment 必须是空操作（不 Popen）。"""
    from insta_engine import recording

    rec = Recorder("android", "SER", tmp_path / "rec", mode="screenrecord")
    rec._stopping = True

    def _no_pop(*_a, **_k):
        raise AssertionError("stop() 后不得再启动录制子进程")

    monkeypatch.setattr(recording.subprocess, "Popen", _no_pop)
    rec._start_segment()
    assert rec._seg == 0, "入口检查必须发生在计数自增之前"


# -- 数据驱动逐组录屏（P1-5，2026-10-24）--------------------------------------------

def test_run_datasets_recorder_factory_per_group(tmp_path):
    """P1-5 回归：--record 不再在数据驱动路径被静默丢弃——recorder_factory
    每组各建一个 Recorder（一次性对象），各组拿到**自己的**产物目录，
    start/stop 逐组配对；run_datasets 结束后还原 runner 原始 recorder。"""
    from insta_engine.dsl import TestCase

    class _GroupRecorder:
        def __init__(self, art_dir):
            self.dir = art_dir
            self.calls: list[str] = []

        def start(self):
            self.calls.append("start")

        def stop(self):
            self.calls.append("stop")
            return []

        def keep(self, files, artifacts_dir):
            self.calls.append("keep")

        def discard(self, files):
            self.calls.append("discard")

    made: list[_GroupRecorder] = []

    def _factory(art_dir):
        rec = _GroupRecorder(Path(art_dir))
        made.append(rec)
        return rec

    case = TestCase.model_validate({
        "caseId": "case_ds", "name": "数据驱动录屏", "target": {"package": "com.x"},
        "steps": [{"id": "s1", "action": "wait", "params": {"ms": 5}}],
        "datasets": [{"name": "组A", "vars": {}}, {"name": "组B", "vars": {}}],
    })
    runner = Runner(_FakeDrv(), tmp_path)
    sentinel = object()
    runner.recorder = sentinel
    results = runner.run_datasets(case, tmp_path, recorder_factory=_factory)

    assert len(results) == 2 and len(made) == 2, "每组必须各建一个 recorder"
    assert [r.calls[:2] for r in made] == [["start", "stop"], ["start", "stop"]], \
        "各组录屏 start/stop 必须逐组配对"
    assert {r.dir.name for r in made} == {"组A", "组B"}, "工厂收到的必须是本组产物目录"
    assert runner.recorder is sentinel, "run_datasets 结束后必须还原原始 recorder"


def test_run_datasets_without_factory_keeps_recorder_none(tmp_path):
    """不传工厂时行为与从前一致（recorder=None，不录屏）——向后兼容。"""
    from insta_engine.dsl import TestCase

    case = TestCase.model_validate({
        "caseId": "case_ds2", "name": "无录屏", "target": {"package": "com.x"},
        "steps": [{"id": "s1", "action": "wait", "params": {"ms": 5}}],
        "datasets": [{"name": "组A", "vars": {}}],
    })
    runner = Runner(_FakeDrv(), tmp_path)
    results = runner.run_datasets(case, tmp_path)
    assert len(results) == 1 and runner.recorder is None
