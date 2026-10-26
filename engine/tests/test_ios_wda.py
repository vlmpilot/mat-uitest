"""iOS 模拟器排序 / boot 带起 WDA / WDA 一键启动（发起人 2026-10-12 三问）。

不打真模拟器：simctl 与 WDA 启动全部 monkeypatch——验证的是排序口径、
boot→WDA 的接线、doctor 修复动作白名单与失败语义。
"""

from unittest import mock

from insta_engine import doctor, ios_wda
from insta_engine.web_api import device as device_api


def _sims_data():
    return {"devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-17-5": [
            {"udid": "u-se", "name": "iPhone SE (3rd generation)",
             "state": "Shutdown", "isAvailable": True},
            {"udid": "u-ipad", "name": "iPad Pro 11-inch",
             "state": "Shutdown", "isAvailable": True},
        ],
        "com.apple.CoreSimulator.SimRuntime.iOS-18-2": [
            {"udid": "u-16pro", "name": "iPhone 16 Pro",
             "state": "Shutdown", "isAvailable": True},
            {"udid": "u-mini", "name": "iPad mini",
             "state": "Booted", "isAvailable": True},
        ],
    }}


def test_ios_simulators_sort_booted_iphone_version(monkeypatch):
    monkeypatch.setattr(device_api, "_simctl_devices", lambda: _sims_data())
    code, body = device_api.api_ios_simulators()
    assert code == 200
    names = [s["name"] for s in body["simulators"]]
    # booted 最前 → iPhone 层内系统版本降序（18.2 在 17.5 前）→ iPad 排 iPhone 后
    assert names == ["iPad mini", "iPhone 16 Pro",
                     "iPhone SE (3rd generation)", "iPad Pro 11-inch"]


def test_ios_sim_boot_also_starts_wda(monkeypatch):
    monkeypatch.setattr(device_api, "_simctl_devices", lambda: _sims_data())
    # boot / shutdown / open -a Simulator 三个子进程一律桩掉
    monkeypatch.setattr(device_api.subprocess, "run",
                        lambda *a, **k: mock.Mock(returncode=0, stdout="", stderr=""))
    seen: dict = {}

    def fake_start(wait_seconds: int = 40, allow_build: bool = True) -> tuple[bool, str]:
        seen["called"] = seen.get("called", 0) + 1
        seen["allow_build"] = allow_build
        return True, "WDA 已就绪 (localhost:8100)"

    monkeypatch.setattr(device_api.ios_wda, "start_on_booted", fake_start)
    code, body = device_api.api_ios_sim_boot({"udid": "u-16pro"})
    assert code == 200 and body["ok"]
    # allow_build=True（发起人 2026-10-12：首次启动模拟器也要自动装好 WDA——
    # 全新模拟器 simctl launch 必失败，不兜底则投屏永远不可用）
    assert seen.get("called") == 1 and seen["allow_build"] is True \
        and body["wda"] == {"started": True, "detail": "WDA 已就绪 (localhost:8100)"}
    # 单活切换：另一台 booted 的 iPad mini 被关闭
    assert body["shutdown"] == ["iPad mini"]


def test_ios_wda_start_requires_booted_sim(monkeypatch):
    monkeypatch.setattr(ios_wda, "booted_udid", lambda: None)
    ok, detail = ios_wda.start_on_booted(wait_seconds=1)
    assert not ok and detail == "没有已启动的模拟器或已连接的真机"


def test_booted_udid_parses_simctl_output(monkeypatch):
    out = ("== Devices ==\n"
           "    iPhone 16 Pro (AB12CD34-5678-90AB-CDEF-1234567890AB) (Booted)\n"
           "    iPad mini (00000000-0000-0000-0000-000000000000) (Shutdown)\n")
    monkeypatch.setattr(ios_wda, "_run",
                        lambda cmd, timeout: mock.Mock(returncode=0, stdout=out))
    assert ios_wda.booted_udid() == "AB12CD34-5678-90AB-CDEF-1234567890AB"


def test_ios_wda_fast_path_reports_build_hint_without_build(monkeypatch):
    """allow_build=False 开关（保留给快路径场景）：WDA 未装 → 明确失败 + 构建指引，不触发构建。"""
    monkeypatch.setattr(ios_wda, "booted_udid", lambda: "u-1")
    monkeypatch.setattr(ios_wda, "_run", lambda cmd, timeout: mock.Mock(
        returncode=1, stdout="", stderr='The request to open "com.facebook.WebDriverAgentRunner" failed.'))
    calls: list = []
    monkeypatch.setattr(ios_wda, "_ensure_wda_source", lambda: calls.append("src"))
    ok, detail = ios_wda.start_on_booted(wait_seconds=1, allow_build=False)
    assert not ok and "构建" in detail and calls == []


def test_ios_wda_build_fallback(monkeypatch):
    """概览一键启动场景（allow_build=True）：未装 WDA → 下载源码 → 构建 → 分离拉起 → 就绪。"""
    from pathlib import Path

    monkeypatch.setattr(ios_wda, "booted_udid", lambda: "u-1")
    monkeypatch.setattr(ios_wda, "_run", lambda cmd, timeout: mock.Mock(
        returncode=1, stdout="", stderr="not installed"))
    calls: list = []
    monkeypatch.setattr(ios_wda, "_ensure_wda_source", lambda: calls.append("src"))
    monkeypatch.setattr(ios_wda, "_build_wda",
                        lambda udid: calls.append("build") or Path("/tmp/fake.xctestrun"))
    monkeypatch.setattr(ios_wda, "_start_wda_service", lambda udid, xr: calls.append("start"))
    monkeypatch.setattr(ios_wda, "_poll_status", lambda seconds: True)
    ok, detail = ios_wda.start_on_booted()
    assert ok and calls == ["src", "build", "start"] and "首次构建" in detail


def test_wda_bundle_id_must_end_with_xctrunner(monkeypatch):
    """快路径的 bundle id 必须是 `...xctrunner`（2026-10-17 实测）。

    这个拼错**不会让任何测试变红**——它只让「装过 WDA 的模拟器免构建秒起」静默退化成
    "每次构建兜底"（DerivedData 命中所以用户只觉得慢）。实测证据：
    `simctl launch …xctruntime` 报 failed to launch，`…xctrunner` 返回 pid。
    ⇒ 必须由断言钉住，否则下次手滑又回来。
    """
    assert ios_wda.WDA_BUNDLE_ID.endswith(".xctrunner"), \
        f"WDA_BUNDLE_ID 后缀错了：{ios_wda.WDA_BUNDLE_ID}（应为 .xctrunner，不是 .xctruntime）"

    seen: list[list[str]] = []

    def fake_run(cmd, timeout):
        seen.append(cmd)
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ios_wda, "booted_udid", lambda: "u-1")
    monkeypatch.setattr(ios_wda, "_run", fake_run)
    monkeypatch.setattr(ios_wda, "_poll_status", lambda seconds: True)
    ok, _ = ios_wda.start_on_booted(wait_seconds=1)
    assert ok, "快路径应就绪"
    assert seen and seen[0][:4] == ["xcrun", "simctl", "launch", "u-1"], seen
    assert seen[0][4] == ios_wda.WDA_BUNDLE_ID, "快路径用的 id 必须与常量一致（防两处漂移）"


def test_doctor_start_wda_fix_action(monkeypatch, tmp_path):
    monkeypatch.setattr(ios_wda, "start_on_booted",
                        lambda wait_seconds=40: (True, "WDA 已就绪"))
    code, body = doctor.run_fix("start-wda", tmp_path)
    assert code == 200 and body["ok"] and body["detail"] == "WDA 已就绪"

    monkeypatch.setattr(ios_wda, "start_on_booted",
                        lambda wait_seconds=40: (False, "没有已启动的模拟器或已连接的真机"))
    code, body = doctor.run_fix("start-wda", tmp_path)
    assert code == 400 and "已连接的真机" in body["error"]


def test_doctor_wda_server_row_carries_start_fix(monkeypatch):
    # 这条测的是 **macOS** 的 WDA 一键启动：非 macOS 上 simctl/devicectl 都不存在，
    # 实现会收起这个按钮（doctor.ios_checks 的 not supported 分支，2026-10-25 D-1）。
    # 不钉平台的话它在 Linux CI 上会因「按钮不该出现」而红。
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(doctor, "_wda_installed", lambda: True)
    monkeypatch.setattr(doctor, "_booted_simulators", lambda: ["iPhone (u) (Booted)"])
    monkeypatch.setattr(doctor, "_wda_status_ok", lambda: False)
    # 宿主机有没有插 USB 真机会改变修复动作（start-wda ↔ start-wda-device）：
    # 不打桩的话本机插上 iPhone 后这条测试就红（2026-10-22 实测）
    monkeypatch.setattr(doctor, "_usb_device_udid", lambda: None)
    rows = {c.id: c.as_dict() for c in doctor.run("ios")}
    assert rows["wda-server"]["fix"] == {"action": "start-wda", "kind": "wda", "label": "启动"}

    # 有 USB 真机 → 修复动作切真机启动
    monkeypatch.setattr(doctor, "_usb_device_udid", lambda: "TEST-UDID")
    rows = {c.id: c.as_dict() for c in doctor.run("ios")}
    assert rows["wda-server"]["fix"]["action"] == "start-wda-device"

    # 服务已起 → 不给修复按钮
    monkeypatch.setattr(doctor, "_wda_status_ok", lambda: True)
    rows = {c.id: c.as_dict() for c in doctor.run("ios")}
    assert rows["wda-server"]["fix"] is None


def test_start_device_service_survives_unwritable_log(monkeypatch, tmp_path):
    """日志目录不可写不能阻断真机 WDA 启动（沙箱/权限场景，2026-10-22 实测）。"""
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)  # 只读目录：mkdir/open 都会 EACCES
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setattr(ios_wda, "WDA_DEVICE_LOG_DIR", ro / "logs")
    monkeypatch.setattr(ios_wda.tempfile, "gettempdir", lambda: str(fallback))
    spawned: dict = {}
    monkeypatch.setattr(ios_wda.subprocess, "Popen",
                        lambda cmd, **kw: spawned.update(kw) or mock.Mock())

    ios_wda._start_wda_device_service("TEST-UDID", tmp_path / "x.xctestrun")

    assert spawned, "Popen 必须被调用——日志写不了不是启动失败的理由"
    assert (fallback / "mat-wda-TEST-UDID.log").exists()
    ro.chmod(0o700)  # 交还 tmp_path 清理权限
