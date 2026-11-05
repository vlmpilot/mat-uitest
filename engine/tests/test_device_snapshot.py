"""`/api/state` 设备快照层（2026-11-04）：探测挪出请求线程后的时延与一致性。

真问题：`harmony_state_devices()` 冷缓存要 1.3s（hdc 探测），每台设备还要两次
`hdc.shell(timeout=10)`；`list_adb_devices()` 冷启动 timeout=10 且失败回退 adbutils。
原来这些同步拼在 `/api/state` 里，插着一台离线设备就能让控制台开页白屏十几秒。

这里用**慢探测**（`Event` 门控 / `sleep`）复现原症状，并证明：
请求立即返回旧快照、TTL 内不重复探测、并发单飞、失败不清旧值、冷启动有时限。
"""

import threading
import time

import pytest
import requests

from insta_engine import web_server
from insta_engine.web_api import device

OLD_DEVICES = [{"serial": "fake-1", "state": "device", "platform": "android"}]
NEW_DEVICES = [{"serial": "fake-2", "state": "device", "platform": "android"}]


@pytest.fixture()
def fast_server(tmp_path, monkeypatch):
    """真 HTTP 服务器；**建服前**就把探测换成即时假实现。

    这样预热请求不会去跑真实的 adb/hdc（本机慢路径 1.3s～十几秒），
    测试里的时延断言才有确定的分母；要测慢探测的场景由用例自己换回去。
    """
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    device.reset_devices_snapshot()
    monkeypatch.setattr(device, "_probe_devices", lambda: list(OLD_DEVICES))
    srv = web_server.make_server(0, root=tmp_path)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            requests.get(f"{base}/api/state", timeout=1)
            break
        except (requests.ConnectionError, requests.ReadTimeout):
            time.sleep(0.05)
    else:
        pytest.fail("测试服务器 15s 内未就绪", pytrace=False)
    yield base
    srv.shutdown()
    srv.server_close()
    device.reset_devices_snapshot()


def test_state_does_not_block_on_slow_probe(fast_server, monkeypatch):
    """慢探测在后台跑：`/api/state` 立即返回旧快照，< 200ms。"""
    base = fast_server
    seen = threading.Event()
    release = threading.Event()

    def slow_probe():
        seen.set()
        release.wait(10)
        return list(NEW_DEVICES)

    monkeypatch.setattr(device, "_probe_devices", slow_probe)
    monkeypatch.setattr(device, "_DEVICES_TTL", 0.0)  # 强制判定过期，触发后台刷新

    started = time.perf_counter()
    resp = requests.get(f"{base}/api/state", timeout=3)
    elapsed = time.perf_counter() - started

    assert resp.status_code == 200
    body = resp.json()
    # 旧快照原样返回，没有被"正在探测"清空
    assert body["devices"] == OLD_DEVICES
    assert isinstance(body["devices_asof"], int)
    assert elapsed < 0.2, f"/api/state 被探测阻塞：{elapsed * 1000:.0f}ms"
    assert seen.wait(2), "过期后没有触发后台刷新"
    release.set()


def test_ttl_suppresses_repeat_probe(fast_server, monkeypatch):
    """TTL 内连续 5 次请求只探测 1 次（含首次冷启动触发的那次）。"""
    base = fast_server
    calls: list[int] = []

    def counting_probe():
        calls.append(1)
        return list(NEW_DEVICES)

    monkeypatch.setattr(device, "_probe_devices", counting_probe)
    device.reset_devices_snapshot()  # 从"完全没有快照"开始，计入冷启动那一次

    for _ in range(5):
        assert requests.get(f"{base}/api/state", timeout=5).status_code == 200

    assert len(calls) == 1, f"TTL 内重复探测了 {len(calls)} 次"


def test_single_flight_under_concurrency(fast_server, monkeypatch):
    """并发 10 个请求同时撞上过期：后台只起 1 个探测线程。"""
    base = fast_server
    calls: list[int] = []
    release = threading.Event()

    def slow_probe():
        calls.append(1)
        release.wait(5)
        return list(NEW_DEVICES)

    monkeypatch.setattr(device, "_probe_devices", slow_probe)
    device.reset_devices_snapshot()

    responses: list[dict] = []
    errors: list[str] = []

    def hit():
        # 10 条连接同一瞬间打进来会撞上 HTTPServer 的 listen backlog（默认 5），
        # 偶发 `Connection reset by peer`——这是测试服务器的连接队列，不是快照层的问题。
        # 重试 2 次即可；探测线程仍被 Event 门控着，单飞窗口不受影响。
        for attempt in range(3):
            try:
                responses.append(requests.get(f"{base}/api/state", timeout=10).json())
                return
            except requests.ConnectionError as exc:
                if attempt == 2:
                    errors.append(repr(exc))
                    return
                time.sleep(0.05)
            except Exception as exc:  # noqa: BLE001 - 记录并发下真实发生的错误，便于定位
                errors.append(repr(exc))
                return

    threads = [threading.Thread(target=hit) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 让仍在门控里的探测跑完，再确认没有第二个线程被启动
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not calls:
        time.sleep(0.02)
    release.set()
    time.sleep(0.2)

    assert len(responses) == 10, f"并发请求失败了 {len(errors)} 个：{errors}"
    assert all(isinstance(r["devices"], list) for r in responses)
    assert len(calls) == 1, f"单飞失效：起了 {len(calls)} 个探测线程"


def test_probe_failure_keeps_previous_snapshot(fast_server, monkeypatch):
    """失败不清旧快照：devices/asof 不变，devices_probe_error 说明原因。"""
    base = fast_server
    first = requests.get(f"{base}/api/state", timeout=5).json()
    old_asof = first["devices_asof"]
    assert first["devices"] == OLD_DEVICES
    assert old_asof is not None
    assert first["devices_probe_error"] is None

    def boom():
        raise RuntimeError("adb 卡住后被中断")

    monkeypatch.setattr(device, "_probe_devices", boom)
    monkeypatch.setattr(device, "_DEVICES_TTL", 0.0)

    requests.get(f"{base}/api/state", timeout=3)  # 触发后台刷新
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and device._devices_error is None:
        time.sleep(0.02)

    body = requests.get(f"{base}/api/state", timeout=3).json()
    assert body["devices"] == OLD_DEVICES, "探测失败把上一次的好快照清空了"
    assert body["devices_asof"] == old_asof, "探测失败不该改写 devices_asof"
    assert body["devices_probe_error"], "失败原因没暴露给前端"
    assert "adb 卡住后被中断" in body["devices_probe_error"]


def test_cold_start_is_time_bounded(fast_server, monkeypatch):
    """首次请求（完全没有快照）最多等约 2s，超时回空列表 + 预热标记。"""
    base = fast_server
    release = threading.Event()

    def slow_probe():
        release.wait(10)
        return list(NEW_DEVICES)

    monkeypatch.setattr(device, "_probe_devices", slow_probe)
    device.reset_devices_snapshot()

    started = time.perf_counter()
    resp = requests.get(f"{base}/api/state", timeout=5)
    elapsed = time.perf_counter() - started

    assert resp.status_code == 200
    body = resp.json()
    assert elapsed <= 2.5, f"冷启动同步等待超时保护失效：{elapsed:.2f}s"
    assert body["devices"] == []
    assert body["devices_asof"] is None
    assert body["devices_warming"] is True
    release.set()


def test_state_fields_are_additive(fast_server):
    """只增不改：原有设备字段语义不变，新增字段类型稳定。"""
    body = requests.get(f"{fast_server}/api/state", timeout=5).json()
    for key in ("version", "cwd", "cases", "caseMeta", "runs", "imports", "indexes",
                "explores", "usage", "ai_mode", "license", "code_stale", "gen_ready",
                "explore_tip", "devices", "ios_supported", "console", "prompt_mode"):
        assert key in body, f"原有字段 {key} 丢了"
    assert isinstance(body["devices"], list)
    assert body["devices_asof"] is None or isinstance(body["devices_asof"], int)
    assert body["devices_probe_error"] is None or isinstance(
        body["devices_probe_error"], str)
    assert isinstance(body["devices_warming"], bool)


def test_state_and_devices_share_the_same_snapshot(fast_server):
    """同一时刻两个接口必须给出**同一份** devices 与 devices_asof。"""
    state = requests.get(f"{fast_server}/api/state", timeout=5).json()
    devices = requests.get(f"{fast_server}/api/devices", timeout=5).json()
    assert devices["devices"] == state["devices"]
    assert devices["devices_asof"] == state["devices_asof"]
    assert devices["devices_probe_error"] == state["devices_probe_error"]
    assert devices["devices_warming"] == state["devices_warming"]
    assert devices["devices"] == OLD_DEVICES
    assert devices["devices_asof"] is not None


def test_devices_endpoint_does_not_block_on_slow_probe(fast_server, monkeypatch):
    """`/api/devices` 同样只读快照：慢探测时 < 200ms，返回旧列表。"""
    seen = threading.Event()
    release = threading.Event()

    def slow_probe():
        seen.set()
        release.wait(10)
        return list(NEW_DEVICES)

    monkeypatch.setattr(device, "_probe_devices", slow_probe)
    monkeypatch.setattr(device, "_DEVICES_TTL", 0.0)

    started = time.perf_counter()
    resp = requests.get(f"{fast_server}/api/devices", timeout=3)
    elapsed = time.perf_counter() - started

    assert resp.status_code == 200
    body = resp.json()
    assert body["devices"] == OLD_DEVICES
    assert isinstance(body["devices_asof"], int)
    assert elapsed < 0.2, f"/api/devices 被探测阻塞：{elapsed * 1000:.0f}ms"
    assert seen.wait(2)
    release.set()


def test_devices_cold_start_is_time_bounded(fast_server, monkeypatch):
    """`/api/devices` 冷启动与 `/api/state` 同口径：≤2s，超时回 warming。"""
    release = threading.Event()

    def slow_probe():
        release.wait(10)
        return list(NEW_DEVICES)

    monkeypatch.setattr(device, "_probe_devices", slow_probe)
    device.reset_devices_snapshot()

    started = time.perf_counter()
    body = requests.get(f"{fast_server}/api/devices", timeout=5).json()
    elapsed = time.perf_counter() - started

    assert elapsed <= 2.5, f"/api/devices 冷启动等待超时保护失效：{elapsed:.2f}s"
    assert body["devices"] == []
    assert body["devices_asof"] is None
    assert body["devices_warming"] is True
    release.set()
