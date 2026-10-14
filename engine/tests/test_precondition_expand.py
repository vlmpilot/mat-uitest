"""R9b：把「可置备的页面状态」前置条件展开成导航步骤（`precondition_expand.py`）。

为什么单独一个文件：这条链路的**顺序**是它最容易错的地方 ——
展开必须发生在「起始状态已保证」之后、「消毒/检查」之前。
顺序错了不会报错，只会让插进去的步骤**没人检查**（同族 C17：静默丢改动）。
"""

from __future__ import annotations

import json

import pytest

from insta_engine import precondition_expand
from insta_engine.manual_import import ManualCase, convert_batch, convert_case


class FakeClient:
    """按顺序弹出桩响应（convert → 展开 → …）。`calls` 记下每次的 context。"""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[str] = []

    def complete_raw(self, messages, context=""):
        self.calls.append(context)
        if not self._responses:
            raise AssertionError(f"桩响应用完了（context={context}）")
        return self._responses.pop(0)


@pytest.fixture
def prompts(tmp_path, monkeypatch):
    """两条任务都要有 prompt（转换 + 展开复用 gen）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "convert.prompt").write_text(
        "IR: {ir} APP: {app_info}", encoding="utf-8")
    (tmp_path / "prompts" / "gen.prompt").write_text(
        "NL: {nl} APP: {app_info}", encoding="utf-8")
    return tmp_path


def _ir(preconditions: list[str] | None = None) -> ManualCase:
    return ManualCase.model_validate({
        "title": "清空搜索历史", "module": "搜索", "priority": "P1",
        "preconditions": preconditions or [],
        "steps": [{"index": 1, "action": "点击「清空」", "expected": "历史搜索被清空"}],
        "source": {"format": "xlsx", "file": "c.xlsx", "row": 2},
    })


def _convert_payload(steps: list[dict] | None = None, confidence: float = 0.9) -> dict:
    return {"confidence": confidence, "issues": [],
            "case": {"caseId": "c1", "name": "清空搜索历史", "target": {"package": "com.a"},
                     "steps": steps or [
                         {"id": "s0", "action": "launch", "params": {"package": "com.a"}},
                         {"id": "s1", "action": "tap",
                          "target": {"strategy": "text", "value": "清空"}}]}}


def _actions(record: dict) -> list[str]:
    return [s["action"] for s in record["dsl"]["steps"]]


# -- 正常路径 -----------------------------------------------------------------------

def test_expand_inserts_steps_after_launch(prompts, monkeypatch):
    """展开的步骤插在 **launch 之后** —— 起点必须是 launch 建立的冷启动首页。"""
    monkeypatch.setenv("MAT_EXPAND_PRECONDITIONS", "1")
    client = FakeClient([
        _convert_payload(),
        {"cases": [{"steps": [
            {"action": "tap", "target": {"strategy": "text", "value": "搜索框"}, "intent": "点搜索框"},
            {"action": "input", "target": {"strategy": "text", "value": "搜索框"},
             "params": {"text": "德约"}, "intent": "输入关键词"},
        ]}]},
    ])
    record = convert_case(client, _ir(["已进入搜索页"]), "com.a")
    assert _actions(record) == ["launch", "tap", "input", "tap"], _actions(record)
    assert record["dsl"]["steps"][1]["intent"].startswith(precondition_expand.MARK)
    assert record["dsl"]["meta"]["expanded_preconditions"] == ["已进入搜索页"]
    assert any("已展开为 2 步导航" in i for i in record["issues"]), record["issues"]


def test_expanded_steps_go_through_the_quality_gate(prompts, monkeypatch):
    """⚠️ 展开出来的步骤**也要过闸门** —— 它是 LLM 产出，一样会写出脏定位。

    这是本设计最容易漏的一点：展开若发生在闸门**之后**，插进去的步骤就**没人检查**。
    """
    monkeypatch.setenv("MAT_EXPAND_PRECONDITIONS", "1")
    client = FakeClient([
        _convert_payload(),
        {"cases": [{"steps": [{"action": "tap",
                    "target": {"strategy": "text", "value": "视频，可选中"}}]}]},
    ])
    record = convert_case(client, _ir(["已进入搜索页"]), "com.a")
    expanded = [s for s in record["dsl"]["steps"] if s["intent"].startswith(precondition_expand.MARK)]
    assert expanded, "展开步骤没插进去"
    assert expanded[0]["target"]["value"] == "视频", "展开步骤的状态后缀也该被剥掉"


def test_expanded_steps_do_not_carry_assertions(prompts, monkeypatch):
    """展开只负责导航：模型给回来的断言要被丢掉（断言归原用例）。"""
    monkeypatch.setenv("MAT_EXPAND_PRECONDITIONS", "1")
    client = FakeClient([
        _convert_payload(),
        {"cases": [{"steps": [{"action": "tap", "target": {"strategy": "text", "value": "搜索框"},
                    "assertions": [{"type": "exists",
                                    "target": {"strategy": "text", "value": "搜索框"}}]}]}]},
    ])
    record = convert_case(client, _ir(["已进入搜索页"]), "com.a")
    expanded = [s for s in record["dsl"]["steps"] if s["intent"].startswith(precondition_expand.MARK)]
    assert expanded[0]["assertions"] == []


def test_expand_drops_launch_from_the_result(prompts, monkeypatch):
    """展开结果里若带 `launch` ⇒ 丢掉（原用例已有，重复 launch 没意义）。"""
    monkeypatch.setenv("MAT_EXPAND_PRECONDITIONS", "1")
    client = FakeClient([
        _convert_payload(),
        {"cases": [{"steps": [{"action": "launch", "params": {"package": "com.a"}},
                   {"action": "tap", "target": {"strategy": "text", "value": "搜索框"}}]}]},
    ])
    record = convert_case(client, _ir(["已进入搜索页"]), "com.a")
    assert _actions(record) == ["launch", "tap", "tap"], _actions(record)


# -- 只展开 ③：其余三类不许触发展开 ---------------------------------------------------

@pytest.mark.parametrize("pre", ["App 已安装", "位置权限尚未授予", "本次冷启动命中开屏广告"])
def test_only_page_states_trigger_expansion(prompts, monkeypatch, pre):
    """① env / ② system / ④ random **都不喂给展开器** —— 连一次调用都不该花。

    ① 不用展开（运行前提）；② 展开不了（动作集改不了权限）；④ 不该展开
    （"导航到一个开屏广告"是荒谬的）。见 `preconditions.classify`。
    """
    monkeypatch.setenv("MAT_EXPAND_PRECONDITIONS", "1")
    client = FakeClient([_convert_payload()])
    convert_case(client, _ir([pre]), "com.a")
    assert client.calls == ["导入转换:清空搜索历史"], f"{pre} 不该触发展开：{client.calls}"


def test_expand_disabled_by_env(prompts, monkeypatch):
    monkeypatch.setenv("MAT_EXPAND_PRECONDITIONS", "0")
    client = FakeClient([_convert_payload()])
    record = convert_case(client, _ir(["已进入搜索页"]), "com.a")
    assert _actions(record) == ["launch", "tap"]
    assert not any("已展开" in i for i in record["issues"])


def test_expand_enabled_defaults_to_on(monkeypatch):
    monkeypatch.delenv("MAT_EXPAND_PRECONDITIONS", raising=False)
    assert precondition_expand.expand_enabled() is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("MAT_EXPAND_PRECONDITIONS", off)
        assert precondition_expand.expand_enabled() is False


# -- 失败路径：不许让整条转换挂掉 -----------------------------------------------------

def test_empty_steps_is_a_legal_answer(prompts, monkeypatch):
    """模型判定"无法确定路径"⇒ 空 steps 是**合法答案**，不插入 + 给出可行动 issue。

    ⚠️ 这就是 `_loose` 校验器存在的理由：用 `TestCase.model_validate` 会把
    "我做不到"当成 schema 错误回喂重试，把模型**逼着编一条路径**出来。
    """
    monkeypatch.setenv("MAT_EXPAND_PRECONDITIONS", "1")
    client = FakeClient([_convert_payload(), {"cases": [{"steps": []}]}])
    record = convert_case(client, _ir(["已进入搜索页"]), "com.a")
    assert _actions(record) == ["launch", "tap"]
    assert any("未能展开" in i for i in record["issues"]), record["issues"]


def test_expand_failure_does_not_break_conversion(prompts, monkeypatch):
    """展开失败（响应形状不对）不该让整条用例转换失败。"""
    monkeypatch.setenv("MAT_EXPAND_PRECONDITIONS", "1")
    client = FakeClient([_convert_payload(), {"bad": "shape"}])
    record = convert_case(client, _ir(["已进入搜索页"]), "com.a")
    assert record["grade"] in ("A", "B"), record
    assert any("未能展开" in i or "展开失败" in i for i in record["issues"])


def test_loose_accepts_the_real_cases_contract():
    """⚠️ 校验器必须兼容 `gen` 的**当前契约** `{"cases":[...]}`。

    这条是**真机实测逼出来的**：第一版按 `.example` 推断契约写成"顶层 steps"，
    真实调用直接报 `缺少 steps`（模型返回 `{"cases":[{...}]}`）。
    ⇒ 判据：**契约以线上为准，别照 `.example` 推断**（跑一次就知道）。
    """
    out = precondition_expand._loose({"cases": [{"steps": [{"action": "tap"}]}]})
    assert out["steps"] == [{"action": "tap"}]


def test_loose_treats_empty_cases_as_legal_no_expansion():
    """模型**偶发拒答**（实测同一输入一次返回 `{"cases": []}`、另一次返回 2 步）
    ⇒ 空数组必须走优雅降级，不能当异常。"""
    out = precondition_expand._loose({"cases": []})
    assert out["steps"] == []


def test_expand_normalize_drops_unknown_actions():
    """动作不在动作集内 ⇒ 丢掉并说明（别把模型编的动作塞进用例）。"""
    steps, notes = precondition_expand._normalize(
        {"steps": [{"action": "teleport", "target": None},
                   {"action": "tap", "target": {"strategy": "text", "value": "搜索"}}]})
    assert [s["action"] for s in steps] == ["tap"]
    assert any("teleport" in n for n in notes)


def test_inserted_steps_get_unique_ids():
    """⚠️ 插入的步骤必须有**不撞车**的 id（重复 id 会让重试时截图互相覆盖）。"""
    from insta_engine.manual_import import _insert_after_launch

    raw = {"steps": [{"id": "s0", "action": "launch"},
                     {"id": "s1", "action": "tap"}]}
    _insert_after_launch(raw, [{"action": "tap", "target": None, "intent": "a"},
                               {"action": "tap", "target": None, "intent": "b"}])
    ids = [s["id"] for s in raw["steps"]]
    assert len(ids) == len(set(ids)), f"id 撞了：{ids}"
    assert [s["action"] for s in raw["steps"]] == ["launch", "tap", "tap", "tap"]


# -- 接线回归：convert_case 必须走 review() -------------------------------------------

def test_convert_case_goes_through_review_so_launch_is_ensured(prompts, monkeypatch):
    """回归：`convert_case` 必须走 `review()`，否则 `ensure_launch` 不生效。

    它曾经**只挑三个子函数调**（`_sanitize_targets` / `_sanitize_assertions` /
    `_quality_issues`）⇒ 漏掉 `ensure_launch` 与"前置条件需置备"两条判据 ——
    正是 `review` 的注释里警告的"各自挑就是漂移的起点"。
    """
    monkeypatch.setenv("MAT_EXPAND_PRECONDITIONS", "0")
    client = FakeClient([_convert_payload(
        steps=[{"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "清空"}}])])
    record = convert_case(client, _ir(), "com.a")
    assert record["dsl"]["steps"][0]["action"] == "launch"
    assert any("已自动补 `launch`" in i for i in record["issues"]), record["issues"]


# -- 额度记账 ------------------------------------------------------------------------

def test_convert_batch_charges_extra_only_when_expansion_happened(tmp_path, prompts, monkeypatch):
    """展开多花 0.5 次额度；**没发出去就不记**（否则用户会莫名少额度）。"""
    monkeypatch.setenv("INSTA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MAT_EXPAND_PRECONDITIONS", "1")
    from insta_engine.quota import UsageStore

    # ① 带 ③ 类前置条件 ⇒ 转换 + 展开 = 1.0
    usage = UsageStore(tmp_path / "home" / "usage.json")
    client = FakeClient([_convert_payload(),
                         {"cases": [{"steps": [{"action": "tap",
                                               "target": {"strategy": "text", "value": "搜索框"}}]}]}])
    convert_batch(client, [_ir(["已进入搜索页"])], "com.a", usage)
    assert json.loads((tmp_path / "home" / "usage.json").read_text())["gen_count"] == 1.0

    # ② 只带 ① 类前置条件 ⇒ 只有转换 = 0.5
    usage2 = UsageStore(tmp_path / "home2" / "usage.json")
    client2 = FakeClient([_convert_payload()])
    convert_batch(client2, [_ir(["App 已安装"])], "com.a", usage2)
    assert json.loads((tmp_path / "home2" / "usage.json").read_text())["gen_count"] == 0.5
