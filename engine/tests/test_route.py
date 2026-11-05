"""生成路由（`MAT_ROUTE` / `MAT_ROUTE_POLICY`）：`mat status` 必须说出真实去向。

这组用例全部**从行为推导**，不钉某一句话：

- 每个任务在 `explicit` 下的去向必须等于 `decide()` 的返回值；
- 轮次规则直接对 `decide(task, round_index=N)` 断言，而不是看文案；
- `legacy` 下 `decide()` 恒为 `auto`，服务端 auto 的实际规则是**本地优先**
  （本地不可用即报错、不回落、不按轮次改道），由 `/usage.routing` 的事实落实；
- 最后一条是**反矛盾断言**：同一任务不能既出现在「云端」分组，又被一句
  「文本都在本地处理」盖住——这正是 review 抓到的 P1。
"""

from __future__ import annotations

import pytest

from insta_engine import route
from insta_engine.__main__ import _print_routing


def _clear(monkeypatch):
    for name in (route.ENV_KEY, route.ENV_POLICY):
        monkeypatch.delenv(name, raising=False)


def _text(lines: list[str]) -> str:
    return "\n".join(lines)


def _group_tasks(lines: list[str]) -> dict[str, set[str]]:
    """把 `  - 本地：a、b` 这类行解析成 {标签前缀: {任务}}，用来做一致性断言。"""
    out: dict[str, set[str]] = {}
    for line in lines:
        if not line.startswith("  - "):
            continue
        label, _, tasks = line[4:].partition("：")
        out[label] = {t.strip() for t in tasks.split("、") if t.strip()}
    return out


# -- 默认值 / 取值规范化 ------------------------------------------------------


def test_default_is_auto(monkeypatch):
    _clear(monkeypatch)
    assert route.preference() == route.AUTO
    assert route.is_explicit() is False
    assert route.unknown_value() == ""


def test_values_are_normalized(monkeypatch):
    for raw, want in (("cloud", route.CLOUD), ("CLOUD ", route.CLOUD),
                      ("local", route.LOCAL), ("auto", route.AUTO), ("", route.AUTO)):
        monkeypatch.setenv("MAT_ROUTE", raw)
        assert route.preference() == want, raw


def test_typo_falls_back_to_auto_and_is_reported(monkeypatch):
    """拼错走安全方向（不外发），但要能被 doctor 报出来，别静默。"""
    monkeypatch.setenv("MAT_ROUTE", "clod")
    assert route.preference() == route.AUTO
    assert route.unknown_value() == "clod"


# -- legacy：服务端 auto = 本地优先，与 decide() 的 auto 一致 -----------------


def test_legacy_with_local_endpoints_is_all_local(monkeypatch):
    """默认输出不能再说 gen 在云端、同时又说文本都在本地处理。

    `decide()` 在 legacy 下恒为 auto；服务端 auto 配了本地端点就走本地，
    所以 route_map 必须把**所有任务**落实成 local，且轮次不改变去向。
    """
    _clear(monkeypatch)
    facts = {"text_local": True, "vision_local": True}
    routes = route.route_map(facts)
    for task in route.TASK_ORDER:
        assert routes[task] == route.LOCAL, task
        for round_index in (1, 2, 5):
            assert route.decide(task, {}, round_index=round_index).route == route.AUTO, (
                task, round_index)

    lines = route.describe_route(facts)
    groups = _group_tasks(lines)
    assert groups["本地（不可用即报错）"] == set(route.TASK_ORDER)
    assert not any("云端" in label for label in groups)
    text = _text(lines)
    assert "不按轮次改道" in text
    assert "不会改走云端" in text


def test_legacy_without_text_endpoint_reports_cloud(monkeypatch):
    """服务端没配文本端点时实际走云端（配置事实，不是故障回落）。

    ⚠️ 这与服务端 `_complete_upstream` 一致：`base` 为空 → 走 MaaS；
    不是"本地不可用 → 报错"（那种只发生在**配了**端点却探测失败时）。
    """
    _clear(monkeypatch)
    facts = {"text_local": False, "vision_local": True}
    routes = route.route_map(facts)
    assert routes["vision-locate"] == route.LOCAL
    for task in route.TEXT_TASKS:
        assert routes[task] == route.CLOUD, task

    text = _text(route.describe_route(facts))
    assert "vision-locate" in text
    assert "文本没有本地端点" in text
    assert "会发给模型服务商" in text
    assert "文本都在本地处理" not in text


def test_legacy_without_vision_endpoint_says_screenshots_leave(monkeypatch):
    _clear(monkeypatch)
    facts = {"text_local": True, "vision_local": False}
    routes = route.route_map(facts)
    assert routes["vision-locate"] == route.CLOUD
    for task in route.TEXT_TASKS:
        assert routes[task] == route.LOCAL

    text = _text(route.describe_route(facts))
    assert "截图会发给模型服务商" in text
    assert "文本优先在本地处理" in text


def test_legacy_without_cloud_config_says_local_only(monkeypatch):
    """云端未配置（无凭证/无云端端点）：只能走本地，本地不可用即报错。"""
    _clear(monkeypatch)
    facts = {"text_local": True, "vision_local": True, "cloud_available": False}
    assert set(route.route_map(facts).values()) == {route.LOCAL}

    text = _text(route.describe_route(facts))
    assert "未配置云端——只能走本地" in text
    assert "本地不可用会直接报错" in text


def test_legacy_without_facts_does_not_guess(monkeypatch):
    """老服务端没有 routing 字段：不按策略表画饼，只说"无法确认"。"""
    _clear(monkeypatch)
    assert set(route.route_map({}).values()) == {route.AUTO}
    text = _text(route.describe_route({}))
    assert "无法确认" in text


# -- explicit：策略表 + 轮次规则，直接对 decide() 断言 -----------------------


@pytest.mark.parametrize("task", route.TASK_ORDER)
def test_explicit_task_route_matches_decide(monkeypatch, task):
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)

    routes = route.route_map({"text_local": True, "vision_local": True})
    assert routes[task] == route.decide(task).route

    expected = route.LOCAL if task == "vision-locate" else route.CLOUD
    for round_index in (1, 2, 5):
        decision = route.decide(task, {}, round_index=round_index)
        assert decision.route == expected, (task, round_index)


def test_explicit_output_says_text_leaves_on_first_round(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    lines = route.describe_route({"text_local": True, "vision_local": True})

    groups = _group_tasks(lines)
    assert groups["本地（不可用即报错）"] == {"vision-locate"}
    assert groups["云端（内容会发出去）"] == set(route.TEXT_TASKS)
    text = _text(lines)
    assert "首轮即走云端" in text
    assert "第 2 轮起强制 cloud" in text
    assert "截图仍在本地" in text


def test_explicit_without_vision_endpoint_says_rejected(monkeypatch):
    """策略表 explicit 且服务端没有本地视觉端点：**拒绝**，不是外发。

    原断言写的是「截图会外发」——那是把实现写错的地方当成了需求（2026-11-04 修）。
    `X-Mat-Route: local` 到服务端后由 `_require_local` 校验配置，缺端点直接 400
    （`cloud/matcloud/core.py:326-328`），客户端也没有改道云端的分支。
    """
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    text = _text(route.describe_route({"text_local": True, "vision_local": False}))
    assert "vision-locate" in text
    assert "会被拒绝" in text
    assert "截图会外发" not in text


# -- MAT_ROUTE 覆盖 -----------------------------------------------------------


def test_status_route_local_override(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_KEY, route.LOCAL)
    facts = {"text_local": True, "vision_local": True}
    assert set(route.route_map(facts).values()) == {route.LOCAL}
    text = _text(route.describe_route(facts))
    assert "全部强制本地" in text
    assert "不会改走云端" in text


def test_status_route_cloud_override(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_KEY, route.CLOUD)
    facts = {"text_local": True, "vision_local": True}
    assert set(route.route_map(facts).values()) == {route.CLOUD}
    text = _text(route.describe_route(facts))
    assert "全部强制云端" in text
    assert "文本与截图都会发给模型服务商" in text


def test_route_local_override_without_endpoints_is_refused(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_KEY, route.LOCAL)
    text = _text(route.describe_route({"text_local": False, "vision_local": False}))
    assert "会被拒绝" in text


def test_route_cloud_override_without_cloud_config_warns(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_KEY, route.CLOUD)
    text = _text(route.describe_route(
        {"text_local": True, "vision_local": True, "cloud_available": False}))
    assert "没有可用的云端配置" in text


# -- 反矛盾：任务级与整体级不能说反 ------------------------------------------


@pytest.mark.parametrize("facts", [
    {"text_local": True, "vision_local": True},
    {"text_local": False, "vision_local": True},
    {"text_local": True, "vision_local": False},
    {"text_local": False, "vision_local": False},
    {"text_local": True, "vision_local": True, "cloud_available": False},
])
def test_no_task_in_both_groups_and_no_blanket_contradiction(monkeypatch, facts):
    """同一任务不得同时出现在两个分组；有文本任务在云端时，不能说"文本都在本地"。"""
    _clear(monkeypatch)
    groups = _group_tasks(route.describe_route(facts))

    seen: set[str] = set()
    for tasks in groups.values():
        assert not (seen & tasks), f"任务重复出现在多个分组: {seen & tasks}"
        seen |= tasks

    text = _text(route.describe_route(facts))
    cloud_text = {t for label, tasks in groups.items() if "云端" in label
                  for t in tasks if t in route.TEXT_TASKS}
    if cloud_text:
        assert "文本都在本地处理" not in text
        assert "文本与截图都在本地处理" not in text


# -- 反矛盾（策略表 explicit）：blocked 时不能说"会发出" ---------------------


def test_explicit_without_cloud_config_does_not_claim_egress(monkeypatch):
    """策略表 explicit + 服务端没有云端配置：文本任务是 `blocked`，不能说"会发出"。

    2026-11-04 补：`is_policy_explicit()` 分支此前只换了分组、没换说明句，
    于是同一段输出里既写「不可用（调用会报错）」，又写「会发出场景描述…」。
    """
    _clear(monkeypatch)
    monkeypatch.setenv(route.ENV_POLICY, route.EXPLICIT)
    facts = {"text_local": True, "vision_local": True, "cloud_available": False}

    routes = route.route_map(facts)
    assert {routes[t] for t in route.TEXT_TASKS} == {route.BLOCKED}

    text = _text(route.describe_route(facts))
    assert "调用会报错" in text
    assert "内容不会发出" in text
    assert "会发出场景描述" not in text, "blocked 时不能说内容发得出去"


@pytest.mark.parametrize("policy", ["", route.EXPLICIT])
@pytest.mark.parametrize("facts", [
    {"text_local": True, "vision_local": True},
    {"text_local": True, "vision_local": False},
    {"text_local": False, "vision_local": True},
    {"text_local": True, "vision_local": True, "cloud_available": False},
    {"text_local": True, "vision_local": False, "cloud_available": False},
])
def test_no_contradiction_in_any_policy(monkeypatch, policy, facts):
    """两种策略 × 五组事实：任务不重复分组，且结论句不与分组冲突。"""
    _clear(monkeypatch)
    if policy:
        monkeypatch.setenv(route.ENV_POLICY, policy)

    routes = route.route_map(facts)
    groups = _group_tasks(route.describe_route(facts))
    seen: set[str] = set()
    for tasks in groups.values():
        assert not (seen & tasks), f"任务重复出现在多个分组: {seen & tasks}"
        seen |= tasks
    assert seen == set(route.TASK_ORDER), "每个任务都必须被归入且仅归入一个分组"

    text = _text(route.describe_route(facts))
    # 只要有文本任务走不了云端（blocked），就不能说内容发得出去。
    if any(routes[t] == route.BLOCKED for t in route.TEXT_TASKS):
        assert "会发出场景描述" not in text
    # 本地视觉端点缺失时不能说"截图会外发"——那条请求是被拒绝的。
    if facts.get("vision_local") is False and policy == route.EXPLICIT:
        assert "截图会外发" not in text



# -- CLI 入口：默认走 describe_route，缺凭证时补 cloud_available -------------


def test_print_routing_uses_describe_route(monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.delenv("GEN_BASE_URL", raising=False)
    monkeypatch.delenv("GEN_API_KEY", raising=False)
    _print_routing({"routing": {"text_local": True, "vision_local": True}})
    out = capsys.readouterr().out
    assert "本地（不可用即报错）" in out
    assert "未配置云端——只能走本地" in out
