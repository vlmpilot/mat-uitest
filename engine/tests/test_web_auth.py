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
def shared_server(tmp_path, monkeypatch):
    """共享模式等价（`--share` 听 0.0.0.0）——成员名额只在共享模式下有意义；
    而给几个人还要看授权（2026-10-25：仅团队授权，见 test_seat_quota.py）。"""
    yield from _mk_server(tmp_path, monkeypatch, host="0.0.0.0")


def _mint(plan: str, days: int = 30) -> str:
    """签一枚授权码（不设 MAT_LICENSE_SECRET ⇒ 走 BETA_PUBLIC_SIGNING_KEY）。"""
    import hashlib
    import hmac
    import uuid

    from insta_engine.licensing import BETA_PUBLIC_SIGNING_KEY

    body = f"{plan}.{int(time.time()) + days * 86400}.{uuid.uuid4().hex[:6]}"
    sig = hmac.new(BETA_PUBLIC_SIGNING_KEY.encode(), body.encode(),
                   hashlib.sha256).hexdigest()[:16]
    raw = f"{body}.{sig}"
    return "MAT-" + "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


@pytest.fixture()
def team_shared_server(tmp_path, monkeypatch):
    """团队授权 + 共享模式：实名闸与成员基线都按团队档走（2026-10-25 起只有这种
    组合才保留"写操作必须实名"——未授权共享本质是单人，见 test_seat_quota.py）。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "license.json").write_text(json.dumps(
        {"plan": "team", "expires_at": int(time.time()) + 30 * 86400,
         "code": _mint("team")}, ensure_ascii=False), encoding="utf-8")
    yield from _mk_server(tmp_path, monkeypatch, host="0.0.0.0")


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


def test_auth_brute_force_throttled_and_audited(locked_server):
    """评审 P1-4（2026-10-20）：/api/auth 此前无计数/无冷却/失败不落审计——
    6 hex 口令在局域网纯脚本爆破零成本。现在：窗口内失败 5 次 ⇒ 429 冷却；
    每次失败落 audit.jsonl；成功后计数清零。"""
    web_server.ConsoleHandler._AUTH_FAILS.clear()
    try:
        base, ws = locked_server
        for _ in range(5):
            assert requests.post(f"{base}/api/auth", json={"key": "wrong"},
                                 timeout=3).status_code == 401
        r = requests.post(f"{base}/api/auth", json={"key": "a1b2c3"}, timeout=3)
        assert r.status_code == 429 and "秒后重试" in r.json()["error"], "超限后连正确口令也要等冷却"
        rows = [json.loads(line) for line in
                (ws / "artifacts" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        fails = [x for x in rows if x["action"] == "/api/auth:fail"]
        assert len(fails) == 5 and all(x["ip"] for x in fails), "失败必须落审计（含来源 IP）"
        assert all("wrong" not in x["target"] for x in fails), "口令本体绝不落审计"
    finally:
        web_server.ConsoleHandler._AUTH_FAILS.clear()


def test_auth_success_resets_fail_counter(locked_server):
    """成功一次清零：不会让「手滑 4 次 + 成功 1 次 + 再手滑 1 次」被误锁。"""
    web_server.ConsoleHandler._AUTH_FAILS.clear()
    try:
        base, _ = locked_server
        for _ in range(4):
            requests.post(f"{base}/api/auth", json={"key": "wrong"}, timeout=3)
        assert requests.post(f"{base}/api/auth", json={"key": "a1b2c3"},
                             timeout=3).status_code == 200
        # 清零后再失败 4 次仍不触发冷却
        for _ in range(4):
            assert requests.post(f"{base}/api/auth", json={"key": "wrong"},
                                 timeout=3).status_code == 401
    finally:
        web_server.ConsoleHandler._AUTH_FAILS.clear()


def test_auth_cookie_httponly(locked_server):
    """评审 P3（2026-10-20）：鉴权 cookie 补 HttpOnly——报告页是同源 HTML，
    一旦有存储型 XSS，JS 可读 cookie 即丢口令。零成本加固。"""
    base, _ = locked_server
    r = requests.post(f"{base}/api/auth", json={"key": "a1b2c3"}, timeout=3)
    cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


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
    # 方案 B（2026-10-25）：空名兜底「本机」——单人模式免名时审计字段仍有值
    assert web_api.runtime.operator_of({}) == "本机"
    assert web_api.runtime.operator_of(None) == "本机"
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


# -- 团队成员（2026-10-25 起：共享名额只随团队授权提供；更高由运营按主机加名额）------------

def test_seat_register_and_dedupe(shared_server, tmp_path):
    """登记/去重是**共享模式**语义（方案 B 后本机单人模式不再登记——见下一条）。"""
    base, ws = shared_server
    # 首次登记占一席；同名再登记=同一人（returning），不重复占席
    r1 = requests.post(f"{base}/api/seats/check", timeout=3, json={"name": "张三"}).json()
    assert r1["ok"] is True and r1["returning"] is False
    r2 = requests.post(f"{base}/api/seats/check", timeout=3, json={"name": "张三"}).json()
    assert r2["returning"] is True and len(r2["seats"]) == 1
    assert (ws / "artifacts" / "team_seats.json").exists()


def test_solo_rename_always_ok(open_server, tmp_path):
    """方案 B 第三处（2026-10-25 用户实测）：单人模式设名后**必须能改名**——
    名字只是本地标签，不登记不限额；此前首次设名占 1/1 席位，改名即
    「成员已满」403。"""
    base, ws = open_server
    for name in ("第一个名字", "第二个名字", "再来一个"):
        r = requests.post(f"{base}/api/seats/check", timeout=3, json={"name": name})
        assert r.status_code == 200 and r.json()["ok"] is True, name
    assert not (ws / "artifacts" / "team_seats.json").exists(), "单人模式不落席位文件"


def test_seat_limit_enforced(shared_server, tmp_path):
    """🔴 2026-10-25：共享模式（--share）下**未授权**（体验/标准）恒 1 人——
    第 2 人 403 且消息指向团队授权；老成员核销不受影响。

    团队授权下的基线放人由 test_seat_quota.py 的 `seat_limit(shared=True)` 成对锁住
    （本机控制台 127.0.0.1 上限恒为 1，见该文件的成对断言）。
    """
    base, ws = shared_server
    assert requests.post(f"{base}/api/seats/check", timeout=3,
                         json={"name": "甲"}).status_code == 200
    r = requests.post(f"{base}/api/seats/check", timeout=3, json={"name": "乙"})
    assert r.status_code == 403 and r.json()["seats_full"] is True
    assert "团队授权" in r.json()["error"]
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


# -- 分享模式下本机仍可用本机能力（_local_only 按客户端来源判定，2026-10-15 修复）--------

@pytest.fixture()
def share_server(tmp_path, monkeypatch):
    """--share 形态：0.0.0.0 监听（无口令，与 --share 既有语义一致）。"""
    yield from _mk_server(tmp_path, monkeypatch, host="0.0.0.0")


def test_share_mode_local_still_has_local_abilities(share_server):
    """--share 下本机（127.0.0.1 来源）浏览工作区/读凭据必须照常 200。

    回归：旧实现把 server_address（0.0.0.0）传给 _local_only ⇒ 分享模式连本机
    也不能打开工作区/执行配置命令。局域网来源的 403 由 api 层单测覆盖
    （test_local_only_gates_by_client_ip）——本机测不出非回环来源。"""
    base, ws = share_server
    r = requests.get(f"{base}/api/fs?path={ws}", timeout=3)
    assert r.status_code == 200 and r.json()["path"] == str(ws), (
        "分享模式下本机应仍可浏览工作区")
    assert requests.get(f"{base}/api/cred", timeout=3).status_code == 200


def test_write_requires_registered_operator(team_shared_server):
    """人数闸（2026-10-12 补洞）：**仅团队授权下的共享模式**要求实名——
    匿名 ⇒ 400；新名字 ⇒ 自动登记；满员 ⇒ 403；老名字核销放行。

    单人模式与未授权共享免名（后者本质也是单人）：见 test_solo_write_anonymous_ok
    与 test_seat_quota.py 的 test_shared_mode_name_free_without_team_authorization。
    """
    base, ws = team_shared_server
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
    # 团队基线 5 人，压到 1 验满员：再换新名字写 ⇒ 403 seats_full
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


def test_unauthorized_share_full_seat_self_unlock(shared_server, tmp_path):
    """🔴 未授权共享的 1 席被旧名占住 + 换浏览器（operator 空）⇒ 自解锁闭环。

    4d82e64 的文案承诺「到『设置 → 成员管理』移除旧名字即可进入」——此前移除端点
    也过实名闸（enforce 只豁免 check/activate），匿名移除 400 ⇒ 承诺成死路。
    现在未授权共享与单人同族免名：匿名移除 ⇒ 名单空 ⇒ 新名字可登记 ⇒ 写操作放行。
    """
    base, ws = shared_server
    art = ws / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    (art / "team_seats.json").write_text(json.dumps(
        {"seats": [{"name": "旧名", "first_seen": "2026-10-20T10:00:00+00:00",
                    "last_seen": "2026-10-20T10:00:00+00:00"}]}, ensure_ascii=False),
        encoding="utf-8")

    # 换浏览器后 operator 空：弹窗填任何新名字 ⇒ 403，文案带即时出路
    r = requests.post(f"{base}/api/seats/check", timeout=3, json={"name": "我"})
    assert r.status_code == 403 and r.json()["seats_full"] is True
    assert "移除旧名字" in r.json()["error"]

    # 按文案自救：匿名移除旧名（enforce 对未授权共享放行）⇒ 解锁
    r = requests.post(f"{base}/api/seats/remove", timeout=3, json={"name": "旧名"})
    assert r.status_code == 200 and r.json()["seats"] == []

    # 再进：新名字登记成功；且写操作同样免名（设名成功也干得了活）
    assert requests.post(f"{base}/api/seats/check", timeout=3,
                         json={"name": "我"}).status_code == 200
    case = json.dumps({"caseId": "u1", "name": "u", "target": {"package": "p"},
                       "steps": [{"id": "s1", "action": "wait", "params": {"ms": 10}}]},
                      ensure_ascii=False)
    assert requests.post(f"{base}/api/case", timeout=3,
                         json={"name": "u.json", "content": case}).status_code == 200


def test_solo_write_anonymous_ok(open_server):
    """方案 B（2026-10-25，发起人批准）：单人模式匿名写直接放行——不再要求设名。

    原行为：匿名 400 need_operator。现仅团队模式（--share）保留该闸（上一条测试）。
    """
    base, ws = open_server
    case = json.dumps({"caseId": "solo1", "name": "s", "target": {"package": "p"},
                       "steps": [{"id": "s1", "action": "wait", "params": {"ms": 10}}]},
                      ensure_ascii=False)
    r = requests.post(f"{base}/api/case", timeout=3,
                      json={"name": "solo1.json", "content": case})
    assert r.status_code == 200, r.text
    assert not (ws / "artifacts" / "team_seats.json").exists(), "单人模式不登记席位"
