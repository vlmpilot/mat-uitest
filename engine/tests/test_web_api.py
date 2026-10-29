"""web_server API 层覆盖（T10）：HTTP 路由/上传/安全边界走真服务器，api_* 直调补枝。

覆盖目标：web_server.py 曾仅 33%（门禁余量 1pp 时的最大缺口）。
"""

import base64
import json
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
import requests

from insta_engine import web_server
from insta_engine.dsl import TestCase, dump_dsl
from insta_engine.web_api import workspace

CSV_CONTENT = """用例编号,用例标题,所属模块,优先级,前置条件,操作步骤,测试数据,预期结果
TC-001,登录成功,登录,P1,已安装App,"1. 输入账号
2. 点击登录",alice,进入首页
"""


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """真 HTTP 服务器（临时端口 + 隔离状态目录 + 临时工作区）。

    ⚠️ **跑完必须关掉**（2026-10-07 修 flaky）：旧夹具只拿到端口号、拿不到 server 对象
    ⇒ 每个测试都留下一个服务器线程 + 一个 watchdog 线程（全量 ~40 个测试 ⇒ 80 个线程）
    ⇒ 负载高的机器上响应被拖到超过客户端 timeout（那条 413 用例全量里挂、单独跑通过）。

    ⚠️ 端口用 `0` 让**内核直接分配**：旧写法"先 `bind(0)` 拿端口 → `close` → 再 `serve`"
    中间有窗口会被抢走（实测循环跑时偶发 setup 失败）。
    """
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))  # 隔离本机 license（开发机已激活 pro）
    srv = web_server.make_server(0, root=tmp_path)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):  # 等端口就绪
        try:
            requests.get(f"{base}/api/state", timeout=1)
            break
        except requests.ConnectionError:
            time.sleep(0.1)
    yield base, tmp_path
    srv.shutdown()          # 关键：跑完就关，不留线程
    srv.server_close()


def _case_json() -> str:
    case = TestCase.model_validate({
        "caseId": "case_web", "name": "web 用例", "target": {"package": "com.insta.demo"},
        "steps": [{"id": "s1", "intent": "等待", "action": "wait", "params": {"ms": 10}}],
    })
    return dump_dsl(case)


# -- HTTP 路由层 ------------------------------------------------------------------

def test_state_and_unknown_route(server):
    base, _ = server
    r = requests.get(f"{base}/api/state", timeout=3)
    assert r.status_code == 200
    body = r.json()
    # ⚠️ `console` 字段**如实反映「构建好的控制台在不在」**：`console_dist/` 是构建产物、
    # 不入库也不对外发布 ⇒ 源码检出里是 "legacy"（此时 `mat console` 退回包内单页）。
    # 所以断言**与实现同源**，而不是钉死 "pro"——钉死会让「没构建过控制台」的环境必红。
    from insta_engine.web_api.runtime import CONSOLE_DIST

    expected_console = "pro" if (CONSOLE_DIST / "index.html").exists() else "legacy"
    assert body["console"] == expected_console and "cases" in body and "usage" in body
    assert requests.get(f"{base}/api/nope", timeout=3).status_code == 404


def test_metrics_route_is_same_implementation_as_cli(server):
    """概览页的质量指标卡消费 `/api/metrics`：必须与 `mat metrics` 同一实现。"""
    base, root = server
    (root / "artifacts" / "eval").mkdir(parents=True)
    (root / "artifacts" / "eval" / "baseline-demo.json").write_text(json.dumps({
        "schema": "mat.eval/1", "suite": "demo.json", "repeat": 1,
        "overall": {"median": 1.0, "min": 1.0, "max": 1.0, "spread_pp": 0.0},
        "per_task": [{"id": "t1"}], "unstable": [], "app_fingerprints": ["fp1"],
    }), encoding="utf-8")
    r = requests.get(f"{base}/api/metrics", timeout=3)
    assert r.status_code == 200
    body = r.json()
    assert body["schema"] == "mat.metrics/1"
    item = next(i for i in body["products"] if "步骤成功率" in i["name"])
    assert item["value"] == "100%", "端点必须直接返回指标卡，不在前端重算"
    assert item["met"] is True and item["target"] == "≥ 95%"



def test_console_index_and_spa_fallback(server):
    base, _ = server
    assert requests.get(f"{base}/", timeout=3).status_code == 200
    assert requests.get(f"{base}/index.html", timeout=3).status_code == 200
    assert requests.get(f"{base}/#/anything", timeout=3).status_code == 200


def test_app_labels_graceful_without_install(server):
    """GET /api/app/labels（2026-10-12 用例分组显示 displayName）：端点必须恒 200。

    未安装的包/无设备 ⇒ labels 为空（前端回落包名），绝不 500——这是显示名的兜底契约。
    """
    base, _ = server
    r = requests.get(f"{base}/api/app/labels?packages=com.a,com.b", timeout=5)
    assert r.status_code == 200 and r.json() == {"labels": {}}
    # 参数缺省同样 200（前端传空列表时）
    r2 = requests.get(f"{base}/api/app/labels", timeout=5)
    assert r2.status_code == 200 and r2.json() == {"labels": {}}


def test_case_write_read_and_validation(server):
    base, root = server
    ok = requests.post(f"{base}/api/case", timeout=3,
                       json={"name": "web-case.json", "content": _case_json(),
                             "operator": "测试员"})
    assert ok.status_code == 200 and (root / "cases" / "web-case.json").exists()
    got = requests.get(f"{base}/api/case?name=web-case.json", timeout=3)
    assert got.status_code == 200 and got.json()["caseId"] == "case_web"
    # 非法名字与非法内容
    assert requests.post(f"{base}/api/case", timeout=3,
                         json={"name": "../evil.json", "content": "{}",
                               "operator": "测试员"}).status_code == 400
    bad_json = requests.post(f"{base}/api/case", timeout=3,
                             json={"name": "bad.json", "content": "{not json}",
                                   "operator": "测试员"})
    assert bad_json.status_code == 400 and "不合法" in bad_json.json()["error"]
    assert requests.get(f"{base}/api/case?name=missing.json", timeout=3).status_code == 404
    assert requests.get(f"{base}/api/case?name=../x.json", timeout=3).status_code == 400


def test_activate_and_jobs_routes(server):
    base, _ = server
    assert requests.post(f"{base}/api/activate", timeout=3, json={"code": ""}).status_code == 400
    bad = requests.post(f"{base}/api/activate", timeout=3, json={"code": "MAT-bogus.1.a.abcdefabcdef"})
    assert bad.status_code == 400 and "error" in bad.json()
    assert requests.get(f"{base}/api/jobs", timeout=3).status_code == 200
    assert requests.get(f"{base}/api/job?id=nope", timeout=3).json() == {"status": "unknown"}
    assert requests.get(f"{base}/api/job/events?id=nope", timeout=3).status_code == 404
    assert requests.post(f"{base}/api/job/cancel", timeout=3,
                         json={"id": "nope", "operator": "测试员"}).status_code in (404, 400)


def test_post_rejects_cross_origin(server):
    """S7 缓解：伪造 Origin 的跨域 POST ⇒ 403（恶意网页无法 CSRF 本地服务）。"""
    base, _ = server
    r = requests.post(f"{base}/api/case", timeout=3,
                      headers={"Origin": "http://evil.example"},
                      json={"name": "csrf.json", "content": _case_json()})
    assert r.status_code == 403 and "cross-origin" in r.json()["error"]
    # Referer 伪装同样拦截（取 host:port 段比对，不是子串匹配）
    r2 = requests.post(f"{base}/api/case", timeout=3,
                       headers={"Referer": "http://evil.example/page"},
                       json={"name": "csrf.json", "content": _case_json()})
    assert r2.status_code == 403


def test_post_allows_same_origin_and_non_browser(server):
    base, _ = server
    port = base.rsplit(":", 1)[1]
    ok = requests.post(f"{base}/api/case", timeout=3,
                       headers={"Origin": f"http://127.0.0.1:{port}"},
                       json={"name": "same-origin.json", "content": _case_json(),
                             "operator": "测试员"})
    assert ok.status_code == 200
    localhost = requests.post(f"{base}/api/case", timeout=3,
                              headers={"Origin": f"http://localhost:{port}"},
                              json={"name": "lh.json", "content": _case_json(),
                                    "operator": "测试员"})
    assert localhost.status_code == 200
    # 非浏览器客户端（curl/pytest）不带 Origin——既有用户零影响
    plain = requests.post(f"{base}/api/case", timeout=3,
                          json={"name": "no-origin.json", "content": _case_json(),
                                "operator": "测试员"})
    assert plain.status_code == 200


def test_doctor_endpoint(server):
    """F2：/api/doctor 是 CLI 同源数据——关键 4 项 + iOS 组 + 超时纪律。"""
    base, _ = server
    r = requests.get(f"{base}/api/doctor?platform=all", timeout=30)
    assert r.status_code == 200
    body = r.json()
    ids = [c["id"] for c in body["checks"]]
    assert ids[:4] == ["python", "adb", "devices", "u2"]
    assert {"wda-lib", "simctl", "simulators", "wda-server"} <= set(ids)
    for c in body["checks"]:
        assert c["status"] in ("ok", "warn", "fail")
    # 超时生效：任何探测挂死都会拖住自动检测（设计 §3.3）
    assert body["duration_ms"] < 15_000
    assert requests.get(f"{base}/api/doctor?platform=ios", timeout=30).status_code == 200


def test_device_key_whitelist_and_online_serial(server):
    """F3：动作白名单 + serial 必须命中在线列表（防伪造）。"""
    base, _ = server
    bad = requests.post(f"{base}/api/device/key", timeout=3,
                        json={"action": "power", "operator": "测试员"})
    assert bad.status_code == 400 and "未知动作" in bad.json()["error"]
    ghost = requests.post(f"{base}/api/device/key", timeout=3,
                          json={"action": "home", "serial": "FAKE-SERIAL-XYZ",
                                "operator": "测试员"})
    assert ghost.status_code == 400 and "不在线" in ghost.json()["error"]


def test_device_key_no_device_graceful(server):
    """无设备时按键 ⇒ 如实 400（不是 500）；有真机的开发环境跳过（避免按键真实作用于设备）。"""
    base, _ = server
    if web_server.list_adb_devices():
        pytest.skip("本机有在线设备,跳过(避免测试按键真实作用于设备)")
    r = requests.post(f"{base}/api/device/key", timeout=20,
                      json={"action": "home", "operator": "测试员"})
    assert r.status_code == 400 and "设备不可用" in r.json()["error"]


def test_scrcpy_validation_paths(server, monkeypatch):
    """F3：伪造 serial ⇒ 400；scrcpy 未装 ⇒ 400 带安装指引（不真正弹窗）。"""
    base, _ = server
    import shutil
    real_which = shutil.which
    # 视为已装（假路径）——让校验真正走到 serial 判定（本机可能本就没装 scrcpy）
    monkeypatch.setattr(shutil, "which", lambda n: "/fake/bin/scrcpy" if n == "scrcpy" else real_which(n))
    ghost = requests.post(f"{base}/api/device/scrcpy", timeout=3,
                          json={"serial": "FAKE-SERIAL-XYZ", "operator": "测试员"})
    assert ghost.status_code == 400 and "不在线" in ghost.json()["error"]
    # 未安装路径直调 api 层
    monkeypatch.setattr(shutil, "which", lambda n: None if n == "scrcpy" else real_which(n))
    code, obj = web_server.api_device_scrcpy({})
    assert code == 400 and "brew install scrcpy" in obj["error"]


def test_import_preview_bad_and_good_upload(server):
    base, _ = server
    bad = requests.post(f"{base}/api/import/preview", timeout=3,
                        json={"name": "x.exe", "content_b64": base64.b64encode(b"x").decode(),
                              "operator": "测试员"})
    assert bad.status_code == 400 and "不支持的格式" in bad.json()["error"]
    good = requests.post(f"{base}/api/import/preview", timeout=3, json={
        "name": "cases.csv", "operator": "测试员",
        "content_b64": base64.b64encode(CSV_CONTENT.encode("utf-8")).decode(),
    })
    assert good.status_code == 200 and good.json()["count"] >= 1, good.text
    assert good.json()["titles"] == ["登录成功"]
    assert requests.post(f"{base}/api/import/preview", timeout=3,
                         json={"name": "cases.csv", "content_b64": "!!not-base64!!",
                               "operator": "测试员"}).status_code == 400


def test_import_convert_requires_package(server):
    base, _ = server
    r = requests.post(f"{base}/api/import/convert", timeout=3, json={
        "name": "cases.csv", "operator": "测试员",
        "content_b64": base64.b64encode(CSV_CONTENT.encode("utf-8")).decode(),
    })
    assert r.status_code == 400 and "package" in r.json()["error"]


def test_oversized_body_rejected(server):
    """超限 body ⇒ **413**（而不是连接重置）。

    ⚠️ **body 刻意给到 8MB，而不是"刚过 1MB"**（2026-10-07 修 flaky）：
    能不能稳定复现取决于"整段 body 塞不塞得进 socket 缓冲"（本机约 100KB–1MB）——
    1.1MB 时**有时塞得进**，服务端即使不读 body 也能让客户端写完再读到 413 ⇒
    这条测试就变成"碰运气"（实测全量门禁里挂 3 次、单独跑却通过）。
    8MB 稳定超过缓冲 ⇒ 只要服务端**不读 body 就必挂** ⇒ 这条测试才真的在测东西。
    """
    base, _ = server
    big = {"name": "big.json", "content": "x" * 8_000_000}
    assert requests.post(f"{base}/api/case", timeout=30, json=big).status_code == 413


def test_explore_artifact_route_validates_id(server, tmp_path):
    base, root = server
    assert requests.get(f"{base}/api/explore?id=../..", timeout=3).status_code == 400
    assert requests.get(f"{base}/api/explore?id=nope", timeout=3).status_code == 404
    run_dir = root / "artifacts" / "explore" / "run1"
    run_dir.mkdir(parents=True)
    (run_dir / "explore.json").write_text(json.dumps({"pages": []}), encoding="utf-8")
    ok = requests.get(f"{base}/api/explore?id=run1", timeout=3)
    assert ok.status_code == 200 and ok.json()["pages"] == []


def test_direct_api_validation_branches(tmp_path, monkeypatch):
    """api_* 直调的校验分支（不起服务器也能覆盖的枝）。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    empty = tmp_path / "empty"
    empty.mkdir()
    code, state = web_server.api_state(empty)
    assert code == 200 and state["cases"] == [] and state["indexes"] == []
    # 订阅页套餐行数据源：未激活 ⇒ free 且无到期日
    assert state["license"] == {"plan": "free", "expires_at": None}
    code, obj = web_server.api_explore(empty, {})
    assert code == 400 and "package" in obj["error"]
    code, obj = web_server.api_import_convert(empty, {"name": "a.csv",
                                                      "content_b64": base64.b64encode(b"x").decode()})
    assert code == 400 and "package" in obj["error"]


def _has_attached_device() -> bool:
    """本机是否插着设备（决定下面那条"无设备"用例能不能成立）。"""
    try:
        from insta_engine.device import list_adb_devices

        return bool(list_adb_devices())
    except Exception:  # noqa: BLE001 —— 探测失败按"没设备"处理（宁可跑，不误跳）
        return False


@pytest.mark.skipif(
    _has_attached_device(),
    reason="该用例的前提是「无设备」：设备插着时会走真实执行分支（多一步 launch，>15s 未终态），"
           "断言变成在等一个正常运行的 job。无设备路径由 "
           "test_api_run_reports_error_job_when_no_device 用 monkeypatch 覆盖，不依赖环境。")
def test_run_job_fails_gracefully_without_device(server):
    """api_run 全链：无设备时 job 应**优雅落 error**（不是 500、不挂死），事件流可查询。"""
    base, root = server
    w = requests.post(f"{base}/api/case", timeout=3,
                      json={"name": "r.json", "content": _case_json(),
                            "operator": "测试员"})
    assert w.status_code == 200
    r = requests.post(f"{base}/api/run", timeout=5, json={"case": "r.json", "operator": "测试员"})
    assert r.status_code == 200, r.text
    jid = r.json().get("id") or r.json().get("job_id")
    assert jid
    status = None
    for _ in range(60):  # 最多等 15s：无设备应在数秒内落 error/failed
        job = requests.get(f"{base}/api/job?id={jid}", timeout=3).json()
        status = job.get("status")
        if status in ("error", "failed", "done"):
            break
        time.sleep(0.25)
    assert status in ("error", "failed", "done"), f"job 未终态: {status}"
    ev = requests.get(f"{base}/api/job/events?id={jid}&since=0", timeout=3)
    assert ev.status_code == 200 and ev.json()["events"], "事件流应有步骤事件"
    assert ev.json()["status"] in ("done", "error", "failed", "cancelled")

    assert requests.post(f"{base}/api/run", timeout=3,
                         json={"case": "missing.json", "operator": "测试员"}).status_code == 400


def test_api_run_reports_error_job_when_no_device(tmp_path, monkeypatch):
    """**无设备时必须是 200 + 落 error 的 job（事件流非空）**，不能是 500。

    为什么单独立这一条：上面那条走真服务器，**设备插着时根本走不到「无设备」分支**
    （job 正常跑完 ⇒ 断言"碰巧"通过，名字里的 without_device 是假的）。
    2026-10-06 设备掉线才把它暴露出来——`api_run` 在 **job 之外**就调 `get_driver()`，
    `AdbError` 直接冒到 HTTP 层 ⇒ 端点返 500。这条测试**不依赖真实设备**，
    用 monkeypatch 强制 `get_driver` 抛异常，所以任何时候都能复现。
    """
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")  # 绕过源码新鲜度检查（与本测试无关）
    root = tmp_path / "ws"
    (root / "cases").mkdir(parents=True)
    (root / "cases" / "r.json").write_text(_case_json(), encoding="utf-8")

    def no_device(*_a, **_k):
        raise RuntimeError("Can't find any android device/emulator")

    monkeypatch.setattr("insta_engine.web_api.runs.get_driver", no_device)

    code, obj = web_server.api_run(root, {"case": "r.json"})
    assert code == 200, "无设备应当是「优雅的 job error」，不是 500"
    jid = obj["job_id"]

    job = None
    for _ in range(100):  # 最多 5s
        job = web_server._JOBS[jid]
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job is not None and job["status"] == "error", f"job 应落 error，实际 {job}"
    assert job["events"], "事件流必须非空——否则前端只看到转圈、看不到原因"
    assert "设备不可用" in job["events"][0]["detail"]


# -- 修复3：server 侧平台路由（评审 I4 / I10） ------------------------------------


def test_case_meta_display_numbers(tmp_path):
    """用例展示编号 #N（发起人 2026-10-12：caseId 随机字母难沟通且大量重复）。

    三条验收：①按文件名序首次分配递增数字；②重复调用稳定不重发（并发口径下
    同文件编号恒定）；③删除用例不回收编号，新文件拿 max+1——沟通口径跨增删稳定。
    """
    root = tmp_path / "ws"
    (root / "cases").mkdir(parents=True)
    (root / "cases" / "b.json").write_text(
        json.dumps({"caseId": "b", "name": "B", "target": {"package": "p"}}), encoding="utf-8")
    (root / "cases" / "a.json").write_text(
        json.dumps({"caseId": "a", "name": "A", "target": {"package": "p"}}), encoding="utf-8")

    meta1 = workspace.case_meta(root)
    assert meta1["a.json"]["no"] == 1 and meta1["b.json"]["no"] == 2
    meta2 = workspace.case_meta(root)
    assert meta2["a.json"]["no"] == 1 and meta2["b.json"]["no"] == 2

    (root / "cases" / "a.json").unlink()
    (root / "cases" / "c.json").write_text(
        json.dumps({"caseId": "c", "name": "C", "target": {"package": "p"}}), encoding="utf-8")
    meta3 = workspace.case_meta(root)
    assert "a.json" not in meta3 and meta3["c.json"]["no"] == 3, "删除不回收、新增续号"


def test_api_run_selected_cases_no_tmp_file(tmp_path, monkeypatch):
    """cases 列表参数（用例列表多选运行，发起人 2026-10-12）：内存临时套件、不落盘。

    与 --app 批量的 _batch_{app}.json 不同，多选运行每次都构建临时套件——若落盘，
    cases/ 会被垃圾文件刷屏。三条验收：①非法名/缺失用例 400；②合法时 200 起 job
    （无设备优雅落 error，与单用例同契约）；③cases/ 不产生 _batch_selected.json。
    """
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    root = tmp_path / "ws"
    (root / "cases").mkdir(parents=True)
    (root / "cases" / "a.json").write_text(_case_json(), encoding="utf-8")
    (root / "cases" / "b.json").write_text(_case_json(), encoding="utf-8")

    def no_device(*_a, **_k):
        raise RuntimeError("Can't find any android device/emulator")

    monkeypatch.setattr("insta_engine.web_api.runs.get_driver", no_device)

    code, _ = web_server.api_run(root, {"cases": ["../x.json"]})
    assert code == 400, "路径穿越必须拒绝"
    code, obj = web_server.api_run(root, {"cases": ["a.json", "ghost.json"]})
    assert code == 400 and "ghost" in obj["error"], "缺失用例要逐个报出"

    code, obj = web_server.api_run(root, {"cases": ["a.json", "b.json"], "reset": "cold"})
    assert code == 200, obj
    assert not (root / "cases" / "_batch_selected.json").exists(), "临时套件不得落盘"
    job = None
    for _ in range(100):  # 最多 5s：无设备应优雅落 error（套件链路同样成立）
        job = web_server._JOBS[obj["job_id"]]
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job is not None and job["status"] == "error"
    assert job["events"], "事件流必须非空"


def test_get_driver_cache_key_includes_platform(monkeypatch):
    """缓存键必须含平台，否则 Android 与 iOS 的默认驱动会互踩同一条目（评审 I10）。"""
    created: list[tuple] = []

    class _FakeDrv:
        def __init__(self, platform):
            self.platform = platform

    def fake_connect(serial, platform="android"):
        drv = _FakeDrv(platform)
        created.append((serial, platform, drv))
        return drv

    monkeypatch.setattr("insta_engine.web_api.runtime.connect_driver", fake_connect)
    monkeypatch.setattr("insta_engine.web_api.runtime._DRIVERS", {})
    a1 = web_server.get_driver(None)
    a2 = web_server.get_driver(None)
    i1 = web_server.get_driver(None, "ios")
    i2 = web_server.get_driver(None, "ios")
    assert a1 is a2 and i1 is i2, "同 (platform, serial) 应命中缓存"
    assert a1 is not i1, "不同平台不得共用缓存条目"
    assert len(created) == 2, f"应只构造两次，实际 {created}"
    assert {c[1] for c in created} == {"android", "ios"}


def test_invalidate_driver_is_platform_scoped(monkeypatch):
    """失效只作用于对应平台，不误伤另一平台缓存（评审 I10）。"""
    monkeypatch.setattr("insta_engine.web_api.runtime.connect_driver",
                        lambda serial, platform="android": object())
    monkeypatch.setattr("insta_engine.web_api.runtime._DRIVERS", {})
    a = web_server.get_driver(None)
    i = web_server.get_driver(None, "ios")
    web_server.invalidate_driver(None, "ios")
    assert web_server.get_driver(None) is a, "失效 iOS 不应影响 Android 缓存"
    assert web_server.get_driver(None, "ios") is not i, "失效后应重建"


def test_api_devices_annotates_platform(server):
    """GET /api/devices 逐项标注 platform（评审 I4 验收）。

    2026-10-19 起 iOS **USB 真机**也在此列（serial=udid）——断言改为「每项都有
    platform 且 iOS 项带 udid」，不再假设只有 adb 设备。
    """
    base, _ = server
    r = requests.get(f"{base}/api/devices", timeout=3)
    assert r.status_code == 200
    for d in r.json()["devices"]:
        assert d["platform"] in ("android", "ios", "harmony"), "每台设备都必须标注 platform"
        if d["platform"] == "ios":
            assert d["serial"], "iOS 真机条目必须带 udid 作 serial"


def test_api_explore_blocks_ios_honestly(tmp_path, monkeypatch):
    """与 CLI 同口径：api_explore 对 iOS 诚实拦截，不在未验证路径上产出图谱（评审 I4）。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    code, obj = web_server.api_explore(tmp_path, {"package": "com.x", "platform": "ios"})
    assert code == 400 and "P3" in obj["error"]


def test_api_run_threads_platform_to_driver(tmp_path, monkeypatch):
    """api_run 必须把请求体的 platform 透传给驱动工厂（评审 I4：此前 server 只认 Android）。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    root = tmp_path / "ws"
    (root / "cases").mkdir(parents=True)
    (root / "cases" / "r.json").write_text(_case_json(), encoding="utf-8")
    seen: dict = {}

    def fake_get_driver(serial, platform="android"):
        seen["platform"] = platform
        raise RuntimeError("stop")  # 走到 job 内 get_driver 即可，不必真跑

    monkeypatch.setattr("insta_engine.web_api.runs.get_driver", fake_get_driver)
    code, obj = web_server.api_run(root, {"case": "r.json", "platform": "ios"})
    assert code == 200
    jid = obj["job_id"]
    for _ in range(100):
        if web_server._JOBS[jid]["status"] != "running":
            break
        time.sleep(0.05)
    assert seen.get("platform") == "ios", "platform 必须透传到 get_driver"


# -- 邀请 API------------------------------------------------
#
# 两条纪律：**读状态不联网**；**联网必须由用户显式点出来**（且首次注册要同意）。
# 配置台是本地网页，"顺手帮用户联网"在这里特别容易发生 —— 所以这两条要有测试。


@pytest.fixture()
def invite_home(tmp_path, monkeypatch):
    from insta_engine import invite

    monkeypatch.setenv("MAT_HOME", str(tmp_path / "mat-home"))
    monkeypatch.delenv("INSTA_HOME", raising=False)
    monkeypatch.delenv("MAT_CLOUD_URL", raising=False)
    monkeypatch.delenv("GEN_API_KEY", raising=False)
    return invite


def test_api_invite_status_reads_local_snapshot_without_network(invite_home, monkeypatch):
    """读状态**只读本地快照** —— 配置台每次打开都不该产生出站请求。"""
    def boom(*_a, **_k):
        raise AssertionError("api_invite_status 不该联网")

    monkeypatch.setattr("requests.request", boom)
    invite_home.save_state({
        "user_id": "inv_1", "invite_code": "ABCD2345", "token": "tok",
        "ledger_synced_at": "2026-10-11 23:00",
        "ledger_cache": {"invited": 3, "qualified": 2, "pending": 1, "capped": 0,
                         "credited_this_month": 2, "monthly_cap": 4,
                         "reward_kind": "server_gen", "reward_amount": 50.0},
    })

    code, body = web_server.api_invite_status()

    assert code == 200
    assert body["joined"] is True
    assert body["invite_code"] == "ABCD-2345"
    assert body["ledger"]["pending"] == 1
    assert body["synced_at"] == "2026-10-11 23:00"


def test_api_invite_status_before_joining(invite_home):
    code, body = web_server.api_invite_status()
    assert code == 200
    assert body["joined"] is False
    assert body["invite_code"] is None
    assert body["ledger"] is None


def test_api_invite_join_refuses_without_consent_and_stays_offline(invite_home, monkeypatch):
    """🔴 没勾选同意就**拒绝**，而且**一个请求都不发**。

    匿名注册会新建服务端状态，用户必须先看到说明。
    少这个字段就拒绝 —— 免得以后有人顺手调用它，把"静默注册"从后门带回来。
    """
    def boom(*_a, **_k):
        raise AssertionError("未同意时不该发任何请求")

    monkeypatch.setattr("requests.request", boom)

    code, body = web_server.api_invite_action({"action": "join"})

    assert code == 400 and "匿名 ID" in body["error"]


def test_api_invite_join_registers_when_consented(invite_home, monkeypatch):
    """阳性对照：勾了同意 ⇒ 真的去注册（证明上一条测的不是个不存在的行为）。"""
    from insta_engine import invite

    called: list[int] = []

    def fake_register():
        called.append(1)
        invite.save_state({"user_id": "inv_1", "invite_code": "ABCD2345", "token": "tok"})
        return {"user_id": "inv_1", "invite_code": "ABCD2345"}

    monkeypatch.setattr(invite, "register", fake_register)

    code, body = web_server.api_invite_action({"action": "join", "agree": True})

    assert code == 200 and body["ok"] is True
    assert called == [1]
    assert web_server.api_invite_status()[1]["joined"] is True


def test_api_invite_never_hands_credentials_to_the_browser(invite_home, monkeypatch):
    """🔴 控制台**不是**取凭证的地方（2026-10-14）：报文里一律不带 `token` / `pro_code`。

    控制台可能跑在 `mat --share` 下 —— **同网段任何一台浏览器都能打开它**。
    当前前端确实不消费这两个字段（邀请卡片 2026-10-13 已移除），但"现在没人用"正是
    "以后有人顺手用一下"的温床，所以在 HTTP 这一层就剥掉。凭证只从 `mat token` 出。
    """
    monkeypatch.setattr(invite_home, "register",
                        lambda: {"user_id": "inv_1", "invite_code": "ABCD2345", "token": "tok"})
    monkeypatch.setattr(invite_home, "sync_ledger",
                        lambda: {"ledger": {"invited": 1, "pro_code": {"code": "MAT-x"}},
                                 "claim": None})

    code, body = web_server.api_invite_action({"action": "join", "agree": True})
    assert code == 200
    assert "token" not in body["result"], "凭证被回给了浏览器"

    code, body = web_server.api_invite_action({"action": "sync"})
    assert code == 200
    assert "pro_code" not in body["result"]["ledger"], "激活码被回给了浏览器"

    assert "token" not in web_server.api_invite_status()[1]


def test_api_invite_accept_requires_a_code(invite_home, monkeypatch):
    monkeypatch.setattr("requests.request",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该联网")))
    code, body = web_server.api_invite_action({"action": "accept", "code": "  "})
    assert code == 400 and "邀请码" in body["error"]


def test_api_invite_rejects_unknown_action(invite_home):
    code, body = web_server.api_invite_action({"action": "delete-everything"})
    assert code == 400 and "未知动作" in body["error"]


def test_api_invite_network_failure_is_a_400_not_a_500(invite_home, monkeypatch):
    """离线时返回**可读的 400**，而不是一个 500 堆栈 —— 界面要能直接显示这句话。"""
    def boom(*_a, **_k):
        raise OSError("offline")

    monkeypatch.setattr("requests.request", boom)
    invite_home.save_state({"user_id": "inv_1", "invite_code": "ABCD2345", "token": "tok"})

    code, body = web_server.api_invite_action({"action": "sync"})

    assert code == 400 and "联网" in body["error"]


def test_api_invite_status_exposes_notice_and_ack_clears_it(invite_home, monkeypatch):
    """概览页的到账提示：`GET` 给内容，`POST action=ack` 清掉 —— 且两者都**不联网**。"""
    def boom(*_a, **_k):
        raise AssertionError("提示与确认都不该联网")

    monkeypatch.setattr("requests.request", boom)
    invite_home.save_state({"invite_code": "ABCD2345", "token": "tok",
                            "ledger_cache": {"credited_this_month": 3, "monthly_cap": 4}})

    assert "新增 3 人" in web_server.api_invite_status()[1]["notice"]

    code, body = web_server.api_invite_action({"action": "ack"})

    assert code == 200 and body["result"] == {"acked": True}
    assert web_server.api_invite_status()[1]["notice"] is None, "点过「知道了」还冒就是 bug"
# -- /api/index 与 /api/describe（Review 页闭环的触发端点） ----------------

def test_api_index_ios_honestly_blocked(tmp_path):
    code, obj = web_server.api_index(tmp_path, {"package": "com.x", "platform": "ios"})
    assert code == 400 and "P3" in obj["error"]


def _wait_job(jid: str) -> dict:
    import time as _t
    for _ in range(200):
        if web_server._JOBS[jid]["status"] != "running":
            return web_server._JOBS[jid]
        _t.sleep(0.05)
    return web_server._JOBS[jid]


def test_api_index_empty_graph_fails_loudly(tmp_path, monkeypatch):
    """空图谱（0 控件）必须在 job 内响亮报错——静默成功=防幻觉底座已废（B2）。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    monkeypatch.setattr("insta_engine.web_api.indexing.get_driver", lambda s, p="android": object())
    monkeypatch.setattr("insta_engine.page_index.load_or_crawl",
                        lambda *a, **k: ({"pages": [], "schema": 2}, True))
    code, obj = web_server.api_index(tmp_path, {"package": "com.x"})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "error" and "图谱为空" in (job["error"] or "")


def test_api_index_happy_path_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    monkeypatch.setattr("insta_engine.web_api.indexing.get_driver", lambda s, p="android": object())
    monkeypatch.setattr("insta_engine.page_index.load_or_crawl",
                        lambda *a, **k: ({"pages": [{"node_count": 12}, {"node_count": 8}], "schema": 2}, False))
    code, obj = web_server.api_index(tmp_path, {"package": "com.x", "refresh": True})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "done" and "页面 2" in (job["summary"] or "") and "控件 20" in (job["summary"] or "")


def test_api_describe_without_graph_is_400(tmp_path):
    code, obj = web_server.api_describe(tmp_path, {"package": "com.missing"})
    assert code == 400 and "没有页面图谱" in obj["error"]


def test_api_describe_skips_manual_and_updates_ai(tmp_path, monkeypatch):
    """manual 标题不被覆盖；AI 描述写回标 ai。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    base = tmp_path / "artifacts" / "page_index"
    base.mkdir(parents=True)
    graph = {"schema": 2, "pages": [
        {"page_key": "pk_manual", "title": "人工名", "title_source": "manual",
         "summary_source": "", "structure": {"key_elements": ["k1"]}},
        {"page_key": "pk_ai", "title": "旧AI名", "title_source": "ai",
         "summary_source": "ai", "structure": {"key_elements": ["k2"]}},
        {"page_key": "pk_new", "title": "", "title_source": "",
         "summary_source": "", "structure": {"key_elements": ["k3"]}},
    ]}
    (base / "com.x.json").write_text(json.dumps(graph), encoding="utf-8")

    class _FakeGen:
        def __init__(self):
            pass

        @classmethod
        def from_env(cls):
            return cls()

        def describe_pages(self, payload):
            keys = [p["page_key"] for p in payload]
            assert keys == ["pk_new"], f"manual 与已有 ai 页都不得重算: {keys}"
            return {"pk_new": {"title": "AI 新名", "summary": "摘要"}}

    monkeypatch.setattr("insta_engine.web_api.describe.GenClient", _FakeGen)
    code, obj = web_server.api_describe(tmp_path, {"package": "com.x"})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "done" and "已补 1/1" in (job["summary"] or "")
    updated = json.loads((base / "com.x.json").read_text(encoding="utf-8"))
    pages = {p["page_key"]: p for p in updated["pages"]}
    assert pages["pk_manual"]["title"] == "人工名", "manual 不得被 AI 覆盖"
    assert pages["pk_new"]["title_source"] == "ai"


def test_api_describe_skips_manual_summary(tmp_path, monkeypatch):
    """人工写过的描述（summary_source=manual）不被 describe 重算——与人工标题同规则。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    base = tmp_path / "artifacts" / "page_index"
    base.mkdir(parents=True)
    graph = {"schema": 2, "pages": [
        {"page_key": "pk_manual_sum", "title": "", "title_source": "",
         "summary": "人工写的用途", "summary_source": "manual",
         "structure": {"key_elements": ["k1"]}},
        {"page_key": "pk_new", "title": "", "title_source": "",
         "summary_source": "", "structure": {"key_elements": ["k2"]}},
    ]}
    (base / "com.x.json").write_text(json.dumps(graph), encoding="utf-8")

    class _FakeGen:
        @classmethod
        def from_env(cls):
            return cls()

        def describe_pages(self, payload):
            keys = [p["page_key"] for p in payload]
            assert keys == ["pk_new"], f"manual 描述页不得重算: {keys}"
            return {"pk_new": {"title": "AI 名", "summary": "AI 用途"}}

    monkeypatch.setattr("insta_engine.web_api.describe.GenClient", _FakeGen)
    code, obj = web_server.api_describe(tmp_path, {"package": "com.x"})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "done"
    updated = json.loads((base / "com.x.json").read_text(encoding="utf-8"))
    pages = {p["page_key"]: p for p in updated["pages"]}
    assert pages["pk_manual_sum"]["summary"] == "人工写的用途"
    assert pages["pk_manual_sum"]["summary_source"] == "manual"


def test_api_app_probe_parses_ios_app_and_silent_fallback(tmp_path):
    """probe：.app 读 Info.plist 返回 bundleId；不存在的路径/未知后缀**静默回空**（200，不报错）。"""
    import plistlib as _pl

    from insta_engine.web_api.apps import api_app_probe
    app_dir = tmp_path / "Demo.app"
    app_dir.mkdir()
    with (app_dir / "Info.plist").open("wb") as fh:      # 二进制 plist（真机产物同格式）
        _pl.dump({"CFBundleIdentifier": "com.demo.app",
                  "CFBundleDisplayName": "Demo",
                  "CFBundleShortVersionString": "1.2"}, fh)
    code, obj = api_app_probe(str(app_dir))
    assert code == 200 and obj["package"] == "com.demo.app"
    assert obj["label"] == "Demo" and obj["version"] == "1.2"
    code, obj = api_app_probe(str(tmp_path / "nope.app"))
    assert code == 200 and obj["package"] == ""
    (tmp_path / "x.txt").write_text("hi", encoding="utf-8")
    code, obj = api_app_probe(str(tmp_path / "x.txt"))
    assert code == 200 and obj["package"] == ""


def test_api_app_probe_regex_fallback_for_malformed_plist(tmp_path):
    """残缺 plist（合法 XML + 尾部被追加说明文字）也必须解析出包名——正则兜底。

    真实案例：demo 构建脚本在 Info.plist 末尾追加了 `安装：xcrun simctl install …`
    说明文字，plistlib 报 ExpatError（junk after document element）。
    """
    from insta_engine.web_api.apps import api_app_probe
    app_dir = tmp_path / "Mat.app"
    app_dir.mkdir()
    (app_dir / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict>\n'
        "\t<key>CFBundleIdentifier</key>\n\t<string>com.mat.demo</string>\n"
        "\t<key>CFBundleShortVersionString</key>\n\t<string>1.0</string>\n"
        "</dict>\n</plist>\n安装：xcrun simctl install booted build/Mat.app\n",
        encoding="utf-8")
    code, obj = api_app_probe(str(app_dir))
    assert code == 200 and obj["package"] == "com.mat.demo"
    assert obj["version"] == "1.0"


def test_api_describe_all_filled_is_noop_200(tmp_path, monkeypatch):
    """全部已填充（ai/manual）时不开 job、不扣额度——200 noop 轻提示（非 400 error）。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    base = tmp_path / "artifacts" / "page_index"
    base.mkdir(parents=True)
    graph = {"schema": 2, "pages": [
        {"page_key": "pk_ai", "title": "T", "title_source": "ai",
         "summary": "s", "summary_source": "ai", "structure": {}},
        {"page_key": "pk_man", "title": "M", "title_source": "manual",
         "summary": "m", "summary_source": "manual", "structure": {}},
    ]}
    (base / "com.x.json").write_text(json.dumps(graph), encoding="utf-8")
    code, obj = web_server.api_describe(tmp_path, {"package": "com.x"})
    assert code == 200 and obj.get("noop") is True
    assert "job_id" not in obj, "noop 不开 job（额度零消耗）"
    assert "人工内容不会被覆盖" in obj["message"]


# -- /api/diagnose、/api/cred、/api/export（第二批） ------------------------

def _write_failed_run(root: Path, name: str = "failrun") -> Path:
    import json as _json
    run_dir = root / "artifacts" / name
    run_dir.mkdir(parents=True)
    payload = {"run": {"case_id": "case_t", "case_name": "失败样例", "status": "failed",
                       "step_success_rate": 0.0, "duration_ms": 100,
                       "steps": [{"step_id": "s1", "intent": "点击登录", "action": "tap",
                                  "status": "failed", "duration_ms": 50,
                                  "error": "AssertionError: 断言失败"}]}}
    (run_dir / "run.json").write_text(_json.dumps(payload), encoding="utf-8")
    return run_dir


def test_api_diagnose_graceful_without_gen_env(tmp_path, monkeypatch):
    monkeypatch.delenv("GEN_BASE_URL", raising=False)
    monkeypatch.delenv("GEN_API_KEY", raising=False)
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    run_dir = _write_failed_run(tmp_path)
    code, obj = web_server.api_diagnose(tmp_path, {"run": run_dir.name})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "error" and "缺少环境变量" in (job["error"] or ""), (
        "无 GEN 配置必须是可读的配置指引错误，而非裸 500")


def test_api_diagnose_no_failed_steps_is_400(tmp_path):
    import json as _json
    run_dir = tmp_path / "artifacts" / "okrun"
    run_dir.mkdir(parents=True)
    payload = {"run": {"case_id": "c", "case_name": "全过", "status": "passed",
                       "step_success_rate": 1.0, "duration_ms": 10,
                       "steps": [{"step_id": "s1", "intent": "启动", "action": "launch",
                                  "status": "passed", "duration_ms": 10}]}}
    (run_dir / "run.json").write_text(_json.dumps(payload), encoding="utf-8")
    code, obj = web_server.api_diagnose(tmp_path, {"run": "okrun"})
    assert code == 400 and "无失败步骤" in obj["error"]


def test_api_cred_roundtrip_local_only(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    host_local, host_lan = "127.0.0.1", "192.168.1.9"
    assert web_server.api_cred_list(host_lan)[0] == 403, "凭据管理不得暴露给局域网（--share）"
    code, obj = web_server.api_cred_write(host_local, {"action": "set", "name": "uat", "value": "secret"})
    assert code == 200 and "uat" in obj["names"]
    assert web_server.api_cred_list(host_local)[1]["names"] == ["uat"]
    code, obj = web_server.api_cred_write(host_local, {"action": "del", "name": "uat"})
    assert code == 200 and obj["names"] == []


def test_api_export_creates_zip(tmp_path):
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "a.json").write_text("{}", encoding="utf-8")
    code, obj = web_server.api_export(tmp_path, {"media": False})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "done"
    zip_path = tmp_path / "artifacts" / (job["link"] or "").rsplit("/", 1)[-1]
    assert zip_path.exists() and zip_path.suffix == ".zip"


# -- /api/calibrate（报告校准两层——分类改判 + 断言校准） -----------------

def _write_calibratable_run(root: Path, name: str = "calrun") -> None:
    import json as _json
    run_dir = root / "artifacts" / name
    run_dir.mkdir(parents=True)
    payload = {"case": {"caseId": "case_cal", "target": {"package": "com.x"}},
               "run": {"case_id": "case_cal", "case_name": "校准样例", "status": "failed",
                       "step_success_rate": 0.0, "duration_ms": 100,
                       "steps": [
                           {"step_id": "s1", "intent": "点击登录", "action": "tap",
                            "status": "failed", "duration_ms": 50,
                            "category": "locate-fail",
                            "error": "LocateFailed: 定位失败（策略链）: text=登录 未命中"},
                           {"step_id": "s2", "intent": "断言欢迎语", "action": "wait",
                            "status": "failed", "duration_ms": 30,
                            "category": "assert-fail",
                            "error": "断言失败: 期望文本 '热搜'，实际 '热榜'"}]}}
    (run_dir / "run.json").write_text(_json.dumps(payload, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    # 用例文件（断言校准要 patch 它）
    (root / "cases").mkdir(exist_ok=True)
    (root / "cases" / f"{name}.json").write_text(_json.dumps({
        "caseId": "case_cal", "name": "校准样例",
        "target": {"package": "com.x"},
        "steps": [
            {"id": "s2", "intent": "断言欢迎语", "action": "wait", "params": {"ms": 10},
             "assertions": [{"type": "text-equals", "expected": "热搜",
                             "target": {"strategy": "text", "value": "欢迎语"}}]},
        ]}, ensure_ascii=False), encoding="utf-8")


def test_api_calibrate_data_lists_failed_steps(tmp_path):
    _write_calibratable_run(tmp_path)
    code, obj = web_server.api_calibrate_data(tmp_path, "calrun")
    assert code == 200 and obj["run"] == "calrun"
    assert len(obj["items"]) == 2 and obj["items"][0]["category"] == "locate-fail"
    assert obj["items"][1]["assertion"]["expected"] == "热搜", (
        "断言失败的期望/实际必须被抠出来（这就是断言校准的原料）")
    assert "app-crash" in obj["categories"]


def test_api_calibrate_apply_all_layers(tmp_path, monkeypatch):
    """一次 apply 落三层：改判写 run.json（human）+ 规则落盘 + 断言校准出 -calibrated.json。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    _write_calibratable_run(tmp_path)
    body = {"run": "calrun", "steps": {
        "s1": {"category": "app-crash", "error_contains": "text=登录 未命中",
               "note": "登录按钮被改名，真崩了"},
        "s2": {"category": "assert-fail", "assertion_fix": "adopt-actual",
               "assertion_expected": "热搜", "assertion_actual": "热榜"},
    }}
    code, obj = web_server.api_calibrate_apply(tmp_path, "127.0.0.1", body)
    assert code == 200 and obj["applied"] == 1, "s1 改判；s2 分类未变（只做断言校准）⇒ unchanged"
    assert obj["unchanged"] == 1 and obj["rules_saved"] == 1

    run_json = tmp_path / "artifacts" / "calrun" / "run.json"
    data = json.loads(run_json.read_text(encoding="utf-8"))
    s1 = data["run"]["steps"][0]
    assert s1["category"] == "app-crash" and s1["category_source"] == "human"
    assert s1["category_before_calibration"] == "locate-fail", "改判前分类必须留痕"
    assert (tmp_path / "calibration" / "rules.json").exists(), "规则必须落盘（下次自动归类）"

    calibrated = tmp_path / "cases" / "calrun-calibrated.json"
    assert calibrated.exists(), "断言校准必须出 -calibrated.json（不覆盖原用例）"
    patched = json.loads(calibrated.read_text(encoding="utf-8"))
    assert patched["steps"][0]["assertions"][0]["expected"] == "热榜", "采纳实际值"

    # 报告重渲染带上了人工标记
    html = (tmp_path / "artifacts" / "calrun" / "report.html").read_text(encoding="utf-8")
    assert "human" in html or "人工" in html


def test_api_calibrate_apply_local_only(tmp_path):
    _write_calibratable_run(tmp_path)
    code, obj = web_server.api_calibrate_apply(
        tmp_path, "192.168.1.9", {"run": "calrun", "steps": {"s1": {"category": "app-crash"}}})
    assert code == 403, "规则会改变后续所有 run 的判定——不得暴露给局域网"


def test_api_calibrate_apply_run_mismatch_guard(tmp_path):
    """结论里的 run 与目标不符 ⇒ 中止（防改错报告，与 CLI 同源）。"""
    _write_calibratable_run(tmp_path)
    code, obj = web_server.api_calibrate_apply(
        tmp_path, "127.0.0.1",
        {"run": "calrun", "steps": {"s1": {"category": "app-crash",
                                           "wrong_run_note": "x"}}})
    assert code == 200  # steps 里不带 run 字段——run 标签由服务端构造，天然防错


# -- /api/apps/install、/api/apps/uninstall（测试准入） ------------------------

class _AppInfoDrv:
    serial = "emulator-5554"

    def app_info(self, pkg):
        return {"versionName": "9.9.9"}

    def screenshot_pil(self):
        import io

        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buf, format="PNG")
        return Image.open(buf)


def test_api_apps_install_android_happy(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    apk = tmp_path / "test.apk"
    apk.write_bytes(b"x")
    monkeypatch.setattr("insta_engine.web_api.apps.get_driver",
                        lambda s, p="android": _AppInfoDrv())
    cmds: list[list[str]] = []
    monkeypatch.setattr("insta_engine.web_api.apps.subprocess.run",
                        lambda cmd, **k: (cmds.append(cmd), mock.Mock(returncode=0,
                                             stdout="Success", stderr=""))[1])
    code, obj = web_server.api_apps_install(
        tmp_path, "127.0.0.1", {"serial": "emulator-5554", "platform": "android", "path": str(apk),
                   "package": "com.x"})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "done"
    assert any("install" in c and "-r" in c and "-t" in c for c in cmds), "覆盖安装+允许测试包"
    assert "版本 9.9.9" in (job["summary"] or ""), "安装后必须回显版本（T76 准入证据链）"


def test_api_apps_installed_lists_only_third_party(monkeypatch):
    """遍历探索的包名候选只列第三方（`pm list packages -3`），排序去重。"""
    monkeypatch.setattr("insta_engine.web_api.apps.list_adb_devices",
                        lambda: [{"serial": "dev-1", "state": "device"}])
    cmds: list[list[str]] = []
    monkeypatch.setattr("insta_engine.web_api.apps.subprocess.run",
                        lambda cmd, **k: (cmds.append(cmd), mock.Mock(
                            returncode=0, stdout="package:com.b\npackage:com.a\n",
                            stderr=""))[1])
    code, obj = web_server.api_apps_installed()
    assert code == 200 and obj["packages"] == ["com.a", "com.b"] and obj["serial"] == "dev-1"
    assert cmds[0][-1] == "-3", "必须带 -3 过滤系统包"


def test_api_apps_installed_requires_online_device(monkeypatch):
    monkeypatch.setattr("insta_engine.web_api.apps.list_adb_devices", lambda: [])
    code, obj = web_server.api_apps_installed()
    assert code == 400 and "设备" in obj["error"]


def test_api_apps_install_rejects_wrong_suffix(tmp_path):
    apk = tmp_path / "test.zip"
    apk.write_bytes(b"x")
    code, obj = web_server.api_apps_install(
        tmp_path, "127.0.0.1", {"serial": "emulator-5554", "platform": "android", "path": str(apk)})
    assert code == 400 and ".apk" in obj["error"]


def test_api_apps_install_ios_real_rejects_ipa_with_extract_hint(tmp_path):
    """真机装 .ipa ⇒ 400 且说清「需先解压」（devicectl 只接受 .app 目录）。"""
    ipa = tmp_path / "x.ipa"
    ipa.write_bytes(b"PK")
    code, obj = web_server.api_apps_install(
        tmp_path, "127.0.0.1", {"serial": "00008101-000A1B2C3D4E5F60", "platform": "ios",
                   "path": str(ipa)})
    assert code == 400 and ".app" in obj["error"] and "解压" in obj["error"]


def test_api_apps_install_ios_real_uses_devicectl(tmp_path, monkeypatch):
    """真机装 .app ⇒ 走 devicectl（`ios_device.install_app`），不再拒绝。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    app = tmp_path / "Demo.app"
    app.mkdir()
    calls: list[tuple] = []
    monkeypatch.setattr("insta_engine.ios_device.install_app",
                        lambda udid, path, timeout=180: (calls.append((udid, str(path))),
                                                         (True, ""))[1])
    code, obj = web_server.api_apps_install(
        tmp_path, "127.0.0.1", {"serial": "00008101-000A1B2C3D4E5F60", "platform": "ios",
                   "path": str(app)})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "done"
    assert calls == [("00008101-000A1B2C3D4E5F60", str(app))]


def test_api_apps_install_ios_real_failure_surfaces_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    app = tmp_path / "Demo.app"
    app.mkdir()
    monkeypatch.setattr("insta_engine.ios_device.install_app",
                        lambda udid, path, timeout=180: (False, "签名不匹配"))
    code, obj = web_server.api_apps_install(
        tmp_path, "127.0.0.1", {"serial": "00008101-000A1B2C3D4E5F60", "platform": "ios",
                   "path": str(app)})
    assert code == 200
    job = _wait_job(obj["job_id"])
    assert job["status"] == "error" and "签名不匹配" in (job["error"] or "")


def test_api_apps_uninstall_local_only_and_happy(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    assert web_server.api_apps_uninstall(
        tmp_path, "192.168.1.9",
        {"serial": "emulator-5554", "platform": "android", "package": "com.x"})[0] == 403, (
        "卸载是破坏性设备操作——分享模式下禁用")
    cmds: list[list[str]] = []
    monkeypatch.setattr("insta_engine.web_api.apps.subprocess.run",
                        lambda cmd, **k: (cmds.append(cmd), mock.Mock(returncode=0,
                                             stdout="Success", stderr=""))[1])
    code, obj = web_server.api_apps_uninstall(
        tmp_path, "127.0.0.1",
        {"serial": "emulator-5554", "platform": "android", "package": "com.x"})
    assert code == 200 and obj["ok"]
    assert any("uninstall" in c and "com.x" in c for c in cmds)


def test_api_apps_uninstall_harmony_uses_hdc(tmp_path, monkeypatch):
    """鸿蒙卸载必须走 hdc uninstall（此前一律落到 simctl，等于装错链路）。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(args, serial=None, timeout=20):
        calls.append((args, serial))
        return mock.Mock(returncode=0, stdout="uninstall bundle successfully", stderr="")

    monkeypatch.setattr("insta_engine.harmony_hdc._run", fake_run)
    code, obj = web_server.api_apps_uninstall(
        tmp_path, "127.0.0.1",
        {"serial": "127.0.0.1:5555", "platform": "harmony", "package": "com.example.app"})
    assert code == 200 and obj["ok"]
    assert calls == [(["uninstall", "com.example.app"], "127.0.0.1:5555")]


def test_api_apps_install_local_only(tmp_path):
    """安装与卸载同为改写设备状态的操作——分享模式下局域网来源禁用（评审 P1-2）。"""
    apk = tmp_path / "test.apk"
    apk.write_bytes(b"x")
    code, obj = web_server.api_apps_install(
        tmp_path, "192.168.1.9",
        {"serial": "emulator-5554", "platform": "android", "path": str(apk)})
    assert code == 403 and "仅限本机" in obj["error"]


def test_api_apps_install_rejects_unsafe_package(tmp_path):
    """package 会拼进 `dumpsys package <pkg>` 设备 shell——非法形状在边界 400。"""
    apk = tmp_path / "test.apk"
    apk.write_bytes(b"x")
    for bad in ("com.x; rm -rf /sdcard/x", "../../etc", "/abs/path", "com.x&reboot"):
        code, obj = web_server.api_apps_install(
            tmp_path, "127.0.0.1",
            {"serial": "emulator-5554", "platform": "android", "path": str(apk),
             "package": bad})
        assert code == 400 and "包名" in obj["error"], bad


@pytest.mark.parametrize("endpoint", ["api_index", "api_describe"])
@pytest.mark.parametrize("bad", ["../x", "/etc/x", "com.x;whoami", "~root", "c:win"])
def test_package_endpoints_reject_unsafe_package(tmp_path, endpoint, bad):
    """契约测试（评审 P1-1/P2-6）：所有把 package 拼进 artifacts 路径/设备 shell 的
    端点都必须过 safe_package_name 闸口——index/describe 曾漏（explore 早已有）。"""
    fn = getattr(web_server, endpoint)
    code, obj = fn(tmp_path, {"package": bad})
    assert code == 400 and "包名" in obj["error"], f"{endpoint} 放行了 {bad!r}"


def test_api_fs_browse_ext_filter(tmp_path):
    (tmp_path / "app.apk").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    code, obj = web_server.api_fs_browse("127.0.0.1", str(tmp_path), ".apk")
    assert code == 200 and obj["files"] == ["app.apk"] and obj["dirs"] == ["sub"]
    code, obj = web_server.api_fs_browse("127.0.0.1", str(tmp_path))
    assert obj.get("files", []) == [], "不带 ext 不得返回文件（工作区切换器行为不变）"


def test_local_only_gates_by_client_ip():
    """本机能力门控按客户端来源 IP 判定（2026-10-15 修复：旧实现误传 server_address，
    --share 下监听地址恒 0.0.0.0 ⇒ 分享模式连本机也被 403）。0.0.0.0 必拒是回归判据：
    谁再把 server 监听地址传进来，这条立刻红。"""
    from insta_engine.web_api.workspace import _local_only
    assert _local_only("127.0.0.1") is None
    assert _local_only("::1") is None
    for ip in ("192.168.1.9", "10.0.0.2", "0.0.0.0"):
        code, obj = _local_only(ip)
        assert code == 403 and "仅限本机" in obj["error"]


# -- 套件保存（编辑=原地覆盖/改标题=重命名删旧，发起人 2026-10-12 反馈编辑保存变新建） --

def _mk_case_and_suite(root: Path, title: str) -> str:
    from insta_engine.web_api.cases import api_suite_save
    (root / "cases").mkdir(exist_ok=True)
    (root / "cases" / "c1.json").write_text(_case_json(), encoding="utf-8")
    code, obj = api_suite_save(root, {"title": title, "cases": ["c1.json"]})
    assert code == 200, obj
    return obj["file"]


def test_suite_save_edit_same_title_overwrites_in_place(tmp_path):
    from insta_engine.web_api.cases import api_suite_save, list_suites
    f = _mk_case_and_suite(tmp_path, "冒烟套件")
    code, obj = api_suite_save(tmp_path, {"file": f, "title": "冒烟套件", "cases": ["c1.json"]})
    assert code == 200 and obj["file"] == f
    assert [s["file"] for s in list_suites(tmp_path)] == [f], "同标题编辑不得产生第二个套件"


def test_suite_save_edit_renamed_title_renames_not_duplicates(tmp_path):
    from insta_engine.web_api.cases import api_suite_save, list_suites
    f = _mk_case_and_suite(tmp_path, "旧名")
    code, obj = api_suite_save(tmp_path, {"file": f, "title": "新名", "cases": ["c1.json"]})
    assert code == 200 and obj["file"] == "新名.suite.json"
    names = [s["file"] for s in list_suites(tmp_path)]
    assert names == ["新名.suite.json"], f"改标题=重命名（写新删旧），旧文件不得残留: {names}"


def test_suite_save_edit_rename_collision_rejected(tmp_path):
    from insta_engine.web_api.cases import api_suite_save, list_suites
    _mk_case_and_suite(tmp_path, "甲")
    f2 = _mk_case_and_suite(tmp_path, "乙")
    code, obj = api_suite_save(tmp_path, {"file": f2, "title": "甲", "cases": ["c1.json"]})
    assert code == 400 and "已存在同名套件" in obj["error"]
    assert len(list_suites(tmp_path)) == 2, "撞名拒绝后两套件均须原样保留"


def test_suite_save_edit_missing_original_404(tmp_path):
    from insta_engine.web_api.cases import api_suite_save
    (tmp_path / "cases").mkdir(exist_ok=True)
    (tmp_path / "cases" / "c1.json").write_text(_case_json(), encoding="utf-8")
    code, obj = api_suite_save(tmp_path, {"file": "幽灵.suite.json", "title": "x", "cases": ["c1.json"]})
    assert code == 404 and "原套件不存在" in obj["error"]


def test_suite_save_with_null_file_creates_not_404(tmp_path):
    """🔴 回归（2026-10-25 用户实测）：「新建套件」前端传 file: null（JSON null），
    `str(body.get("file", ""))` 把 None 变成字符串 "None" → 解析出 cases/None.suite.json
    当"原套件" → 新建永远 404「原套件不存在: None」。null/缺失/空串统一按新建处理。"""
    from insta_engine.web_api.cases import api_suite_save, list_suites
    (tmp_path / "cases").mkdir(exist_ok=True)
    (tmp_path / "cases" / "c1.json").write_text(_case_json(), encoding="utf-8")
    for file_value in (None, ""):  # null 与空串统一按新建
        code, obj = api_suite_save(tmp_path, {"file": file_value, "title": "回归套件",
                                              "cases": ["c1.json"]})
        assert code == 200, (file_value, obj)
    assert [s["file"] for s in list_suites(tmp_path)] == ["回归套件.suite.json"]


# -- 用例重命名（2026-10-25，目录功能的轻量替代） --------------------------------------

def test_case_rename_updates_suite_references(tmp_path):
    """🔴 重命名带着套件引用走：条目文件名同步替换,内容原样保留。"""
    from insta_engine.web_api.cases import api_case_rename, api_suite_save, list_suites
    (tmp_path / "cases").mkdir(exist_ok=True)
    (tmp_path / "cases" / "c1.json").write_text(_case_json(), encoding="utf-8")
    (tmp_path / "cases" / "c2.json").write_text(_case_json(), encoding="utf-8")
    code, obj = api_suite_save(tmp_path, {"title": "混合", "cases": ["c1.json", "c2.json"]})
    assert code == 200, obj
    code, obj = api_case_rename(tmp_path, {"from": "c1.json", "to": "search-bt"})
    assert code == 200 and obj["file"] == "search-bt.json"
    assert obj["suites_updated"] == ["混合.suite.json"]
    suite = json.loads((tmp_path / "cases" / "混合.suite.json").read_text(encoding="utf-8"))
    assert suite["cases"] == ["search-bt.json", "c2.json"], "引用按位置替换,未引用条目不动"
    assert (tmp_path / "cases" / "search-bt.json").exists()
    assert not (tmp_path / "cases" / "c1.json").exists()
    # 套件清单不受影响
    assert [s["file"] for s in list_suites(tmp_path)] == ["混合.suite.json"]


def test_case_rename_rejections(tmp_path):
    """撞名 / .suite.json 命名空间 / 源不存在 / 非法名,全部拒绝且不产生半改状态。"""
    from insta_engine.web_api.cases import api_case_rename
    (tmp_path / "cases").mkdir(exist_ok=True)
    (tmp_path / "cases" / "a.json").write_text(_case_json(), encoding="utf-8")
    (tmp_path / "cases" / "b.json").write_text(_case_json(), encoding="utf-8")
    assert api_case_rename(tmp_path, {"from": "a.json", "to": "b.json"})[0] == 400
    assert api_case_rename(tmp_path, {"from": "a.json", "to": "x.suite.json"})[0] == 400
    assert api_case_rename(tmp_path, {"from": "幽灵.json", "to": "x.json"})[0] == 404
    assert api_case_rename(tmp_path, {"from": "../a.json", "to": "x.json"})[0] == 400
    assert (tmp_path / "cases" / "a.json").exists(), "拒绝路径不得动原文件"


# -- 编辑器 P0/P1：控件清单查询 + 保存警告（发起人 2026-10-12） --------------------

def _write_page_index(root: Path, package: str = "com.insta.demo") -> None:
    idx = root / "artifacts" / "page_index"
    idx.mkdir(parents=True, exist_ok=True)
    (idx / f"{package}.json").write_text(json.dumps({
        "pages": [
            {"activity": "com.insta.demo.MainActivity", "controls": [
                {"res-id": "com.insta.demo:id/submit", "text": "提交", "clickable": True},
                {"res-id": "com.insta.demo:id/result", "text": "结果", "clickable": True},
                {"res-id": "com.insta.demo:id/0_resource_name_obfuscated", "text": "", "clickable": True},
                {"text": "1,234", "clickable": True},          # 动态文案：不得成为候选
                {"text": "设置", "clickable": False},           # 不可点：text 候选要求 clickable
                {"desc": "返回", "clickable": True},            # 无 res-id：desc 作 label 的 text 候选
            ]},
        ]}, ensure_ascii=False), encoding="utf-8")


def test_controls_list_for_filters_and_dedups(tmp_path):
    from insta_engine.page_index import controls_list_for
    _write_page_index(tmp_path)
    items = controls_list_for("com.insta.demo", tmp_path / "artifacts" / "page_index")
    rids = [it["res_id"] for it in items]
    assert "com.insta.demo:id/submit" in rids
    assert "com.insta.demo:id/0_resource_name_obfuscated" not in rids, "退化 res-id 不得入清单"
    texts = [(it["res_id"], it["label"]) for it in items if not it["res_id"]]
    assert ("", "返回") in texts, "无 res-id 的可点控件按 text 候选给出"
    assert all(label != "1,234" for _, label in texts), "动态文案不得成为候选"
    assert controls_list_for("com.absent", tmp_path / "artifacts" / "page_index") == [], "无索引返回空"
    assert controls_list_for("com.insta.demo", tmp_path / "artifacts" / "no_such_dir") == []


def test_api_controls_validates_package(tmp_path):
    from insta_engine.web_api.cases import api_controls
    assert api_controls(tmp_path, "")[0] == 400
    assert api_controls(tmp_path, "a/b")[0] == 400
    assert api_controls(tmp_path, "..")[0] == 400
    _write_page_index(tmp_path)
    code, obj = api_controls(tmp_path, "com.insta.demo")
    assert code == 200 and obj["package"] == "com.insta.demo"
    assert obj["count"] == len(obj["controls"]) >= 1
    assert obj["controls"][0]["page"].startswith("com.insta.demo.MainActivity")


def _case_with_problems() -> str:
    case = TestCase.model_validate({
        "caseId": "case_warn", "name": "问题用例", "target": {"package": "com.insta.demo"},
        "steps": [
            {"id": "s1", "intent": "输入", "action": "input", "params": {}},
            {"id": "s2", "intent": "点击", "action": "tap",
             "target": {"strategy": "res-id", "value": "com.insta.demo:id/submitX"}},
            {"id": "s3", "intent": "清空", "action": "clear",
             "assertions": [{"type": "text-equals", "expected": " "},
                             {"type": "exists"}]},
        ],
    })
    return dump_dsl(case)


def test_case_save_warnings(tmp_path):
    from insta_engine.web_api.cases import api_case_save
    _write_page_index(tmp_path)
    code, obj = api_case_save(tmp_path, {"name": "warn.json", "content": _case_with_problems()})
    assert code == 200 and obj["ok"] is True
    warns = "\n".join(obj["warnings"])
    assert "input 缺少输入文本" in warns
    assert "res-id「com.insta.demo:id/submitX」不在控件清单" in warns
    assert "缺少定位目标（target）" in warns, "clear 无 target 须警告"
    assert "断言1（text-equals）：缺少 expected" in warns
    assert "断言2（exists）：缺少 target（执行时会直接失败）" in warns
    # 干净用例零警告（submit/result 在清单里；断言目标≠操作目标，避开 L4 自指）
    ok_case = TestCase.model_validate({
        "caseId": "case_ok", "name": "干净用例", "target": {"package": "com.insta.demo"},
        "steps": [{"id": "s1", "intent": "点提交", "action": "tap",
                   "target": {"strategy": "res-id", "value": "com.insta.demo:id/submit"},
                   "assertions": [{"type": "exists",
                                   "target": {"strategy": "res-id",
                                              "value": "com.insta.demo:id/result"}}]}],
    })
    code, obj = api_case_save(tmp_path, {"name": "ok.json", "content": dump_dsl(ok_case)})
    assert code == 200 and obj["warnings"] == [], f"干净用例不应有警告: {obj['warnings']}"
    # 非法 JSON 仍 400
    assert api_case_save(tmp_path, {"name": "bad.json", "content": "{oops"})[0] == 400


def test_case_delete_and_suite_ref_guard(tmp_path):
    """用例删除（发起人 2026-10-12 三问之一）：被套件引用 409、无引用删净、非法名 400。"""
    from insta_engine.web_api.cases import api_case_delete
    from insta_engine.web_api.workspace import case_meta

    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "used.json").write_text(json.dumps(
        {"caseId": "case_used", "name": "被引用", "target": {"package": "com.demo"}, "steps": []},
        ensure_ascii=False), encoding="utf-8")
    (tmp_path / "cases" / "free.json").write_text(json.dumps(
        {"caseId": "case_free", "name": "自由", "target": {"package": "com.demo"}, "steps": []},
        ensure_ascii=False), encoding="utf-8")
    # case_meta 透出 caseId（三问之三的数据源）
    assert case_meta(tmp_path)["used.json"]["caseId"] == "case_used"

    # 被套件引用（含「cases/x.json」前缀形态）⇒ 409 且文件仍在
    (tmp_path / "cases" / "smoke.suite.json").write_text(json.dumps(
        {"name": "smoke", "cases": ["used.json", "cases/used.json"]}, ensure_ascii=False),
        encoding="utf-8")
    code, obj = api_case_delete(tmp_path, {"name": "used.json"})
    assert code == 409 and "smoke.suite.json" in obj["error"] and (tmp_path / "cases" / "used.json").exists()

    # 无引用 ⇒ 删净
    code, obj = api_case_delete(tmp_path, {"name": "free.json"})
    assert code == 200 and obj["deleted"] == "free.json"
    assert not (tmp_path / "cases" / "free.json").exists()

    # 套件文件/路径穿越/不存在 ⇒ 400
    assert api_case_delete(tmp_path, {"name": "smoke.suite.json"})[0] == 400
    assert api_case_delete(tmp_path, {"name": "../cases/free.json"})[0] == 400
    assert api_case_delete(tmp_path, {"name": "free.json"})[0] == 400


def test_case_save_accepts_dict_content(tmp_path):
    """结构化编辑器曾把对象直接发上来——边界兜底：dict content 转正规 JSON 落盘。

    实测报错（发起人 2026-10-12 截图）：str(dict) 是 Python 单引号串，
    model_validate_json 报「key must be a string at line 1 column 2」。
    """
    from insta_engine.web_api.cases import api_case_save
    payload = {"caseId": "case_d1", "name": "字典内容", "target": {"package": "com.demo"},
               "steps": [{"id": "s1", "action": "wait", "params": {"ms": 10}}]}
    code, obj = api_case_save(tmp_path, {"name": "dict.json", "content": payload})
    assert code == 200 and obj["ok"] is True
    saved = json.loads((tmp_path / "cases" / "dict.json").read_text(encoding="utf-8"))
    assert saved["caseId"] == "case_d1" and saved["name"] == "字典内容"


def test_list_runs_exposes_batch(tmp_path):
    from insta_engine.web_api.workspace import list_runs
    arts = tmp_path / "artifacts"
    # 套件批次条目：run.batch 透出
    d1 = arts / "01_login"
    d1.mkdir(parents=True)
    (d1 / "run.json").write_text(json.dumps({
        "finished_at": "2026-10-12T10:00:00+00:00",
        "case": {"target": {"package": "com.insta.demo"}},
        "run": {"case_id": "c1", "case_name": "登录", "status": "passed",
                "step_success_rate": 1.0, "failed_categories": {},
                "batch": {"id": "smoke#20261012090000", "suite": "smoke",
                          "index": 1, "total": 2}},
    }, ensure_ascii=False), encoding="utf-8")
    # 旧产物：无 batch → None（前端平铺兼容）
    d2 = arts / "single"
    d2.mkdir(parents=True)
    (d2 / "run.json").write_text(json.dumps({
        "finished_at": "2026-10-12T09:00:00+00:00",
        "case": {"target": {"package": "com.insta.demo"}},
        "run": {"case_id": "c2", "status": "failed", "step_success_rate": 0.5,
                "failed_categories": {"assert-fail": 1}},
    }, ensure_ascii=False), encoding="utf-8")
    runs = list_runs(tmp_path)
    assert len(runs) == 2
    by_dir = {r["dir"]: r for r in runs}
    assert by_dir["01_login"]["batch"] == {"id": "smoke#20261012090000",
                                           "suite": "smoke", "index": 1, "total": 2}
    assert by_dir["single"]["batch"] is None


# -- 探索异常校准 + 标记类 run 校准（发起人 2026-10-13） ----------------------------

def _write_explore(root: Path, name: str = "expcal") -> None:
    import json as _json
    exp_dir = root / "artifacts" / "explore" / name
    exp_dir.mkdir(parents=True)
    (exp_dir / "explore.json").write_text(_json.dumps({
        "explore_id": name, "package": "com.x", "steps": 10, "page_count": 2,
        "max_depth": 2, "duration_seconds": 3.0, "generated_at": "2026-10-13T10:00:00",
        "depth_stats": {"L0": 1, "L1": 1}, "pages": [], "edges": [],
        "findings": [
            {"type": "white-screen", "detail": "页面白屏", "path": "L0 → L1",
             "page_key": "p1", "screenshot": "shots/f0.png"},
            {"type": "white-screen", "detail": "深层白屏", "path": "L0 → L1 → L2",
             "page_key": "p2"},
            {"type": "crash", "detail": "进程崩溃", "path": "L0 → L2"},
        ]}, ensure_ascii=False), encoding="utf-8")


def test_api_explore_calibrate_roundtrip(tmp_path, monkeypatch):
    """探索校准：confirmed/ignored 回写 + operator 留痕 + 报告重渲染徽标 + 列表 calibrated 标记。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    _write_explore(tmp_path)
    body = {"explore_id": "expcal", "operator": "测试员",
            "findings": {"0": {"verdict": "ignored", "note": "加载慢误报"},
                         "1": {"verdict": "confirmed"}}}
    code, obj = web_server.api_explore_calibrate(tmp_path, "127.0.0.1", body)
    assert code == 200 and obj["ok"] and obj["ignored"] == 1 and obj["confirmed"] == 1

    data = json.loads((tmp_path / "artifacts" / "explore" / "expcal" / "explore.json")
                      .read_text(encoding="utf-8"))
    cal = data["findings"][0]["calibration"]
    assert cal["verdict"] == "ignored" and cal["note"] == "加载慢误报"
    assert cal["operator"] == "测试员", "校准人要留痕（团队审计口径）"
    # 原始 findings 不删不改（只追加 calibration 字段）——列表计数口径不变
    assert data["findings"][0]["type"] == "white-screen" and len(data["findings"]) == 3

    html = (tmp_path / "artifacts" / "explore" / "expcal" / "report.html").read_text(encoding="utf-8")
    assert "共 2 处（同因合并）" in html, "同 type 前缀链的两处并一行（合并展开明细里才有逐处标注）"
    assert "已忽略" in html and "已确认" in html, "组内逐处徽标 + 组徽标"
    assert "校准原因: 加载慢误报" in html
    assert "进程崩溃" in html, "不同 type 保持独立一行"

    # 报告列表透出「已校准」标记（ExploreRow Tag 的数据源）
    row = next(r for r in workspace.list_explores(tmp_path) if r["id"] == "expcal")
    assert row["calibrated"] is True

    # verdict="" 清除标注（撤销）：全部清掉 ⇒ calibrated 回落 False
    code, obj = web_server.api_explore_calibrate(
        tmp_path, "127.0.0.1",
        {"explore_id": "expcal", "findings": {"0": {"verdict": ""}, "1": {"verdict": ""}}})
    assert code == 200 and obj["cleared"] == 2
    data = json.loads((tmp_path / "artifacts" / "explore" / "expcal" / "explore.json")
                      .read_text(encoding="utf-8"))
    assert all("calibration" not in f for f in data["findings"])
    row = next(r for r in workspace.list_explores(tmp_path) if r["id"] == "expcal")
    assert row["calibrated"] is False

    # 越界下标必须报出来（不静默丢弃）
    code, obj = web_server.api_explore_calibrate(
        tmp_path, "127.0.0.1", {"explore_id": "expcal", "findings": {"99": {"verdict": "ignored"}}})
    assert code == 200 and obj["missing"] == ["99"]


def test_api_explore_calibrate_local_only(tmp_path):
    _write_explore(tmp_path)
    code, obj = web_server.api_explore_calibrate(
        tmp_path, "192.168.1.9",
        {"explore_id": "expcal", "findings": {"0": {"verdict": "ignored"}}})
    assert code == 403, "探索异常判定与 run 校准同源——不得暴露给局域网"


def test_api_calibrate_apply_marks_ignore_and_false_positive(tmp_path, monkeypatch):
    """标记类校准在线应用：纯标记（不改判）也要落 run.json + 重渲染徽标；passed 步骤进校准数据。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    run_dir = tmp_path / "artifacts" / "calmark"
    run_dir.mkdir(parents=True)
    payload = {"run": {"case_id": "case_mark", "case_name": "标记样例", "status": "failed",
                       "step_success_rate": 0.5, "duration_ms": 10,
                       "steps": [
                           {"step_id": "s0", "intent": "启动", "action": "launch",
                            "status": "passed", "duration_ms": 5},
                           {"step_id": "s1", "intent": "点击登录", "action": "tap",
                            "status": "failed", "duration_ms": 5, "category": "locate-fail",
                            "error": "LocateFailed: 定位失败（策略链）: text=登录 未命中"}]}}
    (run_dir / "run.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # 成功用例校准「是否误判」的数据源：passed 步骤也要出现在校准数据里
    code, obj = web_server.api_calibrate_data(tmp_path, "calmark")
    assert code == 200 and [i["step_id"] for i in obj["items"]] == ["s0", "s1"]

    body = {"run": "calmark", "steps": {
        "s0": {"false_positive": True, "note": "断言没生效，侥幸通过"},
        "s1": {"ignore": True, "note": "风控页挡住，非产品缺陷"}}}
    code, obj = web_server.api_calibrate_apply(tmp_path, "127.0.0.1", body)
    assert code == 200, obj
    assert obj["marked"] == 2 and obj["applied"] == 0, "纯标记不改判也要报出来"

    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    steps = {s["step_id"]: s for s in data["run"]["steps"]}
    assert steps["s0"]["false_positive"] is True
    assert steps["s1"]["ignored"] is True and steps["s1"]["calibration_note"]
    assert steps["s1"]["status"] == "failed", "标记不改判定（对账口径不变）"

    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "已忽略" in html and "误判" in html and "校准原因" in html


def test_ios_state_devices_only_wired_and_tagged(monkeypatch):
    """控制台设备清单（2026-10-19）：iOS **只列 USB** 真机，serial=udid、platform=ios。

    Wi-Fi 设备列出来用户一点投屏就是必失败（USBClient 只认 USB），所以这里守住判据。
    """
    from insta_engine.web_api import device as dev_api

    wired = {"udid": "00008101-000A1B2C3D4E5F60", "name": "手机", "model": "iPhone 15",
             "os_version": "17.4", "transport": "wired"}
    wifi = dict(wired, udid="00008110-000B2C3D4E5F6071", name="平板",
                model="iPad Air", transport="localNetwork")
    monkeypatch.setattr("insta_engine.ios_device.list_devices", lambda: [wifi, wired])
    dev_api._ios_cache[:] = [0.0, []]  # 清 TTL 缓存，避免命中上一轮
    out = dev_api.ios_state_devices()
    assert [d["serial"] for d in out] == ["00008101-000A1B2C3D4E5F60"], "只列 USB 真机"
    assert out[0]["platform"] == "ios" and out[0]["state"] == "device"
    assert out[0]["model"] == "iPhone 15" and out[0]["os"] == "17.4"


# -- 成员名额：本机恒 1 人；共享模式按授权给（2026-10-23 / 2026-10-25） -------------

def test_seats_endpoint_reports_local_single_seat(server):
    """本机监听（默认 `mat console`）⇒ 上限 1、shared=false。

    `--share`（共享模式）由 `make_server(host="0.0.0.0")` 触发；绑定全部网卡在测试里
    不稳定，所以那一侧的口径由 test_seat_quota.py 的 `seat_limit(shared=True)` 成对锁住
    （2026-10-25：仅团队授权才给名额，未授权恒 1）。
    """
    base, _ = server
    data = requests.get(f"{base}/api/seats", timeout=5).json()
    assert data["limit"] == 1, "本机控制台不该出现第二个成员名额"
    assert data["shared"] is False


# -- 前置拒跑（P1-7，2026-10-24）：web 与 CLI 同口径 --------------------------------

def _unprovisionable_case_json() -> str:
    case = TestCase.model_validate({
        "caseId": "case_perm", "name": "位置权限用例", "target": {"package": "com.insta.demo"},
        "preconditions": ["位置权限尚未授予"],
        "steps": [{"id": "s1", "action": "wait", "params": {"ms": 10}}],
    })
    return dump_dsl(case)


def test_api_run_refuses_unprovisionable_precondition(tmp_path, monkeypatch):
    """带否定式权限前置的用例：web 必须与 CLI 同口径**拒跑**（P1-7）——
    此前控制台照跑并产出假定位失败报告，用户按报告排查、方向从源头就错。
    拒跑发生在取设备之前：job 正常完成（非 error）、事件流有 refused、报告落盘。
    """
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    root = tmp_path / "ws"
    (root / "cases").mkdir(parents=True)
    (root / "cases" / "perm.json").write_text(_unprovisionable_case_json(), encoding="utf-8")

    def no_device(*_a, **_k):  # 拒跑在 get_driver 之前——被调用即失败
        raise AssertionError("refused case must not touch a device")

    monkeypatch.setattr("insta_engine.web_api.runs.get_driver", no_device)
    code, obj = web_server.api_run(root, {"case": "perm.json"})
    assert code == 200, obj
    job = _wait_job(obj["job_id"])
    assert job["status"] == "done", f"拒跑应正常完成，实际 {job['status']}"
    assert any(e.get("status") == "refused" or "拒跑" in str(e.get("detail", ""))
               for e in job["events"]), job["events"]
    assert (root / "artifacts" / "perm" / "report.html").exists(), "最小报告必须落盘"


def test_api_run_allow_unmet_preconditions_bypasses_refusal(tmp_path, monkeypatch):
    """allowUnmetPreconditions 放行（与 CLI --allow-unmet-preconditions 同语义）：
    闸门不能是永远打不开的门——放行后按正常链路走（无设备则优雅落 error）。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    root = tmp_path / "ws"
    (root / "cases").mkdir(parents=True)
    (root / "cases" / "perm.json").write_text(_unprovisionable_case_json(), encoding="utf-8")

    def no_device(*_a, **_k):
        raise RuntimeError("Can't find any android device/emulator")

    monkeypatch.setattr("insta_engine.web_api.runs.get_driver", no_device)
    code, obj = web_server.api_run(root, {"case": "perm.json", "allowUnmetPreconditions": True})
    assert code == 200, obj
    job = _wait_job(obj["job_id"])
    assert job["status"] == "error", "放行后走正常执行链路（无设备优雅落 error）"


def test_api_run_suite_refuses_entry_without_device(tmp_path, monkeypatch):
    """套件逐条拒跑（P1-7）：refused 条目 emit skipped 事件、不取设备；
    后续正常条目才走设备链路（无设备时 job 落 error，但拒跑事件已可查）。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    root = tmp_path / "ws"
    (root / "cases").mkdir(parents=True)
    # a_perm 排在前面：先拒跑（continue），b 才触发取设备
    (root / "cases" / "a_perm.json").write_text(_unprovisionable_case_json(), encoding="utf-8")
    (root / "cases" / "b.json").write_text(_case_json(), encoding="utf-8")
    (root / "cases" / "s.suite.json").write_text(
        json.dumps({"name": "s", "cases": ["a_perm.json", "b.json"]}), encoding="utf-8")

    def no_device(*_a, **_k):
        raise RuntimeError("Can't find any android device/emulator")

    monkeypatch.setattr("insta_engine.web_api.runs.get_driver", no_device)
    code, obj = web_server.api_run(root, {"case": "s.suite.json"})
    assert code == 200, obj
    job = _wait_job(obj["job_id"])
    assert job["status"] == "error"
    assert any("拒跑" in str(e.get("detail", "")) for e in job["events"]), \
        f"拒跑条目必须在事件流可见，实际 {job['events']}"
    assert (root / "artifacts" / "01_a_perm" / "report.html").exists()


def test_api_run_app_batch_no_tmp_file(tmp_path, monkeypatch):
    """--app 批量（评审附带项，2026-10-24 修）：与多选分支同法内存构建套件，
    不得再往 cases/ 写 `_batch_<pkg>.json`（污染列表、坏 JSON 曾致 500）。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    root = tmp_path / "ws"
    (root / "cases").mkdir(parents=True)
    (root / "cases" / "a.json").write_text(_case_json(), encoding="utf-8")

    def no_device(*_a, **_k):
        raise RuntimeError("Can't find any android device/emulator")

    monkeypatch.setattr("insta_engine.web_api.runs.get_driver", no_device)
    code, obj = web_server.api_run(root, {"app": "com.insta.demo"})
    assert code == 200, obj
    assert not list((root / "cases").glob("_batch_*")), "--app 批量不得落盘临时套件"


def test_api_state_hides_leftover_batch_files(tmp_path, monkeypatch):
    """兜底过滤：旧工作区残留的 `_batch_*.json`（即使是坏 JSON）不进用例列表。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    root = tmp_path / "ws"
    (root / "cases").mkdir(parents=True)
    (root / "cases" / "good.json").write_text(_case_json(), encoding="utf-8")
    (root / "cases" / "_batch_com_x.json").write_text("{broken", encoding="utf-8")
    code, obj = web_server.api_state(root)
    assert code == 200
    assert obj["cases"] == ["good.json"]


# -- /api/app/probe 来源门控（MAT 评估 2026-10-24 P1-6） ---------------------------

def test_api_app_probe_local_only_gating(tmp_path):
    """probe 可探测任意本机安装包 ⇒ 必须与 /api/fs 同门控：局域网来源 403，本机放行。

    此前 docstring 宣称"同信任模型"但实现没有 `_local_only`——`--share` 下持口令的
    局域网用户可枚举任意 APK 的包名/版本。
    """
    from insta_engine.web_api.apps import api_app_probe

    code, obj = api_app_probe(str(tmp_path / "x.apk"), host="192.168.1.23")
    assert code == 403 and "仅限本机" in obj["error"]

    code, obj = api_app_probe(str(tmp_path / "x.apk"), host="127.0.0.1")
    assert code == 200 and obj == {"package": "", "label": "", "version": ""}, \
        "本机来源照常（不存在的路径静默回空）"


# -- /live 实时视图页（2026-10-24，T4 演示包组件） ------------------------------

def _make_job(meta=None):
    import insta_engine.web_server as ws

    def job(emit, cancel):
        emit("s1", "passed", "ok", 12, "screenshots/step_01.png")
        return "done", None

    return ws.start_job("run", job, meta=meta or {})


def test_live_page_unknown_job_is_404_html(server):
    base, _ = server
    r = requests.get(f"{base}/live/nonexistent00", timeout=5)
    assert r.status_code == 404
    assert "任务不存在" in r.text
    assert "text/html" in r.headers["Content-Type"]


def test_live_page_renders_job_and_poll_endpoints(server):
    base, _ = server
    jid = _make_job(meta={"serial": "SER123", "platform": "android", "operator": "u"})
    # 等 job 跑完（worker 很快），页面任何状态都必须可渲染
    _wait_job(jid)
    r = requests.get(f"{base}/live/{jid}", timeout=5)
    assert r.status_code == 200 and "text/html" in r.headers["Content-Type"]
    body = r.text
    assert jid in body, "页面要内嵌 job_id 供轮询"
    assert "/api/job/events?id=" in body
    assert "/api/screenshot?serial=" in body, "带 serial 的任务应显示投屏"


def test_live_page_without_serial_shows_hint(server):
    base, _ = server
    jid = _make_job(meta={})  # 无 serial（如纯 API 用例/生成任务）
    _wait_job(jid)
    r = requests.get(f"{base}/live/{jid}", timeout=5)
    assert r.status_code == 200
    assert "该任务不使用设备" in r.text
    assert '"serial": ""' in r.text, "boot 数据里 serial 必须为空（投屏分支关闭）"


def test_live_without_id_picks_latest_job(server):
    base, _ = server
    from insta_engine.web_api.live import latest_job_id

    _make_job(meta={"serial": "SER1"})
    jid2 = _make_job(meta={"serial": "SER2"})
    _wait_job(jid2)
    assert latest_job_id() == jid2
    r = requests.get(f"{base}/live", timeout=5)
    assert r.status_code == 200 and jid2 in r.text, "/live 应落到最近的任务"


def test_live_page_not_swallowed_by_spa_fallback(server):
    """/live/* 必须在 SPA 兜底之前接管——被 index.html 吞掉 = 空白页。"""
    base, _ = server
    jid = _make_job(meta={"serial": "S"})
    _wait_job(jid)
    r = requests.get(f"{base}/live/{jid}", timeout=5)
    assert "MAT 实时视图" in r.text, "必须是 live 页本体，不是 console 的 index.html"


def test_api_run_response_carries_live_link(tmp_path, monkeypatch):
    """run/explore 响应带 live 链接（agent/前端给用户的可点入口）。"""
    monkeypatch.setenv("MAT_ALLOW_STALE_CODE", "1")
    root = tmp_path / "ws"
    (root / "cases").mkdir(parents=True)
    (root / "cases" / "r.json").write_text(_case_json(), encoding="utf-8")

    def no_device(*_a, **_k):
        raise RuntimeError("no device")

    monkeypatch.setattr("insta_engine.web_api.runs.get_driver", no_device)
    code, obj = web_server.api_run(root, {"case": "r.json"})
    assert code == 200 and obj["live"] == f"/live/{obj['job_id']}"


# -- /live 与 --share 口令门（2026-10-25 评审 P1 回归） ---------------------------

@pytest.fixture()
def shared_server(tmp_path, monkeypatch):
    """带访问口令的真 HTTP 服务器（--share 形态）。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    key = "share-pass-123"
    srv = web_server.make_server(0, root=tmp_path, auth_key=key)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            requests.get(f"{base}/api/state", timeout=1)
            break
        except requests.ConnectionError:
            time.sleep(0.1)
    yield base, key
    srv.shutdown()
    srv.server_close()


def test_live_page_requires_auth_under_share(shared_server):
    """P1 回归：--share 未认证 → /live 必须给门槛页（HTML 非 JSON），且响应体
    **不得含 serial**（首版曾绕过白名单直读页面元数据，真机探针实证后修）。"""
    base, key = shared_server
    jid = _make_job(meta={"serial": "SER_SECRET", "platform": "android", "operator": "u"})
    _wait_job(jid)

    for path in (f"/live/{jid}", "/live"):
        r = requests.get(f"{base}{path}", timeout=5)
        assert r.status_code == 401, path
        assert "text/html" in r.headers["Content-Type"], "页面类路径应给门槛页而非 401 JSON"
        assert "SER_SECRET" not in r.text, f"门槛页不得泄漏 serial: {path}"

    # 带口令仍可用（门槛不是死门）：登录换 cookie 后 200 且含 BOOT 元数据
    s = requests.Session()
    assert s.post(f"{base}/api/auth", json={"key": key}, timeout=5).status_code == 200
    r3 = s.get(f"{base}/live/{jid}", timeout=5)
    assert r3.status_code == 200 and "SER_SECRET" in r3.text


def test_finetune_stats_and_export(server):
    """微调数据域：stats 走 GET、export 走 POST；数据不足也如实 200（不 500）。

    回归自 2026-10-29 review（P1-9）：两个端点此前零测试，stats 曾误注册进 POST 表
    导致 GET 404——注册表问题只有走真 HTTP 才暴露。
    """
    base, root = server
    run_dir = root / "artifacts" / "ft-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run": {"status": "passed"},
        "case": {"caseId": "ft1", "name": "微调样本", "target": {"package": "com.x"},
                 "steps": [{"id": "s1", "action": "wait", "params": {"ms": 1}}],
                 "meta": {"nl": "打开设置"}},
    }, ensure_ascii=False), encoding="utf-8")
    r = requests.get(f"{base}/api/finetune/stats", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body["pairs"] == 1 and body["min_pairs"] == 200
    assert body["train_pairs"] == 0, "唯一一条被切成留出集 ⇒ 可训练对为 0（门槛如实）"
    p = requests.post(f"{base}/api/finetune/export", timeout=10, json={})
    assert p.status_code == 200 and p.json()["ok"]
    row = json.loads((root / "dataset" / "holdout.jsonl").read_text(
        encoding="utf-8").splitlines()[0])
    assert row["messages"][0]["role"] == "user", "JSONL 必须是 mlx_lm chat 形状"
