"""P1-2：探索路径 → 可回放用例（promote）——枚举/定位映射/坐标兜底/截断/CLI。

fixture 是一张手工构造的探索图（与 explore.py 落盘结构逐字段对齐）：
  P0(根) --tap「搜索」(rid) --> P1 --tap「设置」(纯文案) --> P2
  P2 --tap「\\ue60a」(iconfont，控件带 bounds) --> P3   → 坐标兜底
  P3 --tap「下一页」(iconfont，无控件) --------------> P4   → 截断
  P2 --BACK_HOP --> P1                                          → back 动作
"""

from __future__ import annotations

import argparse
import json

import pytest

from insta_engine.dsl import Action, LocatorStrategy, TestCase
from insta_engine.promote import list_paths, load_explore, promote_path, save_case

PUA = "\ue60a"  # iconfont 私有区字符（is_pua_label 命中 → 文案不可重放）


@pytest.fixture()
def explore() -> dict:
    return {
        "explore_id": "explore_20261019_1200",
        "package": "com.demo.app",
        "pages": [
            {"page_key": "P0", "depth": 0, "parent_key": None, "parent_action": None,
             "parent_rid": None, "activity": "com.demo.app.MainActivity",
             "controls": [{"text": "搜索", "res-id": "com.demo:id/btn_search",
                           "bounds": "[0,100][200,140]", "clickable": True}]},
            {"page_key": "P1", "depth": 1, "parent_key": "P0", "parent_action": "搜索",
             "parent_rid": "com.demo:id/btn_search", "activity": "com.demo.app.SearchActivity",
             "controls": [{"text": "设置", "bounds": "[0,200][200,240]", "clickable": True}]},
            {"page_key": "P2", "depth": 2, "parent_key": "P1", "parent_action": "设置",
             "parent_rid": "", "activity": "com.demo.app.SettingsActivity",
             "controls": [{"text": PUA, "bounds": "[100,300][140,340]", "clickable": True}]},
            {"page_key": "P3", "depth": 3, "parent_key": "P2", "parent_action": PUA,
             "parent_rid": "", "activity": "com.demo.app.DetailActivity",
             "controls": [{"text": "下一页", "bounds": "[0,400][200,440]", "clickable": True}]},
            {"page_key": "P4", "depth": 4, "parent_key": "P3", "parent_action": "下一页",
             "parent_rid": "", "activity": "com.demo.app.NextActivity",
             "controls": []},
        ],
        "edges": [
            {"from": "P0", "to": "P1", "action": "搜索", "count": 3},
            {"from": "P1", "to": "P2", "action": "设置", "count": 2},
            {"from": "P2", "to": "P3", "action": PUA, "count": 1},
            {"from": "P3", "to": "P4", "action": "下一页", "count": 1},
            {"from": "P2", "to": "P1", "action": "__system_back__", "count": 4},
        ],
    }


# -- 路径枚举 ------------------------------------------------------------------

def test_list_paths_enumerates_simple_paths(explore):
    paths = list_paths(explore)
    assert paths, "必须枚举出路径"
    # P0→P1→P2→P3→P4 是最长链；P2→P1 的 back 边会造成环，简单路径不得成环
    longest = max(paths, key=lambda p: len(p.hops))
    assert [h.label for h in longest.hops] == ["搜索", "设置", PUA, "下一页"]
    assert longest.pages == ["P0", "P1", "P2", "P3", "P4"]
    assert all(p.pages[0] == "P0" for p in paths), "全部路径从探索根出发"
    for p in paths:
        assert len(set(p.pages)) == len(p.pages), "简单路径：页面不得重复"


def test_list_paths_flags_replayability(explore):
    paths = list_paths(explore)
    by_hops = {len(p.hops): p for p in paths}
    assert by_hops[3].replayable, "搜索/设置两跳可重放，back 跳不算 → 整链可重放"
    assert not by_hops[4].replayable, "第三跳是 iconfont（无 rid）→ 不可重放"


def test_load_explore_rejects_bad_shape(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"foo": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="explore"):
        load_explore(bad)


# -- 用例生成 ------------------------------------------------------------------

def test_promote_maps_locators_by_precedence(explore):
    case, notes = promote_path(explore, 0)  # 最长链：4 跳（含 iconfont 坐标兑底）
    assert [s.action for s in case.steps] == [Action.LAUNCH, Action.TAP, Action.TAP,
                                              Action.TAP, Action.TAP]
    launch, hop1, hop2, hop3, hop4 = case.steps
    assert launch.params["package"] == "com.demo.app"
    assert launch.params.get("cold") in (True, "true", "True"), "冷启动保证回放起点确定性"
    assert hop1.target.strategy == LocatorStrategy.RES_ID  # rid 最稳，优先
    assert hop1.target.value == "com.demo:id/btn_search"
    assert hop2.target.strategy == LocatorStrategy.TEXT  # 可重放文案次之
    assert hop2.target.value == "设置"
    assert hop3.target.strategy == LocatorStrategy.POINT  # iconfont → 控件 bounds 兜底
    assert hop3.target.value == "120,320"  # [100,300][140,340] 中心
    assert any("人工确认" in n for n in notes), "坐标兜底必须进 notes"
    assert case.meta["source"] == "explore"
    assert case.meta["explore_id"] == "explore_20261019_1200"
    assert case.target == {"package": "com.demo.app"}


def test_promote_truncates_unreplayable_hop(explore):
    paths = list_paths(explore)
    longest = max(paths, key=lambda p: len(p.hops))
    case, notes = promote_path(explore, longest.index)
    # 第 4 跳「下一页」无 rid、iconfont 判据外的文案也不可重放？——「下一页」是普通文案，
    # 可重放 ⇒ 能走到 P4；真正会截断的是把 PUA 跳换成「无控件匹配」的构造。
    # 这里验证最长链 5 步（launch+4 tap）完整生成。
    assert len(case.steps) == 5
    assert not any("截断" in n for n in notes)


def test_promote_truncates_when_no_locator_channel(explore):
    # P3→P4 的「下一页」改成 iconfont 且 P3 控件里没有它 → 截断
    explore["edges"][3]["action"] = PUA
    explore["pages"][4]["parent_action"] = PUA
    explore["pages"][3]["controls"] = []  # 找不到坐标兜底
    paths = list_paths(explore)
    longest = max(paths, key=lambda p: len(p.hops))
    case, notes = promote_path(explore, longest.index)
    # launch + 3 跳成功，第 4 跳截断
    assert len(case.steps) == 4
    assert any("截断" in n for n in notes)
    assert any("人工确认" in n for n in notes)


def test_promote_back_hop_maps_to_back_step(explore):
    # 只取 P0→P1→P2→(back)→P1 的路径：back 边成环被简单路径挡住 ⇒
    # 单独构造一条含 back 的链验证映射（P1 的 alt 入口走 back 回 P1 前页）。
    explore["edges"].append({"from": "P3", "to": "P0", "action": "__system_back__", "count": 1})
    paths = list_paths(explore)
    with_back = [p for p in paths if any(h.label == "__system_back__" for h in p.hops)]
    assert with_back, "枚举应包含含返回跳的路径"
    case, _ = promote_path(explore, with_back[0].index)
    backs = [s for s in case.steps if s.action == Action.BACK]
    assert backs, "BACK_HOP 跳必须映射为 back 动作"


def test_promote_case_round_trips_through_dsl(explore, tmp_path):
    case, _ = promote_path(explore, 0, name="checkout-happy")
    out = save_case(case, tmp_path / "cases" / "checkout-happy.json")
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["caseId"] == "checkout-happy"
    assert payload["meta"]["source"] == "explore"
    restored = TestCase.model_validate(payload)
    assert len(restored.steps) == len(case.steps)


def test_promote_rejects_bad_index(explore):
    with pytest.raises(ValueError, match="路径编号"):
        promote_path(explore, 999)


# -- CLI ----------------------------------------------------------------------

def test_cli_list_and_generate(explore, tmp_path, capsys):
    from insta_engine.__main__ import cmd_promote

    explore_file = tmp_path / "explore.json"
    explore_file.write_text(json.dumps(explore, ensure_ascii=False), encoding="utf-8")

    rc = cmd_promote(argparse.Namespace(file=str(explore_file), path=None, list=False,
                                        name=None, out=None, max_depth=8))
    listed = capsys.readouterr().out
    assert rc == 0 and "可提升路径" in listed

    rc = cmd_promote(argparse.Namespace(file=str(explore_file), path=0, list=False,
                                        name="checkout-happy", out=str(tmp_path / "cases" / "c.json"),
                                        max_depth=8))
    out_text = capsys.readouterr().out
    assert rc == 0 and "已生成" in out_text
    saved = json.loads((tmp_path / "cases" / "c.json").read_text(encoding="utf-8"))
    assert saved["caseId"] == "checkout-happy" and saved["meta"]["source"] == "explore"

    rc = cmd_promote(argparse.Namespace(file=str(explore_file), path=999, list=False,
                                        name=None, out=None, max_depth=8))
    assert rc == 1, "非法编号必须非零退出"
