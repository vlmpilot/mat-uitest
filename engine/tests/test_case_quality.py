"""用例质量闸门（`case_quality`）与**两条产用例路径**的接线测试。

为什么要单独一个文件：这套判据的价值不在"函数写对了"，而在**两条路径都真的装上了**。
「生成」与「转化」出来的用例跑在同一台设备上，闸门只装一条路径 =
一半的失败照样发生（这正是本轮修复前的状态：`mat gen` 一道闸都没有）。

判据清单见 `insta_engine/case_quality.py` 的模块说明 —— 每条都对应一批实测失败。
"""

from __future__ import annotations

import json

import pytest
import requests

from insta_engine import case_quality
from insta_engine.ai_client import GenClient, apply_quality_gate
from insta_engine.case_quality import quality_issues, review
from insta_engine.dsl import TestCase


class FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _fake_llm(monkeypatch, steps: list[dict]) -> None:
    """让 server 模式的生成返回指定 steps（不联网、不调模型）。"""
    payload = json.dumps({"caseId": "case_q1", "name": "闸门测试",
                          "target": {"package": "com.insta.demo"}, "steps": steps})

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        return FakeResp({"content": f"```json\n{payload}\n```"})

    monkeypatch.setattr(requests, "post", fake_post)


# -- 单元：闸门本身 -----------------------------------------------------------------

def test_apply_quality_gate_records_issues_into_meta():
    """issues 必须**落进用例本身**（meta），不能只在命令行刷一下 ——
    跑之前校对、跑之后归因都要能看到。"""
    case = TestCase.model_validate({
        "caseId": "c1", "target": {"package": "com.a"},
        "steps": [{"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "搜索框"}}],
    })
    gated = apply_quality_gate(case)
    issues = gated.meta.get("quality_issues") or []
    assert any("已自动补 `launch`" in i for i in issues), issues
    # 而且**改动要落到用例上**（不只是报了一句）
    assert gated.steps[0].action == "launch"
    assert gated.steps[0].params.get("package") == "com.a"


def test_apply_quality_gate_is_a_noop_for_a_clean_case():
    """干净的用例**不许**被塞 issues（否则闸门变成噪音，人就不看了）。"""
    case = TestCase.model_validate({
        "caseId": "c1", "target": {"package": "com.a"},
        "steps": [{"id": "s0", "action": "launch", "params": {"package": "com.a"}},
                  {"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "搜索"},
                   "assertions": [{"type": "exists",
                                   "target": {"strategy": "text", "value": "搜索结果"}}]}],
    })
    gated = apply_quality_gate(case)
    assert "quality_issues" not in gated.meta


def test_review_sanitizes_before_checking():
    """先消毒再检查 —— 否则会把**已经被剥掉状态后缀**的文案又报一遍（自相矛盾）。"""
    raw = {"steps": [{"id": "s0", "action": "launch", "params": {"package": "com.a"}},
                     {"id": "s1", "action": "tap",
                      "target": {"strategy": "text", "value": "视频，可选中"}}]}
    issues = review(raw)
    assert raw["steps"][1]["target"]["value"] == "视频"
    assert not any("内容快照" in i for i in issues), issues


# -- 接线：生成路径真的装上了闸门 -----------------------------------------------------

def test_generate_applies_the_quality_gate(monkeypatch):
    """`GenClient.generate` 的产物必须过闸门（此前**一道闸都没有**）。

    这个用例锁的是**接线**，不是判据本身 —— 判据写对了但没接上，
    等于没做（同族：测试借用环境现状、没构造被测条件）。
    """
    _fake_llm(monkeypatch, [
        {"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "搜索框"}},
    ])
    case = GenClient("https://cloud.mat.test", "user-token", "", prompt_mode="server").generate("搜索")
    # 缺 launch ⇒ 闸门补上并记 issue（实测 14/37 条失败属这类）
    assert any("已自动补 `launch`" in i for i in (case.meta.get("quality_issues") or []))
    assert case.steps[0].action == "launch"


def test_generate_sanitizes_targetless_assertions(monkeypatch):
    """生成产物里的**空定位断言**必须在落盘前被处置（否则运行期必抛）。"""
    _fake_llm(monkeypatch, [
        {"id": "s0", "action": "launch", "params": {"package": "com.a"}},
        {"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "登录"},
         "assertions": [{"type": "exists", "target": None}]},
    ])
    case = GenClient("https://cloud.mat.test", "user-token", "", prompt_mode="server").generate("登录")
    produced = [a for s in case.steps for a in s.assertions]
    assert produced and all(a.target for a in produced), f"不该留下空定位断言：{produced}"


def test_generate_quality_gate_survives_dump_roundtrip(monkeypatch, tmp_path):
    """闸门改过的东西必须能**原样落盘再读回**（否则改动在写文件时丢掉）。"""
    from insta_engine.dsl import dump_dsl

    _fake_llm(monkeypatch, [
        {"id": "s0", "action": "launch", "params": {"package": "com.a"}},
        {"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "视频，可选中"}},
    ])
    case = GenClient("https://cloud.mat.test", "user-token", "", prompt_mode="server").generate("看视频")
    path = tmp_path / "c.json"
    path.write_text(dump_dsl(case), encoding="utf-8")
    back = TestCase.model_validate_json(path.read_text(encoding="utf-8"))
    assert back.steps[1].target.value == "视频", "状态后缀该在落盘前就被剥掉"


# -- R9a：补 launch（确定性消毒） ----------------------------------------------------

def test_ensure_launch_inserts_launch_with_package():
    """缺 `launch` ⇒ 自动补（用 `target.package`），并记 issue。

    实测 14/37 条失败属这类；补上之后用例至少是**确定性**的（要么过，要么每次
    都因同一个原因失败），而不是"看上一个用例把设备留在哪儿"。
    """
    raw = {"target": {"package": "com.a"},
           "steps": [{"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "登录"}}]}
    issues = review(raw)
    assert raw["steps"][0]["action"] == "launch"
    assert raw["steps"][0]["params"]["package"] == "com.a"
    assert any("已自动补 `launch`" in i for i in issues), issues


def test_ensure_launch_skips_without_package():
    """拿不到 `package` ⇒ **不补**（补不出正确的 launch），只报 issue。"""
    raw = {"steps": [{"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "登录"}}]}
    issues = review(raw)
    assert raw["steps"][0]["action"] == "tap", "没有 package 时不该硬塞 launch"
    assert any("没有 `launch` 步骤" in i for i in issues), issues


def test_ensure_launch_does_not_duplicate():
    raw = {"target": {"package": "com.a"},
           "steps": [{"id": "s0", "action": "launch", "params": {"package": "com.a"}}]}
    issues = review(raw)
    # ⚠️ 这条用例**没有断言** ⇒ R12 会报（那是正确行为，不是回归）；这里只验"没重复补 launch"
    assert not any("已自动补" in i for i in issues), issues
    assert sum(1 for s in raw["steps"] if s["action"] == "launch") == 1


def test_ensure_launch_picks_a_free_step_id():
    """⚠️ step id 必须唯一 —— 重复 id 会让重试时截图/证据互相覆盖。"""
    raw = {"target": {"package": "com.a"},
           "steps": [{"id": "s0", "action": "tap", "target": {"strategy": "text", "value": "登录"}}]}
    review(raw)
    ids = [s["id"] for s in raw["steps"]]
    assert len(ids) == len(set(ids)), f"id 撞了：{ids}"


def test_ensure_launch_keeps_precondition_issue_visible():
    """⚠️ **补了 launch ≠ 前置条件问题解决了** —— 两条判据互不替代。

    这是最容易写错的一处：`launch` 只保证"冷启动到首页"，
    **不保证**到达前置条件描述的那个页面（实测 8 条照红）。
    """
    raw = {"target": {"package": "com.a"},
           "steps": [{"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "清空"}}]}
    issues = review(raw, preconditions=["已进入搜索页"])
    assert any("已自动补 `launch`" in i for i in issues), issues
    assert any("可置备的页面状态" in i for i in issues), issues


# -- 反向：不许误伤 -----------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    {"steps": [{"id": "s0", "action": "launch", "params": {"package": "com.a"}},
               {"id": "s1", "action": "wait", "params": {"ms": 100}}]},
    {"steps": [{"id": "s0", "action": "launch", "params": {"package": "com.a"}},
               {"id": "s1", "action": "tap", "target": {"strategy": "res-id", "value": "a:id/btn"}}]},
])
def test_clean_cases_produce_no_start_state_issue(raw):
    """`res-id` 定位、`wait` 步骤都不该被起始状态判据误报。"""
    assert not any("launch" in i for i in quality_issues(raw)), quality_issues(raw)


# -- R14 / R15 / R16：让"质量"可复算、可追溯 -------------------------------------------

def test_fmt_issue_rejects_unregistered_code():
    """⚠️ R16 的**执行点**：编号未登记直接报错，不静默放过。

    这是这条要求最容易失效的地方 —— 新增一处 issue 却忘了登记编号，
    统计里就会出现一个无法解释的 `?`（或者更糟：被当成普通文本混过去）。
    """
    with pytest.raises(KeyError, match="未知规则编号"):
        case_quality.fmt_issue("R999", "随便什么话")


def test_every_gate_issue_carries_a_registered_code():
    """闸门产出的**每一条** issue 都必须带已登记的编号（R16）。

    构造一条把所有判据都踩一遍的用例，逐条检查编号。
    """
    raw = {"target": {"package": "com.a"},
           "steps": [
               {"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "视频，可选中"},
                "assertions": [{"type": "exists", "target": None}]},
               {"id": "s2", "action": "tap",
                "target": {"strategy": "text", "value": "点“经”之笔丨六网协同如何打开内需新空间"},
                "assertions": [{"type": "exists",
                                "target": {"strategy": "text",
                                           "value": "点“经”之笔丨六网协同如何打开内需新空间"}}]},
               {"id": "s3", "action": "tap", "target": {"strategy": "text", "value": "\ue917"}},
           ]}
    issues = review(raw, preconditions=["已进入搜索结果页", "本次冷启动命中开屏广告"])
    assert issues, "这条用例应该踩中多条判据"
    for issue in issues:
        code = case_quality.rule_code_of(issue)
        assert code != "?", f"issue 没有编号：{issue}"
        assert code in case_quality.RULES, f"编号未登记：{code}"
    codes = {case_quality.rule_code_of(i) for i in issues}
    # 覆盖到各家族：断言消毒 / 导航耦合 / 状态后缀 / 内容快照 / 不可见字符 / 前置条件
    assert {"R1", "R2", "R4", "R5", "R9b", "R9c"} <= codes, codes
    assert "R3" in codes, codes


def test_grade_of_uses_severity_not_confidence():
    """分级只认严重度：BLOCK→C / WARN→B / INFO→A（INFO 是"闸门已替你处置"）。"""
    g = case_quality.grade_of
    assert g([]) == "A"
    assert g([case_quality.fmt_issue("R5", "状态后缀已剥离")]) == "A", "INFO 不该降级"
    assert g([case_quality.fmt_issue("R4", "疑似内容快照")]) == "B"
    assert g([case_quality.fmt_issue("R2", "导航耦合")]) == "C"
    # 同时有 WARN 和 BLOCK ⇒ 取最严重的
    assert g([case_quality.fmt_issue("R4", "x"), case_quality.fmt_issue("R3", "y")]) == "C"


def test_severity_of_is_fail_safe_for_unknown_issues():
    """⚠️ 认不出来的 issue 按 **WARN**（不放行）：模型自述的 `[LLM]` 与手写文本都落这里。"""
    assert case_quality.severity_of("模型随便说了一句") == case_quality.WARN
    assert case_quality.severity_of(case_quality.fmt_issue("LLM", "x")) == case_quality.WARN


def test_quality_stats_is_computable_and_traceable():
    """R15/R16：批次质量口径可复算，且能按规则编号统计。"""
    from insta_engine.manual_import import quality_stats

    results = [
        {"grade": "A", "dsl": {"steps": []}, "issues": []},
        {"grade": "B", "dsl": {"steps": []},
         "issues": [case_quality.fmt_issue("R4", "x")]},
        {"grade": "C", "dsl": None, "issues": [case_quality.fmt_issue("X0", "转换失败")]},
        {"grade": "C", "dsl": {"steps": []},
         "issues": [case_quality.fmt_issue("R3", "y"), case_quality.fmt_issue("R3", "z")]},
    ]
    stats = quality_stats(results)
    assert stats["total"] == 4
    assert stats["blocked"] == 2, "C 级 或 无草稿 都算阻断"
    assert stats["no_blocking_rate"] == 0.5
    assert stats["clean_rate"] == 0.25
    assert stats["by_rule"] == {"R3": 2, "R4": 1, "X0": 1}


def test_quality_stats_names_are_not_overclaimed():
    """⚠️ 口径**命名**本身要被锁住：它不能叫"可执行率"。

    "不改就能跑绿的比例"必须与真机跑测数据 join 才算得出来；
    离线口径冒充它就是代理指标（D10/F14）。
    """
    from insta_engine.manual_import import quality_stats

    keys = set(quality_stats([{"grade": "A", "dsl": {}, "issues": []}]))
    assert "no_blocking_rate" in keys and "clean_rate" in keys
    assert not any("execut" in k or "runnable" in k for k in keys), keys


def test_quality_flags_unprovisionable_system_precondition():
    """⚠️ 会被**拒跑**的用例，等级不该是 A（R17）。

    实测：2 条位置授权用例在导入报告里是 A 级（"无已知缺陷"），
    而它们**一次都跑不成**（前置条件"位置权限尚未授予" ⇒ 执行时被拒跑）。
    ⇒ "会被拒跑"必须在**导入期**就可见，而不是等执行时才撞上。
    """
    raw = {"steps": [{"id": "s0", "action": "launch", "params": {"package": "com.a"}},
                     {"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "同意"}}]}
    issues = quality_issues(raw, preconditions=["位置权限尚未授予"])
    assert any(case_quality.rule_code_of(i) == "R17" for i in issues), issues
    assert case_quality.grade_of(issues) == "B", "会被拒跑 ⇒ 不该是 A"


def test_environment_precondition_does_not_trigger_r17():
    """反向：**环境前提**（App 已安装）不该被 R17 误报 —— 它不会被拒跑。"""
    raw = {"steps": [{"id": "s0", "action": "launch", "params": {"package": "com.a"}},
                     {"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "要闻"},
                      "assertions": [{"type": "exists",
                                      "target": {"strategy": "text", "value": "要闻要论"}}]}]}
    issues = quality_issues(raw, preconditions=["App 已安装"])
    assert not any(case_quality.rule_code_of(i) == "R17" for i in issues), issues
    assert issues == [], issues


# -- R22：一次性启动噪声（跳过广告 / 首启弹窗确认）自动改为可选 -----------------------------


def _raw_steps(steps: list[dict]) -> dict:
    return {"target": {"package": "com.a"}, "steps": steps}


def test_r22_marks_splash_skip_step_optional():
    """`tap 跳过` ⇒ onFailure=continue。

    实测（2026-10-21 tx-news 4/16）：引擎每次 launch 后都 `_skip_splash_ad`，用例再点
    「跳过」时目标必然不存在；而 DSL 默认 abort ⇒ 整条用例停在第 2 步（25% 步骤）。
    """
    raw = _raw_steps([
        {"id": "s1", "action": "launch"},
        {"id": "s2", "action": "tap", "target": {"strategy": "text", "value": "跳过"}},
    ])
    issues = review(raw)
    assert raw["steps"][1]["onFailure"] == "continue"
    assert any("[R22]" in i and "跳过" in i for i in issues), issues


def test_r22_marks_first_launch_dialog_confirm_optional():
    """启动后前 2 步里的 `res-id=...dialog_confirm` 也改可选。

    实测：tx-news 4 条用例的第 3 步都点 `tv_dialog_confirm`（隐私弹窗），而首次同意后
    弹窗再不会出现 ⇒ 重跑必然 LocateFailed。
    """
    raw = _raw_steps([
        {"id": "s1", "action": "launch"},
        {"id": "s2", "action": "tap", "target": {"strategy": "text", "value": "跳过"}},
        {"id": "s3", "action": "tap",
         "target": {"strategy": "res-id", "value": "com.a:id/tv_dialog_confirm"}},
    ])
    review(raw)
    assert raw["steps"][1]["onFailure"] == "continue"
    assert raw["steps"][2]["onFailure"] == "continue"


def test_r22_does_not_weaken_late_confirm_steps():
    """**流程中**的确认步骤（启动 2 步之后）不许被改成可选——那是真断言。"""
    raw = _raw_steps([
        {"id": "s1", "action": "launch"},
        {"id": "s2", "action": "tap", "target": {"strategy": "text", "value": "我的"}},
        {"id": "s3", "action": "tap", "target": {"strategy": "text", "value": "设置"}},
        {"id": "s4", "action": "tap",
         "target": {"strategy": "res-id", "value": "com.a:id/dialog_confirm"}},
    ])
    review(raw)
    assert raw["steps"][3].get("onFailure") in (None, "abort"), "第 4 步不该被改成可选"


def test_r22_ignores_non_tap_and_already_optional():
    raw = _raw_steps([
        {"id": "s1", "action": "launch"},
        {"id": "s2", "action": "wait", "target": {"strategy": "text", "value": "跳过"}},
        {"id": "s3", "action": "tap", "onFailure": "continue",
         "target": {"strategy": "text", "value": "跳过广告"}},
    ])
    issues = review(raw)
    assert raw["steps"][1].get("onFailure") in (None, "abort"), "wait 不改"
    assert raw["steps"][2]["onFailure"] == "continue", "本来就可选，保持"
    assert not [i for i in issues if "[R22]" in i and "s3" in i], issues


# -- R23：平凡断言（断言的就是本步刚点的那个控件）------------------------------------
#
# 这条规则来自 2026-10-22 的 A1（假绿率）复核：tx-news 4 条「频道切换」用例**全部报绿**，
# 而截图显示页面压根没切（还停在 7x24）。链条是：
#   `res-id=channelbar_text`（7 个 tab 共用，实际点中的是第一个） + `params.text=要闻`
#   → 页面没变 → 断言 `exists text=要闻` 因为**频道栏本来就有这个字**而通过。
# 旧 R2 只看「策略+值完全相同」，`res-id` 与 `text` 不同策略 ⇒ 漏网。


def test_r23_flags_assertion_on_the_element_it_just_tapped():
    raw = _raw_steps([
        {"id": "s1", "action": "launch", "params": {"package": "com.a"}},
        {"id": "s2", "action": "tap",
         "target": {"strategy": "res-id", "value": "com.a:id/channelbar_text"},
         "params": {"text": "要闻"},
         "assertions": [{"type": "exists", "target": {"strategy": "text", "value": "要闻"}}]},
    ])
    issues = quality_issues(raw)
    assert any(case_quality.rule_code_of(i) == "R23" for i in issues), issues


def test_r23_does_not_flag_assertion_about_another_control():
    """断言的是**别的**控件（"点我的 → 断订单列表在"）⇒ 不报，那是有意义的断言。"""
    raw = _raw_steps([
        {"id": "s1", "action": "launch", "params": {"package": "com.a"}},
        {"id": "s2", "action": "tap", "target": {"strategy": "text", "value": "我的"},
         "assertions": [{"type": "exists", "target": {"strategy": "text", "value": "我的订单"}}]},
    ])
    issues = quality_issues(raw)
    assert not any(case_quality.rule_code_of(i) == "R23" for i in issues), issues


def test_r23_ignores_not_exists():
    """`tap 跳过` + `not-exists 跳过` 是**正确**写法（证明按钮消失了），不许报。"""
    raw = _raw_steps([
        {"id": "s1", "action": "launch", "params": {"package": "com.a"}},
        {"id": "s2", "action": "tap", "target": {"strategy": "text", "value": "跳过"},
         "assertions": [{"type": "not-exists",
                         "target": {"strategy": "text", "value": "跳过"}}]},
    ])
    issues = quality_issues(raw)
    assert not any(case_quality.rule_code_of(i) in ("R2", "R23") for i in issues), issues


# -- R12：整条用例没有任何断言（原先只有导入路径在管）----------------------------------


def test_r12_flags_case_without_any_assertion():
    """实测 `gen-225111`：某生活服务 App「启动应用并进入个人中心」2 步 0 断言，真机必绿。

    这条规则原先**只写在导入路径**里（`manual_import`），生成路径漏掉 —— 正是
    `case_quality` 模块说明要消灭的"两条路径各一套"。
    """
    raw = _raw_steps([
        {"id": "s1", "action": "launch", "params": {"package": "com.a"}},
        {"id": "s2", "action": "tap", "target": {"strategy": "text", "value": "我的"}},
    ])
    issues = quality_issues(raw)
    assert any(case_quality.rule_code_of(i) == "R12" for i in issues), issues


def test_r12_silent_when_any_step_has_an_assertion():
    raw = _raw_steps([
        {"id": "s1", "action": "launch", "params": {"package": "com.a"}},
        {"id": "s2", "action": "tap", "target": {"strategy": "text", "value": "我的"},
         "assertions": [{"type": "selected", "target": {"strategy": "text", "value": "我的"}}]},
    ])
    issues = quality_issues(raw)
    assert not any(case_quality.rule_code_of(i) == "R12" for i in issues), issues
