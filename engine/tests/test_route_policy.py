"""显式路由策略表（`MAT_ROUTE_POLICY=legacy|explicit`，2026-11-04）。

这一轮只做"决策 + 观测"，**默认行为必须与引入前完全一致**。测试钉三件事：

1. `legacy`（默认）下所有任务都是 `auto`、请求不带 `X-Mat-Route`——与现状逐字节一致；
2. `explicit` 下按"任务 + 轮次"分流（任务去向见 `POLICY_TABLE`，体量不再参与）；
3. `MAT_ROUTE` 显式值永远压过策略表（运维兜底不被盖掉）。
"""

from __future__ import annotations

import json

import pytest

from insta_engine import ai_client, ai_metrics, route

# -- 默认值：legacy 不改行为 --------------------------------------------------


def _clear(monkeypatch):
    for name in (route.ENV_KEY, route.ENV_POLICY):
        monkeypatch.delenv(name, raising=False)


def test_default_policy_is_legacy(monkeypatch):
    _clear(monkeypatch)
    assert route.policy() == route.LEGACY
    assert route.is_policy_explicit() is False


def test_unknown_policy_value_falls_back_to_legacy(monkeypatch):
    """拼错/乱填按 legacy：不能因为一个手误就把流量改道。"""
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, "explit")
    assert route.policy() == route.LEGACY


def test_legacy_decisions_are_auto_for_all_tasks(monkeypatch):
    """默认场景（legacy）下每个任务都返回 auto——与服务端 auto 逐字节一致。"""
    _clear(monkeypatch)
    for task in ("gen", "convert", "map", "polish", "describe", "diagnose",
                 "vision-locate", "unknown-task"):
        decision = route.decide(task, {"nl": "登录并查看订单"})
        assert decision.route == route.AUTO, task
        assert decision.reason == "policy:legacy", task


# -- explicit 策略表 ---------------------------------------------------------


def test_explicit_fixed_rules(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    assert route.decide("vision-locate", {"intent": "登录"}).route == route.LOCAL
    assert route.decide("describe", {"pages": []}).route == route.CLOUD
    assert route.decide("diagnose", {"nl": "x"}).route == route.CLOUD
    assert route.decide("gen", {"nl": "x"}).route == route.CLOUD
    assert route.decide("convert", {"ir": {}}).route == route.CLOUD
    # map/polish 是纯文本小任务，但 2026-11-04 起也走云端 ⇒ 本地只需常驻 VL
    assert route.decide("map", {"headers": []}).route == route.CLOUD
    assert route.decide("polish", {"nl": "x"}).route == route.CLOUD


def test_policy_table_has_single_local_task(monkeypatch):
    """本地只剩 vision-locate：下掉文本 14B 的部署前提，改动不能被顺手改回去。"""
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    assert {task for task, target in route.POLICY_TABLE.items() if target == "local"} == {
        "vision-locate"}
    assert set(route.POLICY_TABLE.values()) == {"local", "cloud"}


def test_map_and_polish_always_cloud_regardless_of_size(monkeypatch):
    """map/polish 的体量差异不改变去向（与 gen/convert 同口径）。"""
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    tiny = {"headers": ["a"], "rows": [["1"]]}
    huge = {"headers": ["c" * 50000], "rows": [["r" * 50000] for _ in range(20)]}
    for task, variables in (("map", tiny), ("map", huge), ("map", None),
                            ("polish", {"nl": "x"}), ("polish", {"nl": "x" * 50000})):
        decision = route.decide(task, variables)
        assert decision.route == route.CLOUD, (task, variables)
        assert decision.reason == f"policy:{task}→cloud"


def test_gen_and_convert_always_cloud_regardless_of_size(monkeypatch):
    """2026-11-04：gen/convert 不再按 token 阈值分流——体量多大都走云端。"""
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    tiny = {"nl": "打开首页"}
    huge = {"nl": "x" * 200000, "app_info": {"controls": "c" * 50000}}
    for task in ("gen", "convert"):
        for variables in (tiny, huge, None):
            decision = route.decide(task, variables)
            assert decision.route == route.CLOUD, (task, variables)
            assert decision.reason == f"policy:{task}→cloud"
            assert isinstance(decision.est_tokens, int)


def test_explicit_unknown_task_stays_auto(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    assert route.decide("something-new", {}).route == route.AUTO


def test_explicit_round_two_or_later_goes_cloud(monkeypatch):
    """同一产物不混源：第 2 轮及以后一律云端（vision 除外，见下条）。

    任务表里现在只有 vision 是 local，其余第 1 轮就已 cloud；轮次规则仍对
    "表里没有的任务"生效——将来新增本地任务或未知任务不会在第 2 轮混源。
    """
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    for task in ("gen", "convert", "map", "polish", "describe", "diagnose", "custom-task"):
        assert route.decide(task, {"nl": "x"}, round_index=2).route == route.CLOUD, task
        assert route.decide(task, {"nl": "x"}, round_index=5).route == route.CLOUD, task
    assert route.decide("custom-task", {}, round_index=1).route == route.AUTO
    assert "round 2" in route.decide("gen", {"nl": "x"}, round_index=2).reason


def test_vision_locate_stays_local_even_on_retry(monkeypatch):
    """`vision-locate` 是"恒定本地"：截图最敏感，重试也不改道云端。"""
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    assert route.decide("vision-locate", {}, round_index=3).route == route.LOCAL


# -- 估算（仅观测，不参与去向） ------------------------------------------------


def test_estimate_tokens_includes_template(monkeypatch):
    _clear(monkeypatch)
    # variables={} 的 JSON 是 "{}"（2 字符），gen 模板按 4000 字符计
    assert route.estimate_tokens("gen", {}) == 2502  # ceil((2+4000)/1.6)
    assert route.estimate_tokens("map", {}) == 377   # ceil((2+600)/1.6)


# -- MAT_ROUTE 永远优先 ------------------------------------------------------


@pytest.mark.parametrize("env_route", ["local", "cloud"])
def test_env_override_beats_policy(monkeypatch, env_route):
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    monkeypatch.setenv(route.ENV_KEY, env_route)
    d = route.decide("describe", {"pages": []})
    assert d.route == env_route
    assert d.reason == f"env override: MAT_ROUTE={env_route}"


def test_env_override_wins_in_legacy_too(monkeypatch):
    """legacy 是"策略表不生效"，不是"忽略 MAT_ROUTE"——运维显式值照旧生效。"""
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_KEY, route.LOCAL)
    assert route.decide("gen", {}).route == route.LOCAL
    monkeypatch.setenv(route.ENV_KEY, "auto")
    assert route.decide("gen", {}).route == route.AUTO


# -- 请求头：默认不带，explicit 才带 -----------------------------------------


class _Resp:
    def __init__(self, body: dict):
        self.status_code = 200
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


def _capture(monkeypatch, body: dict | None = None, *, record: bool = True) -> dict:
    seen: dict = {}
    if record:
        monkeypatch.setattr(ai_client, "_record_metrics", lambda **kw: None)
    monkeypatch.setattr(ai_client, "_note_route", lambda *a, **kw: None)

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        seen["headers"] = dict(headers or {})
        seen["payload"] = json
        return _Resp(body or {"content": "ok", "route": "local"})

    monkeypatch.setattr(ai_client.requests, "post", fake_post)
    return seen


def _client() -> ai_client.GenClient:
    return ai_client.GenClient("https://proxy.invalid", "k", "m", prompt_mode="server")


def test_default_request_has_no_route_header(monkeypatch, tmp_path):
    """默认场景逐字节一致：legacy 不带 X-Mat-Route（服务端仍按自己的 auto 策略）。"""
    _clear(monkeypatch)
    monkeypatch.chdir(tmp_path)
    seen = _capture(monkeypatch)
    ai_client._server_task_text(_client(), "polish", {"nl": "x"}, "ctx", None, None)
    assert "X-Mat-Route" not in seen["headers"]


def test_legacy_with_explicit_mat_route_still_sends_header(monkeypatch, tmp_path):
    """现状行为不变：`MAT_ROUTE=cloud` 在 legacy 下照旧带请求头。"""
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_KEY, "cloud")
    monkeypatch.chdir(tmp_path)
    seen = _capture(monkeypatch)
    ai_client._server_task_text(_client(), "polish", {"nl": "x"}, "ctx", None, None)
    assert seen["headers"]["X-Mat-Route"] == "cloud"


def test_explicit_policy_sends_task_route(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    monkeypatch.chdir(tmp_path)
    seen = _capture(monkeypatch)
    ai_client._server_task_text(_client(), "describe", {"pages": []}, "ctx", None, None)
    assert seen["headers"]["X-Mat-Route"] == "cloud"

    seen = _capture(monkeypatch)
    ai_client._server_task_text(_client(), "map", {"headers": []}, "ctx", None, None)
    assert seen["headers"]["X-Mat-Route"] == "cloud"

    seen = _capture(monkeypatch)
    ai_client._server_task_text(_client(), "vision-locate", {"intent": "登录"},
                               "ctx", None, None)
    assert seen["headers"]["X-Mat-Route"] == "local"


def test_explicit_retry_round_sends_cloud(monkeypatch, tmp_path):
    """未知任务第 1 轮是 auto；第 2 轮起由轮次规则改道 cloud（请求头可见）。"""
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    monkeypatch.chdir(tmp_path)
    seen = _capture(monkeypatch)
    ai_client._server_task_text(_client(), "custom-task", {}, "ctx", None, None, round_index=2)
    assert seen["headers"]["X-Mat-Route"] == "cloud"


# -- 观测：ai.jsonl 记录决策、report 按任务拆分 ------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MAT_HOME", str(tmp_path))
    monkeypatch.setenv("MAT_AI_METRICS", "1")


def _rows() -> list[dict]:
    path = ai_metrics.metrics_path()
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_decision_is_recorded_in_metrics(home, monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    monkeypatch.chdir(tmp_path)
    _capture(monkeypatch, record=False)
    ai_client._server_task_text(_client(), "describe", {"pages": []}, "ctx", None, None,
                               round_index=2)
    row = _rows()[-1]
    assert row["route_decision"] == "cloud"
    assert row["route_reason"] == "policy:round 2→cloud"
    assert row["route_round"] == 2
    assert isinstance(row["route_est_tokens"], int)


def test_legacy_metrics_leave_decision_empty(home, monkeypatch, tmp_path):
    """legacy 下决策为空——观测字段存在，但不改变默认请求行为。"""
    _clear(monkeypatch)
    monkeypatch.chdir(tmp_path)
    _capture(monkeypatch, record=False)
    ai_client._server_task_text(_client(), "polish", {"nl": "x"}, "ctx", None, None)
    row = _rows()[-1]
    assert row["route_decision"] is None
    assert row["route_reason"] == "policy:legacy"


def test_repair_mark_accumulates_without_duplicates():
    """A/B 的"页守卫冲突率"要能看见 JSON 修复之后的页冲突。"""
    assert ai_client._repair_mark("", "page") == "page"
    assert ai_client._repair_mark("json", "page") == "json+page"
    assert ai_client._repair_mark("json+page", "page") == "json+page"
    assert ai_client._repair_mark("json+page", "schema") == "json+page+schema"


def test_route_mix_by_task_snapshot():
    ai_client.route_reset()
    ai_client._note_route("local", "gen")
    ai_client._note_route("maas", "describe")
    ai_client._note_route("weird", "gen")
    by_task = ai_client.route_snapshot_by_task()
    assert by_task["gen"] == {"local": 1, "maas": 0, "unknown": 1}
    assert by_task["describe"] == {"local": 0, "maas": 1, "unknown": 0}
    # 全局口径形状不变（旧消费方不受影响）
    assert ai_client.route_snapshot() == {"local": 1, "maas": 1, "unknown": 1}
    ai_client.route_reset()
    assert ai_client.route_snapshot_by_task() == {}


def test_eval_report_has_route_policy_and_by_task(monkeypatch, tmp_path):
    """report 新增字段是加法：旧字段 `route_mix` 形状不变。"""
    from insta_engine import eval_suite

    ai_client.route_reset()
    ai_client._note_route("local", "gen")
    report = {"route_mix": ai_client.route_snapshot(),
              "route_mix_by_task": ai_client.route_snapshot_by_task(),
              "route_policy": route.policy()}
    assert report["route_mix"] == {"local": 1, "maas": 0, "unknown": 0}
    assert report["route_mix_by_task"]["gen"]["local"] == 1
    assert report["route_policy"] == route.LEGACY
    assert eval_suite is not None
