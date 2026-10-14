"""doctor 检查函数库单测(F2):结构与纪律,不断言环境相关的具体值。

doctor 是「检查逻辑与展示分离」的产物(CLI 与 /api/doctor 同一份数据);
这里守住的是结构契约:关键 4 项的次序(CLI「前 4 项」结论依据)、iOS 组
不计入退出码、wda-server 的降级态、以及 status 三层语义的完备性。
"""

from __future__ import annotations

from insta_engine import doctor


def test_android_checks_structure():
    checks = doctor.android_checks()
    ids = [c.id for c in checks]
    # 关键 4 项的次序是 CLI「前 4 项」结论与退出码的依据,不能动
    assert ids[:4] == ["python", "adb", "devices", "u2"]
    assert all(c.critical for c in checks[:4])
    assert all(c.status in ("ok", "warn", "fail") for c in checks)
    # 提示级检查(可选能力)缺失必须是 warn,不得升格为 fail
    # （scrcpy 已下架,发起人 2026-10-12:可选能力易被误读为缺失告警,后续需要再恢复）
    for cid in ("notify", "vl", "gen"):
        c = next(c for c in checks if c.id == cid)
        assert not c.critical
        if not c.ok:
            assert c.status == "warn", f"{cid} 缺失应为 warn"


def test_ios_checks_structure():
    checks = doctor.ios_checks()
    # usbmux 已隐藏（发起人 2026-10-12）：iOS 真机链路未支持前不展示该检查项
    assert {c.id for c in checks} == {"wda-lib", "simctl", "simulators", "wda-server"}
    # iOS 全组是可选能力:不影响「能否开测」结论(与 CLI 退出码口径一致)
    assert all(not c.critical for c in checks)


def test_run_merges_groups():
    assert len(doctor.run("all")) == len(doctor.android_checks()) + len(doctor.ios_checks())
    assert len(doctor.run("android")) == len(doctor.android_checks())
    assert len(doctor.run("ios")) == len(doctor.ios_checks())


def test_wda_server_downgrades_without_lib(monkeypatch):
    """wda-lib 未装 ⇒ wda-server 呈 warn(诚实降级,不是一片红)——设计表 3-2 注。"""
    monkeypatch.setattr(doctor, "_wda_installed", lambda: False)
    monkeypatch.setattr(doctor, "_wda_status_ok", lambda: False)
    checks = {c.id: c for c in doctor.ios_checks()}
    assert checks["wda-lib"].status == "warn"
    assert checks["wda-server"].status == "warn"


def test_wda_server_fails_when_lib_present(monkeypatch):
    """装了库但服务没起 = 想用 iOS 而当前不可用 ⇒ fail(显式覆盖 status)。"""
    monkeypatch.setattr(doctor, "_wda_installed", lambda: True)
    monkeypatch.setattr(doctor, "_wda_status_ok", lambda: False)
    checks = {c.id: c for c in doctor.ios_checks()}
    assert checks["wda-server"].status == "fail"


def test_check_status_inference():
    ok = doctor.Check(id="x", label="x", ok=True, critical=True)
    crit_fail = doctor.Check(id="y", label="y", ok=False, critical=True)
    opt_fail = doctor.Check(id="z", label="z", ok=False)
    assert (ok.status, crit_fail.status, opt_fail.status) == ("ok", "fail", "warn")
    # as_dict 携带全部字段(配置台渲染需要 status/detail/hint)
    d = opt_fail.as_dict()
    assert d["id"] == "z" and d["status"] == "warn" and "hint" in d and "detail" in d
