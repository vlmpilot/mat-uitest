"""测试数据共享 + 用例编排的最小闭环测试（2026-10-21）。

覆盖：
- `datasets[].ref` 解析（#锚点 / 缺文件 fail-loud）
- setup 失败 ⇒ 整条用例 **skipped**（不是 failed），主步骤全部记 skipped
- teardown best-effort：主步骤失败后仍执行，且不改状态
- 套件展开：平铺 / stages / 嵌套套件 / 非法 on_failure
- 阶段闸门判定：abort 只对真失败生效

设备用极简桩：定位必失败、wait 必成功——只测编排语义，不测定位链。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from insta_engine.__main__ import _stage_should_abort
from insta_engine.dsl import TestCase
from insta_engine.runner import Runner
from insta_engine.suite import expand_suite, load_case_or_suite, resolve_dataset_refs

_WAIT = {"id": "w", "action": "wait", "params": {"seconds": 0.01}}
_TAP_MISS = {"id": "x", "action": "tap", "target": {"strategy": "text", "value": "不存在"}}


class _Driver:
    """极简设备桩：`find` 必抛、其余 no-op。"""

    serial = None

    def dump(self) -> str:
        return "<hierarchy/>"

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"png")

    def app_start(self, package, activity=None) -> None:
        pass

    def press(self, key) -> None:
        pass

    def swipe_ext(self, direction) -> None:
        pass

    def click(self, x, y) -> None:
        pass

    def find(self, by, value, timeout):
        raise RuntimeError("LocateFailed: not found")

    def find_xpath(self, expr, timeout):
        raise RuntimeError("LocateFailed: not found")

    def shell(self, cmd) -> str:
        return ""

    def start_keepalive(self) -> None:
        pass

    def stop_keepalive(self) -> None:
        pass

    def start_evidence(self) -> None:
        pass


def _case(**kw) -> TestCase:
    data = {"caseId": "c1", "name": "t", "target": {"package": "com.demo"},
            "steps": [_WAIT]}
    data.update(kw)
    return TestCase.model_validate(data)


# -- 共享数据集 -----------------------------------------------------------------

def test_dataset_ref_with_anchor(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "accounts.json").write_text(
        json.dumps({"vip": {"phone": "138", "pwd": "x"}}), encoding="utf-8")
    case = _case(datasets=[{"name": "vip", "ref": "data/accounts.json#vip"},
                           {"name": "inline", "vars": {"a": "b"}}])
    got = resolve_dataset_refs(case, tmp_path)
    assert got.datasets[0] == {"name": "vip", "vars": {"phone": "138", "pwd": "x"}}
    assert got.datasets[1] == {"name": "inline", "vars": {"a": "b"}}


def test_dataset_ref_missing_file_is_loud(tmp_path):
    with pytest.raises(ValueError, match="不存在"):
        resolve_dataset_refs(_case(datasets=[{"ref": "data/none.json"}]), tmp_path)


def test_dataset_ref_bad_anchor_is_loud(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"vip": {"p": "1"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="锚点"):
        resolve_dataset_refs(_case(datasets=[{"ref": "a.json#nope"}]), tmp_path)


# -- setup / teardown ------------------------------------------------------------

def test_setup_failure_marks_case_skipped(tmp_path):
    case = _case(setup=[_TAP_MISS], teardown=[_WAIT])
    run = Runner(_Driver(), tmp_path).run(case)
    assert run.status == "skipped"
    assert [s.status for s in run.setup_steps] == ["failed"]
    assert all(s.status == "skipped" for s in run.steps)
    assert run.failed_categories == {}


def test_teardown_runs_after_main_failure(tmp_path):
    case = _case(steps=[_TAP_MISS], teardown=[_WAIT])
    run = Runner(_Driver(), tmp_path).run(case)
    assert run.status == "failed"
    assert [s.status for s in run.teardown_steps] == ["passed"]


# -- 套件展开 --------------------------------------------------------------------

def test_expand_flat_suite_keeps_order(tmp_path):
    names = []
    for i in range(2):
        p = tmp_path / f"c{i}.json"
        p.write_text(json.dumps(_case(caseId=f"c{i}").model_dump(by_alias=True)),
                     encoding="utf-8")
        names.append(p.name)
    got = expand_suite({"name": "s", "cases": names}, tmp_path)
    assert [(c.case_id, stage, gate) for c, _, stage, gate in got] == [
        ("c0", "", "continue"), ("c1", "", "continue")]


def test_expand_stages_and_nested_suite(tmp_path):
    child = tmp_path / "child.suite.json"
    child.write_text(json.dumps({"stages": [{"name": "main", "cases": [
        {"caseId": "inline1", "steps": [_WAIT]}]}]}), encoding="utf-8")
    payload = {"stages": [
        {"name": "setup", "on_failure": "abort", "cases": [
            {"caseId": "i1", "steps": [_WAIT]}]},
        {"name": "main", "cases": [child.name]},
    ]}
    got = expand_suite(payload, tmp_path)
    # 嵌套阶段名 = 父阶段/子阶段（父阶段是引用子套件的那个阶段）
    assert [(c.case_id, stage, gate) for c, _, stage, gate in got] == [
        ("i1", "setup", "abort"), ("inline1", "main/main", "continue")]


def test_expand_rejects_unknown_gate(tmp_path):
    with pytest.raises(ValueError, match="on_failure"):
        expand_suite({"cases": [], "on_failure": "stop"}, tmp_path)


def test_expand_rejects_empty_stage(tmp_path):
    with pytest.raises(ValueError, match="非空数组"):
        expand_suite({"stages": [{"name": "setup", "cases": []}]}, tmp_path)


def test_load_case_or_suite_accepts_stages(tmp_path):
    p = tmp_path / "s.suite.json"
    p.write_text(json.dumps({"stages": [{"name": "x", "cases": ["a.json"]}]}),
                 encoding="utf-8")
    kind, payload = load_case_or_suite(p)
    assert kind == "suite" and "stages" in payload


# -- 阶段闸门 --------------------------------------------------------------------

@pytest.mark.parametrize("gate,status,expected", [
    ("abort", "failed", True),
    ("abort", "cancelled", True),
    ("abort", "passed", False),
    ("abort", "skipped", False),
    ("continue", "failed", False),
])
def test_stage_gate(gate, status, expected):
    assert _stage_should_abort(gate, status) is expected
