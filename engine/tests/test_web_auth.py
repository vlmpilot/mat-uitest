"""--share 访问口令鉴权 + 触发者身份（团队协作 P1，发起人 2026-10-12）。

背景：`mat console --share`（0.0.0.0）此前**无鉴权**——同网段任何人可删改用例、
烧 AI 额度。P1 加口令：cookie（浏览器一次输入长期有效）或 X-MAT-Key 头（curl/脚本）；
SPA 壳放行（无敏感数据），/api/* 与产物路径全拦。
"""

import json
import threading
import time

import pytest
import requests

from insta_engine import web_api, web_server


def _mk_server(tmp_path, monkeypatch, **kw):
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    srv = web_server.make_server(0, root=tmp_path, **kw)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            requests.get(f"{base}/", timeout=1)
            break
        except requests.ConnectionError:
            time.sleep(0.1)
    yield base, tmp_path
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def open_server(tmp_path, monkeypatch):
    """无口令——行为必须与旧版完全一致（本机单人模式零影响）。"""
    yield from _mk_server(tmp_path, monkeypatch)


@pytest.fixture()
def locked_server(tmp_path, monkeypatch):
    yield from _mk_server(tmp_path, monkeypatch, auth_key="a1b2c3")


# -- 未启用口令：零影响 ------------------------------------------------------------

def test_no_password_server_unaffected(open_server):
    base, _ = open_server
    assert requests.get(f"{base}/api/state", timeout=3).status_code == 200
    # /api/auth 在无口令时恒 401（没东西可校验，防止误以为已设防）
    r = requests.post(f"{base}/api/auth", json={"key": "x"}, timeout=3)
    assert r.status_code == 401


# -- API 拦截 ----------------------------------------------------------------------

def test_api_blocked_without_key(locked_server):
    base, _ = locked_server
    r = requests.get(f"{base}/api/state", timeout=3)
    assert r.status_code == 401 and r.json()["auth"] is True


def test_api_open_with_header_key(locked_server):
    base, _ = locked_server
    r = requests.get(f"{base}/api/state", timeout=3,
                     headers={"X-MAT-Key": "a1b2c3"})
    assert r.status_code == 200


# -- SPA 壳与产物 -------------------------------------------------------------------

def test_spa_shell_still_open(locked_server):
    """壳不拦——里面没有敏感数据；同事拿到链接先看到输入口令的界面（由前端 401 处理）。"""
    base, _ = locked_server
    assert requests.get(f"{base}/", timeout=3).status_code == 200


def test_artifacts_html_gets_gate_page(locked_server):
    """报告直达链接（浏览器 Accept: text/html）→ 门槛页而非裸 401 JSON。"""
    base, _ = locked_server
    r = requests.get(f"{base}/artifacts/whatever/index.html", timeout=3,
                     headers={"Accept": "text/html"})
    assert r.status_code == 401 and "访问口令" in r.text and "api/auth" in r.text


def test_artifacts_binary_gets_401_json(locked_server):
    base, _ = locked_server
    r = requests.get(f"{base}/artifacts/whatever/shot.png", timeout=3)
    assert r.status_code == 401 and r.json()["auth"] is True


# -- 口令校验与 cookie ----------------------------------------------------------------

def test_auth_wrong_key_rejected(locked_server):
    base, _ = locked_server
    assert requests.post(f"{base}/api/auth", json={"key": "wrong"},
                         timeout=3).status_code == 401


def test_auth_ok_sets_cookie_then_api_passes(locked_server):
    base, _ = locked_server
    s = requests.Session()
    r = s.post(f"{base}/api/auth", json={"key": "a1b2c3"}, timeout=3)
    assert r.status_code == 200 and "mat_key=a1b2c3" in r.headers.get("Set-Cookie", "")
    assert s.get(f"{base}/api/state", timeout=3).status_code == 200


def test_post_api_blocked_without_key(locked_server):
    """写请求（POST）同样要过口令——删改用例/烧额度的正是 POST。"""
    base, _ = locked_server
    r = requests.post(f"{base}/api/export", json={}, timeout=3)
    assert r.status_code == 401


# -- 触发者身份 -----------------------------------------------------------------------

def test_jobs_return_operator(open_server, tmp_path):
    """start_job 的 meta.operator 必须透出到 GET /api/jobs（任务条显示「谁在跑」的数据源）。"""
    web_api.runtime.start_job("run", lambda: None,
                              meta={"serial": "emu-5554", "platform": "android",
                                    "operator": "张三"})
    try:
        base, _ = open_server
        jobs = requests.get(f"{base}/api/jobs", timeout=3).json()["jobs"]
        hit = [j for j in jobs if j["operator"] == "张三"]
        assert hit and hit[0]["kind"] == "run"
    finally:
        with web_api.runtime._JOB_LOCK:
            for jid in [k for k, v in web_api.runtime._JOBS.items()
                        if (v.get("meta") or {}).get("operator") == "张三"]:
                web_api.runtime._JOBS.pop(jid, None)


def test_operator_of_helper(open_server):
    assert web_api.runtime.operator_of({"operator": " 李四 "}) == "李四"
    assert web_api.runtime.operator_of({}) == ""
    assert web_api.runtime.operator_of(None) == ""
    assert web_api.runtime.operator_of({"operator": "x" * 100}) == "x" * 40


# -- 写操作审计（谁改的/谁下发的）------------------------------------------------------

def test_audit_written_and_readable(open_server, tmp_path):
    """保存用例（POST /api/case）→ audit.jsonl 落一行（含 operator）→ /api/audit 可读。"""
    from insta_engine.dsl import dump_dsl
    case = json.loads(dump_dsl(_mk_case()))
    r = requests.post(f"{open_server[0]}/api/case", timeout=3, json={
        "name": "audit_case.json", "content": case, "operator": "王五"})
    assert r.status_code == 200
    # 落盘断言：工作区 artifacts/audit.jsonl 一行 JSON，operator 透传
    rows = [json.loads(line) for line in
            (open_server[1] / "artifacts" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    hit = [r_ for r_ in rows if r_["action"] == "/api/case" and r_["operator"] == "王五"]
    assert hit and "audit_case.json" in hit[0]["target"] and hit[0]["ip"]
    # 读取端：倒序、可从 API 取到
    api = requests.get(f"{open_server[0]}/api/audit?limit=10", timeout=3).json()
    assert api["total"] >= 1
    assert any(i["operator"] == "王五" for i in api["items"])


def test_audit_never_blocks(open_server, tmp_path):
    """审计是旁观者不是门卫：即使 artifacts 不可写，业务请求也必须照常成功。"""
    (open_server[1] / "artifacts").mkdir(exist_ok=True)
    (open_server[1] / "artifacts" / "audit.jsonl").write_text("x", encoding="utf-8")
    # 目录位置换成文件后 append 必失败——业务不能跟着失败
    r = requests.get(f"{open_server[0]}/api/audit?limit=5", timeout=3)
    assert r.status_code == 200 and r.json()["items"] == []  # 半截行被跳过不炸


def _mk_case():
    from insta_engine.dsl import TestCase
    return TestCase.model_validate({
        "caseId": "audit_case", "name": "审计用例", "target": {"package": "com.insta.demo"},
        "steps": [{"id": "s1", "intent": "等待", "action": "wait", "params": {"ms": 10}}],
    })


# -- 团队成员（商业化：免费基线 5 人，更高由运营按主机加名额）--------------------------------------------

def test_seat_register_and_dedupe(open_server, tmp_path):
    base, ws = open_server
    # 首次登记占一席；同名再登记=同一人（returning），不重复占席
    r1 = requests.post(f"{base}/api/seats/check", timeout=3, json={"name": "张三"}).json()
    assert r1["ok"] is True and r1["returning"] is False
    r2 = requests.post(f"{base}/api/seats/check", timeout=3, json={"name": "张三"}).json()
    assert r2["returning"] is True and len(r2["seats"]) == 1
    assert (ws / "artifacts" / "team_seats.json").exists()


def test_seat_limit_enforced(open_server, tmp_path, monkeypatch):
    """上限默认 5：第 6 人 403 且提示扩容；.env MAT_SEAT_LIMIT 覆盖生效。"""
    base, ws = open_server
    monkeypatch.setenv("MAT_SEAT_LIMIT", "2")
    for name in ("甲", "乙"):
        assert requests.post(f"{base}/api/seats/check", timeout=3,
                             json={"name": name}).status_code == 200
    r = requests.post(f"{base}/api/seats/check", timeout=3, json={"name": "丙"})
    assert r.status_code == 403 and r.json()["seats_full"] is True
    assert "2/2" in r.json()["error"]
    # 老成员不受影响（核销不占新席）
    assert requests.post(f"{base}/api/seats/check", timeout=3,
                         json={"name": "甲"}).status_code == 200


def test_seat_remove_frees(open_server, tmp_path):
    base, ws = open_server
    requests.post(f"{base}/api/seats/check", timeout=3, json={"name": "误填"})
    r = requests.post(f"{base}/api/seats/remove", timeout=3,
                      json={"name": "误填", "operator": "误填"}).json()
    assert r["ok"] and r["seats"] == []
    # 释放后新名字可登记
    assert requests.post(f"{base}/api/seats/check", timeout=3,
                         json={"name": "新人"}).status_code == 200


def test_seat_invalid_name(open_server):
    base, _ = open_server
    assert requests.post(f"{base}/api/seats/check", timeout=3,
                         json={"name": "   "}).status_code == 400


def test_write_requires_registered_operator(open_server):
    """人数闸（2026-10-12 补洞）：写操作匿名 ⇒ 400；新名字 ⇒ 自动登记；满员 ⇒ 403。

    此前「先匿名」或直接裸调 API 可完全绕开名额（名额只在 seats/check 校验）——
    现在 do_POST 对除 seats/check、activate 外的全部写端点先过闸。"""
    base, ws = open_server
    case = json.dumps({"caseId": "g1", "name": "g", "target": {"package": "p"},
                       "steps": [{"id": "s1", "intent": "等待", "action": "wait",
                                  "params": {"ms": 10}}]}, ensure_ascii=False)
    # 匿名写 ⇒ 400 引导先设名字
    r = requests.post(f"{base}/api/case", timeout=3,
                      json={"name": "g.json", "content": case})
    assert r.status_code == 400 and r.json().get("need_operator") is True
    # 未登记的新名字 ⇒ 自动登记放行（成员名单落盘）
    r2 = requests.post(f"{base}/api/case", timeout=3,
                       json={"name": "g.json", "content": case, "operator": "新成员"})
    assert r2.status_code == 200
    seats = json.loads((ws / "artifacts" / "team_seats.json").read_text(encoding="utf-8"))["seats"]
    assert [s["name"] for s in seats] == ["新成员"]
    # 登记满员后（MAT_SEAT_LIMIT 由 open_server 夹具控制），再换新名字写 ⇒ 403 seats_full
    import os
    from unittest import mock
    with mock.patch.dict(os.environ, {"MAT_SEAT_LIMIT": "1"}):
        r3 = requests.post(f"{base}/api/case", timeout=3,
                           json={"name": "g.json", "content": case, "operator": "第二人"})
    assert r3.status_code == 403 and r3.json().get("seats_full") is True
    # 老名字核销放行（写不受影响）
    r4 = requests.post(f"{base}/api/case", timeout=3,
                       json={"name": "g.json", "content": case, "operator": "新成员"})
    assert r4.status_code == 200
