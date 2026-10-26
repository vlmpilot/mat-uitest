"""生成路径的**清单校验**——评审核实出的唯一不准确项。

## 背景（为什么补这一环）

方案声称「防幻觉闸：`case_audit` L1/R18 + `case_quality` 与 gen/import 两条路径共用」。
核实后发现**半对**：`case_quality` 确实两条路径共用，但 **`case_audit`（清单校验、
R19 弹窗）只在 `mat import`（`manual_import.py`）与配置台保存（`web_api/cases.py`）跑，
`mat gen` 不跑**。

⇒ 生成路径的"AI 只能操作真实存在的控件"这道闸**只有 prompt 层注入，没有事后校验**：
模型仍可能写出图谱里不存在的 res-id/文案，而 CLI 不会告诉你。

修法：把审计接进**同一个闸门**（`ai_client.apply_quality_gate(case, index_dir)`），
三个生成入口（`generate` / `generate_calibrated` / `generate_cases`）透传 `index_dir`。
**只有一处实现**——不在调用方各写一份（C9/C15 的教训）。
"""

from __future__ import annotations

import json

from insta_engine.ai_client import GenClient, apply_quality_gate
from insta_engine.dsl import TestCase
from insta_engine.page_index import merge_pages, new_index, save_index


def _case(*targets: tuple[str, str]) -> TestCase:
    """用例：每个 (strategy, value) 一个 tap 步骤。"""
    return TestCase.model_validate({
        "caseId": "c", "name": "n", "target": {"package": "com.t"},
        "steps": [{"id": f"s{i}", "action": "tap",
                   "target": {"strategy": s, "value": v}}
                  for i, (s, v) in enumerate(targets, 1)],
    })


def _index_dir(tmp_path, controls=("登录",), res_ids=("com.t:id/login",)):
    """造一份页面图谱（控件清单里有 `controls` / `res_ids`）。"""
    index = new_index("com.t")
    merge_pages(index, [{
        "page_key": "k1", "depth": 0, "activity": "com.t/.Main",
        "controls": [{"text": t, "clickable": True, "bounds": "[0,0][10,10]"} for t in controls]
                    + [{"res-id": r, "clickable": True, "bounds": "[0,0][10,10]"} for r in res_ids],
    }])
    save_index(tmp_path / "com.t.json", index)
    return tmp_path


# -- 闸门本体 -------------------------------------------------------------------

def test_index_given_runs_list_check(tmp_path):
    """给了 `index_dir` ⇒ 跑 R18：文案/res-id 不在控件清单里就报出来。"""
    idx = _index_dir(tmp_path)
    case = apply_quality_gate(_case(("text", "登录"), ("res-id", "com.t:id/编造的id")), idx)
    issues = case.meta.get("quality_issues") or []
    assert any("R18" in i and "编造的id" in i for i in issues), issues
    # 清单里存在的那个不该被报
    assert not any("R18" in i and "登录" in i for i in issues), issues


def test_without_index_behavior_unchanged(tmp_path):
    """不给 `index_dir` ⇒ 与改动前**逐字一致**（不跑清单校验，也不报 R18）。

    这条是防回归的关键：闸门被 import / 配置台保存 / 多处调用，
    加参数不能让既有调用方的行为发生变化。
    """
    case = apply_quality_gate(_case(("text", "登录"), ("res-id", "com.t:id/编造的id")))
    issues = case.meta.get("quality_issues") or []
    assert not any("R18" in i for i in issues), issues


def test_issues_merged_and_persisted(tmp_path):
    """审计结论与 `case_quality` 的结论**合并**落进 `meta.quality_issues`（持久化到文件）。

    合并而不是覆盖：两条闸门的结论都要能在"跑之前校对"和"跑之后归因"时看到。
    """
    idx = _index_dir(tmp_path)
    # 这条用例同时触发 case_quality 的问题（断言缺定位会被消毒/报 R2）与 R18
    raw = {"caseId": "c", "name": "n", "target": {"package": "com.t"}, "steps": [
        {"id": "s1", "action": "tap",
         "target": {"strategy": "text", "value": "编造的按钮"}},
        {"id": "s2", "action": "wait", "assertions": [{"type": "exists"}]},
    ]}
    case = apply_quality_gate(TestCase.model_validate(raw), idx)
    issues = case.meta.get("quality_issues") or []
    assert any("R18" in i for i in issues), f"缺 R18：{issues}"
    # 落盘后仍在（`dump_dsl` → 重新解析）
    from insta_engine.dsl import dump_dsl

    again = TestCase.model_validate(json.loads(dump_dsl(case)))
    assert (again.meta.get("quality_issues") or []) == issues


def test_audit_runs_after_sanitize(tmp_path):
    """审计必须跑在**消毒后**的用例上：状态后缀已被剥掉，不该再报原样文案。

    （`视频，可选中` 会被 case_quality 的 R5 确定性剥离 ⇒ 审计看到的是 `视频`。）
    """
    idx = _index_dir(tmp_path, controls=("视频",))
    case = apply_quality_gate(_case(("text", "视频，可选中")), idx)
    issues = case.meta.get("quality_issues") or []
    assert not any("R18" in i for i in issues), f"审计跑在了消毒前：{issues}"


# -- 三个生成入口是否真的透传 index_dir ------------------------------------------

def _stub_client(payload) -> GenClient:
    """真 GenClient 实例（走真闸门链路），只挡网络层。"""
    client = GenClient.__new__(GenClient)
    client.complete_raw_text = lambda messages, context="", task=None: \
        json.dumps(payload, ensure_ascii=False)
    return client


def _recorder(monkeypatch):
    """把闸门换成记录器，用来断言"入口有没有把 index_dir 传下来"。"""
    from insta_engine import ai_client

    seen: list = []
    real = ai_client.apply_quality_gate

    def fake(case, index_dir=None):
        seen.append(index_dir)
        return real(case, index_dir)

    monkeypatch.setattr(ai_client, "apply_quality_gate", fake)
    return seen


def test_generate_forwards_index_dir(monkeypatch, tmp_path):
    seen = _recorder(monkeypatch)
    _stub_client({"caseId": "c", "name": "n", "target": {"package": "com.t"},
                  "steps": [{"id": "s1", "action": "launch"}]}).generate(
        "描述", {"package": "com.t"}, index_dir=tmp_path)
    assert seen == [tmp_path], f"generate 没透传：{seen}"


def test_generate_calibrated_forwards_index_dir(monkeypatch, tmp_path):
    """`executor=None` 时直接返回（不连真机），仍要透传。

    ⚠️ 这个入口会**调用闸门两次**（一次给初稿、一次给返回值）——这是既有行为，
    这里只断言"每次调用都拿到了 index_dir"（透传是本次要钉的东西）。
    """
    seen = _recorder(monkeypatch)
    _stub_client({"caseId": "c", "name": "n", "target": {"package": "com.t"},
                  "steps": [{"id": "s1", "action": "launch"}]}).generate_calibrated(
        "描述", {"package": "com.t"}, index_dir=tmp_path)
    assert seen and all(s == tmp_path for s in seen), f"没透传：{seen}"


def test_generate_cases_forwards_index_dir(monkeypatch, tmp_path):
    """配置台走的是 `generate_cases`（复数）——它也必须透传，否则配置台仍缺这一环。"""
    seen = _recorder(monkeypatch)
    _stub_client({"cases": [{"caseId": "c", "name": "n", "target": {"package": "com.t"},
                             "steps": [{"id": "s1", "action": "launch"}]}]}).generate_cases(
        "描述", {"package": "com.t"}, index_dir=tmp_path)
    assert seen == [tmp_path], f"generate_cases 没透传：{seen}"
