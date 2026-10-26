"""跑前闸门（2026-10-23）：`mat run` 执行前对用例做一次**确定性修复**。

## 为什么需要（实测）

闸门原来只长在**生成**（`mat gen`）和**导入**（`mat import` / 配置台）两条路径上，
**历史用例与手改用例永远绕过它**。实测 65 条跑测：

- 失败归因 top1 = "没切到被测 App"（33%）；
- 结构性缺陷 top1/top2 = **A5 无 launch 步骤 14 条** / **A8 从中间状态开始 14 条**；
- 去重后 **15 条失败用例首步不是 launch**，其中 13 条来自 10-21 的手工导入批次。

而同一套 `review()` 对**同样这些用例**能当场补上 launch（本次端到端验证：2 步 → 3 步，
首步变 `launch`，`launch_ms` 有值）。

## 判据

| 场景 | 期望 |
|---|---|
| 首步不是 launch + 已知包名 | 跑之前补 `launch`，并把 `[R6]` 记进 `case.meta.quality_issues` |
| 首步已经是 launch | 不动（幂等）|
| `Runner(..., quality_gate=False)` | 完全不跑闸门，用例原样执行 |
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from insta_engine.driver import NullDriver
from insta_engine.dsl import Action, TestCase
from insta_engine.runner import Runner

NO_LAUNCH = {
    "caseId": "no-launch", "name": "没 launch 的用例",
    "target": {"package": "com.tencent.news"},
    "steps": [
        {"id": "s1", "action": "tap", "intent": "点第一条",
         "target": {"strategy": "text", "value": "某条新闻"}},
    ],
}


def _runner(tmp_path: Path, **kw) -> Runner:
    return Runner(NullDriver(), tmp_path, **kw)


def test_gate_inserts_launch_and_records_the_issue(tmp_path: Path) -> None:
    case = TestCase.model_validate(NO_LAUNCH)
    out = _runner(tmp_path)._apply_run_gate(case)

    assert out.steps[0].action is Action.LAUNCH, [s.action for s in out.steps]
    assert out.steps[0].params.get("package") == "com.tencent.news"
    issues = out.meta.get("quality_issues") or []
    assert any("[R6]" in i for i in issues), issues
    # step id 必须唯一（重复 id 会让重试时截图/证据互相覆盖）
    ids = [s.id for s in out.steps]
    assert len(ids) == len(set(ids)), ids


def test_gate_is_idempotent(tmp_path: Path) -> None:
    """已经补过 launch 的用例再跑一次，不许再插一个。"""
    case = TestCase.model_validate(NO_LAUNCH)
    once = _runner(tmp_path)._apply_run_gate(case)
    twice = _runner(tmp_path)._apply_run_gate(once)
    assert sum(1 for s in twice.steps if s.action is Action.LAUNCH) == 1


def test_run_applies_gate_before_execution(tmp_path: Path, monkeypatch) -> None:
    """接线：`run()` 必须在执行前调用闸门（不是只有 `_apply_run_gate` 可用）。"""
    runner = _runner(tmp_path, quality_gate=True)   # 产品入口（CLI/控制台）显式打开
    calls: list[str] = []
    real = runner._apply_run_gate

    def spy(case):
        calls.append(case.case_id)
        return real(case)

    monkeypatch.setattr(runner, "_apply_run_gate", spy)
    # NullDriver 只支持纯 API 用例，UI 步骤必然失败——这里只关心"闸门被调用过"
    with contextlib.suppress(Exception):
        runner.run(TestCase.model_validate(NO_LAUNCH))
    assert calls == ["no-launch"], calls


def test_library_default_is_off_so_tests_and_calibration_are_unaffected(tmp_path: Path) -> None:
    """`Runner` 默认**不**跑闸门：它也被校准等内部路径复用，且大量测试直接断言步序。

    产品入口（`mat run` / 控制台）显式传 `quality_gate=True` —— 那里才是"历史用例要跑"的场景。
    """
    assert _runner(tmp_path).quality_gate is False


def test_gate_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    runner = _runner(tmp_path, quality_gate=False)
    called: list[int] = []
    monkeypatch.setattr(runner, "_apply_run_gate", lambda c: called.append(1) or c)
    with contextlib.suppress(Exception):
        runner.run(TestCase.model_validate(NO_LAUNCH))
    assert called == [], "quality_gate=False 时不许跑闸门（用例要原样执行）"


def test_gate_failure_does_not_block_execution(tmp_path: Path, monkeypatch) -> None:
    """闸门自己炸了也不能阻断执行（同"证据缺失不改结果"的纪律）。"""
    import insta_engine.ai_client as ai

    def boom(*_a, **_kw):
        raise RuntimeError("闸门内部错误")

    monkeypatch.setattr(ai, "apply_quality_gate", boom)
    case = TestCase.model_validate(NO_LAUNCH)
    out = _runner(tmp_path)._apply_run_gate(case)
    assert out.steps[0].action is Action.TAP          # 原样返回
    assert any("[X0]" in i for i in out.meta["quality_issues"])


def test_pure_api_case_is_not_given_a_launch_step() -> None:
    """纯接口用例**不补** launch：它们本来就不碰设备，补了会把"离线可跑"变成"必须有设备"。

    这是真踩到的回归：`mat run` 接上跑前闸门后，`test_cmd_run_pure_api_case_needs_no_device` 直接红。
    """
    from insta_engine.case_quality import ensure_launch

    raw = {"target": {"package": "com.x"}, "steps": [{"id": "s1", "action": "api"}]}
    assert ensure_launch(raw) == []
    assert [s["action"] for s in raw["steps"]] == ["api"]

    # 反向：只要混了一个 UI 步骤，就必须补（否则它跑在"设备当时停在哪"上）
    mixed = {"target": {"package": "com.x"},
             "steps": [{"id": "s1", "action": "api"}, {"id": "s2", "action": "tap"}]}
    assert ensure_launch(mixed), "混入 UI 步骤就该补 launch"
    assert mixed["steps"][0]["action"] == "launch"


def test_batch_suite_file_goes_to_temp_not_artifacts(tmp_path: Path, monkeypatch) -> None:
    """`mat run <目录> --app X` 的临时套件文件必须落在**系统临时目录**，不能进 `artifacts/`。

    两件事都实测踩过（2026-10-23）：
      · 它曾写死 `artifacts/_suite_<包名>.json` ⇒ 污染用户产物目录，且**不跟随 `--artifacts`**；
      · 那句 `mkdir(exist_ok=True)` 在沙箱/容器里撞到已存在目录会抛 `PermissionError`
        （EEXIST 被映射成权限错误）⇒ 批量跑在干净环境里**直接起不来**（我这次就是被它挡住的）。
    """
    import tempfile as _tempfile

    from insta_engine.__main__ import _write_batch_suite

    monkeypatch.chdir(tmp_path)                       # 让"写进 CWD/artifacts"这种实现无处可躲
    (tmp_path / "artifacts").mkdir()                  # 且**故意让它已存在**（复现 EEXIST 场景）
    cases = [tmp_path / "a.json", tmp_path / "b.json"]
    path = _write_batch_suite("com.demo.shop", cases)

    assert path.parent.parent == Path(_tempfile.gettempdir()).resolve().parent or \
        str(path).startswith(_tempfile.gettempdir()), f"临时套件没落在系统临时目录：{path}"
    assert (tmp_path / "artifacts" / "_suite_com_demo_shop.json").exists() is False, \
        "临时套件不该写进 artifacts/"
    suite = json.loads(path.read_text(encoding="utf-8"))
    assert suite["name"] == "app:com.demo.shop"
    assert suite["cases"] == [c.as_posix() for c in cases]
