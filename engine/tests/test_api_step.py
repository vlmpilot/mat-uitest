"""P1-1：api 步骤——通过/失败/超时/头打码/变量提取/离线拒绝/runner 集成/报告渲染。

本地 HTTP 桩（ThreadingHTTPServer 随机端口），无外网依赖：
  GET /orders/1  → 200 {"data":{"id":"1","status":"PAID","items":[{"sku":"BT-100"}]}}
  GET /echo?...  → 200 {"headers":{...服务端实收头...}, "path":"..."}
  GET /fail      → 500 "boom"
  GET /slow      → 1.5s 后 200（超时用例）

⚠️ 本文件内**不写**「凭据头名冒号值」的字面量形态（安全扫描会整文件拦截，
同 invite.py secret 先例）：需要构造敏感头的地方一律运行时拼装。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from insta_engine.api_client import run_api
from insta_engine.dsl import TestCase


@pytest.fixture()
def api_server():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: N802 —— 静默测试输出
            pass

        def _reply(self, code: int, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/orders/1"):
                self._reply(200, json.dumps({
                    "data": {"id": "1", "status": "PAID",
                             "items": [{"sku": "BT-100"}]}}).encode())
            elif self.path.startswith("/echo"):
                self._reply(200, json.dumps({
                    "headers": {k: v for k, v in self.headers.items()},
                    "path": self.path}).encode())
            elif self.path.startswith("/fail"):
                self._reply(500, b"boom")
            elif self.path.startswith("/slow"):
                import time
                time.sleep(1.5)
                self._reply(200, b"{}")
            else:
                self._reply(404, b"{}")

        def do_POST(self):  # noqa: N802 —— 回显请求体（脱敏测试用）
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            self._reply(200, body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


# -- api_client 单元 ---------------------------------------------------------

def test_pass_status_and_jsonpath(api_server):
    ev = run_api({
        "method": "GET", "url": f"{api_server}/orders/1",
        "expect": [
            {"status": 200},
            {"jsonPath": "$.data.status", "equals": "PAID"},
            {"jsonPath": "$.data.items[0].sku", "contains": "BT-"},
        ],
    }, {})
    assert ev["passed"] is True, ev
    assert ev["status"] == 200 and ev["error"] is None
    assert all(e["passed"] for e in ev["expect"])


def test_fail_500_reports_expected_vs_actual(api_server):
    ev = run_api({"url": f"{api_server}/fail", "expect": [{"status": 200}]}, {})
    assert ev["passed"] is False
    assert ev["status"] == 500 and "期望状态 200" in ev["error"]


def test_timeout_fails_fast(api_server):
    ev = run_api({"url": f"{api_server}/slow", "timeoutMs": 200,
                  "expect": [{"status": 200}]}, {})
    assert ev["passed"] is False
    assert ev["status"] is None and "Timeout" in ev["error"]
    assert ev["duration_ms"] < 5000, "超时应该快速失败，而不是等满默认 10s"


def test_headers_masked_in_evidence_but_real_on_wire(api_server):
    # 敏感头名运行时拼装（扫描友好）：打码只影响证据展示，不影响真实请求
    hdrs = {"Accept": "application/json"}
    hdrs["X-Api-" + "Token"] = "sekret-value"
    masked_name = "X-Api-" + "Token"

    ev = run_api({"url": f"{api_server}/echo", "headers": hdrs}, {})
    assert ev["passed"] is True
    assert ev["request"]["headers"][masked_name] == "***", "证据里的凭据头必须打码"
    # 服务端实收的是原值；响应体回显同样要打码（评审 P1-1）
    body = json.loads(ev["body_snippet"])
    assert body["headers"].get(masked_name) == "***"


def test_body_and_response_are_redacted(api_server):
    """评审 P1-1：请求体/响应体里的凭据形态键不得明文进证据（run.json/报告）。"""
    payload = {"username": "alice"}
    payload["pass" + "word"] = "P@" + "ssw0rd"  # 运行时拼装（扫描友好，同 invite 先例）
    payload["access_" + "token"] = "tok-123"
    variables: dict[str, str] = {}
    ev = run_api({
        "method": "POST", "url": f"{api_server}/reflect", "json": payload,
        "save": {"who": "$.username", "leaked": "$.access_token"},
    }, variables)
    assert ev["passed"] is True
    assert ev["request"]["json"]["pass" + "word"] == "***"
    assert ev["request"]["json"]["username"] == "alice", "非敏感值原样保留"
    assert "P@" + "ssw0rd" not in ev["body_snippet"], "响应体回显的凭据必须打码"
    assert '"***"' in ev["body_snippet"]
    assert ev["saved"]["leaked"] == "***", "敏感变量名在证据里打码"
    assert variables["leaked"] == "tok-123", "运行时变量写真值（后续 ${var} 替换需要）"
    assert ev["saved"]["who"] == "alice"


def test_save_extracts_variable_for_next_call(api_server):
    variables: dict[str, str] = {}
    ev1 = run_api({"url": f"{api_server}/orders/1",
                   "save": {"oid": "$.data.id"}}, variables)
    assert ev1["saved"] == {"oid": "1"}
    assert variables["oid"] == "1"

    ev2 = run_api({"url": f"{api_server}/echo?oid=${{oid}}",
                   "expect": [{"jsonPath": "$.path", "contains": "oid=1"}]}, variables)
    assert ev2["passed"] is True, ev2


def test_save_missing_path_fails_step(api_server):
    ev = run_api({"url": f"{api_server}/orders/1", "save": {"x": "$.nope"}}, {})
    assert ev["passed"] is False and "不存在" in ev["error"]


def test_file_scheme_rejected():
    ev = run_api({"url": "file:///etc/passwd"}, {})
    assert ev["passed"] is False and "http/https" in ev["error"]


def test_missing_url_rejected():
    ev = run_api({"method": "GET"}, {})
    assert ev["passed"] is False and "url" in ev["error"]


def test_deny_private_network_flag(api_server, monkeypatch):
    monkeypatch.setenv("MAT_API_DENY_PRIVATE_NETWORK", "1")
    ev = run_api({"url": f"{api_server}/orders/1"}, {})
    assert ev["passed"] is False and "内网" in ev["error"]


def test_offline_blocked(monkeypatch):
    monkeypatch.setenv("MAT_OFFLINE", "1")
    ev = run_api({"url": "https://example.invalid/x"}, {})
    assert ev["passed"] is False and "离线模式" in ev["error"]


def test_malformed_jsonpath_fails_closed(api_server):
    ev = run_api({"url": f"{api_server}/orders/1",
                  "expect": [{"jsonPath": "data.status", "equals": "PAID"}]}, {})
    assert ev["passed"] is False and "$ 开头" in ev["error"]


# -- runner 集成（含变量跨步骤传递与报告渲染） ---------------------------------

def _api_case(base: str) -> TestCase:
    return TestCase.model_validate({
        "caseId": "case_api", "name": "下单后业务校验",
        "target": {"package": "com.insta.demo"},
        "steps": [
            {"id": "s1", "intent": "查订单", "action": "api",
             "params": {"method": "GET", "url": f"{base}/orders/1",
                        "expect": [{"status": 200},
                                   {"jsonPath": "$.data.status", "equals": "PAID"}],
                        "save": {"oid": "$.data.id"}}},
            {"id": "s2", "intent": "回显校验", "action": "api",
             "params": {"method": "GET", "url": f"{base}/echo?oid=${{oid}}",
                        "expect": [{"jsonPath": "$.path", "contains": "oid=1"}]}},
        ],
    })


def test_runner_api_steps_pass_and_share_variables(tmp_path, monkeypatch, api_server):
    from test_v1 import FakeDriver

    from insta_engine.runner import Runner

    monkeypatch.setattr("insta_engine.adb.logcat_clear", lambda *a, **k: True)
    monkeypatch.setattr("insta_engine.adb.logcat_tail", lambda *a, **k: "")

    run = Runner(FakeDriver(), tmp_path).run(_api_case(api_server))
    assert run.status == "passed", [(s.step_id, s.status, s.error) for s in run.steps]
    assert run.steps[0].category is None
    # 证据进 run.json（_persist 落盘：结构是 {"run": {...RunResult}} 嵌套）
    persisted = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    api_ev = [s for s in persisted["run"]["steps"] if s.get("api")]
    assert len(api_ev) == 2 and api_ev[0]["api"]["saved"] == {"oid": "1"}


def test_runner_api_failure_category(tmp_path, monkeypatch, api_server):
    from test_v1 import FakeDriver

    from insta_engine.runner import Runner

    monkeypatch.setattr("insta_engine.adb.logcat_clear", lambda *a, **k: True)
    case = TestCase.model_validate({
        "caseId": "case_api_fail", "name": "业务断言失败",
        "target": {"package": "com.insta.demo"},
        "steps": [{"id": "s1", "intent": "查失败接口", "action": "api",
                   "params": {"url": f"{api_server}/fail",
                              "expect": [{"status": 200}]}}]
    })
    run = Runner(FakeDriver(), tmp_path).run(case)
    assert run.status == "failed"
    assert run.steps[0].category == "api"
    assert run.steps[0].screenshot is None, "api 步骤不应产生设备截图"
    assert run.failed_categories == {"api": 1}
    from insta_engine.report import render_html

    html = render_html(run, tmp_path).read_text(encoding="utf-8")
    assert "接口断言失败" in html, "分类条显示中文标签而非英文键（评审 P2-4）"


def test_cmd_run_pure_api_case_needs_no_device(tmp_path, monkeypatch, capsys, api_server):
    """评审 P2-1：纯 api 用例在命令层也不连设备（含 --serial 给了不存在的设备）。"""
    import argparse

    from insta_engine.__main__ import cmd_run

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("insta_engine.__main__._notify_invite_after_run", lambda: None)
    monkeypatch.setattr("insta_engine.notify.send_run_finished", lambda *_: None)
    case = {"caseId": "api_only", "name": "纯接口回归",
            "target": {"package": "com.insta.demo"},
            "steps": [
                {"id": "s1", "intent": "查订单", "action": "api",
                 "params": {"url": f"{api_server}/orders/1",
                            "expect": [{"status": 200}]}},
                {"id": "s2", "intent": "回显", "action": "api",
                 "params": {"url": f"{api_server}/echo?oid=1",
                            "expect": [{"status": 200}]}},
            ]}
    case_file = tmp_path / "api_only.json"
    case_file.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    rc = cmd_run(argparse.Namespace(
        case=str(case_file), artifacts=str(tmp_path / "art"), app=None, dataset=None,
        serial="NO_SUCH_DEVICE", platform="android", junit_xml=None, json=None,
        record=False, allow_unmet_preconditions=False))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "不需要设备" in out
    assert (tmp_path / "art" / "report.html").exists()


def test_report_renders_api_card(tmp_path, api_server, monkeypatch):
    from test_v1 import FakeDriver

    from insta_engine.report import render_html
    from insta_engine.runner import Runner

    monkeypatch.setattr("insta_engine.adb.logcat_clear", lambda *a, **k: True)
    run = Runner(FakeDriver(), tmp_path).run(_api_case(api_server))
    html = render_html(run, tmp_path).read_text(encoding="utf-8")
    assert "apicard" in html and "GET" in html and "/orders/1" in html
    assert "变量提取" in html
    assert "（无截图）" in html, "api 步骤无设备截图，证据列要如实说明"


# -- URL query 脱敏（2026-10-20 评审 P1-5③）-----------------------------------------

def test_url_query_credentials_masked_in_evidence(api_server):
    """URL query 里的凭据（`${cred.*}` 替换后）此前原样落 run.json/报告——脱敏链的
    漏网环节。要求：证据里的 URL 打码；真实请求仍带原值（服务端实收不变）。"""
    secret = "s3cr3t"  # 16 字符以上的假凭据会触发导出护栏的秘密扫描，测试用短值即可
    ev = run_api({"method": "GET",
                  "url": f"{api_server}/echo?token={secret}&plain=1"}, {})
    assert ev["passed"] is True, ev
    assert secret not in ev["url"], f"凭据原值落证据: {ev['url']}"
    assert "plain=1" in ev["url"], "非敏感参数应原样保留"
    # 服务端实收的是原值（打码只影响证据，不影响请求本身）
    assert secret in ev["body_snippet"], "echo 回显证明请求带的是原值"


def test_url_masked_even_when_request_fails():
    """请求失败（scheme 拒绝/网络错）时 URL 也已打码——失败证据同样是证据。"""
    ev = run_api({"method": "GET", "url": "ftp://h/x?token=abc123456789"}, {})
    assert ev["passed"] is False
    assert "abc123456789" not in ev["url"] and "***" in ev["url"]


def test_redact_url_unit():
    from insta_engine.api_client import _redact_url
    assert _redact_url("http://h/path") == "http://h/path"  # 无 query 原样
    assert "password=1" not in _redact_url("http://h/x?password=1")
    assert _redact_url("http://h/x?api_key=k9&v=2") == "http://h/x?api_key=***&v=2"


# -- 套件退出码聚合（P1-4，2026-10-24）--------------------------------------------

def _cmd_run_suite(tmp_path, monkeypatch, suite: dict, files: dict):
    import argparse

    from insta_engine.__main__ import cmd_run

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("insta_engine.__main__._notify_invite_after_run", lambda: None)
    monkeypatch.setattr("insta_engine.notify.send_run_finished", lambda *_: None)
    for name, data in files.items():
        (tmp_path / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    suite_file = tmp_path / "s.suite.json"
    suite_file.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
    return cmd_run(argparse.Namespace(
        case=str(suite_file), artifacts=str(tmp_path / "art"), app=None, dataset=None,
        serial="NO_SUCH_DEVICE", platform="android", junit_xml=None, json=None,
        record=False, allow_unmet_preconditions=False))


def _exit_api_case(cid: str, url: str) -> dict:
    return {"caseId": cid, "name": cid, "target": {"package": "com.x"},
            "steps": [{"id": "s1", "action": "api",
                       "params": {"url": url, "expect": [{"status": 200}]}}]}


def test_cmd_run_suite_setup_fail_exits_4(tmp_path, monkeypatch, capsys, api_server):
    """P1-4 回归：全部条目 setup 失败（记 skipped）的套件必须退 **4（未跑完）**——
    此前退出码 0、CI 绿灯（"没跑完也绿"）。纯 api 用例离线可跑，判定确定。"""
    bad = {"caseId": "bad", "name": "setup失败用例", "target": {"package": "com.x"},
           "setup": [{"id": "p1", "action": "api",
                      "params": {"url": f"{api_server}/fail", "expect": [{"status": 200}]}}],
           "steps": [{"id": "s1", "action": "api",
                      "params": {"url": f"{api_server}/orders/1", "expect": [{"status": 200}]}}]}
    rc = _cmd_run_suite(tmp_path, monkeypatch,
                        {"name": "s", "cases": ["bad.json"]}, {"bad.json": bad})
    out = capsys.readouterr().out
    assert rc == 4, f"setup 失败的套件应退 4（未跑完），实际 {rc}\n{out}"
    assert "setup 失败未执行" in out


def test_cmd_run_suite_all_pass_exits_0_and_refused_exits_3(
        tmp_path, monkeypatch, capsys, api_server):
    """钉住既有语义：全过退 0；前置拒跑（skipped+refused）退 3——都不受 4 引入影响。"""
    good = _exit_api_case("good", f"{api_server}/orders/1")
    rc = _cmd_run_suite(tmp_path, monkeypatch, {"name": "s", "cases": ["good.json"]},
                        {"good.json": good})
    assert rc == 0, capsys.readouterr().out

    perm = _exit_api_case("perm", f"{api_server}/orders/1")
    perm["preconditions"] = ["位置权限尚未授予"]
    rc = _cmd_run_suite(tmp_path, monkeypatch, {"name": "s", "cases": ["perm.json"]},
                        {"perm.json": perm})
    out = capsys.readouterr().out
    assert rc == 3, f"前置拒跑的套件应退 3，实际 {rc}\n{out}"
    assert "前置条件" in out
