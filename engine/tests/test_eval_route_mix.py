"""评测的**路由口径**（2026-10-30）：报告要能回答"这批数字是哪个模型出的"。

背景：服务端对 free/pro 做了"本地模型优先、失败静默回落官方 MaaS"。回落是设计使然
（用户侧不能失败），但同一批跑批可能一半 14B、一半官方——不记录口径的话，基线会把
混源数字固化成参照，之后的 compare 全都在比两件不同的事。
"""

from __future__ import annotations

import json

import pytest

from insta_engine import ai_client, eval_suite
from insta_engine.eval_suite import compare, summarize, summary_line

# -- 客户端计数 -------------------------------------------------------------


def test_route_counts_reset_and_snapshot():
    """计数按批清零：`route_mix` 只能是这一批的，不能带历史累计。"""
    ai_client.route_reset()
    ai_client._note_route("local")
    ai_client._note_route("local")
    ai_client._note_route("maas")
    assert ai_client.route_snapshot() == {"local": 2, "maas": 1, "unknown": 0}
    ai_client.route_reset()
    assert ai_client.route_snapshot() == {"local": 0, "maas": 0, "unknown": 0}


def test_route_unknown_when_server_does_not_report():
    """旧服务端不返回 `route` ⇒ 记 unknown，而不是假装走本地。"""
    ai_client.route_reset()
    ai_client._note_route(None)
    ai_client._note_route("")
    ai_client._note_route("weird")
    assert ai_client.route_snapshot() == {"local": 0, "maas": 0, "unknown": 3}


class _Resp:
    def __init__(self, body: dict):
        self.status_code = 200
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


def test_server_task_text_records_route(monkeypatch, tmp_path):
    """真实请求路径上要记账：服务端回什么 route，就记什么。"""
    monkeypatch.setattr(ai_client, "_record_metrics", lambda **kw: None)
    monkeypatch.setattr(ai_client.requests, "post",
                        lambda *a, **kw: _Resp({"content": "ok", "route": "local"}))
    ai_client.route_reset()
    client = ai_client.GenClient("https://proxy.invalid", "k", "m", prompt_mode="server")
    monkeypatch.chdir(tmp_path)  # 避免读到工作区的 prompts/*.append.md
    out = ai_client._server_task_text(client, "polish", {"nl": "x"}, "ctx", None, None)
    assert out == "ok"
    assert ai_client.route_snapshot()["local"] == 1


# -- 报告与对比 -------------------------------------------------------------


def _r(task: str, status: str, passed: int, total: int) -> dict:
    return {"task": task, "run": 1, "status": status, "passed": passed,
            "total": total, "blocked": None, "env_error": None,
            "app_version": "1.0", "app_apk_sha256": "fp1"}


def _report(route_mix: dict, results: list[dict] | None = None) -> dict:
    rs = results or [_r("a", "passed", 3, 3)]
    return {"schema": eval_suite.SCHEMA, "repeat": 1, "threshold": 0.8,
            "aborted": False, "route_mix": route_mix, **summarize(rs, 1, 0.8)}


def test_summary_line_flags_official_only_batch():
    """全走官方的那一批，一行结论里必须自己说明——否则会被当成本地模型的成绩引用。"""
    line = summary_line(_report({"local": 0, "maas": 5, "unknown": 0}))
    assert "全部走官方模型" in line


def test_summary_line_flags_mixed_routes():
    line = summary_line(_report({"local": 3, "maas": 2, "unknown": 0}))
    assert "路由混源" in line


def test_summary_line_silent_when_all_local():
    """全本地是常态，不往一行结论里塞噪声。"""
    line = summary_line(_report({"local": 5, "maas": 0, "unknown": 0}))
    assert "官方模型" not in line and "混源" not in line


def test_compare_warns_when_routes_differ():
    """基线走本地、这次走官方 ⇒ 中位差异里混着"模型不同"，只能提示，不判 FAIL。"""
    base = _report({"local": 10, "maas": 0, "unknown": 0})
    cur = _report({"local": 0, "maas": 10, "unknown": 0},
                  [_r("a", "failed", 0, 3)])
    out = compare(cur, base)
    assert "路由不同" in out["note"]
    assert out["verdict"] == "FAIL"  # 通过率确实掉了，回归判定照旧
    assert any("路由不同" in r for r in out["regressions"]) is False


def test_compare_ignores_route_when_only_one_side_reports():
    """旧基线没有 route_mix（升级前的报告）⇒ 不做任何路由推断。"""
    base = _report({})
    del base["route_mix"]
    cur = _report({"local": 0, "maas": 4, "unknown": 0})
    assert "路由不同" not in compare(cur, base)["note"]


# -- run_eval 写入 route_mix -------------------------------------------------


@pytest.mark.parametrize("routes", [["local", "local"], ["maas", "maas"]])
def test_run_eval_writes_route_mix_of_this_batch(monkeypatch, tmp_path, routes):
    """报告里的 `route_mix` 是本批实际计数（且每次跑批前清零）。"""
    monkeypatch.chdir(tmp_path)
    suite = {"pass_threshold": 0.8,
             "tasks": [{"id": "t1", "case": {"steps": [{"action": "launch"}]}}]}
    (tmp_path / "s.suite.json").write_text(json.dumps(suite), encoding="utf-8")

    def fake_run_task(task, serial, suite, run_idx, repeat, artifacts_root,
                      quality_gate, platform="android"):
        ai_client._note_route(routes[run_idx - 1])
        return _r(task["id"], "passed", 1, 1)

    monkeypatch.setattr(eval_suite, "_run_task", fake_run_task)
    ai_client._note_route("maas")  # 上一批的残留：必须被 run_eval 清零
    report = eval_suite.run_eval(tmp_path / "s.suite.json", repeat=2)
    assert report["route_mix"] == {"local": routes.count("local"),
                                   "maas": routes.count("maas"), "unknown": 0}
