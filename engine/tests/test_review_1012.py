"""2026-10-12 全仓评审修复的回归测试（编号对应评审报告）。

评审核心结论：这批缺陷不是"没被测出来"，是**结构上不可能被测出来**——
桩恒定 dump 让重放分支成为唯一被测路径、桩无条件写盘成功、serial=None 的
多设备形态从未被模拟。本文件逐条把这些形态补上：

- C1  唤醒契约真的被调用（旧 `raw is None` 判据在 raw 删除后恒真）
- C2  serial 未解析时"无 logcat 证据"必须可见，不能伪装成正常
- C4  截图未落盘不得终止整轮探索
- C6  timeout_ms 范围约束（负值/超大值边界拒绝）
- 输入类动作不自动重放（树恒定 ⇒ set_text 被打两遍的旧路径）
- retry 的首跑截图不被覆盖 + step id 唯一性
- 归因提示追加不覆盖（post-verify 告警不丢失）
- 数据组名 `..` 不得写出 base_root
- vision 兜底异常如实记录，不伪装成"未启用"
- A1  Host 头校验拦 DNS rebinding
- A3  包名形状校验（api_explore / page_index / review / DSL target.package）
"""

from __future__ import annotations

import http.client
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from insta_engine.dsl import TestCase
from insta_engine.runner import Runner

# ---- 共享桩 ------------------------------------------------------------------

class _Node:
    """可记录 set_text/click 的节点；get_text 如实回读（输入回退协议需要）。"""

    exists = True

    def __init__(self):
        self.texts: list[str] = []
        self.clicks = 0

    def wait(self, timeout=None):
        return self

    def click(self):
        self.clicks += 1

    def set_text(self, text):
        self.texts.append(text)

    def get_text(self):
        return self.texts[-1] if self.texts else ""


class _Raw:
    """u2 形状 fake：任何选择器 kwargs 都返回同一节点。"""

    def __init__(self, node: _Node):
        self._node = node
        self.shell_cmds: list[str] = []

    def __call__(self, **kwargs):
        return self._node

    def shell(self, cmd):
        self.shell_cmds.append(cmd)
        return ""


class _ReviewDriver:
    """恒定控件树（hash 永远不变 ⇒ post-verify 必走"树无变化"分支）+ serial=None。

    serial=None 是评审点名的形态：多设备下 DeviceDriver 解析不出 serial，
    此前 logcat 证据在这里**静默**丢失。
    """

    serial = None

    def __init__(self, dump: str | None = None):
        self.node = _Node()
        self.raw = _Raw(self.node)
        self.wakes = 0
        self._dump = dump or (
            '<hierarchy><node text="登录" class="Button" package="com.insta.demo"'
            ' bounds="[0,0][10,10]"/></hierarchy>')

    def wake_and_unlock(self):
        self.wakes += 1

    def dump(self) -> str:
        return self._dump

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def find(self, by: str, value: str, timeout: float):
        node = self.raw(**{by: value})
        node.wait(timeout=timeout)
        return node

    def find_xpath(self, expr: str, timeout: float):
        return self.raw()

    def shell(self, cmd):
        return self.raw.shell(cmd)

    def app_start(self, package, activity=None):
        pass

    def press(self, key):
        pass

    def swipe_ext(self, direction):
        pass

    def click(self, x, y):
        pass

    def foreground_app(self):
        return {"package": "com.insta.demo", "activity": ".Main"}

    def app_info(self, pkg):
        return {"versionName": "1.0"}


class _AbsentDriver(_ReviewDriver):
    """所有定位都未命中——评审点名"桩恒命中让失败分支不可测"的对照形态。"""

    class _DeadNode:
        exists = False

        def wait(self, timeout=None):
            return self

    def find(self, by: str, value: str, timeout: float):
        return self._DeadNode()

    def find_xpath(self, expr: str, timeout: float):
        return self._DeadNode()


def _case(package: str = "com.insta.demo", timeout_ms: int | None = None,
          steps: list[dict] | None = None) -> TestCase:
    body = {
        "caseId": "case_review",
        "name": "评审回归",
        "target": {"package": package},
        "steps": steps or [
            {"id": "s1", "intent": "启动", "action": "launch",
             "params": {"package": package}},
            {"id": "s2", "intent": "等待", "action": "wait", "params": {"ms": 10}},
        ],
    }
    if timeout_ms is not None:
        body["steps"][0]["timeoutMs"] = timeout_ms
    return TestCase.model_validate(body)


# ---- C1 唤醒契约 -------------------------------------------------------------

def test_c1_wake_contract_is_invoked(tmp_path):
    """C1：唤醒必须**真的发生**。旧判据 `raw is None ⇒ 桩` 在 raw 从真实驱动删除后
    对真机恒成立，防护从未生效——契约方法被调用才算数。"""
    drv = _ReviewDriver()
    Runner(drv, tmp_path).run(_case())
    assert drv.wakes >= 1, "Runner 执行前必须调用 wake_and_unlock() 契约"


def test_c1_wake_failure_does_not_block_run(tmp_path):
    """C1：唤醒失败（桩未实现/真机异常）只吞掉，不阻塞执行。"""

    class _BrokenWake(_ReviewDriver):
        def wake_and_unlock(self):
            raise RuntimeError("no device")

    drv = _BrokenWake()
    run = Runner(drv, tmp_path).run(_case())
    assert run.status == "passed", "唤醒失败不应让整条用例失败"


def test_c1_base_driver_default_is_noop():
    """C1：BaseDriver 默认无操作（iOS WDA P2），Android 必须自己 override。"""
    from insta_engine.driver import DeviceDriver
    from insta_engine.driver_base import BaseDriver
    from insta_engine.wda_driver import WdaDriver

    assert BaseDriver.wake_and_unlock is not None
    assert DeviceDriver.wake_and_unlock is not BaseDriver.wake_and_unlock, (
        "Android 驱动必须 override wake_and_unlock，否则唤醒防护仍是 no-op")
    # WdaDriver 继承默认 no-op（iOS 场景 WDA 自管屏幕）
    assert WdaDriver.wake_and_unlock is BaseDriver.wake_and_unlock


# ---- C6 timeout 边界 + DSL 形状校验（重复 id / 包名）--------------------

def test_c6_timeout_ms_bounds():
    with pytest.raises(ValidationError):
        _case(timeout_ms=-5), "负值曾穿透校验且整步 passed"
    with pytest.raises(ValidationError):
        _case(timeout_ms=999999999), "277 小时的单步等待必须边界拒绝"
    _case(timeout_ms=400)  # 正常值不受影响


def test_m3_duplicate_step_id_rejected():
    steps = [{"id": "s", "action": "wait", "params": {"ms": 10}},
             {"id": "s", "action": "wait", "params": {"ms": 10}}]
    with pytest.raises(ValidationError):
        TestCase.model_validate({"caseId": "c", "name": "n",
                                 "target": {"package": "com.insta.demo"}, "steps": steps})


def test_a3_dsl_target_package_shape():
    for bad in ("../evil", "/etc/evil", "com/evil", "..", "C:\\evil"):
        with pytest.raises(ValidationError):
            _case(package=bad)
    _case(package="p")  # 短包名兼容（既有测试/用例口径）


# ---- 输入不重放 -----------------------------------------------------------

def test_m1_input_step_is_never_replayed(tmp_path):
    """树恒定不变时 input 步骤此前被重放——set_text 打两遍（脏输入）。"""
    drv = _ReviewDriver()
    case = _case(steps=[{"id": "s1", "intent": "输入", "action": "input",
                         "target": {"strategy": "res-id", "value": "com.insta.demo:id/et"},
                         "params": {"text": "hello"}}])
    result = Runner(drv, tmp_path).run(case).steps[0]
    assert result.status == "passed"
    assert drv.node.texts == ["hello"], (
        f"set_text 被调 {drv.node.texts!r} —— 输入类动作不得重放")
    assert "重放" in (result.warning or ""), "不重放的决定要写进 warning 让用户知道"


# ---- 重试截图不被覆盖 -------------------------------------------------------

def test_m3_retry_keeps_first_attempt_screenshot(tmp_path):
    """retry 二次执行此前覆盖首跑截图——丢的正是"第一次为什么失败"的证据。"""
    drv = _AbsentDriver()
    case = _case(steps=[{"id": "s1", "intent": "点按", "action": "tap",
                         "target": {"strategy": "res-id",
                                    "value": "com.insta.demo:id/absent"},
                         "onFailure": "retry", "timeoutMs": 200}])
    Runner(drv, tmp_path).run(case)
    shots = sorted(p.name for p in (tmp_path / "screenshots").glob("step_01_s1_*.png"))
    assert len(shots) == 2, f"两次尝试应各留一张截图，实际 {shots}"
    assert shots[0].endswith("_a1.png") and shots[1].endswith("_a2.png")


# ---- 失败步骤证据与告警 ------------------------------------------------

def test_c2_m4_missing_logcat_is_visible_and_warnings_accumulate(tmp_path):
    """C2：serial=None ⇒ "logcat 证据缺失"必须出现在 warning；
    归因提示（可见控件）追加不覆盖，两条信号都要在。"""
    drv = _AbsentDriver()
    case = _case(steps=[{"id": "s1", "intent": "点按", "action": "tap",
                         "target": {"strategy": "res-id",
                                    "value": "com.insta.demo:id/absent"},
                         "timeoutMs": 200}])
    result = Runner(drv, tmp_path).run(case).steps[0]
    assert result.status == "failed"
    w = result.warning or ""
    assert "logcat 证据缺失" in w, "没有证据这件事本身必须可见（C2）"
    assert "当前页面可见控件" in w, "归因提示被覆盖丢失"


# ---- 数据组名逃逸 -----------------------------------------------------------

def test_m5_dataset_dotdot_cannot_escape_base_root(tmp_path):
    """数据组名 `..` 此前把产物写到 base_root 上一级。"""
    drv = _ReviewDriver()
    case = TestCase.model_validate({
        "caseId": "case_ds", "name": "数据驱动",
        "target": {"package": "com.insta.demo"},
        "datasets": [{"name": "..", "vars": {}}],
        "steps": [{"id": "s1", "action": "wait", "params": {"ms": 10}}],
    })
    base = tmp_path / "artifacts" / "cases" / "case_ds"
    runs = Runner(drv, tmp_path).run_datasets(case, root=base)
    assert len(runs) == 1, "一个数据组应产生一轮执行"
    _, result, artifacts = runs[0]
    assert result.status == "passed"
    assert Path(artifacts).name == "组1", (
        f"净化后应落回 组1，实际 {Path(artifacts).name!r}")
    assert Path(artifacts).resolve().is_relative_to(base.resolve()), (
        "产物目录越出了 base_root")
    assert (Path(artifacts) / "run.json").exists(), "产物目录里应有 run.json"


# ---- vision 异常如实记录 ----------------------------------------------------

def test_m8_vision_exception_is_recorded_not_silenced(tmp_path, monkeypatch):
    """vision 调用抛异常此前被吞成"未命中或未启用"，用户会去查额度查错方向。

    走 _vision_locate 的真实内部路径（注入 fake client + vision.locate 抛错），
    而不是整体替换 _vision_locate——那会绕过被测的保护层本身。
    """
    import insta_engine.vision as vision_mod

    def boom(*args, **kwargs):
        raise RuntimeError("api down")

    monkeypatch.setattr(Runner, "_vision_allowed", staticmethod(lambda step: True))
    monkeypatch.setattr(vision_mod, "locate", boom)

    class _ShotDrv(_AbsentDriver):
        def screenshot_pil(self):
            from PIL import Image
            return Image.new("RGB", (4, 4), "white")

    drv = _ShotDrv()
    case = _case(steps=[{"id": "s1", "intent": "点按", "action": "tap",
                         "target": {"strategy": "res-id",
                                    "value": "com.insta.demo:id/absent"},
                         "timeoutMs": 200}])
    result = Runner(drv, tmp_path, vision_client=object()).run(case).steps[0]
    assert "调用异常" in (result.error or ""), (
        f"vision 异常被吞：{result.error!r}")
    assert "api down" in (result.error or ""), "真因要可见，不能只剩笼统措辞"


# ---- wait 容错 + params None ---------------------------------------------------

def test_wait_invalid_ms_tolerated(tmp_path):
    case = _case(steps=[{"id": "w", "action": "wait", "params": {"ms": "abc"}}])
    result = Runner(_ReviewDriver(), tmp_path).run(case).steps[0]
    assert result.status == "passed", "非法 ms 应容错回默认值，而不是整步 unknown"


def test_input_none_text_not_string_none(tmp_path):
    drv = _ReviewDriver()
    case = _case(steps=[{"id": "s1", "action": "input",
                         "target": {"strategy": "res-id", "value": "com.insta.demo:id/et"},
                         "params": {"text": None}}])
    Runner(drv, tmp_path).run(case)
    assert drv.node.texts == [""], "params text=null 不应往输入框打字 'None'"


# ---- A1 Host 头校验 ------------------------------------------------------------

def test_a1_host_header_rebinding_is_rejected(tmp_path):
    """A1：rebinding 请求必带恶意域名 Host——do_GET/do_POST 都必须拦。"""
    from insta_engine.web_server import make_server

    server = make_server(0, root=tmp_path)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/api/jobs", headers={"Host": "evil.example"})
        resp = conn.getresponse()
        assert resp.status == 403, "GET 带恶意 Host 必须被拒（此前 do_GET 零校验）"
        resp.read()
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("POST", "/api/job/cancel", body="{}",
                     headers={"Host": "evil.example", "Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 403
        resp.read()
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/api/jobs")  # 正常 Host（http.client 自动带）放行
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


# ---- A3 包名收口 ----------------------------------------------------------------

def test_a3_api_explore_rejects_unsafe_package(tmp_path):
    from insta_engine.web_server import api_explore

    for bad in ("../../evil", "/tmp/abs_escape", ""):
        code, obj = api_explore(tmp_path, {"package": bad})
        assert code == 400, f"包名 {bad!r} 应被拒绝"


def test_a3_page_index_silent_degrade(tmp_path):
    from insta_engine.page_index import controls_context_for, graph_context_for

    assert controls_context_for("../evil", tmp_path) == ""
    assert controls_context_for("/etc/evil", tmp_path) == ""
    assert graph_context_for("/etc/evil", tmp_path) == ""


def test_a3_review_paths_raise_on_unsafe_package(tmp_path):
    from insta_engine.review import html_path, index_path, review_path

    for bad in ("../evil", "/etc/evil"):
        with pytest.raises(ValueError):
            index_path(bad, tmp_path)
        with pytest.raises(ValueError):
            review_path(bad, tmp_path)
        with pytest.raises(ValueError):
            html_path(bad, tmp_path)
    # 正常包名不受影响
    assert index_path("com.insta.demo", tmp_path) == (
        tmp_path / "artifacts" / "page_index" / "com.insta.demo.json")


# ---- C4 探索截图未落盘 -----------------------------------------------------------

def test_c4_missing_screenshot_does_not_kill_exploration(tmp_path):
    """C4：u2 真机上"截图不抛错但文件没写出来"此前 FileNotFoundError 终止整轮探索。"""
    from insta_engine.explore import explore

    class _GhostShotDrv:
        """screenshot 静默不写盘、不抛错——评审探针的崩溃形态。"""

        serial = None

        def dump(self):
            return ('<hierarchy><node class="Button" text="去B" resource-id="com.x:id/go"'
                    ' clickable="true" bounds="[0,0][10,10]" package="com.x"/></hierarchy>')

        def screenshot(self, path):
            pass  # 不写盘、不抛

        def click(self, x, y):
            pass

        def press(self, key):
            pass

        def app_start(self, package):
            pass

        def foreground_app(self):
            return {"package": "com.x", "activity": ".Main"}

    r = explore(_GhostShotDrv(), "com.x", minutes=0.05, max_steps=6,
                artifacts_dir=tmp_path / "ghost", max_depth=2, sleep_scale=0.001,
                index_path=tmp_path / "com.x.json")
    assert r["steps"] > 0, "截图缺失不该终止整轮探索（此前 FileNotFoundError）"


# ---- junit <error> 与 heals 脏数据 ------------------------------------------------

def test_junit_error_element_counts_as_failed(tmp_path):
    """`<error>` 此前既不计数又被算进 failed 差值——failed 改自洽计数。"""
    from insta_engine.junit_summary import summarize

    xml = ('<testsuite tests="2" failures="0" errors="1">'
           '<testcase name="a" classname="C" time="0.1"/>'
           '<testcase name="b" classname="C" time="0.1">'
           '<error message="boom" type="env-issue"/></testcase></testsuite>')
    path = tmp_path / "junit.xml"
    path.write_text(xml, encoding="utf-8")
    s = summarize(path)
    assert s["failed"] == 1 and s["passed"] == 1 and s["total"] == 2


def test_heals_skips_malformed_proposal(tmp_path):
    """run.json 是磁盘产物，缺键提案不能把回写炸成 KeyError。"""
    from insta_engine.dsl import LocatorStrategy
    from insta_engine.heals import apply_heals

    case = _case(steps=[{"id": "s1", "action": "tap",
                         "target": {"strategy": "res-id",
                                    "value": "com.insta.demo:id/username"}}])
    proposals = [
        {"original": "res-id=com.insta.demo:id/username",
         "match": "fuzzy:textContains=登录", "step": "s1"},
        {"bad": "shape"},   # 缺 original/match —— 此前 KeyError
    ]
    healed, notes = apply_heals(case, proposals)
    assert len(notes) == 1
    assert healed.steps[0].target.value == "登录"
    assert healed.steps[0].target.strategy == LocatorStrategy.TEXT
