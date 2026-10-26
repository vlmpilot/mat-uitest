"""生成期**页面一致性**闸门：`case_audit.page_conflicts` + `ai_client.PageConflictError`。

为什么要有这一层（2026-10-21 真机实测）：某生活服务 App 3 条同代用例 0/3，全部挂在
"断言写的是**上一页**的控件"上（首页 tab 选中态断言在外卖页、外卖首页的 res-id 断言在
地址选择页）。R20 只在**跑测后**报 WARN，用例照样落盘、照样上真机。这一层把它前移到
**生成期**：validator 抛 `PageConflictError` → `complete_task_raw` 的内容级重试回路把
"该页可用的候选"回喂给模型重写。
"""

from __future__ import annotations

from insta_engine.dsl import TestCase
from insta_engine.page_index import identities_from_controls, save_index

PKG = "com.x"


def _page(key: str, **kw) -> dict:
    base = {"page_key": key, "depth": 1, "parent_key": None, "parent_action": None,
            "activity": f"{PKG}/.A", "identities": [], "controls": [],
            "alt_entries": [], "hits": 1}
    base.update(kw)
    if not base["identities"] and base["controls"]:
        base["identities"] = sorted(identities_from_controls(base["controls"]))
    return base


def _index(tmp_path, pages: list[dict]):
    save_index(tmp_path / f"{PKG}.json", {"package": PKG, "generated_at": "t",
                                         "sources": ["explore"], "pages": pages, "edges": []})
    return tmp_path


def _graph(tmp_path):
    """root --「外卖」--> takeout（有 takeout_ok）；另一页 mine 有 other_ok。"""
    pages = [
        _page("root", depth=0, controls=[{"text": "外卖", "clickable": True},
                                         {"text": "我的", "clickable": True}]),
        _page("takeout", depth=1, parent_key="root", parent_action="外卖",
              controls=[{"res-id": f"{PKG}:id/takeout_ok", "text": "综合排序", "clickable": True}]),
        _page("mine", depth=1, parent_key="root", parent_action="我的",
              controls=[{"res-id": f"{PKG}:id/other_ok", "text": "我的订单", "clickable": True}]),
    ]
    return _index(tmp_path, pages)


def _raw(s3_value: str, strategy: str = "res-id") -> dict:
    return {"caseId": "case_000001", "name": "外卖页", "target": {"package": PKG},
            "steps": [{"id": "s1", "action": "launch"},
                      {"id": "s2", "action": "tap",
                       "target": {"strategy": "text", "value": "外卖"}},
                      {"id": "s3", "action": "wait",
                       "target": {"strategy": strategy, "value": s3_value}}]}


def test_page_conflicts_reports_cross_page_assertion(tmp_path):
    """断言用了**别的页**的 res-id ⇒ 报冲突，并给出该页候选（供回喂）。"""
    from insta_engine.case_audit import page_conflicts, page_guard_feedback

    idx = _graph(tmp_path)
    case = TestCase.model_validate(_raw(f"{PKG}:id/other_ok"))
    conflicts = page_conflicts(case, idx, PKG)
    assert len(conflicts) == 1, conflicts
    assert conflicts[0]["value"] == f"{PKG}:id/other_ok"
    assert conflicts[0]["page"] == "takeout" or conflicts[0]["activity"].endswith(".A")
    assert any("takeout_ok" in c for c in conflicts[0]["candidates"]), conflicts[0]["candidates"]

    msg = page_guard_feedback(case, idx, PKG)
    assert "不属于它所在的那一页" in msg
    assert "takeout_ok" in msg, "回喂必须带该页可用候选，否则模型只能瞎猜"


def test_page_guard_silent_when_assertion_is_on_expected_page(tmp_path):
    from insta_engine.case_audit import page_conflicts

    idx = _graph(tmp_path)
    assert page_conflicts(TestCase.model_validate(_raw(f"{PKG}:id/takeout_ok")), idx, PKG) == []


def test_page_guard_silent_without_graph(tmp_path):
    """没图谱 ⇒ 一律不判（与 R18 同一条降级纪律：不知道 ≠ 有错）。"""
    from insta_engine.case_audit import page_conflicts

    assert page_conflicts(TestCase.model_validate(_raw(f"{PKG}:id/whatever")), tmp_path, PKG) == []


class _ScriptedClient:
    """按脚本返回的假客户端（local 模式，鸭子类型即可）。"""

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.messages: list[list[dict]] = []

    def complete_raw_text(self, messages, context="生成", task=None, max_tokens=None):
        self.messages.append(messages)
        return json_dumps(self.payloads.pop(0))


def json_dumps(data: dict) -> str:
    import json

    return json.dumps(data, ensure_ascii=False)


def test_generation_retries_with_page_feedback(tmp_path):
    """端到端：首次产出跨页定位 → 闸门抛出 → 重试回喂**带候选**的修复文案 → 第二次通过。"""
    from insta_engine.ai_client import _page_checked, _validate_single, complete_task_raw

    idx = _graph(tmp_path)
    client = _ScriptedClient([_raw(f"{PKG}:id/other_ok"), _raw(f"{PKG}:id/takeout_ok")])
    validator = _page_checked(_validate_single, idx, {"package": PKG})

    case = complete_task_raw(client, "gen", {"nl": "进入外卖页", "app_info": {"package": PKG}},
                             "生成", validator=validator)

    assert isinstance(case, TestCase)
    assert len(client.messages) == 2, "应触发一次内容级重试"
    repair_text = client.messages[1][-1]["content"]
    assert "不属于它所在的那一页" in repair_text
    assert "takeout_ok" in repair_text, "重试请求里必须带该页候选"


def test_page_guard_can_be_disabled(tmp_path, monkeypatch):
    """`MAT_PAGE_GUARD=0` 关掉闸门（老行为：跨页定位照旧落盘，由 R20 事后报）。"""
    from insta_engine.ai_client import _page_checked, _validate_single, complete_task_raw

    idx = _graph(tmp_path)
    monkeypatch.setenv("MAT_PAGE_GUARD", "0")
    client = _ScriptedClient([_raw(f"{PKG}:id/other_ok")])
    case = complete_task_raw(client, "gen", {"nl": "x", "app_info": {"package": PKG}}, "生成",
                             validator=_page_checked(_validate_single, idx, {"package": PKG}))
    assert isinstance(case, TestCase) and len(client.messages) == 1


def test_page_conflicts_covers_res_id_edges(tmp_path):
    """点击按 res-id 时也要能走链（`parent_rid`）：否则闸门在新图谱上全程弃权。"""
    from insta_engine.case_audit import page_conflicts

    pages = [
        _page("root", depth=0, controls=[{"text": "外卖", "res-id": f"{PKG}:id/tab_takeout",
                                          "clickable": True}]),
        _page("takeout", depth=1, parent_key="root", parent_rid=f"{PKG}:id/tab_takeout",
              controls=[{"res-id": f"{PKG}:id/takeout_ok", "clickable": True}]),
        _page("mine", depth=1, parent_key="root", parent_action="我的",
              controls=[{"res-id": f"{PKG}:id/other_ok", "clickable": True}]),
    ]
    idx = _index(tmp_path, pages)
    case = TestCase.model_validate({
        "caseId": "c", "name": "n", "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch"},
                  {"id": "s2", "action": "tap",
                   "target": {"strategy": "res-id", "value": f"{PKG}:id/tab_takeout"}},
                  {"id": "s3", "action": "wait",
                   "target": {"strategy": "res-id", "value": f"{PKG}:id/other_ok"}}]})
    conflicts = page_conflicts(case, idx, PKG)
    assert len(conflicts) == 1 and conflicts[0]["value"] == f"{PKG}:id/other_ok", conflicts


def test_page_guard_coverage_reports_blind_spots(tmp_path):
    """可判率必须如实反映"走链断了多少步"——否则会被读成"检查通过"。

    实测背景（2026-10-21）：某生活服务 App 3 条真实失败用例冲突数全是 0，不是用例没问题，
    而是 `parent_rid` 全为空、走链在第一步就断（闸门按纪律弃权）。
    """
    from insta_engine.case_audit import page_guard_coverage

    idx = _graph(tmp_path)
    # s2 的 tap 用图谱里**不存在**的文案 ⇒ 从 s2 起页面未知 ⇒ 只有 s2 可判（before=root）
    case = TestCase.model_validate({
        "caseId": "c", "name": "n", "target": {"package": PKG},
        "steps": [{"id": "s1", "action": "launch"},
                  {"id": "s2", "action": "tap",
                   "target": {"strategy": "text", "value": "不存在的入口"}},
                  {"id": "s3", "action": "wait",
                   "target": {"strategy": "res-id", "value": f"{PKG}:id/takeout_ok"}}]})
    judged, total = page_guard_coverage(case, idx, PKG)
    assert total == 2, "s1 无定位不计；s2/s3 各有定位"
    assert judged == 1, f"s3 的页面走链断了应判不了，实际 {judged}/{total}"

    ok = TestCase.model_validate(_raw(f"{PKG}:id/takeout_ok"))
    assert page_guard_coverage(ok, idx, PKG) == (2, 2)
