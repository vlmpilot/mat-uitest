"""`mat verify` 的协议与判定（mat.verify/1）。

给 agent 用的东西，判据必须是**可测的**：verdict 五态、next_actions 的顺序、
证据路径必须是相对路径（绝对路径换台机器就失效），以及"码与产物不一致时如实记 note"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from insta_engine import verify as v
from insta_engine.dsl import TestCase as Case
from insta_engine.runner import Runner, RunResult


def _step(step_id: str, status: str, *, category=None, error=None,
          screenshot=None, screen=None, **extra) -> dict:
    return {"step_id": step_id, "action": "tap", "status": status,
            "category": category, "error": error,
            "screenshot": screenshot, "screen": screen, **extra}


def _payload(status: str = "failed", steps=None, cats=None, **over) -> dict:
    base = {"case_id": "c1", "case_name": "登录冒烟", "status": status,
            "step_success_rate": 0.5, "duration_ms": 1234,
            "failed_categories": cats or {}, "steps": steps or []}
    base.update(over)
    return base


def test_pass_verdict_has_no_next_actions(tmp_path):
    """全通过：ok=true、exit=0、next_actions 为空（别给 agent 多余动作）。"""
    out = v.build_verdict(_payload("passed", [_step("s1", "passed")]), 0, tmp_path)
    assert out["schema"] == "mat.verify/1"
    assert (out["verdict"], out["ok"], out["exit_code"]) == ("pass", True, 0)
    assert out["next_actions"] == []
    assert out["steps"] == {"total": 1, "passed": 1, "failed": 0}


def test_fail_verdict_gives_actions_ordered_by_frequency(tmp_path):
    """失败：分类按频次排，next_actions 第一条就是最该先修的那个。"""
    steps = [_step("s1", "failed", category="locate-fail", error="找不到控件\n第二行"),
             _step("s2", "failed", category="locate-fail", error="找不到控件"),
             _step("s3", "failed", category="assert-fail", error="断言不成立")]
    out = v.build_verdict(_payload(steps=steps,
                                   cats={"locate-fail": 2, "assert-fail": 1}), 2, tmp_path)
    assert (out["verdict"], out["ok"], out["exit_code"]) == ("fail", False, 2)
    assert [a["category"] for a in out["next_actions"]] == ["locate-fail", "assert-fail"]
    assert "控件树" in out["next_actions"][0]["action"]
    # 错误只留首行前 300 字：终端里贴整段堆栈对 agent 是噪声
    assert out["failed_steps"][0]["error"] == "找不到控件"


def test_evidence_paths_are_relative_to_artifacts(tmp_path):
    """证据一律相对产物目录：agent 拿到的路径要能直接拼、换机器仍有效。"""
    steps = [_step("s1", "failed", category="locate-fail",
                   screenshot=str(tmp_path / "screens" / "step_01.png"),
                   screen=str(tmp_path / "screens" / "step_01.json"))]
    out = v.build_verdict(_payload(steps=steps, cats={"locate-fail": 1}), 2, tmp_path)
    ev = out["failed_steps"][0]["evidence"]
    assert ev["screenshot"] == "screens/step_01.png"
    assert ev["screen"] == "screens/step_01.json"


def test_notes_flag_false_red_and_heal(tmp_path):
    """假红与自愈提案必须进 notes——否则 agent 会把它们当真实失败去修。"""
    steps = [_step("s1", "failed", category="env-failure", false_red=True,
                   heal={"original": "a", "match": "b"})]
    out = v.build_verdict(_payload(steps=steps, cats={"env-failure": 1}), 2, tmp_path)
    text = "；".join(out["notes"])
    assert "假红" in text
    assert "自愈" in text


def test_exit_code_mapping_is_the_shared_convention():
    """退出码沿用 mat run 的一套：0/2/3/4，其余一律 error（不假装成 pass）。"""
    assert v.verdict_of(0) == "pass"
    assert v.verdict_of(2) == "fail"
    assert v.verdict_of(3) == "blocked"
    assert v.verdict_of(4) == "incomplete"
    assert v.verdict_of(1) == "error"
    assert v.verdict_of(99) == "error"


def test_render_text_is_short_and_actionable(tmp_path):
    steps = [_step("s1", "failed", category="locate-fail", error="找不到")]
    out = v.build_verdict(_payload(steps=steps, cats={"locate-fail": 1}), 2, tmp_path)
    text = v.render_text(out)
    assert "VERIFY FAIL" in text
    assert "locate-fail" in text
    assert len(text.splitlines()) <= 8


def test_run_verify_refuses_unprovisionable_precondition(tmp_path):
    """前置条件无法置备 ⇒ 退出码 3、不连设备（与 mat run 同一判据，不另写一套）。"""
    case = {"caseId": "c-block", "name": "需要未授予定位权限",
            "target": {"package": "com.x"},
            "preconditions": ["定位权限未授予"],
            "steps": [{"id": "s1", "action": "launch"}]}
    path = tmp_path / "c.json"
    path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    code, out = v.run_verify(tmp_path, path)
    assert code == 3
    assert out["verdict"] == "blocked" and out["ok"] is False
    assert out["blocked_preconditions"] == ["定位权限未授予"]
    assert out["notes"], "拒跑必须给出可行动的说明"


def test_run_json_carries_schema_version(tmp_path):
    """run.json 必须有协议版本：消费方（verify/控制台/CI）要能判字段代际。"""
    class _Drv:
        serial = "stub"
        platform = "android"

    case = Case.model_validate({"caseId": "c1", "name": "n",
                                    "target": {"package": "com.x"},
                                    "steps": [{"id": "s1", "action": "launch"}]})
    runner = Runner(_Drv(), tmp_path)
    runner._persist(case, RunResult(case_id="c1", case_name="n", status="passed"))
    payload = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "mat.run/1"
    assert payload["run"]["case_id"] == "c1", "旧字段一个都不能少"


# -- 并发：连接与执行都必须串行（2026-10-30） --------------------------------


def _trace():
    """记录"同时进行中"的峰值：并发是否正确串行，只有峰值能说明。"""
    import threading

    state = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def enter():
        with guard:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])

    def leave():
        with guard:
            state["now"] -= 1

    return state, enter, leave


def test_connect_driver_is_serialized(monkeypatch):
    """连设备要串行：u2 并发初始化同一台设备会互抢 ATX 服务，失败会被误判成用例问题。

    控制台侧 `runtime.get_driver` 一直有全局锁；CLI/MCP 走的 `_connect_driver` 此前没有。
    """
    import time

    from insta_engine import __main__ as m
    from insta_engine import driver_factory

    state, enter, leave = _trace()

    class Drv:
        serial = "serial-a"
        platform = "android"

    def fake_connect(serial, platform):
        enter()
        try:
            time.sleep(0.05)      # 模拟 adb/u2 真实握手耗时
            return Drv()
        finally:
            leave()

    monkeypatch.setattr(driver_factory, "connect_driver", fake_connect)
    import threading

    threads = [threading.Thread(target=m._connect_driver, args=("serial-a",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] == 1, f"并发连接峰值 {state['peak']}，应为 1"


def test_run_verify_serializes_same_device(monkeypatch, tmp_path):
    """同一台设备上的 verify 必须排队：并发跑会互抢点击，产出的报告看着正常但无效。"""
    import time

    from insta_engine import driver_factory, report
    from insta_engine import runner as runner_mod

    state, enter, leave = _trace()

    class Drv:
        serial = "serial-a"
        platform = "android"

    class FakeRunner:
        def __init__(self, *a, **kw):
            pass

        def run(self, case):
            enter()
            try:
                time.sleep(0.05)
                return runner_mod.RunResult(case_id=case.case_id, case_name=case.name,
                                            status="passed", steps=[])
            finally:
                leave()

    monkeypatch.setattr(driver_factory, "connect_driver", lambda s, p: Drv())
    monkeypatch.setattr(runner_mod, "Runner", FakeRunner)
    monkeypatch.setattr(report, "render_html", lambda run, art: art / "report.html")

    case = {"caseId": "c1", "name": "n", "target": {"package": "com.x"},
            "steps": [{"id": "s1", "action": "launch"}]}
    path = tmp_path / "c.json"
    path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")

    import threading

    out = []
    guard = threading.Lock()

    def worker():
        code, verdict = v.run_verify(tmp_path, path)
        with guard:
            out.append((code, verdict["verdict"]))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out == [(0, "pass")] * 3, out
    assert state["peak"] == 1, f"并发执行峰值 {state['peak']}，应为 1"


# -- 控制台占用守卫（2026-10-31）--------------------------------------------
#
# `device_lock` 是进程内锁，控制台与 verify 是两个进程；控制台在跑套件时 agent
# 再发 verify，两边会同时初始化同一台设备的 u2/ATX 服务——结果"看着正常、实际无效"。
# 跨进程文件锁是后续项，先用一次 HTTP 探测把最常见的撞车挡住。


def test_job_is_busy_conservative_matching():
    """只有双方 serial 都明确且不同才放行；serial 缺失一律当同一台（保守）。"""
    running = {"kind": "run", "status": "running", "serial": "serial-a"}
    assert v._job_is_busy(running, "serial-a") is True
    assert v._job_is_busy(running, None) is True, "任务有 serial 而我们没指定 ⇒ 当同一台"
    assert v._job_is_busy(running, "serial-b") is False, "明确是另一台设备 ⇒ 不冲突"

    no_serial = {"kind": "run", "status": "running", "serial": None}
    assert v._job_is_busy(no_serial, "serial-b") is True, "任务不知道在哪台跑 ⇒ 保守当冲突"

    assert v._job_is_busy({**running, "status": "done"}, "serial-a") is False
    assert v._job_is_busy({**running, "kind": "index"}, "serial-a") is False, "建图谱不抢设备"
    assert v._job_is_busy({**running, "kind": "gen"}, "serial-a") is False, "生成不碰设备"


def test_console_busy_is_skipped_when_no_console(monkeypatch):
    """控制台没起 ⇒ 静默放行（守卫不能变成新的失败源）。"""
    monkeypatch.setenv("MAT_CONSOLE_PORT", "1")   # 1 端口必然连不上
    monkeypatch.delenv("MAT_VERIFY_SKIP_CONSOLE_CHECK", raising=False)
    assert v._console_busy("serial-a") is None


def test_console_busy_respects_skip_switch(monkeypatch):
    """CI 里可能跑着无关的控制台：给一个显式关掉守卫的开关。"""
    monkeypatch.setenv("MAT_VERIFY_SKIP_CONSOLE_CHECK", "1")
    assert v._console_busy("serial-a") is None


def test_run_verify_blocks_when_console_is_running(monkeypatch, tmp_path):
    """控制台在跑 ⇒ 不连设备、直接返回 blocked(3)，并给可行动的提示。"""
    from insta_engine import driver_factory

    monkeypatch.setattr(v, "_console_busy",
                        lambda serial: {"kind": "run", "id": "abcdef123456",
                                        "serial": "serial-a"})

    def must_not_connect(*_a, **_kw):
        raise AssertionError("命中控制台占用时不该去连设备")

    monkeypatch.setattr(driver_factory, "connect_driver", must_not_connect)

    case = {"caseId": "c1", "name": "n", "target": {"package": "com.x"},
            "steps": [{"id": "s1", "action": "launch"}]}
    path = tmp_path / "c.json"
    path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")

    code, out = v.run_verify(tmp_path, path)
    assert code == 3
    assert out["verdict"] == "blocked"
    assert out["blocked_reason"] == "device_busy"
    assert "控制台任务执行中" in " ".join(out["notes"])


def test_precondition_block_carries_its_own_reason(tmp_path):
    """两种 blocked 要能区分：agent 对「等一会儿」和「改用例」的动作完全不同。"""
    case = {"caseId": "c2", "name": "需要未授予定位权限", "target": {"package": "com.x"},
            "preconditions": ["定位权限未授予"], "steps": [{"id": "s1", "action": "launch"}]}
    path = tmp_path / "c2.json"
    path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    code, out = v.run_verify(tmp_path, path)
    assert code == 3 and out["blocked_reason"] == "precondition"


# -- timeout_s(协议 v3 §3.3)-------------------------------------------------

def _write_case(tmp_path, name="c-timeout"):
    """最小可执行用例(launch 一步);超时场景在锁上,不会真跑。"""
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({
        "caseId": "case_t", "name": "超时用例", "target": {"package": "com.x"},
        "steps": [{"id": "s1", "action": "launch", "params": {"package": "com.x"}}],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def test_verify_timeout_returns_error_and_never_connects(tmp_path, monkeypatch):
    """🔴 超时语义三件套：exit=1 + queued=true + notes 说「排队」——且**绝不连设备**
    （连接被调 = AssertionError；与控制台占用守卫同一纪律）。"""
    from insta_engine.web_api.runtime import device_lock

    def _forbidden_connect(*a, **k):
        raise AssertionError("超时分支不允许连接设备")

    monkeypatch.setattr("insta_engine.__main__._connect_driver", _forbidden_connect)
    lock = device_lock("S-busy")
    lock.acquire()                                   # 外部先占住设备锁
    try:
        code, out = v.run_verify(
            tmp_path, _write_case(tmp_path), serial="S-busy", timeout_s=0.2)
    finally:
        lock.release()
    assert code == 1 and out["verdict"] == "error"
    assert out["queued"] is True and out["wait_ms"] >= 150
    assert any("排队" in n for n in out["notes"]), "notes 必须说这是排队不是用例失败"


def test_verify_idle_lock_reports_queued_false(tmp_path, monkeypatch):
    """锁空闲：queued=false、wait_ms=0——没排队就别谎报排过队。

    connect 打桩为 None（无设备路径）——这条测的是锁语义,不是连接。"""
    monkeypatch.setattr("insta_engine.__main__._connect_driver", lambda *a, **k: None)
    code, out = v.run_verify(tmp_path, _write_case(tmp_path), serial="S-free",
                             timeout_s=5)
    assert out["queued"] is False and out["wait_ms"] == 0


def test_verify_timeout_zero_is_immediate_error(tmp_path, monkeypatch):
    """timeout_s=0（探测语义）：拿不到锁立刻失败,wait_ms≈0,同样不连设备。"""
    monkeypatch.setattr("insta_engine.__main__._connect_driver",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该连")))
    from insta_engine.web_api.runtime import device_lock
    lock = device_lock("S-zero")
    lock.acquire()
    try:
        code, out = v.run_verify(tmp_path, _write_case(tmp_path),
                                 serial="S-zero", timeout_s=0)
    finally:
        lock.release()
    assert code == 1 and out["queued"] is True and out["wait_ms"] == 0


# -- 装包(协议 v3 §3.1)-------------------------------------------------------

def _mk_apk(tmp_path, rel="build/app-debug.apk"):
    """工作区内造一个假 APK(内容随意,sha256 由函数算)。"""
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"PK\x03\x04 fake apk")
    return p


class _FakeAdb:
    """可编排的 adb 桩:记录调用,按脚本返回。"""

    def __init__(self, install_ok=True, installed_pkgs=(), new_pkg="com.real.app"):
        self.calls = []
        self.install_ok = install_ok
        self.installed_pkgs = set(installed_pkgs)
        self.new_pkg = new_pkg

    def __call__(self, serial, *args, timeout=15):
        self.calls.append((serial, *args))
        joined = " ".join(args)
        if joined.startswith("install "):
            if not self.install_ok:
                return None
            self.installed_pkgs.add(self.new_pkg)
            return "Success"
        if joined.startswith("shell pm list packages"):
            return "".join(f"package:{p}\n" for p in sorted(self.installed_pkgs))
        if joined.startswith("shell pm path"):
            return f"package:{args[-1]}\n" if args[-1] in self.installed_pkgs else None
        if joined.startswith("uninstall"):
            self.installed_pkgs.discard(args[-1])
            return "Success"
        return ""


def test_install_apk_rejects_outside_workspace(tmp_path):
    from insta_engine import verify as v

    fake = _FakeAdb()
    outside = tmp_path.parent / "evil.apk"
    outside.write_bytes(b"PK")
    with pytest.raises(ValueError, match="工作区外"):
        v.install_apk(tmp_path, fake, "serial-a", outside, "com.x")
    assert fake.calls == [], "拒绝路径不得执行任何 adb"


def test_install_apk_rejects_non_apk(tmp_path):
    from insta_engine import verify as v

    bad = tmp_path / "build" / "app.zip"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"x")
    with pytest.raises(ValueError, match="\\.apk"):
        v.install_apk(tmp_path, _FakeAdb(), "serial-a", bad, "com.x")


def test_install_apk_success_and_audit(tmp_path):
    from insta_engine import verify as v

    apk = _mk_apk(tmp_path)
    fake = _FakeAdb(installed_pkgs={"com.other"})
    out = v.install_apk(tmp_path, fake, "serial-a", apk, "com.real.app")
    assert out["ok"] is True
    audit = tmp_path / "artifacts" / "verify" / "_installs.jsonl"
    rows = [json.loads(line) for line in audit.read_text().splitlines()]
    assert rows[-1]["result"] == "ok" and rows[-1]["sha256"]


def test_install_apk_mismatch_uninstalls_and_fails(tmp_path):
    """装完发现不是期望的包 ⇒ 卸载回退 + ok=false——错误包不留存。

    这条走的是**装前读不出包名**（假 APK）的兜底路径；装前能读出时由
    `test_install_apk_rejects_wrong_package_before_touching_device` 守住。
    """
    from insta_engine import verify as v

    apk = _mk_apk(tmp_path)
    fake = _FakeAdb(install_ok=True, installed_pkgs={"com.other"})
    out = v.install_apk(tmp_path, fake, "serial-a", apk, "com.expected.app")
    assert out["ok"] is False
    assert "com.expected.app" in out["error"] and "com.real.app" in out["error"]
    uninstalls = [c for c in fake.calls if c[:2] == ("serial-a", "uninstall") or
                  (len(c) > 1 and c[0] == "uninstall")]
    assert uninstalls, "装错的包必须卸载回退"


def test_install_apk_install_failure(tmp_path):
    from insta_engine import verify as v

    apk = _mk_apk(tmp_path)
    fake = _FakeAdb(install_ok=False)
    out = v.install_apk(tmp_path, fake, "serial-a", apk, "com.real.app")
    assert out["ok"] is False and "安装失败" in out["error"]


def test_console_busy_sends_key_when_configured(monkeypatch):
    """--share 下 /api/jobs 要口令(X-MAT-Key)——MAT_CONSOLE_KEY 让守卫在多人场景继续工作。"""
    import urllib.request

    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"jobs": []}'

    class _Opener:
        def __init__(self, handler):
            pass

        def open(self, req, timeout=None):
            captured["headers"] = dict(req.header_items())
            return _Resp()

    monkeypatch.setenv("MAT_CONSOLE_KEY", "team-secret")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: _Opener(h))
    assert v._console_busy("serial-a") is None
    headers_lc = {k.lower(): val for k, val in captured["headers"].items()}
    assert headers_lc.get("x-mat-key") == "team-secret", \
        "share 模式下守卫必须带口令探测,否则在最需要它的场景失效"


def test_busy_blocked_verdict_carries_queued_fields(monkeypatch):
    """🔴 评审 P0-1 回归锁:busy 分支的 verdict 必须带 queued/wait_ms——
    此前缺字段,控制台一忙 test_verify 全量就 KeyError(flaky 实锤)。"""
    monkeypatch.setattr(v, "_console_busy",
                        lambda serial: {"id": "j1", "kind": "run", "status": "running",
                                        "serial": "S-busy"})
    import tempfile
    from pathlib import Path as _P
    case_path = _P(tempfile.mkdtemp()) / "c.json"
    case_path.write_text(json.dumps({
        "caseId": "case_busy", "name": "busy", "target": {"package": "com.x"},
        "steps": [{"id": "s1", "action": "launch", "params": {"package": "com.x"}}],
    }, ensure_ascii=False), encoding="utf-8")
    code, out = v.run_verify(case_path.parent, case_path, serial="S-busy")
    assert out["queued"] is False and out["wait_ms"] == 0, "没排队就如实 false/0"
    assert out["blocked_reason"] == "device_busy"


# -- 协议 schema(协议 v4 §4.2)-------------------------------------------------

def test_verdict_model_rejects_unknown_keys():
    """extra=forbid:未知键必须报错——漏枚举的动态键在生成时就炸,不是消费方炸。"""
    from insta_engine.verify_models import VerifyVerdict

    with pytest.raises(Exception, match="unknown_key"):
        VerifyVerdict.model_validate({"verdict": "pass", "ok": True, "exit_code": 0,
                                      "unknown_key": 1})


def test_built_verdict_fits_model_every_branch(tmp_path):
    """build_verdict 三条 return 路径的产出都必须能过模型(extra=forbid)——
    漏枚举的动态键在这里炸(评审 P0-1 的协议化守护)。"""
    from insta_engine.verify_models import model_from

    v1 = v.build_verdict({"case_id": "c1", "case_name": "n", "status": "skipped",
                          "steps": [], "preconditions": ["定位权限未授予"]},
                         v.EXIT_BLOCKED, tmp_path)
    model_from(v1)  # blocked 分支
    v2 = v.build_verdict({"case_id": "c1", "case_name": "n", "status": "error",
                          "steps": []}, v.EXIT_ERROR, tmp_path)
    model_from(v2)  # error 分支
    v3 = v.build_verdict({"case_id": "c1", "case_name": "n", "status": "passed",
                          "step_success_rate": 1.0, "steps": [{"id": "s1",
                          "status": "passed", "action": "launch"}]}, v.EXIT_PASS,
                         tmp_path)
    model_from(v3)  # pass 分支
    for verdict in (v1, v2, v3):
        assert "queued" in verdict and "wait_ms" in verdict, "字段必在(单一出口)"


# -- 子集批量(§4.1) -----------------------------------------------------------

def _mk_case_file(tmp_path, name: str, case_id: str, *, smoke: bool = True,
                  package: str = "com.real.app"):
    """造一条最小用例文件；smoke 走 meta.tags（不看优先级，避免用例质量闸干扰）。

    `package=""` 表示**不写 target.package**（老用例/手工用例的真实形态之一）。
    """
    target = {"package": package} if package else {}
    p = tmp_path / "cases" / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "caseId": case_id, "name": name, "target": target,
        "meta": {"tags": ["smoke"] if smoke else []},
        "steps": [{"id": "s1", "action": "launch", "params": {"package": package}}],
    }, ensure_ascii=False), encoding="utf-8")
    return p


def test_run_subset_install_failure_runs_nothing(tmp_path, monkeypatch):
    """装包在首条之前装一次：装不上 ⇒ 整体 error、exit=1、**一条都不跑**。

    同时是 F821 的回归锁——子集路径曾调用未定义的 `install_apk()`（定义在 verify.py），
    只在「带 --apk 的批量验证」这一组合下才 NameError，单条路径的测试全绿也发现不了。
    """
    from insta_engine import verify_subset as vs

    c1 = _mk_case_file(tmp_path, "a", "case_a")
    c2 = _mk_case_file(tmp_path, "b", "case_b")
    ran: list = []
    monkeypatch.setattr(v, "install_apk",
                        lambda *a, **k: {"ok": False, "error": "签名不符"})
    monkeypatch.setattr(v, "run_verify",
                        lambda *a, **k: ran.append(a) or (0, {"verdict": "pass"}))

    out = vs.run_subset(tmp_path, [c1, c2], mode="smoke", package="com.real.app",
                        apk=_mk_apk(tmp_path))

    assert out["verdict"] == "error" and out["exit_code"] == 1
    assert ran == [], "装包失败就不该跑任何一条（否则报的是别的问题，误导 agent）"
    assert out["selection"]["install_error"] == "签名不符"
    assert out["install"]["ok"] is False


def test_run_subset_aggregates_pass(tmp_path, monkeypatch):
    """选中若干条且全过 ⇒ 聚合 pass / exit=0；summary 计数与 selected 对齐。"""
    from insta_engine import verify_subset as vs

    c1 = _mk_case_file(tmp_path, "a", "case_a")
    c2 = _mk_case_file(tmp_path, "b", "case_b")
    monkeypatch.setattr(v, "run_verify",
                        lambda root, path, **k: (0, {
                            "case_id": path.stem, "verdict": "pass", "duration_ms": 12,
                            "failed_categories": {}, "artifacts": {}}))

    out = vs.run_subset(tmp_path, [c1, c2], mode="smoke", package="com.real.app")

    assert out["verdict"] == "pass" and out["exit_code"] == 0
    assert out["summary"]["selected"] == 2 and out["summary"]["passed"] == 2
    assert [c["case_id"] for c in out["cases"]] == ["a", "b"]


def test_run_subset_empty_selection_is_blocked(tmp_path):
    """子集为空 ⇒ blocked/exit=3，绝不能把「什么都没跑」当成「全过」。"""
    from insta_engine import verify_subset as vs

    c1 = _mk_case_file(tmp_path, "a", "case_a", smoke=False)
    out = vs.run_subset(tmp_path, [c1], mode="smoke", package="com.real.app")

    assert out["verdict"] == "blocked" and out["exit_code"] == 3
    assert out["summary"]["selected"] == 0


def test_verify_cli_subset_does_not_require_case(tmp_path):
    """`mat verify --subset smoke` 必须能在**没有位置参数**时跑起来。

    回归锁：`case` 曾是位置必填，argparse 在进 cmd_verify 之前就报
    "the following arguments are required: case" ⇒ 批量子集功能对用户整体不可见
    （单测直调 run_subset 全绿，命令行根本到不了）。
    """
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "insta_engine", "verify", "--subset", "smoke",
         "--package", "com.demo.app", "--json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert "required: case" not in (r.stderr or ""), f"argparse 仍要求位置参数: {r.stderr}"
    assert r.returncode == 3, f"空工作区 ⇒ 空子集 ⇒ blocked/3；实际 {r.returncode}: {r.stderr}"
    assert json.loads(r.stdout)["verdict"] == "blocked"


def test_verify_cli_subset_needs_package(tmp_path):
    """`--subset` 缺 `--package` ⇒ 退出 1 并说清原因（别抛 ValueError 栈回溯）。"""
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "insta_engine", "verify", "--subset", "smoke", "--json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1
    assert "--package" in (r.stderr or ""), r.stderr
    assert "Traceback" not in (r.stderr or ""), "给 agent 的是结论，不是栈回溯"


def test_verify_cli_needs_case_or_subset(tmp_path):
    """两个都不给 ⇒ 明确报错并退出 1（不要落到"跑 None"的路径上）。"""
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "insta_engine", "verify"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1
    assert "--subset" in (r.stderr or "")


def test_verify_json_stdout_is_pure_json_without_device(tmp_path):
    """`--json` 的 stdout 必须能被 `json.loads` **整段**吃掉。

    回归锁：设备连接失败时排查提示曾走 print()→stdout，JSON 前面混三行人读文本。
    人肉看 `| tail` 一切正常，脚本 `json.loads(stdout)` 却整段崩——最容易被漏的一类坏。
    用不存在的 serial 触发，不依赖本机有没有插设备。
    """
    import subprocess
    import sys

    p = _mk_case_file(tmp_path, "c", "case_c")
    r = subprocess.run(
        [sys.executable, "-m", "insta_engine", "verify", str(p), "--json",
         "--no-quality-gate", "--serial", "MAT-NO-SUCH-DEVICE"],
        cwd=tmp_path, capture_output=True, text=True, timeout=180)

    assert r.returncode == 1, f"连不上设备 ⇒ error/1；实际 {r.returncode}\n{r.stderr}"
    data = json.loads(r.stdout)  # 整段解析：混一个字符都会抛
    assert data["verdict"] == "error" and data["schema"] == "mat.verify/1"
    assert data["next_actions"], "设备连不上却没给下一步动作——agent 只能自己猜"
    assert data["next_actions"][0]["category"] == "env-failure"
    assert "连接设备失败" in r.stderr, "排查提示应走 stderr"


# -- 装前包名校验（apkmeta）---------------------------------------------------

_REAL_APK = Path(__file__).resolve().parents[2] / "demo" / "apk" / "demo-app-good.apk"


@pytest.mark.skipif(not _REAL_APK.exists(), reason="演示 APK 不在（公开导出树里没有）")
def test_read_package_name_from_real_apk():
    """真 APK 的清单是二进制 AXML，纯标准库要能读出 package 属性。"""
    from insta_engine.apkmeta import read_package_name

    assert read_package_name(_REAL_APK) == "com.insta.demo"


def test_read_package_name_returns_none_for_garbage(tmp_path):
    """读不出就返回 None（不能抛）——调用方据此退回装后校验。"""
    from insta_engine.apkmeta import read_package_name

    bad = tmp_path / "bad.apk"
    bad.write_bytes(b"PK\x03\x04 not a real apk")
    assert read_package_name(bad) is None
    assert read_package_name(tmp_path / "missing.apk") is None


@pytest.mark.skipif(not _REAL_APK.exists(), reason="演示 APK 不在（公开导出树里没有）")
def test_install_apk_rejects_wrong_package_before_touching_device(tmp_path):
    """包名不符必须**装前**拒绝：adb 一次都不该被调用。

    2026-10-31 真机实测的旧行为：只靠装后 `pm path <期望>` 判断，在「APK 实际包名
    不同、且该包本来就装着」时覆盖安装不新增包名 ⇒ 报"装到的是 未知"，而且设备
    已经被装过一次再卸掉。装前读清单就没有这个窗口。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    apk = ws / "app.apk"
    apk.write_bytes(_REAL_APK.read_bytes())
    fake = _FakeAdb()

    out = v.install_apk(ws, fake, "serial-a", apk, "com.wrong.pkg")

    assert out["ok"] is False
    assert "com.insta.demo" in out["error"], "错误信息必须给出 APK 的真实包名"
    assert "com.wrong.pkg" in out["error"]
    assert fake.calls == [], f"装前就该拒绝，不该碰设备：{fake.calls}"


def test_install_failure_action_is_install_not_env(tmp_path, monkeypatch):
    """装包失败 ⇒ next_actions 分类是 `install`，不是通用的 `env-failure`。

    2026-10-31 真机实测：`--package` 写错时 agent 被告知"先修环境（设备断开/App 不在
    前台/锁屏）"——用例一条都没跑过，让它去查设备是把排查方向带偏。
    """
    monkeypatch.setattr(v, "install_apk",
                        lambda *a, **k: {"ok": False, "error": "包名不符", "pkg": "x"})
    p = _mk_case_file(tmp_path, "c", "case_c")

    code, verdict = v.run_verify(tmp_path, p, serial="serial-a", quality_gate=False,
                                 apk=_mk_apk(tmp_path), package="com.real.app")

    assert code == 1 and verdict["verdict"] == "error"
    assert verdict["next_actions"], "装包失败必须给下一步"
    assert verdict["next_actions"][0]["category"] == "install"
    assert "target.package" in verdict["next_actions"][0]["action"]


# -- 包名过滤（2026-10-31 评审 P0-1）--------------------------------------------

def test_select_subset_filters_by_package(tmp_path):
    """`--package` 必须过滤候选：此前候选池是全部 cases，要验 A 却跑 B 的用例。

    实测（2026-10-31，主仓 cases/ 28 条）：`--package com.example.news` 选出 2 条，
    两条都是 com.example.shop。配合 `--apk` 更糟——装 A 的包跑 B 的用例。
    """
    from insta_engine import verify_subset as vs

    a = _mk_case_file(tmp_path, "tx", "case_tx", package="com.example.news")
    b = _mk_case_file(tmp_path, "mt", "case_mt", package="com.example.shop")
    c = _mk_case_file(tmp_path, "nopkg", "case_nopkg", package="")
    cases = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in (a, b, c)]

    sel, info = vs.select_subset(tmp_path, cases, "smoke", "com.example.news")

    assert [c.get("caseId") for _, c in sel] == ["case_tx"]
    assert info["considered"] == 1, "considered 应是「该包的候选数」，不是全部文件数"
    assert info["skipped_other_package"] == 1
    assert info["skipped_no_package"] == 1


def test_run_subset_runs_only_matching_package(tmp_path, monkeypatch):
    """端到端：同目录两个 App 的用例，只跑 --package 指定的那个。"""
    from insta_engine import verify_subset as vs

    a = _mk_case_file(tmp_path, "tx", "case_tx", package="com.example.news")
    b = _mk_case_file(tmp_path, "mt", "case_mt", package="com.example.shop")
    ran: list[str] = []
    monkeypatch.setattr(v, "run_verify",
                        lambda root, path, **k: ran.append(path.stem) or (0, {
                            "case_id": path.stem, "verdict": "pass", "duration_ms": 1,
                            "failed_categories": {}, "artifacts": {}}))

    out = vs.run_subset(tmp_path, [a, b], mode="smoke", package="com.example.news")

    assert ran == ["tx"], f"只该跑包名匹配的那条，实际 {ran}"
    assert out["summary"]["selected"] == 1
    assert out["selection"]["skipped_other_package"] == 1


def test_empty_selection_explains_why_when_filtered(tmp_path):
    """空子集不能只说「不代表通过」——要说清是被包名过滤掉的。"""
    from insta_engine import verify_subset as vs

    b = _mk_case_file(tmp_path, "mt", "case_mt", package="com.example.shop")
    out = vs.run_subset(tmp_path, [b], mode="smoke", package="com.example.news")

    assert out["verdict"] == "blocked" and out["exit_code"] == 3
    note = " ".join(out["notes"])
    assert "属于其它 App" in note and "target.package" in note


def test_empty_selection_distinguishes_no_candidate_from_untagged(tmp_path):
    """「该包没候选」与「有候选但没标 smoke」必须写成两句不同的话。

    回归锁（2026-10-31 复核）：`_empty_note` 原来只要 `skipped_other_package > 0`
    就报「该包候选 0 条……用 --package 选对包」。而主仓 28 条用例分属 6 个包，
    这个计数**几乎恒 >0** ⇒ 实测 `--subset smoke --package <A 包>`
    （该包 10 条候选、0 条标 smoke）被写成「候选 0 条 + 选对包」——
    包名本来就是对的，这句话把 agent 指向了错误的修法。
    """
    from insta_engine import verify_subset as vs

    a = _mk_case_file(tmp_path, "tx1", "case_tx1", smoke=False, package="com.example.news")
    b = _mk_case_file(tmp_path, "mt", "case_mt", smoke=True, package="com.example.shop")
    out = vs.run_subset(tmp_path, [a, b], mode="smoke", package="com.example.news")

    assert out["verdict"] == "blocked" and out["exit_code"] == 3
    assert out["selection"]["considered"] == 1
    note = " ".join(out["notes"])
    assert "该包候选 0 条" not in note, "该包有候选，不能报「候选 0 条」"
    assert "1 条候选都不满足" in note
    assert "属于其它 App" in note, "被包名排除的条数仍要写，排查时要用"
    assert "meta.tags" in note, "要说明 smoke 是标签命中，别让 agent 去改 --package"


# -- 批量装包异常（评审 P0-2）--------------------------------------------------

def test_run_subset_install_path_error_is_json_not_traceback(tmp_path):
    """批量模式下装包参数不合法 ⇒ 结论 + exit=1，**不能是栈回溯**。

    回归锁：`install_apk` 对「工作区外路径 / 非 .apk」抛 ValueError，单条模式一直
    捕获、批量模式漏了 ⇒ 用户看到 traceback 且没有 JSON（实测 `--apk /etc/hosts`）。
    """
    from insta_engine import verify_subset as vs

    c = _mk_case_file(tmp_path, "a", "case_a")
    out = vs.run_subset(tmp_path, [c], mode="smoke", package="com.real.app",
                        apk=Path("/etc/hosts"))

    assert out["verdict"] == "error" and out["exit_code"] == 1
    assert "工作区外" in out["install"]["error"]
    assert out["next_actions"][0]["category"] == "install"
    assert out["summary"]["selected"] == 0, "一条都没跑，不能写得像跑过"
    assert "一条都没跑" in " ".join(out["notes"])


# -- 聚合的 next_actions 与 mode（评审 P2-c/P2-d）-------------------------------

def test_aggregate_carries_next_actions_and_subset_mode():
    """批量模式也要给「下一步修什么」，且 `mode` 是契约里的 `subset`。"""
    from insta_engine import verify_subset as vs

    results = [
        {"case_id": "a", "verdict": "fail", "failed_categories": {"locate-fail": 2},
         "duration_ms": 1, "artifacts": {}},
        {"case_id": "b", "verdict": "fail", "failed_categories": {"locate-fail": 1,
                                                                  "assert-fail": 1},
         "duration_ms": 1, "artifacts": {}},
        {"case_id": "c", "verdict": "blocked", "blocked_reason": "device_busy",
         "failed_categories": {}, "duration_ms": 0, "artifacts": {}},
    ]
    out = vs.aggregate("smoke", results, {"mode": "smoke", "considered": 3, "selected": 3})

    assert out["mode"] == "subset", "方案 §4.1 的契约是 subset；子模式在 selection.mode"
    cats = [a["category"] for a in out["next_actions"]]
    assert cats[0] == "locate-fail", f"按频次降序，实际 {cats}"
    # blocked_reason 是 device_busy（协议字段，下划线），动作分类是 device-busy（连字符）。
    # 两者混用会静默落到 unknown 动作——所以这里断言**精确**词表，不留 or。
    assert set(cats) == {"locate-fail", "assert-fail", "device-busy"}, f"未知分类：{cats}"
    top = out["next_actions"][0]
    assert top["count"] == 3 and top["cases"] == ["a", "b"]
    assert top["action"], "动作不能为空——agent 拿到的必须是「做什么」"
    # 聚合产物同样要能通过我们发布的 schema（P1-3 的同类风险：手工拼的 dict 多了键）
    from insta_engine.verify_models import VerifyVerdict

    VerifyVerdict.model_validate(out)


def test_history_excludes_date_named_batch_runs(tmp_path):
    """评测跑批不能驱动 last-failed：`eval/` 与日期命名目录都不进索引。

    实测（2026-10-31）：2479 份 run.json 里只有 64 份在 `eval/`，日期命名的另有 798
    份——只排 `eval/` 等于只挡住 3%。
    """
    from insta_engine import verify_subset as vs

    def mk(rel: str, status: str):
        d = tmp_path / "artifacts" / rel
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps(
            {"case": {"caseId": "c1"}, "run": {"status": status},
             "finished_at": "2026-10-31T10:00:00Z"}), encoding="utf-8")

    mk("登录-20261031-100000", "failed")          # 日常：要进索引
    mk("eval/x-20261031-110000", "failed")        # 评测：不进
    mk("2026-10-25-d3-r1/x-20261031-120000", "failed")  # 日期命名跑批：不进
    mk("m1-stat/x-20261031-130000", "failed")     # 未按约定的跑批：仍会进（已知边界）

    hist = vs.build_history(tmp_path, force=True)

    paths = [e["run_json"] for e in hist["c1"]]
    assert len(paths) == 2, f"应只剩「日常」与「未按约定的 m1-stat」两条，实际 {paths}"
    assert {e["status"] for e in hist["c1"]} == {"failed"}
    assert all("eval/" not in p and "/2026-10-25-d3-r1/" not in p for p in paths)
    assert any("登录-20261031-100000" in p for p in paths), "日常 run 必须留在索引里"


# -- 判定落盘（2026-11-01：控制台/复盘要靠它，stdout 只有一次机会）-----------------

def test_verify_persists_verdict_next_to_run(tmp_path, monkeypatch):
    """`run_verify` 必须把判定写成 `<产物目录>/verify.json`，且内容 == 返回的 verdict。"""
    from insta_engine import verify as v

    art = tmp_path / "art" / "case-20261101-000000"
    art.mkdir(parents=True)
    canned = {"schema": "mat.verify/1", "verdict": "fail", "ok": False, "exit_code": 2,
              "case_id": "c1", "next_actions": [{"category": "locate-fail", "action": "x"}],
              "artifacts": {"dir": str(art), "run_json": str(art / "run.json")}}
    monkeypatch.setattr(v, "_run_verify", lambda *a, **k: (2, dict(canned)))

    code, verdict = v.run_verify(tmp_path, tmp_path / "case.json")

    assert code == 2 and verdict["verdict"] == "fail"
    saved = json.loads((art / "verify.json").read_text(encoding="utf-8"))
    assert saved == verdict, "落盘内容必须与返回给 agent 的完全一致（两套就会分叉）"


def test_verify_does_not_fail_when_artifacts_unwritable(tmp_path, monkeypatch):
    """落盘失败不能反过来影响判定——附加价值不许变成新的失败点。"""
    from insta_engine import verify as v

    canned = {"schema": "mat.verify/1", "verdict": "pass", "ok": True, "exit_code": 0,
              "artifacts": {"dir": "/proc/definitely-not-writable"}}
    monkeypatch.setattr(v, "_run_verify", lambda *a, **k: (0, dict(canned)))

    code, verdict = v.run_verify(tmp_path, tmp_path / "case.json")
    assert code == 0 and verdict["verdict"] == "pass"


def test_subset_persists_aggregate(tmp_path, monkeypatch):
    """批量判定也要落盘（`artifacts/verify/_last_subset.json`）——整批过没过看这份。"""
    from insta_engine import verify_subset as vs

    c = _mk_case_file(tmp_path, "a", "case_a")
    monkeypatch.setattr(v, "run_verify", lambda *a, **k: (0, {
        "case_id": "case_a", "verdict": "pass", "duration_ms": 1,
        "failed_categories": {}, "artifacts": {}}))

    out = vs.run_subset(tmp_path, [c], mode="smoke", package="com.real.app")

    saved = json.loads((tmp_path / "artifacts" / "verify" / "_last_subset.json")
                       .read_text(encoding="utf-8"))
    assert saved["verdict"] == out["verdict"] == "pass"
    assert saved["summary"] == out["summary"]
