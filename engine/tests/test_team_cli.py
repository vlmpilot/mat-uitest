"""团队协作 CLI：门控、推送/拉取、报告分享、审计输出。

客户端这层最容易出的错是**静默半成功**（部分文件推失败但退出码 0、pull 覆盖了本地
不该覆盖的文件）。用例按这个口径钉：失败要计数、默认不覆盖、档位不够先给可行动提示。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from insta_engine import team as team_mod
from insta_engine.entitlements import EntitlementError


@pytest.fixture(autouse=True)
def _team_ok(monkeypatch):
    """默认按"已授权 + 有 token"跑；单条用例要测门控时自行覆盖。"""
    monkeypatch.setattr(team_mod, "entitlements", _FakeEnt(allow=True))
    monkeypatch.setattr(team_mod, "_bearer", lambda: "tok")


class _FakeEnt:
    #: `_require` 里 `except entitlements.EntitlementError` 也要能跟着替身走
    EntitlementError = EntitlementError

    def __init__(self, allow: bool):
        self.allow = allow
        self.asked: list[str] = []

    def require(self, feature: str) -> None:
        self.asked.append(feature)
        if not self.allow:
            raise EntitlementError(f"当前套餐（free）不含「{feature}」")


class Recorder:
    def __init__(self, cases=None, detail=None):
        self.calls: list[tuple] = []
        self.cases = cases or []
        self.detail = detail or {}

    def __call__(self, method, path, payload=None, bearer=None):
        self.calls.append((method, path, payload))
        if path.startswith("/team/cases?") or (path == "/team/cases" and method == "GET"):
            return {"cases": self.cases}
        if path.startswith("/team/cases/") and method == "GET":
            return self.detail
        if path == "/team/cases":
            return {"updated_at": "2026-10-29T08:00:00+00:00"}
        if path.startswith("/audit"):
            return {"events": self.detail.get("events", [])}
        if path == "/report/share":
            return {"id": "a" * 32, "path": "/r/" + "a" * 32, "days": 30,
                    "expires_at": 0}
        return {}


def _write_case(root: Path, name: str, pkg: str = "com.demo", case_name: str = "登录冒烟"):
    (root / "cases").mkdir(parents=True, exist_ok=True)
    (root / "cases" / name).write_text(json.dumps({
        "caseId": "c1", "name": case_name, "target": {"package": pkg},
        "steps": [{"action": "launch"}]}), encoding="utf-8")


# -- 门控 -----------------------------------------------------------------

def test_push_requires_team_share(tmp_path, monkeypatch):
    fake = _FakeEnt(allow=False)
    monkeypatch.setattr(team_mod, "entitlements", fake)
    _write_case(tmp_path, "a.json")
    with pytest.raises(SystemExit) as exc:
        team_mod.push(tmp_path)
    assert "不含" in str(exc.value) and fake.asked == ["team_share"]


def test_share_requires_report_share(tmp_path, monkeypatch):
    fake = _FakeEnt(allow=False)
    monkeypatch.setattr(team_mod, "entitlements", fake)
    run = tmp_path / "run.json"
    run.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        team_mod.share_report(run)
    assert fake.asked == ["report_share"]


def test_audit_requires_audit_log(monkeypatch):
    fake = _FakeEnt(allow=False)
    monkeypatch.setattr(team_mod, "entitlements", fake)
    with pytest.raises(SystemExit):
        team_mod.audit()
    assert fake.asked == ["audit_log"]


def test_missing_token_gives_actionable_message(tmp_path, monkeypatch):
    monkeypatch.setattr(team_mod, "_bearer", lambda: (_ for _ in ()).throw(
        SystemExit("缺少云端凭据")))
    with pytest.raises(SystemExit):
        team_mod.push(tmp_path)


# -- push -----------------------------------------------------------------

def test_push_sends_valid_cases_and_skips_noise(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(team_mod.cloudapi, "request", rec)
    _write_case(tmp_path, "one.json", "com.demo", "登录冒烟")
    _write_case(tmp_path, "two.json", "com.other", "搜索")
    (tmp_path / "cases" / "group.suite.json").write_text('{"name":"组","cases":[]}',
                                                          encoding="utf-8")
    (tmp_path / "cases" / "broken.json").write_text("{not json", encoding="utf-8")
    _write_case(tmp_path, "nopkg.json", "")
    runner = tmp_path / "cases" / "nopkg.json"
    doc = json.loads(runner.read_text(encoding="utf-8"))
    doc["target"] = {}
    runner.write_text(json.dumps(doc), encoding="utf-8")

    assert team_mod.push(tmp_path) == 0
    sent = [(c[2]["app"], c[2]["name"]) for c in rec.calls]
    assert sent == [("com.demo", "登录冒烟"), ("com.other", "搜索")], "套件与坏文件必须跳过"


def test_push_filters_by_app_and_counts_failures(tmp_path, monkeypatch):
    class Flaky(Recorder):
        def __call__(self, method, path, payload=None, bearer=None):
            if payload and payload.get("app") == "com.bad":
                raise team_mod.cloudapi.CloudError("服务端 500")
            return super().__call__(method, path, payload, bearer)

    rec = Flaky()
    monkeypatch.setattr(team_mod.cloudapi, "request", rec)
    _write_case(tmp_path, "a.json", "com.good")
    _write_case(tmp_path, "b.json", "com.bad")
    assert team_mod.push(tmp_path) == 1, "有失败要非零退出"
    assert [c[2]["app"] for c in rec.calls] == ["com.good"], "失败的请求不进成功记录"

    rec2 = Recorder()
    monkeypatch.setattr(team_mod.cloudapi, "request", rec2)
    assert team_mod.push(tmp_path, app="com.good") == 0
    assert [c[2]["app"] for c in rec2.calls] == ["com.good"]


# -- pull -----------------------------------------------------------------

def test_pull_writes_files_but_does_not_overwrite_by_default(tmp_path, monkeypatch):
    rec = Recorder(cases=[{"app": "com.demo", "case_id": "case_login", "name": "登录冒烟",
                           "actor": "u", "updated_at": "", "size": 10}],
                   detail={"content": '{"name": "登录冒烟"}', "app": "com.demo",
                           "case_id": "case_login", "name": "登录冒烟", "actor": "u",
                           "updated_at": ""})
    monkeypatch.setattr(team_mod.cloudapi, "request", rec)
    assert team_mod.pull(tmp_path) == 0
    dest = tmp_path / "cases" / "登录冒烟.json"
    assert dest.exists() and "登录冒烟" in dest.read_text(encoding="utf-8")

    dest.write_text("local edit", encoding="utf-8")
    assert team_mod.pull(tmp_path) == 0
    assert dest.read_text(encoding="utf-8") == "local edit", "默认不覆盖本地改动"

    assert team_mod.pull(tmp_path, force=True) == 0
    assert "登录冒烟" in dest.read_text(encoding="utf-8")


def test_pull_keeps_same_name_cases_apart(tmp_path, monkeypatch):
    """同名不同 caseId 的两条用例都要落地（2026-10-30 实测：第二条被当成"本地已有"丢掉）。

    团队库自 0.7.6 起按 caseId 存，客户端文件名却只能是标题——所以重名时要靠
    `标题__caseId.json` 区分；第二次拉取必须幂等（各认各的文件，不重复写）。
    """
    meta = [{"app": "com.demo", "case_id": cid, "name": "登录冒烟",
             "actor": "u", "updated_at": "", "size": 10} for cid in ("c-a", "c-b")]

    def fake(method, path, payload=None, bearer=None):
        if method == "GET" and path.startswith("/team/cases/"):
            parts = path.strip("/").split("/")   # team/cases/<app>/<case_id>
            pkg, cid = parts[-2], parts[-1]
            return {"content": json.dumps({"caseId": cid, "name": "登录冒烟"}),
                    "app": pkg, "case_id": cid, "name": "登录冒烟",
                    "actor": "u", "updated_at": ""}
        if method == "GET":
            return {"cases": meta}
        return {}

    monkeypatch.setattr(team_mod.cloudapi, "request", fake)
    assert team_mod.pull(tmp_path) == 0
    files = sorted(p.name for p in (tmp_path / "cases").glob("*.json"))
    assert files == ["登录冒烟.json", "登录冒烟__c-b.json"], files
    assert json.loads((tmp_path / "cases" / "登录冒烟.json").read_text())["caseId"] == "c-a"

    assert team_mod.pull(tmp_path) == 0, "第二次拉取应幂等（不重复写新文件）"
    assert len(list((tmp_path / "cases").glob("*.json"))) == 2


def test_pull_empty_team_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(team_mod.cloudapi, "request", Recorder(cases=[]))
    assert team_mod.pull(tmp_path) == 0


# -- share ----------------------------------------------------------------

def test_share_report_builds_html_with_steps(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(team_mod.cloudapi, "request", rec)
    run = tmp_path / "run.json"
    run.write_text(json.dumps({
        "case": {"name": "登录冒烟"},
        "run": {"case_name": "登录冒烟", "status": "passed", "duration_ms": 1234,
                "steps": [{"step_id": "s1", "action": "launch", "status": "passed",
                           "error": None}]}}), encoding="utf-8")
    assert team_mod.share_report(run) == 0
    payload = rec.calls[0][2]
    assert payload["title"] == "登录冒烟"
    assert "<table>" in payload["html"] and "s1" in payload["html"]
    assert "1234" in payload["html"]


def test_share_report_rejects_bad_json(tmp_path, monkeypatch):
    monkeypatch.setattr(team_mod.cloudapi, "request", Recorder())
    run = tmp_path / "run.json"
    run.write_text("not json", encoding="utf-8")
    assert team_mod.share_report(run) == 1


# -- audit ----------------------------------------------------------------

def test_audit_prints_events(tmp_path, monkeypatch, capsys):
    rec = Recorder(detail={"events": [
        {"id": 2, "actor": "alice", "action": "report.share", "target": "abc",
         "detail": "", "created_at": "2026-10-29T08:00:00+00:00"},
        {"id": 1, "actor": "bob", "action": "case.push", "target": "a/n",
         "detail": "", "created_at": "2026-10-29T07:00:00+00:00"}]})
    monkeypatch.setattr(team_mod.cloudapi, "request", rec)
    assert team_mod.audit(limit=10) == 0
    out = capsys.readouterr().out
    assert "alice" in out and "case.push" in out and "共 2 条" in out
    assert rec.calls[0][1] == "/audit?limit=10"
