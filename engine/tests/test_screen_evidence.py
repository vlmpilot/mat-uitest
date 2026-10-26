"""失败现场证据（`Runner._write_screen_evidence`）与"从现场提断言候选"
（`scripts/fix_assertions_from_run.py`）的回归测试。

## 为什么要有这份证据（2026-10-24）

A2/A3 的正确断言**只能从真机那一屏推出来**，而 `run.json` 此前只留截图 —— 截图里没有 res-id，
人得肉眼抄文案，机器更没法用。图谱也补不上：内容型 App 的入口（「早晚报」「播客」）常常
压根没被探索到（tx-news 44 页图谱里没有早晚报页 ⇒ R25 自动改写覆盖率只有 1/10——图谱没探索到入口页时改写帮不上）。

## 本文件守的四件事

1. 证据文件里必须有**动作前后两屏**（只有一屏就分不清"本来就在"和"刚出现"）；
2. 候选必须排除**常驻控件**（tab/底栏）—— 断言它恒真，是 R23/R24 的坑；
3. 候选必须排除**内容标题**（长文案/含标点）—— 跨运行必变，是 R4 的坑；
4. 改写要落在**断言**上，且保留原文件（`--out` 另存）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import fix_assertions_from_run as fixer  # noqa: E402

from insta_engine.driver import NullDriver  # noqa: E402
from insta_engine.dsl import Step, TestCase  # noqa: E402
from insta_engine.page_index import digest_page  # noqa: E402
from insta_engine.runner import Runner, StepResult  # noqa: E402

PKG = "com.x"
TAB = f"{PKG}:id/channelbar_text"


def _ctl(text=None, desc=None, rid=None, clickable=False, cls="android.widget.TextView") -> dict:
    control: dict = {"class": cls, "bounds": "[0,0][100,50]"}
    if clickable:
        control["clickable"] = True
    if text:
        control["text"] = text
    if desc:
        control["desc"] = desc
    if rid:
        control["res-id"] = rid
    return control


def _xml(controls: list[dict]) -> str:
    """把控件摘要反过来拼成 a11y XML（`digest_page` 要能解析回来）。"""
    nodes = []
    for c in controls:
        attrs = [f'class="{c["class"]}"', f'bounds="{c["bounds"]}"',
                 'package="com.x"', 'text=""', 'content-desc=""', 'resource-id=""']
        if c.get("clickable"):
            attrs.append('clickable="true"')
        if c.get("text"):
            attrs[3] = f'text="{c["text"]}"'
        if c.get("desc"):
            attrs[4] = f'content-desc="{c["desc"]}"'
        if c.get("res-id"):
            attrs[5] = f'resource-id="{c["res-id"]}"'
        nodes.append(f'<node {" ".join(attrs)} />')
    return f'<?xml version="1.0" encoding="UTF-8"?><hierarchy>{"".join(nodes)}</hierarchy>'


# -- 1. 证据落盘 ---------------------------------------------------------------


def test_screen_evidence_keeps_both_screens(tmp_path):
    """前后两屏都要落盘：只留当前屏就分不清"本来就在（tab）"和"刚出现（目标页独有）"。"""
    before = [_ctl(text="要闻", rid=TAB), _ctl(text="关注", rid=TAB)]
    after = before + [_ctl(text="订阅", rid=f"{PKG}:id/btn_subscribe", clickable=True)]
    step = Step(id="s1", action="tap", target={"strategy": "text", "value": "早晚报"})
    result = StepResult(step_id="s1", action="tap", status="failed", error="AssertionError: x")

    runner = Runner(NullDriver(), tmp_path)
    runner._write_screen_evidence(2, step, result, _xml(before), _xml(after),
                                  digest_page(_xml(after), PKG), PKG, 1)

    assert result.screen == "screens/step_02_s1_a1.json"
    doc = json.loads((tmp_path / result.screen).read_text(encoding="utf-8"))
    assert doc["step_id"] == "s1" and doc["target"]["value"] == "早晚报"
    assert {c.get("text") for c in doc["before"]} == {"要闻", "关注"}
    assert "订阅" in {c.get("text") for c in doc["after"]}
    # 页签名：前后不同才说明"动作真的换了屏"，比人眼看文案可靠
    assert doc["before_page"] and doc["after_page"] and doc["before_page"] != doc["after_page"]


def test_screen_evidence_missing_before_xml_is_tolerated(tmp_path):
    """`wait_stable_xml` 自己抛时 `before_xml` 是空的 —— 证据照写，不许因此吞掉整条失败。"""
    after = [_ctl(text="订阅", rid=f"{PKG}:id/btn_subscribe")]
    step = Step(id="s1", action="tap", target={"strategy": "text", "value": "早晚报"})
    result = StepResult(step_id="s1", action="tap", status="failed", error="boom")

    runner = Runner(NullDriver(), tmp_path)
    runner._write_screen_evidence(1, step, result, "", _xml(after),
                                  digest_page(_xml(after), PKG), PKG, 1)
    doc = json.loads((tmp_path / result.screen).read_text(encoding="utf-8"))
    assert doc["before"] == [] and doc["before_page"] is None
    assert doc["after"], "当前屏还是要有"


# -- 2. 候选筛选 ---------------------------------------------------------------


def _screen(before: list[dict], after: list[dict], tapped: str = "早晚报") -> dict:
    return {"before": before, "after": after, "before_page": "aaaa", "after_page": "bbbb",
            "target": {"strategy": "text", "value": tapped}, "action": "tap"}


def test_candidates_skip_persistent_and_content_controls():
    """常驻 tab（动作前就有）与内容标题（长/带标点）都不能当到达断言。"""
    tabs = [_ctl(text="要闻", rid=TAB), _ctl(text="关注", rid=TAB)]
    screen = _screen(tabs, tabs + [
        _ctl(text="订阅", rid=f"{PKG}:id/btn_subscribe", clickable=True),
        _ctl(text="热点聚焦"),
        _ctl(text="退出职场后，她去义乌做跨境电商，用互联网思维打出爆款"),
    ])
    ranked = fixer.candidates(screen, "早晚报")
    labels = [c["label"] for c in ranked]
    assert labels[0] == "订阅", ranked           # rid + 短 + 唯一 ⇒ 排第一
    assert "热点聚焦" in labels
    assert not any("义乌" in label for label in labels), "内容标题必须被排除"
    assert not any(label in ("要闻", "关注") for label in labels), "常驻 tab 必须被排除"


def test_candidates_exclude_the_tapped_control_itself():
    """刚点的那个控件即使"看起来是新的"也不能当断言（R23：等于没断）。"""
    screen = _screen([], [_ctl(text="清空", rid=f"{PKG}:id/clear", clickable=True)])
    assert fixer.candidates(screen, "清空") == []


def test_candidates_empty_when_page_did_not_change():
    """动作后页面没变（整屏都是老控件）⇒ 给不出候选，别硬凑一个。"""
    same = [_ctl(text="要闻", rid=TAB)]
    assert fixer.candidates(_screen(same, same), "某条新闻") == []


# -- 3. 端到端：从 run.json + screen 到建议/改写 ----------------------------------


def _run_fixture(tmp_path: Path) -> Path:
    case = TestCase.model_validate({
        "caseId": "case_007", "name": "首页打开「早晚报」", "target": {"package": PKG},
        "steps": [
            {"id": "s0", "action": "launch", "params": {"package": PKG}},
            {"id": "s1", "action": "tap", "target": {"strategy": "text", "value": "早晚报"},
             "assertions": [{"type": "exists",
                             "target": {"strategy": "text", "value": "早晚报"}}]},
        ],
    })
    run_dir = tmp_path / "01_case_007"
    (run_dir / "screens").mkdir(parents=True)
    screen = _screen([_ctl(text="要闻", rid=TAB)],
                     [_ctl(text="要闻", rid=TAB),
                      _ctl(text="订阅", rid=f"{PKG}:id/btn_subscribe", clickable=True),
                      _ctl(text="热点聚焦")])
    (run_dir / "screens" / "step_02_s1_a1.json").write_text(
        json.dumps(screen, ensure_ascii=False), encoding="utf-8")
    (run_dir / "run.json").write_text(json.dumps({
        "case": json.loads(case.model_dump_json(by_alias=True)),
        "run": {"case_id": "case_007", "status": "failed", "steps": [
            {"step_id": "s0", "status": "passed"},
            {"step_id": "s1", "status": "failed", "category": "assert-fail",
             "screen": "screens/step_02_s1_a1.json"},
        ]},
    }, ensure_ascii=False), encoding="utf-8")
    return run_dir / "run.json"


def test_propose_marks_coupled_and_suggests_arrival_target(tmp_path):
    proposals = fixer.propose(_run_fixture(tmp_path))
    assert len(proposals) == 1
    item = proposals[0]
    assert item["coupled"] is True, "点完又断言它存在 ⇒ A2 形态"
    assert item["proposal"]["target"] == {"strategy": "res-id", "value": f"{PKG}:id/btn_subscribe"}
    assert item["page_changed"] is True


def test_apply_fix_rewrites_assertion_and_keeps_original(tmp_path):
    run_json = _run_fixture(tmp_path)
    original = json.loads(run_json.read_text(encoding="utf-8"))["case"]
    proposals = fixer.propose(run_json)
    out = fixer.apply_fix(run_json, proposals[0], tmp_path / "fixed")

    assert out is not None and out.exists()
    fixed = json.loads(out.read_text(encoding="utf-8"))
    assertion = fixed["steps"][1]["assertions"][0]
    assert assertion["type"] == "exists"
    assert assertion["target"]["value"] == f"{PKG}:id/btn_subscribe"
    # 原文件不动；改写留痕（人要知道产物与手稿不一样了）
    assert json.loads(run_json.read_text(encoding="utf-8"))["case"] == original
    assert any("[R2]" in i for i in fixed["meta"]["quality_issues"])


# -- 4. 报告里要能找到这份证据 --------------------------------------------------


def test_report_links_to_screen_evidence(tmp_path):
    """证据藏在目录里等于没有：报告必须给入口（否则没人知道该去哪儿提候选）。"""
    from insta_engine.report import render_html
    from insta_engine.runner import RunResult

    run = RunResult(case_id="c1", case_name="x", status="failed", step_success_rate=0.5,
                    steps=[StepResult(step_id="s1", action="tap", status="failed",
                                      category="assert-fail", error="断言失败",
                                      screen="screens/step_01_s1_a1.json")])
    html = render_html(run, tmp_path).read_text(encoding="utf-8")
    assert "screens/step_01_s1_a1.json" in html
    assert "控件树证据" in html
